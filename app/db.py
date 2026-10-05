"""SQLite storage for todos, using only the standard library."""

import os
import sqlite3
from pathlib import Path

from app import clock

DEFAULT_DATABASE_PATH = "./todos.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    due_date   TEXT,             -- 'YYYY-MM-DD' or NULL
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
)
"""

# Columns a caller may change through update_todo().
EDITABLE = ("title", "done", "due_date")


def database_path() -> str:
    return os.getenv("DATABASE_PATH", DEFAULT_DATABASE_PATH)


def connect(path: str) -> sqlite3.Connection:
    # FastAPI may open and close the connection on different worker threads.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    conn.execute(SCHEMA)
    conn.commit()
    conn.close()


def _to_dict(row: sqlite3.Row) -> dict:
    todo = dict(row)
    todo["done"] = bool(todo["done"])
    return todo


def count_todos(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]


def list_todos(conn: sqlite3.Connection) -> list[dict]:
    return [_to_dict(row) for row in conn.execute("SELECT * FROM todos")]


def get_todo(conn: sqlite3.Connection, todo_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    return _to_dict(row) if row else None


def create_todo(conn: sqlite3.Connection, title: str, due_date: str | None = None, done: bool = False) -> dict:
    now = clock.timestamp()
    cursor = conn.execute(
        "INSERT INTO todos (title, done, due_date, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (title, int(done), due_date, now, now),
    )
    conn.commit()
    return get_todo(conn, cursor.lastrowid)


def update_todo(conn: sqlite3.Connection, todo_id: int, changes: dict) -> dict | None:
    """Apply only the given fields (plus a fresh updated_at). Returns None if not found."""
    fields = {key: value for key, value in changes.items() if key in EDITABLE}
    if "done" in fields:
        fields["done"] = int(fields["done"])
    fields["updated_at"] = clock.timestamp()

    assignments = ", ".join(f"{column} = ?" for column in fields)  # column names come from EDITABLE
    cursor = conn.execute(f"UPDATE todos SET {assignments} WHERE id = ?", (*fields.values(), todo_id))
    conn.commit()
    return get_todo(conn, todo_id) if cursor.rowcount else None


def delete_todo(conn: sqlite3.Connection, todo_id: int) -> bool:
    cursor = conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    conn.commit()
    return cursor.rowcount > 0
