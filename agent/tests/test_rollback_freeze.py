import httpx

from agent import freeze, rollback
from agent.smoke import CheckResult, SmokeReport
from agent.tests.fakes import FakeGitHub


# ---- last stable SHA -------------------------------------------------------
def test_last_stable_requires_verify_success_not_just_green_run():
    gh = FakeGitHub()
    gh.add_run(3, "frozen", jobs={"test": "success", "build": "success", "deploy": "skipped", "verify": "skipped"})
    gh.add_run(2, "broken", conclusion="failure", jobs={"test": "success", "verify": "failure"})
    gh.add_run(1, "good")
    assert rollback.find_last_stable_sha(gh) == "good"


def test_last_stable_skips_excluded_sha_and_handles_none():
    gh = FakeGitHub()
    gh.add_run(2, "bad")  # verified, but we know it's broken (e.g. the health monitor caught it)
    assert rollback.find_last_stable_sha(gh, exclude={"bad"}) is None
    gh.add_run(1, "older")
    assert rollback.find_last_stable_sha(gh, exclude={"bad"}) == "older"


def test_verify_job_with_display_name_is_recognised():
    gh = FakeGitHub()
    gh.add_run(1, "good", jobs={"verify (smoke checks)": "success"})
    assert rollback.find_last_stable_sha(gh) == "good"


def test_image_and_deploy_url():
    image = rollback.image_for("Me/Todo", "abc123")
    assert image == "ghcr.io/me/todo:abc123"
    assert rollback.deploy_url("https://api.render.com/deploy/srv-1?key=k", image) == \
        "https://api.render.com/deploy/srv-1?key=k&imgURL=ghcr.io%2Fme%2Ftodo%3Aabc123"


# ---- rollback flow ---------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def hook_client(calls, status=200):
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status)
    return httpx.Client(transport=httpx.MockTransport(handler))


def smoke_sequence(*healthy):
    results = list(healthy)

    def fake(url, **kwargs):
        return SmokeReport(url, results.pop(0), 1, [])
    return fake


def version_live(waited=30.0, ok=True, seen=None):
    """Fake wait_for_version: records its call and 'waits' on the fake clock."""
    def fake(url, expected, *, timeout, poll_every, accept_missing, sleep, clock, log):
        seen.append((expected, accept_missing)) if seen is not None else None
        sleep(waited)
        return CheckResult("version", ok, "version live" if ok else "new version did not go live")
    return fake


def do_rollback(gh, smoke_fn, calls, status=200, hook="https://hook.test/deploy?key=s3cret", version_fn=None):
    clock = Clock()
    return rollback.rollback(gh, hook_url=hook, app_url="https://app.test", broken_sha="bad",
                             http=hook_client(calls, status), smoke_fn=smoke_fn,
                             version_fn=version_fn or version_live(), sleep=clock.sleep,
                             clock=clock, log=lambda *_: None, max_wait=120, poll_every=30)


def test_rollback_deploys_stable_image_and_waits_until_healthy():
    gh = FakeGitHub()
    gh.add_run(2, "bad", conclusion="failure", jobs={"verify": "failure"})
    gh.add_run(1, "good")
    calls, seen = [], []
    result = do_rollback(gh, smoke_sequence(False, True), calls, version_fn=version_live(seen=seen))
    assert result.ok and not result.escalate
    assert result.stable_sha == "good"
    assert calls == ["https://hook.test/deploy?key=s3cret&imgURL=ghcr.io%2Fme%2Ftodo%3Agood"]
    assert seen == [("good", True)]  # waited for the stable SHA (old images without a version are fine)
    assert result.seconds == 60  # 30 s until the version was live + one 30 s smoke retry


def test_rollback_whose_version_never_goes_live_escalates_without_smoke_checks():
    gh = FakeGitHub()
    gh.add_run(1, "good")
    calls = []
    result = do_rollback(gh, smoke_sequence(), calls, version_fn=version_live(waited=120, ok=False))
    assert not result.ok and result.escalate
    assert "did not go live" in result.reason and result.smoke is None
    assert len(calls) == 1


def test_rollback_that_never_gets_healthy_escalates_without_retrying():
    gh = FakeGitHub()
    gh.add_run(1, "good")
    calls = []
    result = do_rollback(gh, smoke_sequence(False, False, False, False), calls)
    assert not result.ok and result.escalate
    assert "did not pass" in result.reason
    assert len(calls) == 1  # no second rollback


def test_rollback_hook_error_is_reported_without_leaking_the_hook_url():
    gh = FakeGitHub()
    gh.add_run(1, "good")
    result = do_rollback(gh, smoke_sequence(), [], status=500)
    assert not result.ok
    assert "s3cret" not in result.reason


def test_rollback_without_hook_secret_names_the_missing_secret():
    result = do_rollback(FakeGitHub(), smoke_sequence(), [], hook="")
    assert not result.ok and result.missing_secret == "RENDER_DEPLOY_HOOK_URL"


def test_rollback_without_stable_version():
    result = do_rollback(FakeGitHub(), smoke_sequence(), [])
    assert not result.ok and "no stable version" in result.reason


# ---- deploy freeze ---------------------------------------------------------
def test_freeze_deploys_when_no_incident():
    assert freeze.should_deploy(FakeGitHub(), "x") == (True, "no active incident")


def test_freeze_blocks_random_push_during_incident():
    gh = FakeGitHub()
    gh.create_issue("Incident", "", ["incident:active"])
    gh.commit_prs["x"] = [{"number": 5, "head": {"ref": "feature/foo"}}]
    deploy, reason = freeze.should_deploy(gh, "x")
    assert deploy is False and "freeze" in reason


def test_freeze_allows_agent_fix_commits():
    gh = FakeGitHub()
    gh.create_issue("Incident", "", ["incident:active"])
    gh.commit_prs["x"] = [{"number": 5, "head": {"ref": "agent/fix-1-2"}}]
    assert freeze.should_deploy(gh, "x")[0] is True


def test_freeze_cli_writes_github_output(tmp_path):
    out = tmp_path / "out.txt"
    gh = FakeGitHub()
    gh.create_issue("Incident", "", ["incident:active"])
    assert freeze.main(["--sha", "x"], gh=gh, env={"GITHUB_OUTPUT": str(out)}) == 0
    text = out.read_text()
    assert "deploy=false\n" in text and "reason=deploy freeze" in text


def test_freeze_cli_real_error_exits_1():
    class Broken(FakeGitHub):
        def find_open_incident(self):
            raise RuntimeError("API down")
    assert freeze.main(["--sha", "x"], gh=Broken(), env={}) == 1
