from app import db


def test_create_and_get(conn):
    todo = db.create_todo(conn, "Buy milk", due_date="2026-10-07")
    assert todo["id"] > 0
    assert todo["title"] == "Buy milk"
    assert todo["done"] is False
    assert todo["due_date"] == "2026-10-07"
    assert todo["created_at"] == todo["updated_at"]
    assert db.get_todo(conn, todo["id"]) == todo


def test_get_missing_returns_none(conn):
    assert db.get_todo(conn, 999) is None


def test_update_changes_only_given_fields(conn):
    todo = db.create_todo(conn, "Old", due_date="2026-10-07")
    updated = db.update_todo(conn, todo["id"], {"done": True})
    assert updated["done"] is True
    assert updated["title"] == "Old"
    assert updated["due_date"] == "2026-10-07"

    cleared = db.update_todo(conn, todo["id"], {"due_date": None})
    assert cleared["due_date"] is None


def test_update_missing_returns_none(conn):
    assert db.update_todo(conn, 999, {"title": "x"}) is None


def test_delete(conn):
    todo = db.create_todo(conn, "Temp")
    assert db.delete_todo(conn, todo["id"]) is True
    assert db.delete_todo(conn, todo["id"]) is False
    assert db.list_todos(conn) == []


def test_init_db_is_idempotent(db_path):
    db.init_db(db_path)
    conn = db.connect(db_path)
    db.create_todo(conn, "Keep me")
    conn.close()

    db.init_db(db_path)  # second startup must not wipe data
    conn = db.connect(db_path)
    assert db.count_todos(conn) == 1
    conn.close()
