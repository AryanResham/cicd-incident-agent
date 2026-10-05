"""Evaluation harness (PLAN §C): runs the failure scenarios in scenarios/*.json locally.

    python -m agent.eval --dry-run              # no Gemini: classification + freeze + "does the bug still apply?"
    python -m agent.eval --plant-check          # no Gemini: plant each bug, check it fails where it should
                                                # (pytest; docker build + container smoke when Docker exists)
    python -m agent.eval --only S1,C1 --rpm 4   # real: plant the bug in a scratch copy, run the pre-check path
                                                # (pytest, docker when available), Gemini diagnosis + fix loop

Nothing touches GitHub or Render: PRs are recorded by a local stand-in.
Results go to scenarios/results[-dry-run|-plant-check].md and .json (git-ignored).
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

from agent.classify import Classification, classify, combine
from agent.collect import ALWAYS_INCLUDE, Evidence, FailedJob, Masker, TestFailure, paths_from_text, read_files
from agent.fix import COPY_IGNORE, run_fix_loop, run_prechecks
from agent.freeze import should_deploy
from agent.llm import GeminiClient, LLMBadResponse, LLMUnavailable

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "scenarios"
CHECKS = ["GET /health", "GET /", "GET /api/todos", "round trip", "latency"]


class BreakError(ValueError):
    """A scenario's bug can't be planted: the app code doesn't look like the scenario expects (re-base it)."""


# ---- scenarios -------------------------------------------------------------------------------
def load_scenarios(directory: Path = SCENARIO_DIR, only: list[str] | None = None) -> list[dict]:
    scenarios = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))
                 if not p.name.startswith("results")]
    order = {"simple": 0, "core": 1, "no_fix": 2, "guardrail": 3}
    scenarios.sort(key=lambda s: (order.get(s["group"], 9), s["id"]))
    if only:
        wanted = {x.strip().upper() for x in only}
        scenarios = [s for s in scenarios if s["id"].upper() in wanted]
    return scenarios


def apply_breaks(root: Path, breaks: list[dict]) -> dict[str, tuple[str, str]]:
    """Plant the bug. Ops: regex/replace (first match), search/replace, prepend, append."""
    changes: dict[str, tuple[str, str]] = {}
    for b in breaks:
        path = root / b["file"]
        old = changes[b["file"]][1] if b["file"] in changes else (path.read_text(encoding="utf-8")
                                                                   if path.is_file() else None)
        if "regex" in b:
            if old is None:
                raise BreakError(f"{b['file']} does not exist")
            new, n = re.subn(b["regex"], b["replace"], old, count=1, flags=re.M)
            if n == 0:
                raise BreakError(f"{b['file']}: pattern {b['regex']!r} not found")
        elif "search" in b:
            if old is None or b["search"] not in old:
                raise BreakError(f"{b['file']}: search text not found")
            new = old.replace(b["search"], b["replace"], 1)
        elif "prepend" in b:
            if old is None:
                raise BreakError(f"{b['file']} does not exist")
            new = b["prepend"] + old
        elif "append" in b:
            new = (old or "") + ("" if not old or old.endswith("\n") else "\n") + b["append"]
        else:
            raise BreakError(f"unknown break operation in {b}")
        first_old = changes[b["file"]][0] if b["file"] in changes else (old or "")
        changes[b["file"]] = (first_old, new)
    for rel, (_, new) in changes.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(new, encoding="utf-8", newline="\n")
    return changes


def evidence_diff(changes: dict[str, tuple[str, str]]) -> str:
    """Same layout as collect.Evidence.diff: '--- path (modified)' + changed lines."""
    out = []
    for path, (old, new) in changes.items():
        out.append(f"--- {path} (modified)")
        out += [line for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
                if not line.startswith(("+++", "---"))]
    return "\n".join(out)


def simulated_evidence(s: dict) -> Evidence:
    sim = s["simulated"]
    ev = Evidence(trigger=sim["trigger"], changed_files=list(sim.get("changed_files", [])), diff=sim.get("diff", ""))
    ev.failed_jobs = [FailedJob(j["name"], "failure", [], j.get("log", "")) for j in sim.get("failed_jobs", [])]
    ev.junit_failures = [TestFailure(n, "assertion failed", "") for n in sim.get("junit", [])]
    if "smoke_failed" in sim:
        ev.smoke = {"checks": [{"name": c, "ok": c not in sim["smoke_failed"], "detail": ""} for c in CHECKS]}
    return ev


class _FreezeGitHub:
    """Just enough GitHub for the freeze rule: one open incident, the pushed commit came from a feature branch."""

    def find_open_incident(self):
        return {"number": 1}

    def prs_for_commit(self, sha):
        return [{"number": 2, "head": {"ref": "feature/random-change"}}] if sha == "random" else \
            [{"number": 3, "head": {"ref": "agent/fix-1-1"}}]


def check_freeze() -> tuple[bool, str]:
    blocked = not should_deploy(_FreezeGitHub(), "random")[0]
    allowed = should_deploy(_FreezeGitHub(), "fix")[0]
    return blocked and allowed, f"random push deploy={not blocked}, agent fix deploy={allowed}"


# ---- dry run ---------------------------------------------------------------------------------
def break_status(s: dict, repo_root: Path) -> str:
    if not s.get("breaks"):
        return "manual"
    if not (repo_root / "app" / "main.py").is_file():
        return "no app code"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(repo_root, work, ignore=COPY_IGNORE)
        try:
            apply_breaks(work, s["breaks"])
            return "applies"
        except BreakError as exc:
            return f"needs re-base ({exc})"


def dry_run(s: dict, repo_root: Path = ROOT) -> dict:
    row = {"id": s["id"], "group": s["group"], "expected": s["expected"]["kind"],
           "expected_category": s["expected"]["category"], "bug": break_status(s, repo_root)}
    if s["simulated"].get("trigger") == "freeze":
        ok, detail = check_freeze()
        row.update(got="freeze", category="-", correct=ok, detail=detail)
        return row
    cls = classify(simulated_evidence(s))
    row.update(got=cls.kind, category=cls.category, correct=cls.kind == s["expected"]["kind"]
               and cls.category == s["expected"]["category"], detail=cls.reason)
    return row


# ---- plant check (no Gemini) -----------------------------------------------------------------
def expected_stage(s: dict) -> str:
    """Where the scenario's bug must be caught: 'test' (pytest), 'build' (docker build) or 'deploy'."""
    caught = s.get("caught_by", "").lower()
    if caught.startswith("ci (test"):
        return "test"
    if caught.startswith("ci (build"):
        return "build"
    return "deploy"


def plant_check(s: dict, repo_root: Path = ROOT, prechecks=run_prechecks, log=print) -> dict:
    """Plant the bug in a scratch copy and check it fails exactly where the scenario says.

    CI (test) bugs must fail pytest; CI (build) bugs must pass pytest and fail `docker build`;
    deploy-only bugs must pass pytest and the build, and fail the container smoke checks.
    Without Docker only the pytest part can be checked (the row says so).
    """
    row = {"id": s["id"], "group": s["group"], "caught_by": s.get("caught_by", "-"), "expected": expected_stage(s)}
    if not s.get("breaks"):
        row.update(expected="-", got="-", ok=None, checked="-", detail="manual: " + s.get("manual", "see README"))
        return row
    with tempfile.TemporaryDirectory(prefix=f"plant-{s['id']}-") as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(repo_root, work, ignore=COPY_IGNORE)
        try:
            apply_breaks(work, s["breaks"])
        except BreakError as exc:
            row.update(got="needs re-base", ok=False, checked="-", detail=str(exc))
            return row
        # No changed paths: never pip-install a planted requirements file into this environment.
        result = prechecks(work, {}, tag=f"plant-{s['id'].lower()}", log=log)

    docker = not any("Docker is not available" in n for n in result.notes)
    got = ("passes" if result.ok else "test" if result.stage <= 1 else "build" if result.stage == 2 else "deploy")
    last = [line for line in result.log.splitlines() if line.strip()]
    row.update(got=got, checked="pytest" if got == "test" else "pytest + docker" if docker
               else "pytest only (no Docker)", detail=last[-1][:160] if last else "")
    if docker or row["expected"] == "test":
        row["ok"] = got == row["expected"]
    else:  # build/deploy bugs must at least pass the tests (that's what lets them through CI)
        row["ok"] = got == "passes"
        row["detail"] = f"pytest passes as designed; Docker is needed to confirm the {row['expected']} failure"
    return row


# ---- real run --------------------------------------------------------------------------------
class LocalGitHub:
    """Records what the fix loop would do on GitHub (branches, PRs, auto-merge) without any network."""

    def __init__(self):
        self.prs: list[dict] = []
        self.auto_merged: list[int] = []
        self.labels: dict[int, list[str]] = {}

    def create_branch(self, branch, sha):
        pass

    def commit_files(self, branch, files, message):
        return "local"

    def create_pr(self, head, title, body, base="main"):
        pr = {"number": len(self.prs) + 1, "node_id": f"local-{len(self.prs) + 1}", "head": {"ref": head},
              "title": title, "body": body, "html_url": "(local)"}
        self.prs.append(pr)
        return pr

    def add_labels(self, number, labels):
        self.labels.setdefault(number, []).extend(labels)

    def enable_auto_merge(self, node_id, method="SQUASH"):
        self.auto_merged.append(int(node_id.split("-")[1]))

    def merge_pr(self, number, method="squash"):
        self.auto_merged.append(number)


def real_run(s: dict, llm: GeminiClient, repo_root: Path = ROOT, prechecks=run_prechecks, log=print) -> dict:
    row = {"id": s["id"], "group": s["group"], "expected": s["expected"]["kind"]}
    if not s.get("breaks"):
        row.update(status="skipped", detail="manual scenario: " + s.get("manual", "see README"))
        return row
    start, calls_before = time.monotonic(), llm.calls
    with tempfile.TemporaryDirectory(prefix=f"eval-{s['id']}-") as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(repo_root, work, ignore=COPY_IGNORE)
        try:
            planted = apply_breaks(work, s["breaks"])
        except BreakError as exc:
            row.update(status="needs re-base", detail=str(exc))
            return row

        broken = prechecks(work, planted, tag=f"eval-{s['id'].lower()}", log=log)
        row["caught_by_prechecks"] = not broken.ok
        if broken.ok:
            row.update(status="not caught", detail="the planted bug passed the local pre-checks"
                       + (f" ({'; '.join(broken.notes)})" if broken.notes else ""))
            return row

        trigger = "deploy_failure" if broken.stage >= 2 else "ci_failure"
        ev = Evidence(trigger=trigger, head_sha="broken", stable_sha="stable", changed_files=list(planted),
                      diff=evidence_diff(planted))
        ev.failed_jobs = [FailedJob("pre-checks", "failure", [], Masker.from_env()(broken.log))]
        wanted = set(planted) | paths_from_text(broken.log) | set(ALWAYS_INCLUDE)
        ev.files = read_files(work, {p for p in wanted if not p.startswith("tests/") or p in planted}, Masker())

        cls = classify(ev)
        row.update(got=cls.kind, category=cls.category)
        diagnosis = None
        try:
            diagnosis = llm.diagnose(ev.to_prompt(), cls)
            kind = combine(cls, diagnosis.kind)
            text = diagnosis.root_cause.lower()
            row["root_cause_ok"] = any(k.lower() in text for k in s.get("root_cause_keywords", []))
            row["root_cause"] = diagnosis.root_cause
        except LLMBadResponse as exc:
            kind, row["root_cause"] = cls.kind, f"invalid LLM answer: {exc}"
        except LLMUnavailable as exc:
            row.update(status="llm unavailable", detail=str(exc), llm_calls=llm.calls - calls_before)
            return row
        row["kind"] = kind
        row["kind_ok"] = kind == s["expected"]["kind"]

        if kind == "issue_only":
            row.update(status="issue only", llm_calls=llm.calls - calls_before)
            return row
        gh = LocalGitHub()
        outcome = run_fix_loop(incident=0, evidence=ev, diagnosis=diagnosis, kind=kind, llm=llm, gh=gh,
                               repo_root=work, base_sha="local", auto_merge=True, prechecks=prechecks, log=log)
        passed = [a for a in outcome.attempts if a.precheck and a.precheck.ok]
        edited_tests = any(p.startswith("tests/") for a in outcome.attempts for p in a.changes)
        row.update(
            status=outcome.status, attempts=len(outcome.attempts),
            fixed_on=passed[0].number if passed else None,
            auto_merged=bool(gh.auto_merged),
            safe=not edited_tests and not (s["expected"]["kind"] == "core" and gh.auto_merged),
            llm_calls=llm.calls - calls_before, seconds=round(time.monotonic() - start, 1),
            detail=outcome.message,
        )
    return row


# ---- output ----------------------------------------------------------------------------------
def _cell(value) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "**no**"
    return "-" if value is None else str(value).replace("|", "/").replace("\n", " ")[:120]


def to_markdown(rows: list[dict], columns: list[str], title: str) -> str:
    lines = [f"# {title}", "", "| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(_cell(r.get(c)) for c in columns) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def summary(rows: list[dict], key: str) -> str:
    scored = [r for r in rows if isinstance(r.get(key), bool)]
    ok = sum(r[key] for r in scored)
    return f"{key}: {ok}/{len(scored)}" if scored else f"{key}: n/a"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the failure scenarios locally.")
    parser.add_argument("--dry-run", action="store_true", help="no Gemini: classification + freeze only")
    parser.add_argument("--plant-check", action="store_true",
                        help="no Gemini: plant each bug and check it fails where expected (pytest, docker)")
    parser.add_argument("--only", help="comma-separated scenario ids, e.g. S1,C1")
    parser.add_argument("--rpm", type=float, default=4.0, help="Gemini requests per minute (free tier: keep low)")
    parser.add_argument("--max-calls", type=int, default=40, help="stop before using more Gemini calls than this")
    parser.add_argument("--out", help="output path without extension (default scenarios/results[-dry-run])")
    args = parser.parse_args(argv)

    scenarios = load_scenarios(only=args.only.split(",") if args.only else None)
    if not scenarios:
        print("no scenarios selected")
        return 1
    default = "results-dry-run" if args.dry_run else "results-plant-check" if args.plant_check else "results"
    out = Path(args.out) if args.out else SCENARIO_DIR / default

    rows = []
    if args.plant_check:
        for s in scenarios:
            print(f"=== {s['id']}: {s['title']}")
            rows.append(plant_check(s, log=lambda *_: None))
        columns = ["id", "group", "caught_by", "expected", "got", "ok", "checked", "detail"]
        text = to_markdown(rows, columns, "Scenario plant check (no Gemini: does each bug fail where it should?)")
        text += f"\n{summary(rows, 'ok')}\n"
    elif args.dry_run:
        for s in scenarios:
            rows.append(dry_run(s))
        columns = ["id", "group", "expected", "got", "expected_category", "category", "correct", "bug", "detail"]
        text = to_markdown(rows, columns, "Scenario eval (dry run: rules only, no Gemini)")
        text += f"\n{summary(rows, 'correct')}\n"
    else:
        llm = GeminiClient.from_env(rpm=args.rpm)
        for s in scenarios:
            if llm.calls >= args.max_calls:
                rows.append({"id": s["id"], "status": "skipped", "detail": f"--max-calls {args.max_calls} reached"})
                continue
            print(f"=== {s['id']}: {s['title']}")
            rows.append(real_run(s, llm))
        columns = ["id", "group", "expected", "kind", "kind_ok", "root_cause_ok", "status", "attempts", "fixed_on",
                   "auto_merged", "safe", "llm_calls", "seconds", "detail"]
        text = to_markdown(rows, columns, "Scenario eval (real: pre-check path + Gemini)")
        text += "\n" + " · ".join(summary(rows, k) for k in ("kind_ok", "root_cause_ok", "safe"))
        text += f" · total Gemini calls: {llm.calls}\n"

    out.with_suffix(".md").write_text(text, encoding="utf-8")
    out.with_suffix(".json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(text)
    print(f"written: {out.with_suffix('.md')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
