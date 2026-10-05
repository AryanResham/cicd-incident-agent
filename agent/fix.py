"""The fix loop (PLAN §B3 ④ and ⑤), max 3 attempts.

Each attempt: Gemini writes a patch -> guardrails -> pre-checks in the runner on
a scratch copy of the repo (pytest; docker build + run the container + smoke
checks, when Docker exists) -> errors are fed into the next attempt.
The first attempt that passes becomes a PR on `agent/fix-<incident>-<n>`:
  SIMPLE + confidence >= 0.8 + guardrails ok + AGENT_AUTO_MERGE on  -> auto-merge
  anything else (CORE, low confidence, ...)                          -> `needs-human`
If every attempt fails, the best one is still opened as an unmerged PR.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agent.classify import CORE, SIMPLE, patch_kind
from agent.collect import Evidence
from agent.github_api import LABEL_AGENT_FIX, LABEL_NEEDS_HUMAN, GitHub, GitHubError
from agent.llm import Diagnosis, Edit, LLMBadResponse, LLMUnavailable
from agent.patching import PatchError, PatchRejected, compute_changes, unified_diff, write_changes
from agent.smoke import run_smoke

MAX_ATTEMPTS = 3
MIN_AUTO_MERGE_CONFIDENCE = 0.8
CONTAINER_PORT = 18080
COPY_IGNORE = shutil.ignore_patterns(".git", ".venv", "venv", "__pycache__", ".pytest_cache", "*.db", ".claude")


def branch_name(incident: int, attempt: int) -> str:
    return f"agent/fix-{incident}-{attempt}"


def attempts_used(gh: GitHub, incident: int) -> int:
    """Attempts already turned into PRs for this incident (a fix that failed live counts too)."""
    prefix = f"agent/fix-{incident}-"
    return sum(1 for pr in gh.list_prs("all") if pr.get("head", {}).get("ref", "").startswith(prefix))


# ---- pre-checks ------------------------------------------------------------------
@dataclass
class PrecheckResult:
    ok: bool
    stage: int  # how far it got: 1 = deps, 2 = pytest passed, 3 = image built, 4 = container smoke passed
    log: str = ""  # what went wrong (fed back to the LLM)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        names = ["dependencies", "pytest", "docker build", "container smoke checks"]
        passed = ", ".join(names[: self.stage]) or "nothing"
        text = f"{'PASSED' if self.ok else 'FAILED'} (passed: {passed})"
        return text + "".join(f"\n- note: {n}" for n in self.notes)


def _tail(text: str, lines: int = 120) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _run(cmd: list[str], cwd: Path, timeout: int, run=subprocess.run) -> tuple[bool, str]:
    try:
        proc = run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{' '.join(cmd)}: timed out after {timeout}s"
    except FileNotFoundError as exc:
        return False, f"{cmd[0]}: {exc}"
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def run_prechecks(workdir: Path, changes: dict, *, tag: str = "1", run=subprocess.run,
                  docker: str | None = "auto", smoke_fn=run_smoke, log=print) -> PrecheckResult:
    """Everything the CI pipeline would do, but inside this runner, before anything reaches production."""
    workdir = Path(workdir)
    notes: list[str] = []

    if any("requirements" in p for p in changes):
        req = "requirements-dev.txt" if (workdir / "requirements-dev.txt").exists() else "requirements.txt"
        ok, out = _run([sys.executable, "-m", "pip", "install", "-q", "-r", req], workdir, 600, run)
        if not ok:
            return PrecheckResult(False, 0, f"pip install -r {req} failed:\n{_tail(out)}", notes)

    target = ["tests"] if (workdir / "tests").is_dir() else []
    ok, out = _run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", *target], workdir, 900, run)
    if not ok:
        return PrecheckResult(False, 1, f"pytest failed:\n{_tail(out)}", notes)
    log("pre-check: pytest passed")

    docker = shutil.which("docker") if docker == "auto" else docker
    if not docker:
        notes.append("Docker is not available here: docker build and container smoke checks were skipped")
        return PrecheckResult(True, 2, "", notes)

    image = f"agent-precheck:{tag}"
    ok, out = _run([docker, "build", "-t", image, "."], workdir, 900, run)
    if not ok:
        return PrecheckResult(False, 2, f"docker build failed:\n{_tail(out)}", notes)

    name = f"agent-precheck-{tag}"
    _run([docker, "rm", "-f", name], workdir, 60, run)
    ok, out = _run([docker, "run", "-d", "--name", name, "-p", f"{CONTAINER_PORT}:8000", "-e", "PORT=8000", image],
                   workdir, 120, run)
    if not ok:
        return PrecheckResult(False, 3, f"docker run failed:\n{_tail(out)}", notes)
    try:
        report = smoke_fn(f"http://127.0.0.1:{CONTAINER_PORT}", warmup_timeout=60, retries=2, backoff=3, log=log)
        if report.healthy:
            return PrecheckResult(True, 4, "", notes)
        _, logs = _run([docker, "logs", "--tail", "80", name], workdir, 60, run)
        return PrecheckResult(False, 3, f"container smoke checks failed:\n{report.summary()}\n"
                                        f"container logs:\n{_tail(logs, 80)}", notes)
    finally:
        _run([docker, "rm", "-f", name], workdir, 60, run)


# ---- the loop ----------------------------------------------------------------------
@dataclass
class Attempt:
    number: int
    explanation: str = ""
    confidence: float = 0.0
    edits: list[Edit] = field(default_factory=list)
    changes: dict = field(default_factory=dict)
    precheck: PrecheckResult | None = None
    kind: str = SIMPLE
    kind_reason: str = ""
    error: str = ""

    @property
    def score(self) -> int:
        """How good this attempt was (to pick the 'best attempt' after giving up)."""
        if not self.changes:
            return -1
        return self.precheck.stage + (10 if self.precheck.ok else 0) if self.precheck else 0


@dataclass
class FixOutcome:
    status: str  # auto_merge | needs_human | gave_up | llm_unavailable | no_patch
    kind: str
    attempts: list[Attempt]
    message: str
    pr: dict | None = None

    @property
    def llm_calls(self) -> int:
        return len(self.attempts)


def _pr_body(incident: int, attempt: Attempt, kind: str, diagnosis: Diagnosis | None, auto: bool,
             why_not_auto: str) -> str:
    pre = attempt.precheck.summary() if attempt.precheck else "not run"
    failure = f"\n\n<details><summary>Pre-check output</summary>\n\n```\n{_tail(attempt.precheck.log, 60)}\n```\n" \
              f"</details>" if attempt.precheck and not attempt.precheck.ok else ""
    return (
        f"Automated fix for incident #{incident} (attempt {attempt.number}).\n\n"
        f"**Root cause:** {diagnosis.root_cause if diagnosis else 'diagnosis not available'}\n\n"
        f"**What this changes:** {attempt.explanation or '-'}\n\n"
        f"| | |\n|---|---|\n| Kind | {kind.upper()} ({attempt.kind_reason}) |\n"
        f"| Confidence | {attempt.confidence:.2f} |\n| Pre-checks in the runner | {pre.splitlines()[0]} |\n"
        f"| Auto-merge | {'yes, after CI passes' if auto else 'no: ' + why_not_auto} |\n"
        + "".join(f"\n- {n}" for n in (attempt.precheck.notes if attempt.precheck else []))
        + f"\n\n```diff\n{unified_diff(attempt.changes)[:6000]}\n```{failure}\n\n"
        f"_Opened by the incident agent. It never edits tests/ or .github/._"
    )


def open_pr(gh: GitHub, incident: int, attempt: Attempt, base_sha: str, kind: str,
            diagnosis: Diagnosis | None, auto: bool, why_not_auto: str = "") -> dict:
    branch = branch_name(incident, attempt.number)
    try:
        gh.create_branch(branch, base_sha)
    except GitHubError as exc:
        if exc.status != 422:  # 422 = branch already exists (re-run): just add a commit on top
            raise
    files = {path: new for path, (_, new) in attempt.changes.items()}
    gh.commit_files(branch, files, f"fix: incident #{incident} attempt {attempt.number}\n\n{attempt.explanation}")
    title = f"fix(agent): incident #{incident} attempt {attempt.number}"
    if not auto:
        title = "[needs human] " + title
    pr = gh.create_pr(branch, title, _pr_body(incident, attempt, kind, diagnosis, auto, why_not_auto))
    gh.add_labels(pr["number"], [LABEL_AGENT_FIX] + ([] if auto else [LABEL_NEEDS_HUMAN]))
    return pr


def try_auto_merge(gh: GitHub, pr: dict) -> tuple[bool, str]:
    try:
        gh.enable_auto_merge(pr["node_id"])
        return True, "auto-merge enabled; it merges once the required CI checks pass"
    except GitHubError as exc:
        if "clean status" in str(exc).lower():  # checks already passed: auto-merge can't be queued
            gh.merge_pr(pr["number"])
            return True, "checks had already passed, merged"
        return False, f"auto-merge could not be enabled ({exc})"


def run_fix_loop(
    *,
    incident: int,
    evidence: Evidence,
    diagnosis: Diagnosis | None,
    kind: str,
    llm,
    gh: GitHub,
    repo_root: str | Path,
    base_sha: str,
    auto_merge: bool,
    start_attempt: int = 1,
    max_attempts: int = MAX_ATTEMPTS,
    previous_errors: list[str] | None = None,
    prechecks=run_prechecks,
    log=print,
) -> FixOutcome:
    attempts: list[Attempt] = []
    errors = list(previous_errors or [])
    llm_down = ""

    for n in range(start_attempt, max_attempts + 1):
        try:
            proposal = llm.propose_fix(evidence.to_prompt(), diagnosis, evidence.files, errors)
        except LLMUnavailable as exc:
            llm_down = str(exc)
            break
        except LLMBadResponse as exc:  # counts as a failed attempt
            attempts.append(Attempt(n, error=f"invalid LLM answer: {exc}"))
            errors.append(f"Your previous answer was invalid: {exc}")
            continue
        attempt = Attempt(n)
        attempts.append(attempt)
        attempt.explanation, attempt.confidence, attempt.edits = (proposal.explanation, proposal.confidence,
                                                                  proposal.edits)
        log(f"attempt {n}: {len(proposal.edits)} edit(s), confidence {proposal.confidence:.2f}")

        with tempfile.TemporaryDirectory(prefix="agent-fix-") as tmp:
            work = Path(tmp) / "repo"
            shutil.copytree(repo_root, work, ignore=COPY_IGNORE)
            try:
                attempt.changes = compute_changes(work, proposal.edits)
            except (PatchRejected, PatchError) as exc:
                attempt.error = f"patch rejected: {exc}"
                errors.append(attempt.error)
                log(f"attempt {n}: {attempt.error}")
                continue
            write_changes(work, attempt.changes)
            attempt.kind, attempt.kind_reason = patch_kind(attempt.changes)
            attempt.precheck = prechecks(work, attempt.changes, tag=f"{incident}-{n}", log=log)

        if not attempt.precheck.ok:
            attempt.error = attempt.precheck.log
            errors.append(f"Your patch:\n{unified_diff(attempt.changes)[:2000]}\nfailed the pre-checks:\n"
                          f"{attempt.precheck.log}")
            log(f"attempt {n}: pre-checks failed")
            continue

        final_kind = CORE if CORE in (kind, attempt.kind) else SIMPLE
        reasons = []
        if final_kind == CORE:
            reasons.append("CORE change, a human must review it" if kind == CORE
                           else f"upgraded to CORE: {attempt.kind_reason}")
        if attempt.confidence < MIN_AUTO_MERGE_CONFIDENCE:
            reasons.append(f"confidence {attempt.confidence:.2f} < {MIN_AUTO_MERGE_CONFIDENCE}")
        if not auto_merge:
            reasons.append("AGENT_AUTO_MERGE is off")
        auto = not reasons
        pr = open_pr(gh, incident, attempt, base_sha, final_kind, diagnosis, auto, "; ".join(reasons))
        if auto:
            merged, msg = try_auto_merge(gh, pr)
            if merged:
                return FixOutcome("auto_merge", final_kind, attempts, f"attempt {n} passed the pre-checks; {msg}", pr)
            gh.add_labels(pr["number"], [LABEL_NEEDS_HUMAN])
            reasons.append(msg)
        return FixOutcome("needs_human", final_kind, attempts,
                          f"attempt {n} passed the pre-checks; not merged: {'; '.join(reasons)}", pr)

    candidates = [a for a in attempts if a.changes]
    if not attempts and llm_down:
        return FixOutcome("llm_unavailable", kind, attempts, f"diagnosis/fix pending: {llm_down}")
    if not candidates:
        return FixOutcome("no_patch", kind, attempts, "no attempt produced an applicable patch"
                          + (f"; LLM unavailable: {llm_down}" if llm_down else ""))
    best = max(candidates, key=lambda a: a.score)
    pr = open_pr(gh, incident, best, base_sha, CORE if CORE in (kind, best.kind) else SIMPLE, diagnosis, False,
                 f"all {len(attempts)} attempt(s) failed the pre-checks; this is the best one")
    return FixOutcome("gave_up", kind, attempts, f"{len(attempts)} attempt(s) failed; best attempt "
                      f"({best.number}) opened as an unmerged PR" + (f"; LLM unavailable: {llm_down}" if llm_down else ""),
                      pr)
