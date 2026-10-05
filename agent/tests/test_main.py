import pytest

from agent import fix, main, report
from agent.collect import Evidence, FailedJob, TestFailure
from agent.llm import Diagnosis, Edit, FixProposal, LLMUnavailable
from agent.notify import notify
from agent.rollback import RollbackResult
from agent.tests.fakes import FakeGitHub

DEP_LOG = "ModuleNotFoundError: No module named 'requests'"
DEP_FIX = FixProposal("add requests", 0.95, [Edit("requirements.txt", "fastapi\n", "fastapi\nrequests\n")])


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("import requests\n")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    return tmp_path


class FakeLLM:
    def __init__(self, diagnosis=None, fixes=(), down=False):
        self.diagnosis = diagnosis or Diagnosis("dependency", "simple", "requests missing from requirements.txt",
                                                ["requirements.txt"], 0.9)
        self.fixes = list(fixes)
        self.down = down
        self.calls = 0
        self.fix_errors = []

    def diagnose(self, evidence, rule):
        self.calls += 1
        if self.down:
            raise LLMUnavailable("Gemini daily quota exhausted")
        return self.diagnosis

    def propose_fix(self, evidence, diagnosis, files, previous_errors):
        self.calls += 1
        self.fix_errors.append(list(previous_errors))
        return self.fixes.pop(0)


class World:
    """A fake GitHub + Render + Gemini world to drive main.Agent through."""

    def __init__(self, repo, *, env=None, llm=None, rollback_ok=True, evidence=None):
        self.gh = FakeGitHub()
        self.gh.add_run(1, "stable1")
        self.llm = llm or FakeLLM(fixes=[DEP_FIX])
        self.rollbacks = []
        self.order = []
        self.rollback_ok = rollback_ok
        self.evidence = evidence
        self.logs = []
        env = {"RENDER_DEPLOY_HOOK_URL": "https://hook", "RENDER_APP_URL": "https://app", **(env or {})}

        def fake_rollback(gh, **kw):
            self.rollbacks.append(kw["broken_sha"])
            self.order.append("rollback")
            if self.rollback_ok:
                return RollbackResult(True, "ok", stable_sha="stable1", image="img", seconds=42)
            return RollbackResult(False, "rollback to stable1 did not pass the smoke checks", stable_sha="stable1")

        def fake_collect(gh, **kw):
            self.order.append("collect")
            ev = self.evidence or Evidence(trigger=kw["trigger"])
            ev.trigger = kw["trigger"]
            ev.head_sha = kw["run"]["head_sha"] if kw.get("run") else None
            ev.stable_sha = kw.get("stable_sha")
            return ev

        self.agent = main.Agent(self.gh, env=env, llm_factory=lambda: self.llm, rollback_fn=fake_rollback,
                                prechecks=lambda work, changes, tag="", log=print: fix.PrecheckResult(True, 4),
                                collect_fn=fake_collect, repo_root=repo, log=self.logs.append)
        self.agent.base_sha = lambda: "mainsha"

    def fail(self, run_id, sha, trigger="ci_failure", branch="main", **extra):
        self.gh.add_run(run_id, sha, conclusion="failure", branch=branch,
                        jobs={"test": "failure"} if trigger == "ci_failure" else {"verify": "failure"}, **extra)
        return self.agent.run(trigger, str(run_id))

    def issue(self, number=None):
        issues = list(self.gh.issues.values())
        return self.gh.issues[number] if number else issues[0]

    def all_comments(self, number):
        return "\n".join(self.gh.comments[number])


def dep_evidence():
    return Evidence(trigger="ci_failure", failed_jobs=[FailedJob("test", "failure", ["Run tests"], DEP_LOG)])


def core_evidence():
    return Evidence(trigger="ci_failure", failed_jobs=[FailedJob("test", "failure", [], "1 failed")],
                    junit_failures=[TestFailure("tests.test_todos::test_mark_done", "assert 'upcoming' == 'done'", "")])


# ---- CI failures -----------------------------------------------------------------------------
def test_ci_failure_simple_is_fixed_and_auto_merged_without_rollback(repo):
    w = World(repo, evidence=dep_evidence())
    assert w.fail(2, "broken2") == 0
    assert w.rollbacks == []  # nothing was deployed, so no rollback
    issue = w.issue()
    assert issue["title"].startswith("Incident: ci failure on broken2")
    assert w.gh.labels_of(issue["number"]) == ["incident:active"]
    pr = next(iter(w.gh.prs.values()))
    assert pr["head"]["ref"] == f"agent/fix-{issue['number']}-1"
    assert w.gh.auto_merge == [pr["node_id"]]
    assert issue["state"] == "open"  # closes only after the fix is deployed and verified
    state = report.read_state(issue["body"])
    assert state["kind"] == "simple" and state["llm_calls"] == 2
    assert "Fix: auto_merge" in w.all_comments(issue["number"])


def test_core_failure_gets_suggested_pr_and_needs_human(repo):
    llm = FakeLLM(diagnosis=Diagnosis("test_failure", "core", "PATCH ignores done", ["app/main.py"], 0.7),
                  fixes=[DEP_FIX])
    w = World(repo, llm=llm, evidence=core_evidence())
    w.fail(2, "broken2")
    issue = w.issue()
    assert "needs-human" in w.gh.labels_of(issue["number"])
    pr = next(iter(w.gh.prs.values()))
    assert "needs-human" in pr["labels"][1]["name"] and w.gh.auto_merge == []


def test_llm_decision_can_only_make_it_safer(repo):
    llm = FakeLLM(diagnosis=Diagnosis("dependency", "core", "unsure", [], 0.4), fixes=[DEP_FIX])
    w = World(repo, llm=llm, evidence=dep_evidence())
    w.fail(2, "broken2")
    assert w.gh.auto_merge == []
    assert report.read_state(w.issue()["body"])["kind"] == "core"


def test_llm_unavailable_means_diagnosis_pending(repo):
    w = World(repo, llm=FakeLLM(down=True), evidence=dep_evidence())
    w.fail(2, "broken2")
    issue = w.issue()
    assert "Diagnosis pending" in w.all_comments(issue["number"])
    assert "needs-human" in w.gh.labels_of(issue["number"])
    assert w.gh.prs == {}


def test_issue_only_never_calls_the_llm(repo):
    ev = Evidence(trigger="ci_failure", failed_jobs=[FailedJob("build", "failure", [], "Error: Bad credentials")])
    w = World(repo, evidence=ev)
    w.fail(2, "broken2")
    assert w.llm.calls == 0
    assert "No code fix possible" in w.all_comments(w.issue()["number"])


def test_flaky_failure_is_rerun_once_without_issue(repo):
    ev = Evidence(trigger="ci_failure", failed_jobs=[FailedJob("build", "failure", [], "toomanyrequests")])
    w = World(repo, evidence=ev)
    w.fail(2, "broken2")
    assert w.gh.reruns == [2] and w.gh.issues == {}


def test_flaky_failure_twice_is_issue_only(repo):
    ev = Evidence(trigger="ci_failure", failed_jobs=[FailedJob("build", "failure", [], "toomanyrequests")])
    w = World(repo, evidence=ev)
    w.gh.add_run(2, "broken2", conclusion="failure", jobs={"build": "failure"}, run_attempt=2)
    w.agent.run("ci_failure", "2")
    assert w.gh.reruns == [] and w.llm.calls == 0
    assert "needs-human" in w.gh.labels_of(w.issue()["number"])


def test_auto_merge_kill_switch(repo):
    w = World(repo, env={"AGENT_AUTO_MERGE": "false"}, evidence=dep_evidence())
    w.fail(2, "broken2")
    assert w.gh.auto_merge == [] and "AGENT_AUTO_MERGE is off" in w.all_comments(w.issue()["number"])


def test_agent_disabled_does_nothing(repo):
    w = World(repo, env={"AGENT_ENABLED": "false"}, evidence=dep_evidence())
    assert w.fail(2, "broken2") == 0
    assert w.gh.issues == {} and w.rollbacks == []


# ---- loop guard + one incident at a time --------------------------------------------------------
def test_failures_on_agent_branches_never_open_incidents(repo):
    w = World(repo, evidence=dep_evidence())
    incident = w.gh.create_issue("Incident", "", ["incident:active"])
    w.fail(2, "fixsha", branch="agent/fix-1-1")
    assert len(w.gh.issues) == 1
    assert "belongs to the current incident" in w.all_comments(incident["number"])


def test_unrelated_ci_failure_during_incident_is_only_noted(repo):
    w = World(repo, evidence=dep_evidence())
    incident = w.gh.create_issue("Incident", "", ["incident:active"])
    w.fail(2, "other")
    assert len(w.gh.issues) == 1 and w.gh.prs == {}
    assert "One incident at a time" in w.all_comments(incident["number"])


def test_feature_branch_failures_are_ignored(repo):
    w = World(repo, evidence=dep_evidence())
    w.fail(2, "x", branch="feature/foo")
    assert w.gh.issues == {}


# ---- production failures ---------------------------------------------------------------------
def test_deploy_failure_rolls_back_first_then_fixes(repo):
    ev = Evidence(trigger="deploy_failure", failed_jobs=[FailedJob("verify", "failure", [], "")],
                  smoke={"checks": [{"name": "GET /health", "ok": False}]},
                  changed_files=["Dockerfile"], diff="--- Dockerfile (modified)\n-CMD a\n+CMD b")
    w = World(repo, evidence=ev)
    w.fail(2, "broken2", trigger="deploy_failure")
    assert w.order[:2] == ["rollback", "collect"]  # rollback before anything else
    assert w.rollbacks == ["broken2"]
    state = report.read_state(w.issue()["body"])
    assert state["rollback_seconds"] == 42 and state["stable_sha"] == "stable1"
    assert any("Rolled back to stable `stable1`" in a for a in state["actions"])
    assert w.gh.auto_merge  # SIMPLE (Dockerfile only) -> auto-merge


def test_failed_rollback_escalates_and_stops(repo):
    w = World(repo, rollback_ok=False, evidence=dep_evidence())
    w.fail(2, "broken2", trigger="deploy_failure")
    number = w.issue()["number"]
    assert set(w.gh.labels_of(number)) == {"incident:active", "needs-human", "rollback-failed"}
    assert "ROLLBACK FAILED" in w.all_comments(number)
    assert w.llm.calls == 0 and len(w.rollbacks) == 1


def test_rollback_kill_switch(repo):
    w = World(repo, env={"AGENT_ROLLBACK": "false"}, evidence=dep_evidence())
    w.fail(2, "broken2", trigger="deploy_failure")
    assert w.rollbacks == []
    assert "Rollback skipped" in w.issue()["body"]


def test_fix_that_fails_live_is_rolled_back_and_retried(repo):
    w = World(repo, evidence=dep_evidence())
    w.fail(2, "broken2")  # incident + PR agent/fix-N-1 auto-merged
    number = w.issue()["number"]
    pr = next(iter(w.gh.prs.values()))
    w.gh.commit_prs["merge1"] = [pr]
    w.llm.fixes = [DEP_FIX]
    w.fail(3, "merge1", trigger="deploy_failure")  # the merged fix failed verify
    assert w.rollbacks == ["merge1"]
    assert f"agent/fix-{number}-2" in w.gh.branches
    assert "verify failure" in w.llm.fix_errors[-1][0] or "failed the live checks" in w.llm.fix_errors[-1][0]
    assert len(w.gh.issues) == 1


def test_gives_up_after_three_attempts(repo):
    w = World(repo, evidence=dep_evidence())
    incident = w.gh.create_issue("Incident", report.build_body({"kind": "simple", "category": "dependency"}),
                                 ["incident:active"])
    for n in (1, 2, 3):
        w.gh.create_pr(f"agent/fix-{incident['number']}-{n}", "t", "b")
    w.gh.commit_prs["merge3"] = [w.gh.prs[max(w.gh.prs)]]
    w.fail(9, "merge3", trigger="deploy_failure")
    assert "giving up" in w.all_comments(incident["number"])
    assert "needs-human" in w.gh.labels_of(incident["number"])
    assert w.llm.calls == 0


# ---- success + health -------------------------------------------------------------------------
def test_verify_success_of_fix_closes_incident_and_lifts_freeze(repo):
    w = World(repo, evidence=dep_evidence())
    w.fail(2, "broken2")
    number = w.issue()["number"]
    pr = next(iter(w.gh.prs.values()))
    w.gh.add_run(3, "merge1")
    w.gh.commit_prs["merge1"] = [pr]
    w.agent.run("verify_success", "3")
    assert w.gh.issues[number]["state"] == "closed"
    closing = w.gh.comments[number][-1]
    assert "deploy freeze is lifted" in closing and "MTTR" in closing
    assert report.read_state(w.gh.issues[number]["body"])["status"] == "resolved"


def test_verify_success_ignores_frozen_runs_and_non_fix_commits(repo):
    w = World(repo)
    incident = w.gh.create_issue("Incident", "", ["incident:active"])
    w.gh.add_run(3, "frozen", jobs={"test": "success", "verify": "skipped"})
    w.agent.run("verify_success", "3")
    assert w.gh.issues[incident["number"]]["state"] == "open" and w.gh.comments[incident["number"]] == []
    w.gh.add_run(4, "manual")
    w.agent.run("verify_success", "4")
    assert w.gh.issues[incident["number"]]["state"] == "open"
    assert "not an agent fix" in w.all_comments(incident["number"])


SMOKE_DOWN = {"healthy": False, "checks": [{"name": "GET /health", "ok": False, "detail": "ConnectError"}]}


def test_health_failure_rolls_back_and_opens_one_issue(repo):  # N2
    w = World(repo)
    w.agent.run("health", "77", SMOKE_DOWN)
    assert w.rollbacks == [None]
    issue = w.issue()
    assert w.gh.labels_of(issue["number"]) == ["incident:live-down"]
    assert "nothing to fix in code" in w.all_comments(issue["number"])
    # Still down on the next run: comment, no new issue, no second rollback.
    w.agent.run("health", "78", SMOKE_DOWN)
    assert len(w.gh.issues) == 1 and w.rollbacks == [None]
    assert "No second rollback" in w.all_comments(issue["number"])
    assert w.llm.calls == 0


def test_health_failure_during_incident_comments_on_it(repo):
    w = World(repo)
    incident = w.gh.create_issue("Incident", "", ["incident:active"])
    w.agent.run("health", "77", SMOKE_DOWN)
    assert len(w.gh.issues) == 1
    assert "Health monitor" in w.all_comments(incident["number"])


def test_health_ok_closes_only_live_down_issues(repo):
    w = World(repo)
    live = w.gh.create_issue("down", "", ["incident:live-down"])
    code = w.gh.create_issue("incident", "", ["incident:active", "incident:live-down"])
    w.agent.run("health_ok")
    assert w.gh.issues[live["number"]]["state"] == "closed"
    assert w.gh.issues[code["number"]]["state"] == "open"


# ---- small pieces -------------------------------------------------------------------------
def test_flags():
    assert main.flag({}, "AGENT_ENABLED") is True
    assert main.flag({"AGENT_ENABLED": "False "}, "AGENT_ENABLED") is False
    assert main.flag({"AGENT_ENABLED": "0"}, "AGENT_ENABLED") is True  # only "false" switches off


def test_cli_requires_run_id():
    with pytest.raises(SystemExit):
        main.main(["--trigger", "ci_failure"])


def test_report_state_round_trip_and_mttr():
    state = {"trigger": "ci_failure", "detected_at": "2026-10-05T10:00:00+00:00",
             "resolved_at": "2026-10-05T10:12:30+00:00", "root_cause": "x --> y", "actions": ["a"]}
    report.add_event(state, "detected", "2026-10-05T10:00:00+00:00")
    body = report.build_body(state, "EVIDENCE")
    assert report.read_state(body) == state
    assert "| MTTR | 12m 30s |" in body
    state["status"] = "resolved"
    rebuilt = report.rebuild_body(body, state)
    assert "EVIDENCE" in rebuilt and rebuilt.count("agent-state") == 1
    assert report.read_state(rebuilt)["status"] == "resolved"


def test_notify_comments_or_opens_issue():
    gh = FakeGitHub()
    n = notify(gh, "hello", title="T", labels=["needs-human"], log=lambda *_: None)
    assert gh.issues[n]["title"] == "T"
    assert notify(gh, "again", issue=n, labels=["x"], log=lambda *_: None) == n
    assert gh.comments[n] == ["again"] and "x" in gh.labels_of(n)
