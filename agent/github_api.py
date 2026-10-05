"""Thin wrapper around the GitHub REST (and one GraphQL) API.

Every method is a single, small API call so the rest of the agent stays easy to
read. The httpx client can be injected, so tests use httpx.MockTransport and
never touch the network.
"""

from __future__ import annotations

import base64
import os

import httpx

API_URL = "https://api.github.com"
CI_WORKFLOW_FILE = "ci-cd.yml"  # the main pipeline (workflow name "CI/CD")

# Labels the agent uses. Colours are only cosmetic.
LABEL_ACTIVE = "incident:active"  # an open incident -> deploy freeze
LABEL_LIVE_DOWN = "incident:live-down"  # opened by the health monitor path
LABEL_NEEDS_HUMAN = "needs-human"
LABEL_ROLLBACK_FAILED = "rollback-failed"
LABEL_AGENT_FIX = "agent-fix"
LABELS = {
    LABEL_ACTIVE: "d73a4a",
    LABEL_LIVE_DOWN: "b60205",
    LABEL_NEEDS_HUMAN: "fbca04",
    LABEL_ROLLBACK_FAILED: "5319e7",
    LABEL_AGENT_FIX: "0e8a16",
}


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API error {status}: {message}")
        self.status = status


class GitHub:
    def __init__(self, repo: str, token: str, client: httpx.Client | None = None):
        self.repo = repo  # "owner/name"
        self.client = client or httpx.Client(timeout=30.0)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @classmethod
    def from_env(cls, env=os.environ) -> "GitHub":
        """AGENT_TOKEN (a PAT) is preferred: events created with GITHUB_TOKEN don't trigger workflows."""
        token = env.get("AGENT_TOKEN") or env.get("GITHUB_TOKEN")
        repo = env.get("GITHUB_REPOSITORY")
        if not token or not repo:
            raise RuntimeError("AGENT_TOKEN/GITHUB_TOKEN and GITHUB_REPOSITORY must be set")
        return cls(repo, token)

    # ---- low level -------------------------------------------------------
    def request(self, method: str, path: str, *, expected=(200, 201, 202, 204), **kwargs) -> httpx.Response:
        url = path if path.startswith("http") else f"{API_URL}{path}"
        resp = self.client.request(method, url, headers=self.headers, follow_redirects=True, **kwargs)
        if resp.status_code not in expected:
            raise GitHubError(resp.status_code, resp.text[:500])
        return resp

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs).json()

    def post(self, path: str, payload: dict | None = None, **kwargs):
        resp = self.request("POST", path, json=payload or {}, **kwargs)
        return resp.json() if resp.content else {}

    def patch(self, path: str, payload: dict):
        return self.request("PATCH", path, json=payload).json()

    @property
    def _r(self) -> str:
        return f"/repos/{self.repo}"

    # ---- labels + issues -------------------------------------------------
    def ensure_labels(self) -> None:
        """Create the agent's labels if they don't exist yet (422 = already exists)."""
        for name, color in LABELS.items():
            self.request("POST", f"{self._r}/labels", json={"name": name, "color": color},
                         expected=(201, 422))

    def list_issues(self, labels: list[str] | None = None, state: str = "open") -> list[dict]:
        params = {"state": state, "per_page": 50}
        if labels:
            params["labels"] = ",".join(labels)
        issues = self.get(f"{self._r}/issues", params=params)
        return [i for i in issues if "pull_request" not in i]  # the issues API also returns PRs

    def find_open_incident(self) -> dict | None:
        """The current incident = the oldest open issue labelled incident:active."""
        issues = self.list_issues([LABEL_ACTIVE])
        return min(issues, key=lambda i: i["number"]) if issues else None

    def find_open_issue(self, label: str) -> dict | None:
        issues = self.list_issues([label])
        return min(issues, key=lambda i: i["number"]) if issues else None

    def get_issue(self, number: int) -> dict:
        return self.get(f"{self._r}/issues/{number}")

    def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> dict:
        return self.post(f"{self._r}/issues", {"title": title, "body": body, "labels": labels or []})

    def update_issue(self, number: int, **fields) -> dict:
        return self.patch(f"{self._r}/issues/{number}", fields)

    def comment(self, number: int, body: str) -> dict:
        return self.post(f"{self._r}/issues/{number}/comments", {"body": body})

    def add_labels(self, number: int, labels: list[str]) -> None:
        self.post(f"{self._r}/issues/{number}/labels", {"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        self.request("DELETE", f"{self._r}/issues/{number}/labels/{label}", expected=(200, 404))

    def close_issue(self, number: int, comment: str | None = None) -> None:
        if comment:
            self.comment(number, comment)
        self.update_issue(number, state="closed")

    # ---- git data: branches + commits ------------------------------------
    def get_branch_sha(self, branch: str) -> str:
        return self.get(f"{self._r}/git/ref/heads/{branch}")["object"]["sha"]

    def create_branch(self, branch: str, sha: str) -> None:
        self.post(f"{self._r}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})

    def get_file(self, path: str, ref: str) -> str | None:
        resp = self.request("GET", f"{self._r}/contents/{path}", params={"ref": ref}, expected=(200, 404))
        if resp.status_code == 404:
            return None
        return base64.b64decode(resp.json()["content"]).decode("utf-8")

    def commit_files(self, branch: str, files: dict[str, str], message: str) -> str:
        """Commit {path: new content} on top of `branch` in one commit; returns the new commit SHA."""
        parent = self.get_branch_sha(branch)
        base_tree = self.get(f"{self._r}/git/commits/{parent}")["tree"]["sha"]
        tree = self.post(f"{self._r}/git/trees", {
            "base_tree": base_tree,
            "tree": [{"path": p, "mode": "100644", "type": "blob", "content": c} for p, c in files.items()],
        })
        commit = self.post(f"{self._r}/git/commits",
                           {"message": message, "tree": tree["sha"], "parents": [parent]})
        self.patch(f"{self._r}/git/refs/heads/{branch}", {"sha": commit["sha"]})
        return commit["sha"]

    def compare(self, base: str, head: str) -> dict:
        """`git diff base..head` as the API sees it: {'files': [{'filename', 'status', 'patch'}...]}."""
        return self.get(f"{self._r}/compare/{base}...{head}")

    # ---- pull requests ---------------------------------------------------
    def create_pr(self, head: str, title: str, body: str, base: str = "main") -> dict:
        return self.post(f"{self._r}/pulls", {"title": title, "head": head, "base": base, "body": body})

    def list_prs(self, state: str = "all") -> list[dict]:
        return self.get(f"{self._r}/pulls", params={"state": state, "per_page": 100})

    def prs_for_commit(self, sha: str) -> list[dict]:
        """PRs whose merge produced (or contain) this commit."""
        return self.get(f"{self._r}/commits/{sha}/pulls")

    def enable_auto_merge(self, pr_node_id: str, method: str = "SQUASH") -> None:
        query = """mutation($id: ID!, $method: PullRequestMergeMethod!) {
          enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) { clientMutationId }
        }"""
        resp = self.request("POST", f"{API_URL}/graphql",
                            json={"query": query, "variables": {"id": pr_node_id, "method": method}})
        errors = resp.json().get("errors")
        if errors:
            raise GitHubError(422, "; ".join(e.get("message", "") for e in errors))

    def merge_pr(self, number: int, method: str = "squash") -> None:
        """Merge now (only used when the PR is already 'clean', i.e. its checks have passed)."""
        self.request("PUT", f"{self._r}/pulls/{number}/merge", json={"merge_method": method})

    # ---- actions ---------------------------------------------------------
    def list_workflow_runs(self, workflow_file: str = CI_WORKFLOW_FILE, branch: str = "main",
                           status: str = "completed", per_page: int = 30) -> list[dict]:
        data = self.get(f"{self._r}/actions/workflows/{workflow_file}/runs",
                        params={"branch": branch, "status": status, "per_page": per_page})
        return data["workflow_runs"]

    def get_run(self, run_id: int | str) -> dict:
        return self.get(f"{self._r}/actions/runs/{run_id}")

    def list_jobs(self, run_id: int | str) -> list[dict]:
        return self.get(f"{self._r}/actions/runs/{run_id}/jobs", params={"per_page": 50})["jobs"]

    def job_logs(self, job_id: int | str) -> str:
        """Plain-text log of one job (GitHub answers with a redirect to blob storage)."""
        return self.request("GET", f"{self._r}/actions/jobs/{job_id}/logs").text

    def list_artifacts(self, run_id: int | str) -> list[dict]:
        return self.get(f"{self._r}/actions/runs/{run_id}/artifacts")["artifacts"]

    def download_artifact(self, artifact_id: int | str) -> bytes:
        """The artifact as zip bytes."""
        return self.request("GET", f"{self._r}/actions/artifacts/{artifact_id}/zip").content

    def rerun_failed_jobs(self, run_id: int | str) -> None:
        self.request("POST", f"{self._r}/actions/runs/{run_id}/rerun-failed-jobs")
