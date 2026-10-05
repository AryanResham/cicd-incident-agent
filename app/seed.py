"""Demo todos, inserted at startup when the table is empty (see docs/API.md)."""

import sqlite3
from datetime import date, timedelta

from app import db

# (title, deadline in days from today or None, done)
SEED_TODOS = [
    ("Submit project title", -3, False),
    ("Write pytest tests", 0, False),
    ("Set up GitHub Actions", 1, False),
    ("Prepare demo slides", 7, False),
    ("Read FastAPI docs", None, False),
    ("Initialize git repo", -2, True),
]


def seed_if_empty(conn: sqlite3.Connection, today: date) -> bool:
    """Insert the seed todos relative to `today`. Returns True if anything was inserted."""
    if db.count_todos(conn) > 0:
        return False
    for title, offset, done in SEED_TODOS:
        due = None if offset is None else (today + timedelta(days=offset)).isoformat()
        db.create_todo(conn, title, due_date=due, done=done)
    return True
