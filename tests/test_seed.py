from datetime import date

from fastapi.testclient import TestClient

from app import db
from app.main import create_app, get_today
from app.seed import seed_if_empty
from app.status import compute_status, summarize

FROZEN_TODAY = date(2026, 10, 5)  # same date the API tests freeze

EXPECTED = {
    "Submit project title": ("2026-10-02", False, "overdue"),
    "Write pytest tests": ("2026-10-05", False, "due_today"),
    "Set up GitHub Actions": ("2026-10-06", False, "due_tomorrow"),
    "Prepare demo slides": ("2026-10-12", False, "upcoming"),
    "Read FastAPI docs": (None, False, "no_deadline"),
    "Initialize git repo": ("2026-10-03", True, "done"),
}


def _status(todo):
    due = date.fromisoformat(todo["due_date"]) if todo["due_date"] else None
    return compute_status(todo["done"], due, FROZEN_TODAY)


def test_seed_inserts_contract_rows(conn):
    assert seed_if_empty(conn, FROZEN_TODAY) is True
    todos = db.list_todos(conn)
    assert {t["title"]: (t["due_date"], t["done"], _status(t)) for t in todos} == EXPECTED


def test_seed_summary(conn):
    seed_if_empty(conn, FROZEN_TODAY)
    statuses = [_status(t) for t in db.list_todos(conn)]
    assert summarize(statuses) == {"overdue": 1, "due_soon": 2, "open": 5, "done": 1}


def test_seed_runs_only_once(conn):
    assert seed_if_empty(conn, FROZEN_TODAY) is True
    assert seed_if_empty(conn, FROZEN_TODAY) is False
    assert db.count_todos(conn) == 6


def test_seed_skipped_when_table_has_rows(conn):
    db.create_todo(conn, "My own todo")
    assert seed_if_empty(conn, FROZEN_TODAY) is False
    assert [t["title"] for t in db.list_todos(conn)] == ["My own todo"]


def test_seed_deadlines_follow_today(conn):
    seed_if_empty(conn, date(2026, 12, 31))
    dues = {t["title"]: t["due_date"] for t in db.list_todos(conn)}
    assert dues["Set up GitHub Actions"] == "2027-01-01"
    assert dues["Prepare demo slides"] == "2027-01-07"


def test_app_startup_seeds_once(client):
    # `client` already started the app once (and seeded it); start a second app on the same DB.
    restarted = create_app()
    restarted.dependency_overrides[get_today] = lambda: FROZEN_TODAY
    with TestClient(restarted) as second:
        assert len(second.get("/api/todos").json()) == 6
        assert second.get("/api/todos/summary").json() == {"overdue": 1, "due_soon": 2, "open": 5, "done": 1}
