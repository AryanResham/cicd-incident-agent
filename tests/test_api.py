from datetime import date, datetime

import pytest

from app import clock
from app.main import get_today

TODO_FIELDS = {"id", "title", "done", "due_date", "status", "created_at", "updated_at"}


def add(client, title="Task", due_date=None):
    body = {"title": title}
    if due_date is not None:
        body["due_date"] = due_date
    response = client.post("/api/todos", json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ---------- health + seed through the API ----------

def test_health(client, monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "dev"}


def test_health_reports_app_version(client, monkeypatch):
    monkeypatch.setenv("APP_VERSION", "abc1234")
    assert client.get("/health").json() == {"status": "ok", "version": "abc1234"}


def test_seeded_list_is_sorted_by_status(client):
    todos = client.get("/api/todos").json()
    assert [t["status"] for t in todos] == [
        "overdue", "due_today", "due_tomorrow", "upcoming", "no_deadline", "done",
    ]
    assert todos[0]["title"] == "Submit project title"
    assert set(todos[0]) == TODO_FIELDS


def test_seeded_summary(client):
    assert client.get("/api/todos/summary").json() == {"overdue": 1, "due_soon": 2, "open": 5, "done": 1}


# ---------- create + get ----------

def test_create_without_deadline(empty_client):
    response = empty_client.post("/api/todos", json={"title": "  Buy milk  "})
    assert response.status_code == 201
    todo = response.json()
    assert set(todo) == TODO_FIELDS
    assert todo["title"] == "Buy milk"  # trimmed
    assert todo["done"] is False
    assert todo["due_date"] is None
    assert todo["status"] == "no_deadline"
    assert datetime.fromisoformat(todo["created_at"]).utcoffset().total_seconds() == 5.5 * 3600
    assert todo["created_at"] == todo["updated_at"]


@pytest.mark.parametrize(
    ("due", "status"),
    [("2026-10-04", "overdue"), ("2026-10-05", "due_today"), ("2026-10-06", "due_tomorrow"), ("2026-10-07", "upcoming")],
)
def test_create_with_deadline(empty_client, due, status):
    todo = add(empty_client, "Dated", due)
    assert todo["due_date"] == due
    assert todo["status"] == status  # past deadlines are allowed


def test_create_with_explicit_null_deadline(empty_client):
    response = empty_client.post("/api/todos", json={"title": "x", "due_date": None})
    assert response.status_code == 201
    assert response.json()["status"] == "no_deadline"


def test_get_by_id(empty_client):
    todo = add(empty_client, "Find me", "2026-10-06")
    response = empty_client.get(f"/api/todos/{todo['id']}")
    assert response.status_code == 200
    assert response.json() == todo


def test_list_contains_created_todo(empty_client):
    todo = add(empty_client, "Listed")
    assert empty_client.get("/api/todos").json() == [todo]


# ---------- PATCH semantics ----------

def test_patch_title_keeps_deadline(empty_client):
    todo = add(empty_client, "Old", "2026-10-06")
    response = empty_client.patch(f"/api/todos/{todo['id']}", json={"title": "  New  "})
    assert response.status_code == 200
    assert response.json()["title"] == "New"
    assert response.json()["due_date"] == "2026-10-06"  # omitted = unchanged


def test_patch_change_deadline(empty_client):
    todo = add(empty_client, "Move", "2026-10-06")
    updated = empty_client.patch(f"/api/todos/{todo['id']}", json={"due_date": "2026-10-01"}).json()
    assert updated["due_date"] == "2026-10-01"
    assert updated["status"] == "overdue"
    assert updated["title"] == "Move"


def test_patch_null_removes_deadline(empty_client):
    todo = add(empty_client, "Relax", "2026-10-01")
    updated = empty_client.patch(f"/api/todos/{todo['id']}", json={"due_date": None}).json()
    assert updated["due_date"] is None
    assert updated["status"] == "no_deadline"


def test_mark_done_and_undo(empty_client):
    todo = add(empty_client, "Late", "2026-10-01")
    done = empty_client.patch(f"/api/todos/{todo['id']}", json={"done": True}).json()
    assert done["done"] is True
    assert done["status"] == "done"  # deadline ignored once done

    undone = empty_client.patch(f"/api/todos/{todo['id']}", json={"done": False}).json()
    assert undone["done"] is False
    assert undone["status"] == "overdue"  # back to its deadline status
    assert undone["due_date"] == "2026-10-01"


def test_patch_refreshes_updated_at(empty_client, monkeypatch):
    todo = add(empty_client, "Stamp")
    monkeypatch.setattr(clock, "timestamp", lambda: "2026-10-05T23:59:59+05:30")
    updated = empty_client.patch(f"/api/todos/{todo['id']}", json={"done": True}).json()
    assert updated["updated_at"] == "2026-10-05T23:59:59+05:30"
    assert updated["created_at"] == todo["created_at"]


def test_patch_with_empty_body_changes_nothing(empty_client):
    todo = add(empty_client, "Same", "2026-10-06")
    updated = empty_client.patch(f"/api/todos/{todo['id']}", json={}).json()
    assert {k: updated[k] for k in ("title", "done", "due_date")} == {"title": "Same", "done": False, "due_date": "2026-10-06"}


# ---------- delete + 404s ----------

def test_delete(empty_client):
    todo = add(empty_client, "Bye")
    response = empty_client.delete(f"/api/todos/{todo['id']}")
    assert response.status_code == 204
    assert response.content == b""
    assert empty_client.get(f"/api/todos/{todo['id']}").status_code == 404
    assert empty_client.get("/api/todos").json() == []


@pytest.mark.parametrize(
    ("method", "body"),
    [("get", None), ("patch", {"title": "x"}), ("delete", None)],
)
def test_missing_todo_is_404(empty_client, method, body):
    kwargs = {"json": body} if body is not None else {}
    response = empty_client.request(method.upper(), "/api/todos/999", **kwargs)
    assert response.status_code == 404
    assert response.json() == {"detail": "Todo not found"}


# ---------- validation (422) ----------

@pytest.mark.parametrize(
    "body",
    [
        {},  # missing title
        {"title": ""},
        {"title": "    "},  # blank after trimming
        {"title": "x" * 201},
        {"title": None},
        {"title": 123},
        {"title": "ok", "due_date": "2026-02-30"},  # not a real date
        {"title": "ok", "due_date": "2026-13-01"},
        {"title": "ok", "due_date": "05-10-2026"},
        {"title": "ok", "due_date": "2026-10-05T10:00:00"},
        {"title": "ok", "due_date": "tomorrow"},
        {"title": "ok", "due_date": 20261005},
        {"title": "ok", "done": True},  # unknown field on create
        {"title": "ok", "priority": "high"},
    ],
)
def test_create_rejects_invalid_body(empty_client, body):
    response = empty_client.post("/api/todos", json=body)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    assert "loc" in response.json()["detail"][0]
    assert empty_client.get("/api/todos").json() == []  # nothing was stored


def test_create_rejects_non_object_body(empty_client):
    assert empty_client.post("/api/todos", json=["title"]).status_code == 422


def test_title_of_exactly_200_chars_is_allowed(empty_client):
    assert add(empty_client, "x" * 200)["title"] == "x" * 200


@pytest.mark.parametrize(
    "body",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"title": None},  # title can't be removed
        {"done": None},
        {"done": "true"},
        {"done": 1},
        {"due_date": "2026-02-30"},
        {"due_date": 5},
        {"status": "done"},  # computed, not writable
        {"id": 5},
        {"color": "red"},
    ],
)
def test_patch_rejects_invalid_body(empty_client, body):
    todo = add(empty_client, "Keep", "2026-10-06")
    response = empty_client.patch(f"/api/todos/{todo['id']}", json=body)
    assert response.status_code == 422
    assert empty_client.get(f"/api/todos/{todo['id']}").json() == todo  # unchanged


def test_non_integer_id_is_422(empty_client):
    assert empty_client.get("/api/todos/abc").status_code == 422


# ---------- sorting + summary ----------

def test_sort_order_within_groups(empty_client):
    a = add(empty_client, "upcoming late", "2026-10-20")
    b = add(empty_client, "no deadline 1")
    c = add(empty_client, "upcoming early", "2026-10-08")
    d = add(empty_client, "overdue recent", "2026-10-04")
    e = add(empty_client, "upcoming early twin", "2026-10-08")
    f = add(empty_client, "overdue old", "2026-09-01")
    g = add(empty_client, "no deadline 2")
    h = add(empty_client, "done undated")
    i = add(empty_client, "done dated", "2026-10-30")
    j = add(empty_client, "today", "2026-10-05")
    k = add(empty_client, "tomorrow", "2026-10-06")
    for todo in (h, i):
        empty_client.patch(f"/api/todos/{todo['id']}", json={"done": True})

    ids = [t["id"] for t in empty_client.get("/api/todos").json()]
    expected = [f, d, j, k, c, e, a, b, g, i, h]
    assert ids == [t["id"] for t in expected]


def test_summary_counts(empty_client):
    add(empty_client, "o1", "2026-09-30")
    add(empty_client, "o2", "2026-10-04")
    add(empty_client, "t", "2026-10-05")
    add(empty_client, "tm", "2026-10-06")
    add(empty_client, "u", "2026-10-07")
    add(empty_client, "n")
    finished = add(empty_client, "d", "2026-09-01")
    empty_client.patch(f"/api/todos/{finished['id']}", json={"done": True})

    assert empty_client.get("/api/todos/summary").json() == {"overdue": 2, "due_soon": 2, "open": 6, "done": 1}


def test_summary_when_empty(empty_client):
    assert empty_client.get("/api/todos/summary").json() == {"overdue": 0, "due_soon": 0, "open": 0, "done": 0}


def test_status_follows_the_injected_today(empty_client):
    todo = add(empty_client, "Tomorrow's job", "2026-10-06")
    assert todo["status"] == "due_tomorrow"

    empty_client.app.dependency_overrides[get_today] = lambda: date(2026, 10, 7)  # two days later
    assert empty_client.get(f"/api/todos/{todo['id']}").json()["status"] == "overdue"
