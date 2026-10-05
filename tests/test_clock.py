from datetime import date, datetime, timezone

from app import clock
from app.status import compute_status


def test_default_timezone_is_kolkata(monkeypatch):
    monkeypatch.delenv("APP_TIMEZONE", raising=False)
    assert clock.app_timezone().key == "Asia/Kolkata"


def test_near_midnight_utc_is_already_the_next_day_in_kolkata(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Kolkata")
    # 19:00 UTC on Oct 4 is 00:30 IST on Oct 5
    assert clock.today(datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc)) == date(2026, 10, 5)
    # 18:29 UTC is 23:59 IST, still Oct 4
    assert clock.today(datetime(2026, 10, 4, 18, 29, tzinfo=timezone.utc)) == date(2026, 10, 4)


def test_deadline_status_uses_app_timezone(monkeypatch):
    moment = datetime(2026, 10, 4, 19, 0, tzinfo=timezone.utc)
    deadline = date(2026, 10, 5)

    monkeypatch.setenv("APP_TIMEZONE", "Asia/Kolkata")
    assert compute_status(False, deadline, clock.today(moment)) == "due_today"

    monkeypatch.setenv("APP_TIMEZONE", "UTC")
    assert compute_status(False, deadline, clock.today(moment)) == "due_tomorrow"


def test_timestamp_is_timezone_aware(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Kolkata")
    stamp = clock.timestamp()
    assert stamp.endswith("+05:30")
    assert datetime.fromisoformat(stamp).tzinfo is not None
