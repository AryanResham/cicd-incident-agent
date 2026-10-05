import json

from agent import smoke
from agent.tests.fakes import FakeTodoApp


def run(app: FakeTodoApp, **kwargs):
    sleeps: list[float] = []
    report = smoke.run_smoke("http://app.test/", transport=app.transport(), sleep=sleeps.append,
                             log=lambda *_: None, **kwargs)
    return report, sleeps


def test_healthy_app_passes_all_checks_and_cleans_up():
    app = FakeTodoApp()
    report, sleeps = run(app)
    assert report.healthy
    assert report.rounds == 1
    assert sleeps == []
    assert [c.name for c in report.checks] == ["GET /health", "GET /", "GET /api/todos", "round trip", "latency"]
    assert app.todos == {}  # the round-trip todo was deleted
    assert "POST /api/todos" in app.calls and "DELETE /api/todos/1" in app.calls


def test_unhealthy_app_retries_with_backoff_then_fails():
    report, sleeps = run(FakeTodoApp(health_status=500))
    assert not report.healthy
    assert report.rounds == 4  # first round + 3 retries
    assert sleeps == [5.0, 10.0, 20.0]
    assert [c.name for c in report.failed] == ["GET /health"]
    assert report.failed[0].status_code == 500


def test_down_app_reports_connection_errors():
    report, _ = run(FakeTodoApp(down=True), retries=1)
    assert not report.healthy
    assert "ConnectError" in report.checks[0].detail


def test_health_only_relies_on_status_field():
    app = FakeTodoApp(version="abc123")  # {"status": "ok", "version": ...} passes
    assert run(app, retries=0)[0].healthy
    app.health_body = {"status": "starting", "version": "abc123"}
    report, _ = run(app, retries=0)
    assert [c.name for c in report.failed] == ["GET /health"]


def test_missing_page_fails_only_page_check():
    report, _ = run(FakeTodoApp(page_status=404), retries=0)
    assert [c.name for c in report.failed] == ["GET /"]


def test_mark_done_broken_fails_round_trip_but_still_deletes():
    app = FakeTodoApp(done_works=False)
    report, _ = run(app, retries=0)
    assert [c.name for c in report.failed] == ["round trip"]
    assert "status 'done'" in report.failed[0].detail
    assert app.todos == {}


def test_wrong_create_status_fails_round_trip():
    report, _ = run(FakeTodoApp(create_status=200), retries=0)
    assert "expected 201" in report.failed[0].detail


def test_slow_app_fails_latency_check():
    report, _ = run(FakeTodoApp(delay=0.05), retries=0, latency_limit=0.01)
    assert [c.name for c in report.failed] == ["latency"]


def test_recovers_on_retry():
    app = FakeTodoApp(health_status=503)
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        app.health_status = 200  # the app wakes up while we wait

    report = smoke.run_smoke("http://app.test", transport=app.transport(), sleep=sleep, log=lambda *_: None)
    assert report.healthy and report.rounds == 2 and sleeps == [5.0]


def test_cli_exit_codes_and_json(tmp_path, monkeypatch, capsys):
    app = FakeTodoApp()
    real_run = smoke.run_smoke

    def fake_run(url, **kwargs):
        return real_run(url, transport=app.transport(), sleep=lambda s: None, log=lambda *_: None, **kwargs)

    monkeypatch.setattr(smoke, "run_smoke", fake_run)
    out = tmp_path / "smoke.json"
    assert smoke.main(["--url", "http://app.test", "--json", str(out)]) == 0
    assert json.loads(out.read_text())["healthy"] is True
    assert "HEALTHY" in capsys.readouterr().out

    app.health_status = 500
    assert smoke.main(["--url", "http://app.test", "--retries", "0"]) == 1


# ---- waiting for the new version (verify job + rollback) -----------------------------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def wait(app, expected="new", **kwargs):
    clock = Clock()
    kwargs = {"timeout": 600, "poll_every": 15, **kwargs}
    return smoke.wait_for_version("http://app.test", expected, transport=app.transport(), sleep=clock.sleep,
                                  clock=clock, log=lambda *_: None, **kwargs), clock


def test_wait_for_version_passes_once_the_new_image_is_live():
    app = FakeTodoApp(version="old")
    clock = Clock()

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.t >= 45:
            app.version = "new"  # Render finished rolling out the new image

    result = smoke.wait_for_version("http://app.test", "new", timeout=600, poll_every=15,
                                    transport=app.transport(), sleep=sleep, clock=clock, log=lambda *_: None)
    assert result.ok and result.name == "version"
    assert clock.t == 45


def test_wait_for_version_tolerates_errors_and_cold_starts():
    app = FakeTodoApp(down=True)
    clock = Clock()

    def sleep(seconds):
        clock.sleep(seconds)
        app.down = clock.t < 30
        app.health_status = 502 if clock.t < 60 else 200
        app.version = "new"

    result = smoke.wait_for_version("http://app.test", "new", timeout=600, poll_every=15,
                                    transport=app.transport(), sleep=sleep, clock=clock, log=lambda *_: None)
    assert result.ok and clock.t == 60


def test_wait_for_version_times_out_when_old_version_stays_live():
    result, clock = wait(FakeTodoApp(version="old"))
    assert not result.ok
    assert "new version did not go live" in result.detail and "'old'" in result.detail
    assert clock.t <= 600


def test_wait_for_version_accepts_missing_field_only_when_asked():
    app = FakeTodoApp()
    app.health_body = {"status": "ok"}  # an image built before /health had a version
    assert not wait(app, timeout=60)[0].ok
    assert wait(app, timeout=60, accept_missing=True)[0].ok


def test_cli_expect_version(tmp_path, monkeypatch, capsys):
    app = FakeTodoApp(version="abc")
    real_run, real_wait = smoke.run_smoke, smoke.wait_for_version
    monkeypatch.setattr(smoke, "run_smoke", lambda url, **kw: real_run(
        url, transport=app.transport(), sleep=lambda s: None, log=lambda *_: None, **kw))
    monkeypatch.setattr(smoke, "wait_for_version", lambda url, expected, **kw: real_wait(
        url, expected, transport=app.transport(), sleep=lambda s: None, log=lambda *_: None, **kw))
    out = tmp_path / "smoke.json"

    assert smoke.main(["--url", "http://app.test", "--expect-version", "abc", "--json", str(out)]) == 0
    assert [c["name"] for c in json.loads(out.read_text())["checks"]][:2] == ["version", "GET /health"]

    calls_before = len(app.calls)
    assert smoke.main(["--url", "http://app.test", "--expect-version", "zzz", "--wait", "0",
                       "--json", str(out)]) == 1
    data = json.loads(out.read_text())
    assert data["healthy"] is False and [c["name"] for c in data["checks"]] == ["version"]
    assert app.calls[calls_before:] == ["GET /health"]  # the old version is not smoke-tested
    assert "new version did not go live" in capsys.readouterr().out


def test_report_summary_lists_failures():
    report = smoke.SmokeReport("http://x", False, 1, [smoke.CheckResult("GET /health", False, "boom", 502)])
    assert "FAIL  GET /health [502]" in report.summary()
