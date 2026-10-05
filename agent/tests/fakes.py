"""Shared fakes for the agent's unit tests (no network, no real services)."""

from __future__ import annotations

import json

import httpx


class FakeTodoApp:
    """In-memory imitation of the to-do API (docs/API.md), served through httpx.MockTransport.

    Flags let a test break one behaviour at a time.
    """

    def __init__(self, *, health_status=200, page_status=200, done_works=True, create_status=201,
                 delay=0.0, down=False, version="dev"):
        self.todos: dict[int, dict] = {}
        self.next_id = 1
        self.health_status = health_status
        self.page_status = page_status
        self.done_works = done_works
        self.create_status = create_status
        self.delay = delay
        self.down = down
        self.version = version  # what /health reports (the image's APP_VERSION)
        self.health_body: dict | None = None  # overrides the /health body
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
            body = self.health_body if self.health_body is not None else {"status": "ok", "version": self.version}
            return httpx.Response(self.health_status, json=body)
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


class FakeGitHub:
    """In-memory stand-in for agent.github_api.GitHub (same method names, no HTTP)."""

    def __init__(self, repo="Me/Todo"):
        self.repo = repo
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, list[str]] = {}
        self.prs: dict[int, dict] = {}
        self.commit_prs: dict[str, list[dict]] = {}
        self.runs: list[dict] = []  # newest first, like the API
        self.jobs: dict[int, list[dict]] = {}
        self.logs: dict[int, str] = {}
        self.artifacts: dict[int, list[dict]] = {}
        self.artifact_zips: dict[int, bytes] = {}
        self.compares: dict[tuple[str, str], dict] = {}
        self.branches: dict[str, str] = {"main": "mainsha"}
        self.commits: list[tuple[str, dict, str]] = []
        self.auto_merge: list[str] = []
        self.auto_merge_error: Exception | None = None
        self.merged: list[int] = []
        self.reruns: list = []
        self._next = 1

    def _number(self) -> int:
        self._next += 1
        return self._next - 1

    # issues
    def ensure_labels(self):
        pass

    def list_issues(self, labels=None, state="open"):
        out = []
        for issue in self.issues.values():
            names = {lbl["name"] for lbl in issue["labels"]}
            if issue["state"] == state and all(lbl in names for lbl in labels or []):
                out.append(issue)
        return out

    def find_open_issue(self, label):
        issues = self.list_issues([label])
        return min(issues, key=lambda i: i["number"]) if issues else None

    def find_open_incident(self):
        return self.find_open_issue("incident:active")

    def get_issue(self, number):
        return self.issues[number]

    def create_issue(self, title, body, labels=None):
        n = self._number()
        self.issues[n] = {"number": n, "title": title, "body": body, "state": "open",
                          "labels": [{"name": lbl} for lbl in labels or []],
                          "created_at": "2026-10-05T10:00:00Z", "html_url": f"https://gh/issues/{n}"}
        self.comments[n] = []
        return self.issues[n]

    def update_issue(self, number, **fields):
        self.issues[number].update(fields)
        return self.issues[number]

    def comment(self, number, body):
        self.comments.setdefault(number, []).append(body)
        return {"id": len(self.comments[number])}

    def add_labels(self, number, labels):
        target = self.issues.get(number) or self.prs[number]
        for lbl in labels:
            if lbl not in [x["name"] for x in target["labels"]]:
                target["labels"].append({"name": lbl})

    def remove_label(self, number, label):
        target = self.issues[number]
        target["labels"] = [x for x in target["labels"] if x["name"] != label]

    def close_issue(self, number, comment=None):
        if comment:
            self.comment(number, comment)
        self.issues[number]["state"] = "closed"

    def labels_of(self, number):
        target = self.issues.get(number) or self.prs[number]
        return [x["name"] for x in target["labels"]]

    # git
    def get_branch_sha(self, branch):
        return self.branches[branch]

    def create_branch(self, branch, sha):
        self.branches[branch] = sha

    def commit_files(self, branch, files, message):
        self.commits.append((branch, dict(files), message))
        self.branches[branch] = f"commit{len(self.commits)}"
        return self.branches[branch]

    def get_file(self, path, ref):
        return None

    def compare(self, base, head):
        return self.compares.get((base, head), {"files": []})

    # PRs
    def create_pr(self, head, title, body, base="main"):
        n = self._number()
        self.prs[n] = {"number": n, "node_id": f"PR_{n}", "html_url": f"https://gh/pull/{n}", "title": title,
                       "body": body, "head": {"ref": head}, "labels": [], "state": "open"}
        return self.prs[n]

    def list_prs(self, state="all"):
        return list(self.prs.values())

    def prs_for_commit(self, sha):
        return self.commit_prs.get(sha, [])

    def enable_auto_merge(self, node_id, method="SQUASH"):
        if self.auto_merge_error:
            raise self.auto_merge_error
        self.auto_merge.append(node_id)

    def merge_pr(self, number, method="squash"):
        self.merged.append(number)

    # actions
    def list_workflow_runs(self, workflow_file="ci-cd.yml", branch="main", status="completed", per_page=30):
        return [r for r in self.runs if r.get("head_branch", "main") == branch][:per_page]

    def get_run(self, run_id):
        return next(r for r in self.runs if r["id"] == int(run_id))

    def list_jobs(self, run_id):
        return self.jobs.get(int(run_id), [])

    def job_logs(self, job_id):
        return self.logs.get(int(job_id), "")

    def list_artifacts(self, run_id):
        return self.artifacts.get(int(run_id), [])

    def download_artifact(self, artifact_id):
        return self.artifact_zips[int(artifact_id)]

    def rerun_failed_jobs(self, run_id):
        self.reruns.append(run_id)

    # helpers for tests
    def add_run(self, run_id, sha, conclusion="success", jobs=None, branch="main", **extra):
        """Add a CI/CD run. jobs = {name: conclusion}; default = all four jobs succeeded."""
        jobs = jobs or {"test": "success", "build": "success", "deploy": "success", "verify": "success"}
        run = {"id": run_id, "head_sha": sha, "head_branch": branch, "conclusion": conclusion,
               "html_url": f"https://gh/runs/{run_id}", "run_attempt": 1, "event": "push",
               "updated_at": "2026-10-05T09:58:00Z", **extra}
        self.runs.append(run)
        self.jobs[run_id] = [{"id": run_id * 100 + i, "name": name, "conclusion": c,
                              "steps": [{"name": f"Run {name}", "conclusion": c, "number": 1}]}
                             for i, (name, c) in enumerate(jobs.items())]
        return run
