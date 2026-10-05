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


def test_report_summary_lists_failures():
    report = smoke.SmokeReport("http://x", False, 1, [smoke.CheckResult("GET /health", False, "boom", 502)])
    assert "FAIL  GET /health [502]" in report.summary()
