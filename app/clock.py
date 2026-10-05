"""The app's single source of "now" and "today", in APP_TIMEZONE."""

import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Kolkata"


def app_timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE))


def now() -> datetime:
    """Current time as a timezone-aware datetime in APP_TIMEZONE."""
    return datetime.now(app_timezone())


def today(moment: datetime | None = None) -> date:
    """Today's date in APP_TIMEZONE.

    `moment` (an aware datetime) lets tests ask "what date is it here at that instant?".
    """
    if moment is None:
        moment = datetime.now(timezone.utc)
    return moment.astimezone(app_timezone()).date()


def timestamp() -> str:
    """ISO 8601 timestamp for created_at / updated_at, e.g. 2026-10-05T10:15:00+05:30."""
    return now().isoformat(timespec="seconds")
