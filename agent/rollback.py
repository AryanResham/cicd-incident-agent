"""Rollback to the last stable image (PLAN §B3 ①). Pure rules: never needs Gemini.

"Last stable SHA" = head SHA of the most recent CI/CD run on main whose `verify`
job concluded `success`. (A run can be green with deploy/verify *skipped*
because of the deploy freeze, so the run conclusion alone is not enough.)
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass

import httpx

from agent.github_api import GitHub
from agent.smoke import SmokeReport, run_smoke

VERIFY_JOB = "verify"
MAX_WAIT = 600.0  # give Render up to 10 minutes to roll the old image out
POLL_EVERY = 30.0


@dataclass
class RollbackResult:
    ok: bool
    reason: str
    stable_sha: str | None = None
    image: str | None = None
    seconds: float = 0.0
    smoke: SmokeReport | None = None
    missing_secret: str | None = None

    @property
    def escalate(self) -> bool:
        """A failed rollback means something bigger is wrong: stop automating, call a human."""
        return not self.ok


def _is_verify_job(job: dict) -> bool:
    # Match the job id "verify" even if the job got a display name like "verify (smoke checks)".
    return job.get("name", "").strip().lower().split(" ")[0] == VERIFY_JOB


def find_last_stable_sha(gh: GitHub, exclude: set[str] | None = None, max_runs: int = 30) -> str | None:
    """Newest main run whose verify job succeeded (skipping SHAs known to be broken)."""
    exclude = exclude or set()
    for run in gh.list_workflow_runs(branch="main", status="completed", per_page=max_runs):
        if run["head_sha"] in exclude or run.get("conclusion") != "success":
            continue  # verify is the last job, so a verified run is always a successful run
        if any(_is_verify_job(j) and j.get("conclusion") == "success" for j in gh.list_jobs(run["id"])):
            return run["head_sha"]
    return None


def image_for(repo: str, sha: str) -> str:
    return f"ghcr.io/{repo.lower()}:{sha}"


def deploy_url(hook_url: str, image: str) -> str:
    """Render deploy hook + `imgURL` (URL-encoded) = deploy exactly this image."""
    sep = "&" if "?" in hook_url else "?"
    return f"{hook_url}{sep}imgURL={urllib.parse.quote(image, safe='')}"


def trigger_deploy(hook_url: str, image: str, http: httpx.Client) -> None:
    resp = http.post(deploy_url(hook_url, image), timeout=30.0)
    resp.raise_for_status()


def rollback(
    gh: GitHub,
    *,
    hook_url: str | None,
    app_url: str | None,
    broken_sha: str | None = None,
    http: httpx.Client | None = None,
    smoke_fn=run_smoke,
    max_wait: float = MAX_WAIT,
    poll_every: float = POLL_EVERY,
    sleep=time.sleep,
    clock=time.monotonic,
    log=print,
) -> RollbackResult:
    """Redeploy the last stable image and wait until the smoke checks pass. Tried once only."""
    start = clock()

    def done(ok: bool, reason: str, **extra) -> RollbackResult:
        return RollbackResult(ok, reason, seconds=round(clock() - start, 1), **extra)

    if not hook_url:
        return done(False, "RENDER_DEPLOY_HOOK_URL is not set, so the agent cannot redeploy anything",
                    missing_secret="RENDER_DEPLOY_HOOK_URL")
    if not app_url:
        return done(False, "RENDER_APP_URL is not set, so the rollback cannot be verified",
                    missing_secret="RENDER_APP_URL")

    stable = find_last_stable_sha(gh, exclude={broken_sha} if broken_sha else None)
    if not stable:
        return done(False, "no stable version found (no CI/CD run on main with a successful verify job)")
    image = image_for(gh.repo, stable)

    log(f"rollback: deploying {image}")
    try:
        trigger_deploy(hook_url, image, http or httpx.Client())
    except httpx.HTTPError as exc:
        # Never echo the hook URL: it contains Render's secret key.
        return done(False, f"Render deploy hook call failed ({type(exc).__name__})", stable_sha=stable, image=image)

    report = None
    while True:
        sleep(poll_every)  # give Render time to swap the image before checking
        report = smoke_fn(app_url, retries=1, log=log)
        if report.healthy:
            return done(True, f"stable version {stable[:7]} is live and healthy",
                        stable_sha=stable, image=image, smoke=report)
        if clock() - start >= max_wait:
            return done(False, f"rollback to {stable[:7]} did not pass the smoke checks within {max_wait:.0f}s",
                        stable_sha=stable, image=image, smoke=report)
        log("rollback: not healthy yet, waiting")
