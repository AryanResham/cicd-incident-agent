"""Shared fakes for the agent's unit tests (no network, no real services)."""

from __future__ import annotations

import json

import httpx


class FakeTodoApp:
    """In-memory imitation of the to-do API (docs/API.md), served through httpx.MockTransport.

    Flags let a test break one behaviour at a time.
    """

    def __init__(self, *, health_status=200, page_status=200, done_works=True, create_status=201,
                 delay=0.0, down=False):
        self.todos: dict[int, dict] = {}
        self.next_id = 1
        self.health_status = health_status
        self.page_status = page_status
        self.done_works = done_works
        self.create_status = create_status
        self.delay = delay
        self.down = down
        self.calls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.delay:
            import time
            time.sleep(self.delay)
        if path == "/health":
            return httpx.Response(self.health_status, json={"status": "ok"})
        if path == "/":
            return httpx.Response(self.page_status, html="<html><body>To-Do</body></html>")
        if path == "/api/todos" and method == "GET":
            return httpx.Response(200, json=list(self.todos.values()))
        if path == "/api/todos" and method == "POST":
            body = json.loads(request.content)
            todo = {"id": self.next_id, "title": body["title"], "done": False,
                    "due_date": body.get("due_date"), "status": "upcoming"}
            self.todos[self.next_id] = todo
            self.next_id += 1
            return httpx.Response(self.create_status, json=todo)
        if path.startswith("/api/todos/"):
            todo_id = int(path.rsplit("/", 1)[1])
            todo = self.todos.get(todo_id)
            if todo is None:
                return httpx.Response(404, json={"detail": "Todo not found"})
            if method == "GET":
                return httpx.Response(200, json=todo)
            if method == "PATCH":
                body = json.loads(request.content)
                if "done" in body and self.done_works:
                    todo["done"] = body["done"]
                    todo["status"] = "done" if body["done"] else "upcoming"
                return httpx.Response(200, json=todo)
            if method == "DELETE":
                del self.todos[todo_id]
                return httpx.Response(204)
        return httpx.Response(404, json={"detail": "Not Found"})
