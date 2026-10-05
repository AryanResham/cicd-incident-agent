"""Pure deadline logic: status, sort order and summary counts (no DB, no clock)."""

from datetime import date, timedelta

# Display order for GET /api/todos (also the full list of status values).
STATUS_ORDER = ["overdue", "due_today", "due_tomorrow", "upcoming", "no_deadline", "done"]


def compute_status(done: bool, due_date: date | None, today: date) -> str:
    """Status of one todo; rules are checked in the order of docs/API.md."""
    if done:
        return "done"
    if due_date is None:
        return "no_deadline"
    if due_date < today:
        return "overdue"
    if due_date == today:
        return "due_today"
    if due_date == today + timedelta(days=1):
        return "due_tomorrow"
    return "upcoming"


def sort_key(todo: dict) -> tuple:
    """Status group first, then due_date ascending (no date last), then id."""
    due = todo["due_date"]
    return (STATUS_ORDER.index(todo["status"]), due is None, due or date.min, todo["id"])


def summarize(statuses: list[str]) -> dict:
    """Counts for the header: overdue, due soon (today + tomorrow), open (not done), done."""
    return {
        "overdue": statuses.count("overdue"),
        "due_soon": statuses.count("due_today") + statuses.count("due_tomorrow"),
        "open": sum(1 for s in statuses if s != "done"),
        "done": statuses.count("done"),
    }
