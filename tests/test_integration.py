"""Integration tests: the page and the API working together, as the browser uses them."""


def ids(client):
    return [todo["id"] for todo in client.get("/api/todos").json()]


def summary(client):
    return client.get("/api/todos/summary").json()


def test_page_is_html_and_uses_the_api(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/api/todos" in response.text
    assert "/api/todos/summary" in response.text


def test_page_is_served_from_any_working_directory(tmp_path, monkeypatch, request):
    # In Docker (or under a process manager) uvicorn may not start from the repo root.
    monkeypatch.chdir(tmp_path)
    client = request.getfixturevalue("client")  # app created after the chdir
    response = client.get("/")
    assert response.status_code == 200
    assert "/api/todos" in response.text


def test_full_user_journey(client):
    # Seed: [overdue, due_today, due_tomorrow, upcoming, no_deadline, done]
    seeded = ids(client)
    assert summary(client) == {"overdue": 1, "due_soon": 2, "open": 5, "done": 1}

    # Create a todo due tomorrow: it joins the due_tomorrow group, after the seeded one (higher id).
    response = client.post("/api/todos", json={"title": "  Record demo video  ", "due_date": "2026-10-06"})
    assert response.status_code == 201
    todo = response.json()
    todo_id = todo["id"]
    assert todo["title"] == "Record demo video"
    assert todo["status"] == "due_tomorrow"
    assert ids(client) == seeded[:3] + [todo_id] + seeded[3:]
    assert summary(client) == {"overdue": 1, "due_soon": 3, "open": 6, "done": 1}

    # Mark it done: status "done" and it moves to the end of the list.
    response = client.patch(f"/api/todos/{todo_id}", json={"done": True})
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert response.json()["due_date"] == "2026-10-06"
    assert ids(client) == seeded + [todo_id]
    assert summary(client) == {"overdue": 1, "due_soon": 2, "open": 5, "done": 2}

    # Undo: back to its deadline status and position.
    response = client.patch(f"/api/todos/{todo_id}", json={"done": False})
    assert response.json()["status"] == "due_tomorrow"
    assert ids(client) == seeded[:3] + [todo_id] + seeded[3:]
    assert summary(client) == {"overdue": 1, "due_soon": 3, "open": 6, "done": 1}

    # Edit the title only: the deadline is kept.
    response = client.patch(f"/api/todos/{todo_id}", json={"title": "Record the demo video"})
    assert response.json()["title"] == "Record the demo video"
    assert response.json()["due_date"] == "2026-10-06"
    assert response.json()["status"] == "due_tomorrow"
    assert summary(client) == {"overdue": 1, "due_soon": 3, "open": 6, "done": 1}

    # Clear the deadline: no_deadline, after the seeded no_deadline todo, before the done one.
    response = client.patch(f"/api/todos/{todo_id}", json={"due_date": None})
    assert response.json()["due_date"] is None
    assert response.json()["status"] == "no_deadline"
    assert ids(client) == seeded[:5] + [todo_id] + seeded[5:]
    assert summary(client) == {"overdue": 1, "due_soon": 2, "open": 6, "done": 1}

    # Delete it: gone, and back to the seed summary.
    assert client.delete(f"/api/todos/{todo_id}").status_code == 204
    assert client.get(f"/api/todos/{todo_id}").status_code == 404
    assert ids(client) == seeded
    assert summary(client) == {"overdue": 1, "due_soon": 2, "open": 5, "done": 1}
