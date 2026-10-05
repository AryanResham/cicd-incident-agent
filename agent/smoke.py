"""Smoke checks against a running copy of the to-do app (PLAN §B2).

Used by the `verify` job, the health monitor, after every rollback and by the
fix loop's in-runner pre-checks.

    python -m agent.smoke --url https://my-app.onrender.com [--json out.json]

Exit code 0 = healthy, 1 = unhealthy.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field

import httpx

WARMUP_TIMEOUT = 90.0  # Render's free tier can take ~1 min to wake up
REQUEST_TIMEOUT = 15.0
LATENCY_LIMIT = 3.0  # seconds per request, after warm-up
RETRIES = 3  # extra rounds after the first one
BACKOFF = 5.0  # seconds; doubles every retry (5, 10, 20)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    status_code: int | None = None
    elapsed: float = 0.0


@dataclass
class SmokeReport:
    base_url: str
    healthy: bool
    rounds: int
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.ok]

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        lines = [f"Smoke checks for {self.base_url}: {'HEALTHY' if self.healthy else 'UNHEALTHY'}"
                 f" (after {self.rounds} round(s))"]
        for c in self.checks:
            code = f" [{c.status_code}]" if c.status_code is not None else ""
            lines.append(f"  {'PASS' if c.ok else 'FAIL'}  {c.name}{code} {c.elapsed:.2f}s  {c.detail}".rstrip())
        return "\n".join(lines)


def _short(text: str, limit: int = 300) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "..."


class _Round:
    """One full pass over every check. Records a CheckResult per check."""

    def __init__(self, client: httpx.Client, latency_limit: float):
        self.client = client
        self.latency_limit = latency_limit
        self.results: list[CheckResult] = []
        self.slowest: tuple[str, float] = ("", 0.0)

    def request(self, method: str, path: str, **kwargs) -> tuple[httpx.Response | None, float, str]:
        start = time.monotonic()
        try:
            resp = self.client.request(method, path, **kwargs)
            error = ""
        except httpx.HTTPError as exc:
            resp, error = None, f"{type(exc).__name__}: {exc}"
        elapsed = time.monotonic() - start
        if elapsed > self.slowest[1]:
            self.slowest = (f"{method} {path}", elapsed)
        return resp, elapsed, error

    def add(self, name: str, ok: bool, detail: str, resp: httpx.Response | None, elapsed: float) -> bool:
        self.results.append(CheckResult(name, ok, detail, resp.status_code if resp is not None else None,
                                        round(elapsed, 3)))
        return ok

    def check_health(self) -> None:
        resp, elapsed, err = self.request("GET", "/health")
        if resp is None:
            self.add("GET /health", False, err, None, elapsed)
            return
        try:
            body = resp.json()
            body_ok = isinstance(body, dict) and body.get("status") == "ok"  # extra fields (version) are fine
        except ValueError:
            body_ok = False
        ok = resp.status_code == 200 and body_ok
        self.add("GET /health", ok, "" if ok else f"expected 200 with status 'ok', got: {_short(resp.text)}",
                 resp, elapsed)

    def check_page(self) -> None:
        resp, elapsed, err = self.request("GET", "/")
        if resp is None:
            self.add("GET /", False, err, None, elapsed)
            return
        is_html = "text/html" in resp.headers.get("content-type", "")
        ok = resp.status_code == 200 and is_html
        self.add("GET /", ok, "" if ok else f"expected 200 text/html, got: {_short(resp.text)}", resp, elapsed)

    def check_list(self) -> None:
        resp, elapsed, err = self.request("GET", "/api/todos")
        if resp is None:
            self.add("GET /api/todos", False, err, None, elapsed)
            return
        try:
            is_list = isinstance(resp.json(), list)
        except ValueError:
            is_list = False
        ok = resp.status_code == 200 and is_list
        self.add("GET /api/todos", ok, "" if ok else f"expected 200 JSON list, got: {_short(resp.text)}",
                 resp, elapsed)

    def check_round_trip(self) -> None:
        """POST a todo with a deadline -> PATCH done -> GET shows done -> DELETE."""
        name = "round trip"
        title = f"smoke-check {uuid.uuid4().hex[:8]}"
        due = (dt.date.today() + dt.timedelta(days=3)).isoformat()
        resp, elapsed, err = self.request("POST", "/api/todos", json={"title": title, "due_date": due})
        if resp is None or resp.status_code != 201:
            detail = err or f"POST /api/todos expected 201, got {resp.status_code}: {_short(resp.text)}"
            self.add(name, False, detail, resp, elapsed)
            return
        try:
            todo_id = resp.json()["id"]
        except (ValueError, KeyError, TypeError):
            self.add(name, False, f"POST /api/todos returned no id: {_short(resp.text)}", resp, elapsed)
            return

        total = elapsed
        try:
            resp, elapsed, err = self.request("PATCH", f"/api/todos/{todo_id}", json={"done": True})
            total += elapsed
            if resp is None or resp.status_code != 200:
                detail = err or f"PATCH expected 200, got {resp.status_code}: {_short(resp.text)}"
                self.add(name, False, detail, resp, total)
                return
            resp, elapsed, err = self.request("GET", f"/api/todos/{todo_id}")
            total += elapsed
            status = None
            if resp is not None and resp.status_code == 200:
                try:
                    status = resp.json().get("status")
                except (ValueError, AttributeError):
                    status = None
            if status != "done":
                detail = err or (f"GET after PATCH expected status 'done', got "
                                 f"{resp.status_code}: {_short(resp.text)}")
                self.add(name, False, detail, resp, total)
                return
        finally:
            # Always clean up the test todo, even if a step above failed.
            del_resp, elapsed, err = self.request("DELETE", f"/api/todos/{todo_id}")
            total += elapsed
        if del_resp is None or del_resp.status_code != 204:
            detail = err or f"DELETE expected 204, got {del_resp.status_code}: {_short(del_resp.text)}"
            self.add(name, False, detail, del_resp, total)
            return
        self.add(name, True, "POST 201 -> PATCH done -> GET status=done -> DELETE 204", del_resp, total)

    def check_latency(self) -> None:
        what, slowest = self.slowest
        ok = slowest <= self.latency_limit
        detail = f"slowest: {what} {slowest:.2f}s (limit {self.latency_limit:.1f}s)"
        self.results.append(CheckResult("latency", ok, detail, None, round(slowest, 3)))

    def run(self) -> list[CheckResult]:
        self.check_health()
        self.check_page()
        self.check_list()
        self.check_round_trip()
        self.check_latency()
        return self.results


def run_smoke(
    base_url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    warmup_timeout: float = WARMUP_TIMEOUT,
    retries: int = RETRIES,
    backoff: float = BACKOFF,
    latency_limit: float = LATENCY_LIMIT,
    sleep=time.sleep,
    log=print,
) -> SmokeReport:
    """Warm up, then run every check; retry the whole round with backoff before declaring 'down'."""
    base_url = base_url.rstrip("/")
    with httpx.Client(base_url=base_url, timeout=REQUEST_TIMEOUT, transport=transport,
                      follow_redirects=True) as client:
        # Warm-up: wakes a sleeping Render instance. Its result does not count.
        start = time.monotonic()
        try:
            client.get("/health", timeout=warmup_timeout)
            log(f"warm-up: /health answered after {time.monotonic() - start:.1f}s")
        except httpx.HTTPError as exc:
            log(f"warm-up: no answer after {time.monotonic() - start:.1f}s ({type(exc).__name__})")

        results: list[CheckResult] = []
        rounds = 0
        for attempt in range(retries + 1):
            rounds += 1
            results = _Round(client, latency_limit).run()
            if all(r.ok for r in results):
                return SmokeReport(base_url, True, rounds, results)
            failed = ", ".join(r.name for r in results if not r.ok)
            if attempt < retries:
                wait = backoff * (2 ** attempt)
                log(f"round {rounds} failed ({failed}); retrying in {wait:.0f}s")
                sleep(wait)
        return SmokeReport(base_url, False, rounds, results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-check a deployed to-do app.")
    parser.add_argument("--url", required=True, help="base URL of the app, e.g. https://x.onrender.com")
    parser.add_argument("--json", dest="json_path", help="also write the report as JSON to this file")
    parser.add_argument("--retries", type=int, default=RETRIES)
    parser.add_argument("--warmup-timeout", type=float, default=WARMUP_TIMEOUT)
    args = parser.parse_args(argv)

    report = run_smoke(args.url, retries=args.retries, warmup_timeout=args.warmup_timeout)
    print(report.summary())
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
    return 0 if report.healthy else 1


if __name__ == "__main__":
    sys.exit(main())
