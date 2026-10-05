import subprocess
from types import SimpleNamespace

import pytest

from agent import fix
from agent.classify import CORE, SIMPLE
from agent.collect import Evidence
from agent.github_api import GitHubError
from agent.llm import Diagnosis, Edit, FixProposal, LLMBadResponse, LLMUnavailable
from agent.smoke import SmokeReport
from agent.tests.fakes import FakeGitHub

DIAG = Diagnosis("dependency", "simple", "requests is missing from requirements.txt", ["requirements.txt"], 0.9)
GOOD = FixProposal("add requests", 0.95, [Edit("requirements.txt", "fastapi\n", "fastapi\nrequests\n")])
LOGIC = FixProposal("flip done", 0.9, [Edit("app/main.py", "    return done\n", "    return not done\n")])
TEST_EDIT = FixProposal("edit the test", 0.99, [Edit("tests/test_main.py", "assert False", "assert True")])


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app" / "main.py").write_text("def toggle(done):\n    return done\n")
    (root / "requirements.txt").write_text("fastapi\n")
    (root / "tests" / "test_main.py").write_text("def test_x():\n    assert False\n")
    return root


class FakeLLM:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def propose_fix(self, evidence, diagnosis, files, previous_errors):
        self.prompts.append(list(previous_errors))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def prechecks_from(*results):
    """Fake pre-checks: returns the given PrecheckResults in order and records the patched files."""
    results = list(results)
    seen = []

    def fake(workdir, changes, tag="", log=print):
        seen.append({p: (workdir / p).read_text() for p in changes})
        return results.pop(0)
    fake.seen = seen
    return fake


OK = fix.PrecheckResult(True, 4)
FAIL_PYTEST = fix.PrecheckResult(False, 1, "pytest failed: E ModuleNotFoundError")


def loop(repo, gh, llm, prechecks, kind=SIMPLE, auto_merge=True, **kwargs):
    return fix.run_fix_loop(incident=7, evidence=Evidence(trigger="ci_failure"), diagnosis=DIAG, kind=kind, llm=llm,
                            gh=gh, repo_root=repo, base_sha="base", auto_merge=auto_merge, prechecks=prechecks,
                            log=lambda *_: None, **kwargs)


def test_simple_fix_passes_and_is_auto_merged(repo):
    gh = FakeGitHub()
    pre = prechecks_from(OK)
    out = loop(repo, gh, FakeLLM(GOOD), pre)
    assert out.status == "auto_merge" and out.kind == SIMPLE
    assert pre.seen[0]["requirements.txt"] == "fastapi\nrequests\n"  # pre-checks ran on the patched copy
    assert (repo / "requirements.txt").read_text() == "fastapi\n"  # the real checkout is untouched
    assert gh.branches["agent/fix-7-1"] == "commit1"
    assert gh.commits[0][1] == {"requirements.txt": "fastapi\nrequests\n"}
    assert gh.auto_merge == [out.pr["node_id"]]
    assert gh.labels_of(out.pr["number"]) == ["agent-fix"]
    assert "Auto-merge | yes" in out.pr["body"] and "+requests" in out.pr["body"]


def test_failed_precheck_feeds_error_into_next_attempt(repo):
    gh = FakeGitHub()
    llm = FakeLLM(GOOD, GOOD)
    out = loop(repo, gh, llm, prechecks_from(FAIL_PYTEST, OK))
    assert out.status == "auto_merge"
    assert [a.number for a in out.attempts] == [1, 2]
    assert llm.prompts[0] == [] and "ModuleNotFoundError" in llm.prompts[1][0]
    assert "agent/fix-7-1" not in gh.branches  # nothing pushed for the failed attempt
    assert "agent/fix-7-2" in gh.branches


def test_logic_change_is_upgraded_to_core_and_not_merged(repo):
    gh = FakeGitHub()
    out = loop(repo, gh, FakeLLM(LOGIC), prechecks_from(OK))
    assert out.status == "needs_human" and out.kind == CORE
    assert "upgraded to CORE" in out.message
    assert gh.auto_merge == []
    assert "needs-human" in gh.labels_of(out.pr["number"])


def test_core_incident_is_never_merged(repo):
    gh = FakeGitHub()
    out = loop(repo, gh, FakeLLM(GOOD), prechecks_from(OK), kind=CORE)
    assert out.status == "needs_human" and gh.auto_merge == []
    assert out.pr["title"].startswith("[needs human]")


@pytest.mark.parametrize("proposal, auto_merge, reason", [
    (FixProposal("x", 0.5, GOOD.edits), True, "confidence 0.50 < 0.8"),
    (GOOD, False, "AGENT_AUTO_MERGE is off"),
])
def test_low_confidence_or_kill_switch_blocks_auto_merge(repo, proposal, auto_merge, reason):
    gh = FakeGitHub()
    out = loop(repo, gh, FakeLLM(proposal), prechecks_from(OK), auto_merge=auto_merge)
    assert out.status == "needs_human" and reason in out.message and gh.auto_merge == []


def test_auto_merge_failure_falls_back_to_needs_human(repo):
    gh = FakeGitHub()
    gh.auto_merge_error = GitHubError(422, "Auto merge is not allowed for this repository")
    out = loop(repo, gh, FakeLLM(GOOD), prechecks_from(OK))
    assert out.status == "needs_human" and "auto-merge could not be enabled" in out.message
    assert "needs-human" in gh.labels_of(out.pr["number"])


def test_clean_status_merges_directly(repo):
    gh = FakeGitHub()
    gh.auto_merge_error = GitHubError(422, "Pull request is in clean status")
    out = loop(repo, gh, FakeLLM(GOOD), prechecks_from(OK))
    assert out.status == "auto_merge" and gh.merged == [out.pr["number"]]


def test_test_edits_are_rejected_then_gives_up_with_best_attempt(repo):  # E1 / E2
    gh = FakeGitHub()
    llm = FakeLLM(TEST_EDIT, GOOD, LLMBadResponse("not json"))
    out = loop(repo, gh, llm, prechecks_from(FAIL_PYTEST))
    assert out.status == "gave_up"
    assert len(out.attempts) == 3
    assert "patch rejected" in out.attempts[0].error and "tests" in out.attempts[0].error
    assert "invalid LLM answer" in out.attempts[2].error
    assert out.pr["head"]["ref"] == "agent/fix-7-2"  # the best (only applicable) attempt
    assert "needs-human" in gh.labels_of(out.pr["number"]) and gh.auto_merge == []
    assert "Pre-check output" in out.pr["body"]


def test_nothing_applicable(repo):
    out = loop(repo, FakeGitHub(), FakeLLM(TEST_EDIT, TEST_EDIT, TEST_EDIT), prechecks_from())
    assert out.status == "no_patch" and out.pr is None


def test_llm_unavailable_before_first_attempt(repo):
    out = loop(repo, FakeGitHub(), FakeLLM(LLMUnavailable("quota")), prechecks_from())
    assert out.status == "llm_unavailable" and "quota" in out.message


def test_start_attempt_continues_numbering(repo):
    gh = FakeGitHub()
    out = loop(repo, gh, FakeLLM(GOOD), prechecks_from(OK), start_attempt=3, previous_errors=["live smoke failed"])
    assert out.attempts[0].number == 3 and "agent/fix-7-3" in gh.branches


def test_attempts_used_counts_this_incidents_prs():
    gh = FakeGitHub()
    gh.create_pr("agent/fix-7-1", "t", "b")
    gh.create_pr("agent/fix-7-2", "t", "b")
    gh.create_pr("agent/fix-70-1", "t", "b")
    gh.create_pr("feature/x", "t", "b")
    assert fix.attempts_used(gh, 7) == 2


# ---- pre-checks with a fake subprocess --------------------------------------------------
class FakeRun:
    def __init__(self, fail_on=None):
        self.cmds = []
        self.fail_on = fail_on

    def __call__(self, cmd, cwd=None, capture_output=True, text=True, timeout=None):
        self.cmds.append(cmd)
        failed = self.fail_on is not None and self.fail_on in cmd
        return SimpleNamespace(returncode=1 if failed else 0, stdout=f"output of {cmd[1:3]}", stderr="")


def smoke_result(healthy):
    return lambda url, **kw: SmokeReport(url, healthy, 1, [])


def test_prechecks_without_docker_skip_with_note(repo):
    run = FakeRun()
    result = fix.run_prechecks(repo, {"app/main.py": ("", "")}, run=run, docker=None, log=lambda *_: None)
    assert result.ok and result.stage == 2
    assert "skipped" in result.notes[0]
    assert run.cmds[0][1:4] == ["-m", "pytest", "-q"] and run.cmds[0][-1] == "tests"


def test_prechecks_install_changed_requirements_first(repo):
    run = FakeRun()
    fix.run_prechecks(repo, {"requirements.txt": ("", "")}, run=run, docker=None, log=lambda *_: None)
    assert run.cmds[0][1:4] == ["-m", "pip", "install"]


def test_prechecks_full_docker_path(repo):
    run = FakeRun()
    result = fix.run_prechecks(repo, {}, run=run, docker="docker", smoke_fn=smoke_result(True), tag="7-1",
                               log=lambda *_: None)
    assert result.ok and result.stage == 4
    flat = [" ".join(c) for c in run.cmds]
    assert any(c.startswith("docker build -t agent-precheck:7-1") for c in flat)
    assert any("docker run -d --name agent-precheck-7-1 -p 18080:8000 -e PORT=8000" in c for c in flat)
    assert flat[-1] == "docker rm -f agent-precheck-7-1"  # always cleaned up


def test_prechecks_container_unhealthy_includes_logs(repo):
    run = FakeRun()
    result = fix.run_prechecks(repo, {}, run=run, docker="docker", smoke_fn=smoke_result(False), log=lambda *_: None)
    assert not result.ok and result.stage == 3 and "container logs" in result.log


def test_prechecks_pytest_failure(repo):
    result = fix.run_prechecks(repo, {}, run=FakeRun(fail_on="pytest"), docker=None, log=lambda *_: None)
    assert not result.ok and result.stage == 1 and result.log.startswith("pytest failed")


def test_prechecks_timeout(repo):
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    result = fix.run_prechecks(repo, {}, run=run, docker=None, log=lambda *_: None)
    assert not result.ok and "timed out" in result.log
