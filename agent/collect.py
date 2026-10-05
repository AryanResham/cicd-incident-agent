"""Evidence collection (PLAN §B3 ②): failed logs, JUnit failures, smoke results, diff, files.

Everything that leaves the runner (the Gemini prompt, issue comments) goes
through `Masker` first, so secrets never end up in a prompt or an issue.
"""

from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from agent.github_api import GitHub

LOG_TAIL_LINES = 300
DIFF_LIMIT = 15_000
FILE_LIMIT = 12_000
JUNIT_ARTIFACT = "junit-results"
SECRET_ENV_VARS = ("GITHUB_TOKEN", "AGENT_TOKEN", "GEMINI_API_KEY", "RENDER_DEPLOY_HOOK_URL", "RENDER_API_KEY")
ALWAYS_INCLUDE = ("requirements.txt", "Dockerfile")

# ---- secret masking --------------------------------------------------------
TOKEN_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API keys (Gemini)
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
# "Bearer xyz", "?key=xyz", "API_TOKEN=xyz", "password: xyz" -> keep the name, hide the value.
NAMED_SECRET = re.compile(
    r"(?i)(\bbearer\s+|[?&](?:key|token|secret|sig|signature)=|"
    r"\b\w*(?:token|secret|password|passwd|api_?key)\w*\s*[:=]\s*[\"']?)([A-Za-z0-9._~+/\-]{6,})"
)


class Masker:
    """Replaces known secret values (from the environment) and token-looking strings with ***."""

    def __init__(self, secrets: list[str] | None = None):
        values = {s.strip() for s in secrets or [] if s and len(s.strip()) >= 6}
        self.secrets = sorted(values, key=len, reverse=True)  # longest first

    @classmethod
    def from_env(cls, env=os.environ) -> "Masker":
        return cls([env.get(name, "") for name in SECRET_ENV_VARS])

    def __call__(self, text: str, aggressive: bool = True) -> str:
        """aggressive=False skips the name=value heuristic (used for source files, so code stays intact)."""
        for secret in self.secrets:
            text = text.replace(secret, "***")
        for pattern in TOKEN_PATTERNS:
            text = pattern.sub("***", text)
        if aggressive:
            text = NAMED_SECRET.sub(lambda m: m.group(1) + "***", text)
        return text


# ---- data ------------------------------------------------------------------
@dataclass
class FailedJob:
    name: str
    conclusion: str
    failed_steps: list[str]
    log_tail: str


@dataclass
class TestFailure:
    name: str
    message: str
    details: str


@dataclass
class Evidence:
    trigger: str
    run_id: str | None = None
    run_url: str | None = None
    head_sha: str | None = None
    head_branch: str | None = None
    stable_sha: str | None = None
    failed_jobs: list[FailedJob] = field(default_factory=list)
    junit_failures: list[TestFailure] = field(default_factory=list)
    smoke: dict | None = None
    changed_files: list[str] = field(default_factory=list)
    diff: str = ""
    files: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def failed_job_names(self) -> list[str]:
        return [j.name for j in self.failed_jobs]

    def failure_text(self) -> str:
        """Logs + test failures + failed smoke checks: what the classification rules read."""
        parts = [j.log_tail for j in self.failed_jobs]
        parts += [f"{t.name}: {t.message}\n{t.details}" for t in self.junit_failures]
        if self.smoke:
            parts += [f"smoke {c['name']} [{c.get('status_code')}]: {c.get('detail', '')}"
                      for c in self.smoke.get("checks", []) if not c.get("ok")]
        return "\n".join(parts)

    def to_prompt(self, max_chars: int = 30_000) -> str:
        """Compact, already-masked text for the LLM."""
        out = [f"Trigger: {self.trigger}", f"Broken commit: {self.head_sha} (branch {self.head_branch})",
               f"Last stable commit: {self.stable_sha}"]
        for job in self.failed_jobs:
            out.append(f"\n## Failed job '{job.name}' (failed steps: {', '.join(job.failed_steps) or '?'})"
                       f"\n```\n{job.log_tail}\n```")
        if self.junit_failures:
            out.append("\n## Failing tests (JUnit)")
            out += [f"- {t.name}: {t.message}\n```\n{t.details[:1500]}\n```" for t in self.junit_failures[:10]]
        if self.smoke:
            out.append("\n## Smoke check results")
            out += [f"- {'PASS' if c.get('ok') else 'FAIL'} {c['name']} [{c.get('status_code')}] {c.get('detail', '')}"
                    for c in self.smoke.get("checks", [])]
        if self.diff:
            out.append(f"\n## Diff stable..broken\n```diff\n{self.diff}\n```")
        out += [f"\nNote: {n}" for n in self.notes]
        text = "\n".join(out)
        if len(text) > max_chars:
            text = text[: max_chars // 2] + "\n...[trimmed]...\n" + text[-max_chars // 2:]
        return text


# ---- logs ------------------------------------------------------------------
TIMESTAMP = re.compile(r"^﻿?\d{4}-\d\d-\d\dT[\d:.]+Z ")


def clean_log(raw: str) -> list[str]:
    """Drop the timestamp GitHub puts in front of every log line."""
    return [TIMESTAMP.sub("", line) for line in raw.splitlines()]


def tail_failed_step(lines: list[str], n: int = LOG_TAIL_LINES) -> list[str]:
    """~n lines ending just after the first `##[error]` (that's the failed step); else the log tail."""
    for i, line in enumerate(lines):
        if "##[error]" in line:
            end = min(len(lines), i + 20)
            return lines[max(0, end - n): end]
    return lines[-n:]


# ---- junit -----------------------------------------------------------------
def parse_junit(xml_text: str) -> list[TestFailure]:
    failures = []
    root = ET.fromstring(xml_text)
    for case in root.iter("testcase"):
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
                failures.append(TestFailure(name, node.get("message", "")[:500], (node.text or "")[-3000:]))
    return failures


def junit_from_zip(data: bytes) -> list[TestFailure]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if name.endswith(".xml"):
                return parse_junit(zf.read(name).decode("utf-8", "replace"))
    return []


# ---- files -----------------------------------------------------------------
ROOT_FILES = re.compile(r"Dockerfile|\.dockerignore|requirements[\w-]*\.txt")
PATH_IN_TEXT = re.compile(r"(?<![\w.-])((?:app|tests)/[\w./-]+\.(?:py|html|txt|json|toml|ini|cfg))")


def paths_from_text(text: str) -> set[str]:
    """Repo-relative paths mentioned in logs/tracebacks (e.g. /home/runner/work/x/x/app/main.py)."""
    found = {m.group(1) for m in PATH_IN_TEXT.finditer(text)}
    for name in ("Dockerfile", "requirements.txt", ".dockerignore"):
        if name in text:
            found.add(name)
    return found


def read_files(repo_root: str | Path, paths: set[str] | list[str], masker: Masker) -> dict[str, str]:
    root = Path(repo_root).resolve()
    files = {}
    for rel in sorted(paths):
        path = (root / rel).resolve()
        if root not in path.parents or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > FILE_LIMIT:
            text = text[:FILE_LIMIT] + "\n# ...[file trimmed]..."
        files[rel] = masker(text, aggressive=False)
    return files


# ---- main entry --------------------------------------------------------------
def collect(
    gh: GitHub,
    *,
    trigger: str,
    run: dict | None = None,
    stable_sha: str | None = None,
    smoke: dict | None = None,
    repo_root: str | Path = ".",
    masker: Masker | None = None,
) -> Evidence:
    """Gather everything we know about the failure. Each source is optional and failures are noted."""
    masker = masker or Masker.from_env()
    ev = Evidence(trigger=trigger, stable_sha=stable_sha, smoke=smoke)
    if run:
        ev.run_id, ev.run_url = str(run["id"]), run.get("html_url")
        ev.head_sha, ev.head_branch = run.get("head_sha"), run.get("head_branch")
        try:
            for job in gh.list_jobs(run["id"]):
                if job.get("conclusion") not in ("failure", "timed_out"):
                    continue
                steps = [s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"]
                tail = tail_failed_step(clean_log(gh.job_logs(job["id"])))
                ev.failed_jobs.append(FailedJob(job["name"], job["conclusion"], steps, masker("\n".join(tail))))
        except Exception as exc:  # evidence is best effort; the incident must go on
            ev.notes.append(f"could not read job logs: {exc}")
        try:
            for art in gh.list_artifacts(run["id"]):
                if art["name"] == JUNIT_ARTIFACT:
                    ev.junit_failures = junit_from_zip(gh.download_artifact(art["id"]))
            for t in ev.junit_failures:
                t.message, t.details = masker(t.message), masker(t.details)
        except Exception as exc:
            ev.notes.append(f"could not read JUnit results: {exc}")

    if stable_sha and ev.head_sha and stable_sha != ev.head_sha:
        try:
            cmp = gh.compare(stable_sha, ev.head_sha)
            patches = []
            for f in cmp.get("files", []):
                ev.changed_files.append(f["filename"])
                patches.append(f"--- {f['filename']} ({f.get('status')})\n{f.get('patch', '(binary or too large)')}")
            diff = "\n".join(patches)
            ev.diff = masker(diff[:DIFF_LIMIT] + ("\n...[diff trimmed]..." if len(diff) > DIFF_LIMIT else ""))
        except Exception as exc:
            ev.notes.append(f"could not compute diff {stable_sha[:7]}..{ev.head_sha[:7]}: {exc}")

    wanted = set(ev.changed_files) | paths_from_text(ev.failure_text()) | set(ALWAYS_INCLUDE)
    wanted = {p for p in wanted if p.startswith(("app/", "tests/")) or ROOT_FILES.fullmatch(p)}
    ev.files = read_files(repo_root, wanted, masker)
    return ev
