"""Markdown incident report + the small state the agent keeps inside the incident issue.

The issue body is laid out as:
    <report>  <!-- evidence -->  <evidence>  <!-- agent-state: {...json...} -->
so a later run (e.g. after a fix was deployed) can read the state back and
re-render the report without any extra storage.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from agent.collect import Evidence

EVIDENCE_MARK = "<!-- evidence -->"
STATE_RE = re.compile(r"<!-- agent-state: (\{.*?\}) -->", re.S)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_state(body: str | None) -> dict:
    m = STATE_RE.search(body or "")
    return json.loads(m.group(1)) if m else {}


def add_event(state: dict, text: str, when: str | None = None) -> None:
    state.setdefault("timeline", []).append([when or now_iso(), text])


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {secs}s" if hours else f"{minutes}m {secs}s"


def mttr(state: dict) -> str | None:
    if state.get("detected_at") and state.get("resolved_at"):
        delta = parse_time(state["resolved_at"]) - parse_time(state["detected_at"])
        return _duration(max(0.0, delta.total_seconds()))
    return None


def render_report(state: dict) -> str:
    """The human-readable incident report (what failed, root cause, actions, timeline, MTTR)."""
    sha = (state.get("broken_sha") or "")[:7] or "n/a"
    stable = (state.get("stable_sha") or "")[:7] or "n/a"
    conf = state.get("confidence")
    lines = [
        f"## Incident report: {state.get('status', 'open')}",
        "",
        "| | |",
        "|---|---|",
        f"| Trigger | `{state.get('trigger', '?')}` ([failed run]({state['run_url']})) |" if state.get("run_url")
        else f"| Trigger | `{state.get('trigger', '?')}` |",
        f"| Broken commit | `{sha}` |",
        f"| Last stable commit | `{stable}` |",
        f"| Category | {state.get('category', '?')} |",
        f"| Kind | **{str(state.get('kind', '?')).upper()}** |",
        f"| Confidence | {conf:.2f} |" if isinstance(conf, (int, float)) else "| Confidence | n/a |",
        f"| Gemini calls | {state.get('llm_calls', 0)} |",
        f"| Time to restore (rollback) | {state['rollback_seconds']:.0f}s |" if state.get("rollback_seconds")
        else "| Time to restore (rollback) | no rollback needed/possible |",
        f"| MTTR | {mttr(state) or 'not resolved yet'} |",
        "",
        "### What failed",
        state.get("what_failed", "-"),
        "",
        "### Root cause",
        state.get("root_cause", "diagnosis pending"),
        "",
        "### Actions",
    ]
    lines += [f"- {a}" for a in state.get("actions", [])] or ["- none yet"]
    lines += ["", "### Timeline (UTC)"]
    lines += [f"- `{when[11:19]}` {text}" for when, text in state.get("timeline", [])]
    return "\n".join(lines)


def evidence_markdown(ev: Evidence) -> str:
    out = ["<details><summary>Evidence (secrets masked)</summary>", ""]
    for job in ev.failed_jobs:
        out += [f"**Failed job `{job.name}`** (steps: {', '.join(job.failed_steps) or '?'})",
                "```", "\n".join(job.log_tail.splitlines()[-80:]), "```"]
    if ev.junit_failures:
        out.append("**Failing tests**")
        out += [f"- `{t.name}`: {t.message[:200]}" for t in ev.junit_failures[:15]]
    if ev.smoke:
        out.append("**Smoke checks**")
        out += [f"- {'PASS' if c.get('ok') else 'FAIL'} {c['name']} [{c.get('status_code')}] {c.get('detail', '')}"
                for c in ev.smoke.get("checks", [])]
    if ev.diff:
        out += ["**Diff stable..broken**", "```diff", ev.diff[:5000], "```"]
    out += [f"- note: {n}" for n in ev.notes]
    out += ["", "</details>"]
    return "\n".join(out)


def build_body(state: dict, evidence_md: str = "") -> str:
    state_json = json.dumps(state, separators=(",", ":")).replace("-->", "--\\u003e")
    return f"{render_report(state)}\n\n{EVIDENCE_MARK}\n{evidence_md}\n\n<!-- agent-state: {state_json} -->"


def rebuild_body(old_body: str, state: dict) -> str:
    """Re-render the report part, keep the evidence part of an existing issue body."""
    evidence = ""
    if EVIDENCE_MARK in (old_body or ""):
        evidence = STATE_RE.sub("", old_body.split(EVIDENCE_MARK, 1)[1]).strip()
    return build_body(state, evidence)
