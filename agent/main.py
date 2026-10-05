"""Incident agent entry point (PLAN §B3): rollback first, then diagnose, then fix or hand over.

    python -m agent.main --trigger ci_failure|deploy_failure|health --run-id <id>

Extra triggers used by the workflows:
    --trigger verify_success --run-id <id>   a CI/CD run passed verify: close the incident if it was the fix
    --trigger health_ok                      the health monitor is green: close open incident:live-down issues
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from agent import report
from agent.classify import ISSUE_ONLY, RERUN, Classification, classify, combine
from agent.collect import Evidence, Masker, collect
from agent.fix import MAX_ATTEMPTS, attempts_used, run_fix_loop
from agent.freeze import fix_pr_for_commit
from agent.github_api import (LABEL_ACTIVE, LABEL_LIVE_DOWN, LABEL_NEEDS_HUMAN, LABEL_ROLLBACK_FAILED, GitHub)
from agent.llm import Diagnosis, GeminiClient, LLMBadResponse, LLMUnavailable
from agent.notify import notify
from agent.rollback import RollbackResult, find_last_stable_sha, rollback
from agent.smoke import run_smoke

TRIGGERS = ("ci_failure", "deploy_failure", "health", "verify_success", "health_ok")


def flag(env, name: str) -> bool:
    """Kill switches: only the string "false" turns a feature off; default is on."""
    return str(env.get(name, "true")).strip().lower() != "false"


def _labels(issue: dict) -> set[str]:
    return {lbl["name"] for lbl in issue.get("labels", [])}


@dataclass
class Agent:
    """Everything the orchestration needs; tests swap in fakes."""
    gh: GitHub
    env: dict = field(default_factory=lambda: dict(os.environ))
    llm_factory: object = None
    rollback_fn: object = rollback
    smoke_fn: object = run_smoke
    prechecks: object = None
    collect_fn: object = collect
    repo_root: Path = Path(".")
    log: object = print

    def __post_init__(self):
        self.llm_factory = self.llm_factory or (lambda: GeminiClient.from_env(self.env, log=self.log))
        self.masker = Masker.from_env(self.env)

    # ---- small helpers ---------------------------------------------------------------
    def base_sha(self) -> str:
        """The commit the fix branches start from: the checked-out main (falls back to the API)."""
        try:
            out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo_root, capture_output=True,
                                 text=True, timeout=30)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return self.gh.get_branch_sha("main")

    def say(self, issue: int, state: dict, text: str, labels: list[str] | None = None) -> None:
        """Timeline event: comment on the issue and refresh the report in the issue body."""
        report.add_event(state, text.splitlines()[0][:200])
        notify(self.gh, text, issue=issue, labels=labels, log=self.log)
        self.save(issue, state)

    def save(self, issue: int, state: dict) -> None:
        body = self.gh.get_issue(issue).get("body", "")
        self.gh.update_issue(issue, body=report.rebuild_body(body, state))

    def do_rollback(self, broken_sha: str | None) -> RollbackResult | None:
        if not flag(self.env, "AGENT_ROLLBACK"):
            self.log("rollback disabled by AGENT_ROLLBACK=false")
            return None
        return self.rollback_fn(self.gh, hook_url=self.env.get("RENDER_DEPLOY_HOOK_URL"),
                                app_url=self.env.get("RENDER_APP_URL"), broken_sha=broken_sha,
                                smoke_fn=self.smoke_fn, log=self.log)

    def rollback_text(self, rb: RollbackResult | None) -> str:
        if rb is None:
            return "Rollback skipped (AGENT_ROLLBACK=false)."
        if rb.ok:
            return (f"Rolled back to stable `{rb.stable_sha[:7]}` in {rb.seconds:.0f}s: "
                    f"the stable version is live and passes the smoke checks.")
        return f"ROLLBACK FAILED: {rb.reason}. Stopping automation; a human is needed."

    # ---- entry ----------------------------------------------------------------------
    def run(self, trigger: str, run_id: str | None = None, smoke: dict | None = None) -> int:
        if trigger == "health_ok":
            return self.handle_health_ok()
        if not flag(self.env, "AGENT_ENABLED"):
            self.log("AGENT_ENABLED=false: the incident agent is switched off, doing nothing")
            return 0
        self.gh.ensure_labels()
        if trigger == "verify_success":
            return self.handle_verify_success(run_id)
        if trigger == "health":
            return self.handle_health(run_id, smoke)
        return self.handle_pipeline_failure(trigger, run_id)

    # ---- CI / deploy failures -------------------------------------------------------------
    def handle_pipeline_failure(self, trigger: str, run_id: str) -> int:
        run = self.gh.get_run(run_id)
        sha, branch = run["head_sha"], run.get("head_branch", "")
        incident = self.gh.find_open_incident()

        if branch.startswith("agent/"):  # loop guard: the agent's own branches never open incidents
            if incident:
                notify(self.gh, f"CI failed on the agent's branch `{branch}` ([run]({run.get('html_url')})). "
                       "This belongs to the current incident; no new incident opened.",
                       issue=incident["number"], log=self.log)
            return 0
        if branch != "main":
            self.log(f"ignoring failure on branch {branch!r}: incidents are only about main")
            return 0

        fix_pr = fix_pr_for_commit(self.gh, sha)
        if incident:
            return self.continue_incident(incident, trigger, run, fix_pr)
        return self.new_incident(trigger, run)

    def new_incident(self, trigger: str, run: dict) -> int:
        production = trigger == "deploy_failure"
        sha = run["head_sha"]
        state = {"status": "open", "trigger": trigger, "broken_sha": sha, "run_url": run.get("html_url"),
                 "detected_at": run.get("updated_at") or report.now_iso(), "llm_calls": 0, "actions": []}
        report.add_event(state, f"{trigger} detected on `{sha[:7]}`", state["detected_at"])

        rb = self.do_rollback(sha) if production else None  # ① production first, before anything else
        if production:
            report.add_event(state, self.rollback_text(rb))
            state["actions"].append(self.rollback_text(rb))
            if rb and rb.ok:
                state["rollback_seconds"] = rb.seconds
        stable = (rb.stable_sha if rb and rb.stable_sha else None) or find_last_stable_sha(self.gh, exclude={sha})
        state["stable_sha"] = stable

        ev = self.collect_fn(self.gh, trigger=trigger, run=run, stable_sha=stable, repo_root=self.repo_root,
                             masker=self.masker)  # ②
        cls = classify(ev)  # ③ rules
        state.update(category=cls.category, kind=cls.kind, what_failed=self._what_failed(ev),
                     root_cause=f"(rules) {cls.reason}")
        report.add_event(state, f"classified by rules: {cls.category} / {cls.kind.upper()} ({cls.reason})")

        if cls.kind == RERUN:
            if run.get("run_attempt", 1) == 1:
                self.gh.rerun_failed_jobs(run["id"])
                self.log(f"flaky failure ({cls.signals[0] if cls.signals else ''}): re-ran the failed jobs once")
                return 0
            cls = Classification("flaky", ISSUE_ONLY, "CI infrastructure error repeated after a re-run", cls.signals)
            state["kind"] = cls.kind

        labels = [LABEL_ACTIVE]
        if rb is not None and not rb.ok:
            labels += [LABEL_NEEDS_HUMAN, LABEL_ROLLBACK_FAILED]
        title = f"Incident: {trigger.replace('_', ' ')} on {sha[:7]} ({cls.category})"
        number = notify(self.gh, report.build_body(state, report.evidence_markdown(ev)), title=title,
                        labels=labels, log=self.log)

        if rb is not None and not rb.ok:  # the rollback itself failed: escalate, no more automation
            msg = self.rollback_text(rb)
            if rb.missing_secret:
                msg += f"\n\nSet the repository secret/variable **`{rb.missing_secret}`** and re-run."
            self.say(number, state, msg)
            return 0
        return self.diagnose_and_fix(number, state, ev, cls)

    def diagnose_and_fix(self, number: int, state: dict, ev: Evidence, cls: Classification,
                         start_attempt: int = 1, previous_errors: list[str] | None = None,
                         diagnosis: Diagnosis | None = None) -> int:
        if cls.kind == ISSUE_ONLY:
            state["actions"].append("issue only: no code change can fix this")
            self.say(number, state, f"**No code fix possible** ({cls.category}): {cls.reason}.\n\n"
                     "Signals: " + "; ".join(f"`{s}`" for s in cls.signals[:5]), labels=[LABEL_NEEDS_HUMAN])
            return 0

        llm = self.llm_factory()
        kind = cls.kind
        if diagnosis is None:
            try:
                diagnosis = llm.diagnose(ev.to_prompt(), cls)
                kind = combine(cls, diagnosis.kind)
                state.update(root_cause=diagnosis.root_cause, confidence=diagnosis.confidence, kind=kind)
                self.say(number, state, f"Diagnosis ({kind.upper()}, confidence {diagnosis.confidence:.2f}): "
                         f"{diagnosis.root_cause}")
            except LLMUnavailable as exc:
                state["llm_calls"] = state.get("llm_calls", 0) + getattr(llm, "calls", 0)
                state["actions"].append("diagnosis pending (Gemini unavailable)")
                state["diagnosis_pending"] = True  # a re-run of the same failure resumes this incident
                self.say(number, state, f"**Diagnosis pending** (Gemini unavailable: {exc}). The stable version "
                         "is still live. Re-run this incident later with the `workflow_dispatch` of the "
                         "incident agent workflow.", labels=[LABEL_NEEDS_HUMAN])
                return 0
            except LLMBadResponse as exc:
                report.add_event(state, f"LLM diagnosis invalid ({exc}); continuing with the rule-based diagnosis")

        outcome = run_fix_loop(
            incident=number, evidence=ev, diagnosis=diagnosis, kind=kind, llm=llm, gh=self.gh,
            repo_root=self.repo_root, base_sha=self.base_sha(), auto_merge=flag(self.env, "AGENT_AUTO_MERGE"),
            start_attempt=start_attempt, previous_errors=previous_errors, log=self.log,
            **({"prechecks": self.prechecks} if self.prechecks else {}))
        state["llm_calls"] = state.get("llm_calls", 0) + getattr(llm, "calls", 0)
        state["kind"] = outcome.kind
        for a in outcome.attempts:
            state["actions"].append(f"attempt {a.number}: " + ("pre-checks passed" if a.precheck and a.precheck.ok
                                                                else f"failed ({a.error.splitlines()[0][:100] if a.error else '?'})"))
        pr_link = f" PR #{outcome.pr['number']}" if outcome.pr else ""
        state["actions"].append(f"{outcome.status}:{pr_link} {outcome.message}")
        labels = [] if outcome.status == "auto_merge" else [LABEL_NEEDS_HUMAN]
        extra = ("\n\nThe incident closes (and the deploy freeze lifts) when the fix is deployed and passes the "
                 "smoke checks." if outcome.status == "auto_merge" else
                 "\n\nThe stable version stays live and the deploy freeze stays on until a human merges or closes this.")
        self.say(number, state, f"**Fix: {outcome.status}**{pr_link}: {outcome.message}{extra}", labels=labels)
        return 0

    def continue_incident(self, incident: dict, trigger: str, run: dict, fix_pr: dict | None) -> int:
        """A failure while an incident is open: part of the current incident, never a new one."""
        number, sha = incident["number"], run["head_sha"]
        state = report.read_state(incident.get("body")) or {"actions": [], "timeline": []}
        state.setdefault("actions", [])
        production = trigger == "deploy_failure"

        # The same failure handled again (workflow_dispatch) after Gemini was unavailable: resume it.
        pending = state.get("diagnosis_pending", "diagnosis pending (Gemini unavailable)" in state["actions"])
        if pending and state.get("broken_sha") == sha:
            return self.resume_pending(number, state, run)

        if not production and fix_pr is None:
            self.say(number, state, f"CI also failed on `{sha[:7]}` ([run]({run.get('html_url')})) while this "
                     "incident is open. One incident at a time: it is not handled separately.")
            return 0

        if production:
            rb = self.do_rollback(sha)
            state["actions"].append(self.rollback_text(rb))
            if rb is not None and not rb.ok:
                self.say(number, state, self.rollback_text(rb), labels=[LABEL_NEEDS_HUMAN, LABEL_ROLLBACK_FAILED])
                return 0
            self.say(number, state, f"{trigger} on `{sha[:7]}`. {self.rollback_text(rb)}")

        if fix_pr is None:
            return 0  # e.g. a human bypassed the freeze; we rolled back and noted it

        used = attempts_used(self.gh, number)
        what = "was deployed but failed the live checks" if production else "was merged but CI failed on main"
        if used >= MAX_ATTEMPTS:
            state["actions"].append(f"gave up after {used} attempts")
            self.say(number, state, f"Fix PR #{fix_pr['number']} {what}. That was attempt {used} of {MAX_ATTEMPTS}: "
                     "giving up. The stable version stays live and the deploy freeze stays on.",
                     labels=[LABEL_NEEDS_HUMAN])
            return 0

        ev = self.collect_fn(self.gh, trigger=trigger, run=run, stable_sha=state.get("stable_sha"),
                             repo_root=self.repo_root, masker=self.masker)
        cls = Classification(state.get("category", "deploy_failure"), state.get("kind", "core"),
                             "continuing the current incident")
        diagnosis = Diagnosis(cls.category, cls.kind, state.get("root_cause", ""), [],
                              float(state.get("confidence") or 0.0)) if state.get("root_cause") else None
        self.say(number, state, f"Fix PR #{fix_pr['number']} {what} (attempt {used} of {MAX_ATTEMPTS}). "
                 "Trying again.")
        error = f"Attempt {used} (PR #{fix_pr['number']}) {what}:\n{ev.failure_text()[-3000:]}"
        return self.diagnose_and_fix(number, state, ev, cls, start_attempt=used + 1, previous_errors=[error],
                                     diagnosis=diagnosis)

    def resume_pending(self, number: int, state: dict, run: dict) -> int:
        """Retry the diagnosis of an incident that stopped at 'diagnosis pending'."""
        state["diagnosis_pending"] = False
        self.gh.remove_label(number, LABEL_NEEDS_HUMAN)
        ev = self.collect_fn(self.gh, trigger=state.get("trigger", "ci_failure"), run=run,
                             stable_sha=state.get("stable_sha"), repo_root=self.repo_root, masker=self.masker)
        cls = classify(ev)
        self.say(number, state, f"Retrying the pending diagnosis for `{run['head_sha'][:7]}`.")
        return self.diagnose_and_fix(number, state, ev, cls)

    # ---- success paths ---------------------------------------------------------------------
    def handle_verify_success(self, run_id: str) -> int:
        run = self.gh.get_run(run_id)
        if run.get("head_branch") != "main":
            return 0
        verified = any(j["name"].lower().startswith("verify") and j.get("conclusion") == "success"
                       for j in self.gh.list_jobs(run_id))
        incident = self.gh.find_open_incident()
        if not verified or not incident:
            return 0
        number, sha = incident["number"], run["head_sha"]
        state = report.read_state(incident.get("body"))
        state.setdefault("actions", [])
        fix_pr = fix_pr_for_commit(self.gh, sha)
        if fix_pr is None:
            self.say(number, state, f"`{sha[:7]}` was deployed and passed verify, but it is not an agent fix. "
                     "Close this incident manually if it is resolved.")
            return 0
        state.update(status="resolved", resolved_at=report.now_iso())
        state["actions"].append(f"fix PR #{fix_pr['number']} deployed and verified; incident closed")
        report.add_event(state, f"fix `{sha[:7]}` (PR #{fix_pr['number']}) deployed and verified")
        self.save(number, state)
        self.gh.close_issue(number, comment=f"Resolved: fix PR #{fix_pr['number']} is live and passes the smoke "
                            f"checks. **The deploy freeze is lifted.**\n\n{report.render_report(state)}")
        return 0

    # ---- health monitor ----------------------------------------------------------------------
    def handle_health(self, run_id: str | None, smoke: dict | None) -> int:
        live = self.gh.find_open_issue(LABEL_LIVE_DOWN)
        incident = self.gh.find_open_incident()
        checks = "\n".join(f"- {'PASS' if c.get('ok') else 'FAIL'} {c['name']}: {c.get('detail', '')}"
                           for c in (smoke or {}).get("checks", []))
        if live:  # no spam, and no second rollback for the same outage
            notify(self.gh, f"Still unhealthy at {report.now_iso()}. No second rollback; a human is needed.\n\n"
                   f"{checks}", issue=live["number"],
                   labels=[] if LABEL_NEEDS_HUMAN in _labels(live) else [LABEL_NEEDS_HUMAN], log=self.log)
            return 0

        rb = self.do_rollback(None)
        state = {"status": "open", "trigger": "health", "detected_at": report.now_iso(), "actions": [],
                 "stable_sha": rb.stable_sha if rb else None, "llm_calls": 0}
        report.add_event(state, "health monitor: live app unhealthy", state["detected_at"])
        report.add_event(state, self.rollback_text(rb))
        state["actions"].append(self.rollback_text(rb))
        if rb and rb.ok:
            state["rollback_seconds"] = rb.seconds

        if incident:  # already handling an incident: add to its timeline instead of opening another issue
            istate = report.read_state(incident.get("body")) or {"actions": [], "timeline": []}
            istate.setdefault("actions", []).append(self.rollback_text(rb))
            labels = [LABEL_NEEDS_HUMAN, LABEL_ROLLBACK_FAILED] if rb and not rb.ok else None
            self.say(incident["number"], istate, f"Health monitor: the live app was unhealthy.\n{checks}\n\n"
                     f"{self.rollback_text(rb)}", labels=labels)
            return 0

        ev = Evidence(trigger="health", smoke=smoke, stable_sha=state["stable_sha"])
        cls = classify(ev)
        state.update(category=cls.category, kind=cls.kind, root_cause=cls.reason,
                     what_failed="The scheduled health check failed:\n" + checks)
        labels = [LABEL_LIVE_DOWN]
        if rb is None or not rb.ok:
            labels += [LABEL_NEEDS_HUMAN] + ([LABEL_ROLLBACK_FAILED] if rb else [])
        number = notify(self.gh, report.build_body(state, report.evidence_markdown(ev)),
                        title=f"Live app down ({report.now_iso()[:16]}Z)", labels=labels, log=self.log)
        if rb and rb.ok:
            self.say(number, state, "The stable version was redeployed and is healthy again. No code changed since "
                     "the last stable version, so there is nothing to fix in code (e.g. Render restarted or "
                     "suspended the service). This issue closes automatically on the next green health check.")
        return 0

    def handle_health_ok(self) -> int:
        for issue in self.gh.list_issues([LABEL_LIVE_DOWN]):
            if LABEL_ACTIVE in _labels(issue):
                continue  # code incidents are closed by the fix path, not by the health monitor
            self.gh.close_issue(issue["number"], comment=f"Health checks are green again at {report.now_iso()}. "
                                "Closing.")
        return 0

    @staticmethod
    def _what_failed(ev: Evidence) -> str:
        parts = []
        if ev.failed_jobs:
            parts.append("Failed jobs: " + ", ".join(f"`{j.name}`" for j in ev.failed_jobs))
        if ev.junit_failures:
            parts.append(f"{len(ev.junit_failures)} failing test(s): "
                         + ", ".join(f"`{t.name}`" for t in ev.junit_failures[:5]))
        if ev.smoke:
            failed = [c["name"] for c in ev.smoke.get("checks", []) if not c.get("ok")]
            if failed:
                parts.append("Failed smoke checks: " + ", ".join(failed))
        return "\n".join(parts) or "see evidence below"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Autonomous CI/CD incident response agent")
    parser.add_argument("--trigger", required=True, choices=TRIGGERS)
    parser.add_argument("--run-id", help="the failed GitHub Actions run id (for health: the monitor's run)")
    parser.add_argument("--smoke-json", help="smoke report JSON written by `python -m agent.smoke --json`")
    parser.add_argument("--repo-root", default=".", help="checkout of main (used for files and pre-checks)")
    args = parser.parse_args(argv)
    if args.trigger in ("ci_failure", "deploy_failure", "verify_success") and not args.run_id:
        parser.error(f"--run-id is required for --trigger {args.trigger}")

    smoke = None
    if args.smoke_json and Path(args.smoke_json).is_file():
        smoke = json.loads(Path(args.smoke_json).read_text(encoding="utf-8"))
    agent = Agent(GitHub.from_env(), repo_root=Path(args.repo_root))
    return agent.run(args.trigger, args.run_id, smoke)


if __name__ == "__main__":
    sys.exit(main())
