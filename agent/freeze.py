"""Deploy freeze (PLAN §B3): used by the `deploy` job of ci-cd.yml.

    python -m agent.freeze --sha <commit sha>

Writes `deploy=true|false` and `reason=...` to $GITHUB_OUTPUT (or prints them).
Rule: deploy, unless an issue labelled `incident:active` is open AND the commit
does not come from an agent fix PR (head branch `agent/fix-*`).
Exit code is 0 unless something actually went wrong (e.g. the API is unreachable).
"""

from __future__ import annotations

import argparse
import os
import sys

from agent.github_api import GitHub

FIX_BRANCH_PREFIX = "agent/fix-"


def fix_pr_for_commit(gh: GitHub, sha: str) -> dict | None:
    """The agent fix PR this commit came from, if any."""
    for pr in gh.prs_for_commit(sha):
        if pr.get("head", {}).get("ref", "").startswith(FIX_BRANCH_PREFIX):
            return pr
    return None


def should_deploy(gh: GitHub, sha: str) -> tuple[bool, str]:
    incident = gh.find_open_incident()
    if incident is None:
        return True, "no active incident"
    pr = fix_pr_for_commit(gh, sha)
    if pr is not None:
        return True, f"commit comes from agent fix PR #{pr['number']} for incident #{incident['number']}"
    return False, f"deploy freeze: incident #{incident['number']} is open and this commit is not an agent fix"


def write_output(deploy: bool, reason: str, env=os.environ) -> None:
    lines = f"deploy={'true' if deploy else 'false'}\nreason={reason}\n"
    path = env.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines)
    print(lines, end="")


def main(argv: list[str] | None = None, gh: GitHub | None = None, env=os.environ) -> int:
    parser = argparse.ArgumentParser(description="Decide whether this commit may be deployed.")
    parser.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    try:
        deploy, reason = should_deploy(gh or GitHub.from_env(env), args.sha)
    except Exception as exc:  # a real error: don't guess, fail the step loudly
        print(f"freeze check failed: {exc}", file=sys.stderr)
        return 1
    write_output(deploy, reason, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
