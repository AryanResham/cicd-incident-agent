import base64
import json

import httpx
import pytest

from agent.github_api import GitHub, GitHubError


class Recorder:
    """MockTransport handler: answers from a {(method, path): response} table and records requests."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(404, json={"message": f"no route {key}"})
        answer = self.routes[key]
        return answer(request) if callable(answer) else answer

    def body(self, i: int) -> dict:
        return json.loads(self.requests[i].content)


def make(routes) -> tuple[GitHub, Recorder]:
    rec = Recorder(routes)
    return GitHub("me/todo", "tok123", client=httpx.Client(transport=httpx.MockTransport(rec))), rec


def test_sends_auth_headers_and_raises_on_errors():
    gh, rec = make({("GET", "/repos/me/todo/issues/1"): httpx.Response(500, text="boom")})
    with pytest.raises(GitHubError) as err:
        gh.get_issue(1)
    assert err.value.status == 500
    assert rec.requests[0].headers["authorization"] == "Bearer tok123"


def test_find_open_incident_ignores_pull_requests_and_picks_oldest():
    issues = [
        {"number": 9, "labels": [{"name": "incident:active"}]},
        {"number": 4, "labels": [{"name": "incident:active"}]},
        {"number": 2, "pull_request": {}, "labels": []},
    ]
    gh, rec = make({("GET", "/repos/me/todo/issues"): httpx.Response(200, json=issues)})
    assert gh.find_open_incident()["number"] == 4
    assert rec.requests[0].url.params["labels"] == "incident:active"
    assert rec.requests[0].url.params["state"] == "open"


def test_find_open_incident_none():
    gh, _ = make({("GET", "/repos/me/todo/issues"): httpx.Response(200, json=[])})
    assert gh.find_open_incident() is None


def test_create_issue_comment_close():
    gh, rec = make({
        ("POST", "/repos/me/todo/issues"): httpx.Response(201, json={"number": 7}),
        ("POST", "/repos/me/todo/issues/7/comments"): httpx.Response(201, json={"id": 1}),
        ("PATCH", "/repos/me/todo/issues/7"): httpx.Response(200, json={"number": 7, "state": "closed"}),
    })
    assert gh.create_issue("t", "b", ["needs-human"])["number"] == 7
    gh.close_issue(7, comment="done")
    assert rec.body(0)["labels"] == ["needs-human"]
    assert rec.body(1) == {"body": "done"}
    assert rec.body(2) == {"state": "closed"}


def test_ensure_labels_tolerates_existing():
    gh, rec = make({("POST", "/repos/me/todo/labels"): httpx.Response(422, json={"message": "exists"})})
    gh.ensure_labels()
    assert len(rec.requests) == 5


def test_commit_files_builds_tree_commit_and_moves_ref():
    gh, rec = make({
        ("GET", "/repos/me/todo/git/ref/heads/agent/fix-3-1"): httpx.Response(200, json={"object": {"sha": "p1"}}),
        ("GET", "/repos/me/todo/git/commits/p1"): httpx.Response(200, json={"tree": {"sha": "t0"}}),
        ("POST", "/repos/me/todo/git/trees"): httpx.Response(201, json={"sha": "t1"}),
        ("POST", "/repos/me/todo/git/commits"): httpx.Response(201, json={"sha": "c1"}),
        ("PATCH", "/repos/me/todo/git/refs/heads/agent/fix-3-1"): httpx.Response(200, json={}),
    })
    sha = gh.commit_files("agent/fix-3-1", {"requirements.txt": "fastapi\n"}, "fix: deps")
    assert sha == "c1"
    tree = rec.body(2)
    assert tree["base_tree"] == "t0"
    assert tree["tree"][0] == {"path": "requirements.txt", "mode": "100644", "type": "blob", "content": "fastapi\n"}
    assert rec.body(3) == {"message": "fix: deps", "tree": "t1", "parents": ["p1"]}
    assert rec.body(4) == {"sha": "c1"}


def test_get_file_decodes_and_handles_missing():
    content = base64.b64encode(b"hello").decode()
    gh, _ = make({("GET", "/repos/me/todo/contents/app/main.py"): httpx.Response(200, json={"content": content})})
    assert gh.get_file("app/main.py", "main") == "hello"
    assert gh.get_file("nope.py", "main") is None


def test_enable_auto_merge_raises_on_graphql_errors():
    gh, rec = make({("POST", "/graphql"): httpx.Response(200, json={"errors": [{"message": "auto-merge off"}]})})
    with pytest.raises(GitHubError, match="auto-merge off"):
        gh.enable_auto_merge("PR_node")
    assert rec.body(0)["variables"] == {"id": "PR_node", "method": "SQUASH"}


def test_actions_endpoints():
    gh, rec = make({
        ("GET", "/repos/me/todo/actions/workflows/ci-cd.yml/runs"):
            httpx.Response(200, json={"workflow_runs": [{"id": 1}]}),
        ("GET", "/repos/me/todo/actions/runs/1/jobs"): httpx.Response(200, json={"jobs": [{"id": 11}]}),
        ("GET", "/repos/me/todo/actions/jobs/11/logs"):
            httpx.Response(302, headers={"location": "https://blob.test/log.txt"}),
        ("GET", "/log.txt"): httpx.Response(200, text="line1\nline2"),
        ("POST", "/repos/me/todo/actions/runs/1/rerun-failed-jobs"): httpx.Response(201),
    })
    assert gh.list_workflow_runs() == [{"id": 1}]
    assert rec.requests[0].url.params["branch"] == "main"
    assert gh.list_jobs(1) == [{"id": 11}]
    assert gh.job_logs(11) == "line1\nline2"  # redirect followed
    assert "authorization" not in rec.requests[-1].headers  # token not leaked to blob storage
    gh.rerun_failed_jobs(1)
