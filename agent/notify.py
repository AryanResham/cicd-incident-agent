"""Alerts (PLAN §B6). GitHub only for now: every alert is an issue or an issue comment.

Everything the agent tells humans goes through `notify()`, so adding Discord or
Slack later means changing this one function (e.g. also POST to a webhook).
"""

from __future__ import annotations

from agent.github_api import GitHub


def notify(gh: GitHub, message: str, *, issue: int | None = None, title: str | None = None,
           labels: list[str] | None = None, log=print) -> int:
    """Comment on `issue`, or open a new issue when no issue number is given. Returns the issue number."""
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    if issue is not None:
        log(f"[notify] #{issue}: {first_line[:120]}")
        gh.comment(issue, message)
        if labels:
            gh.add_labels(issue, labels)
        return issue
    created = gh.create_issue(title or "Incident", message, labels or [])
    log(f"[notify] opened issue #{created['number']}: {title}")
    return created["number"]
