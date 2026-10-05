"""Gemini client (PLAN §B5): structured JSON, client-side throttle, 429 handling.

Two calls exist: `diagnose()` (once per incident) and `propose_fix()` (once per
fix attempt, max 3). Output is forced to JSON with a response schema and then
validated again in code. When the API is rate-limited for too long, the key is
missing or the daily quota is gone, `LLMUnavailable` is raised so the caller can
degrade to "diagnosis pending" (the rollback never needs the LLM).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

import httpx
from google.genai import errors as genai_errors

from agent.classify import CATEGORIES, CORE, ISSUE_ONLY, SIMPLE, Classification

DEFAULT_MODEL = "gemini-3.8-flash"  # override with the AGENT_MODEL variable
DEFAULT_RPM = 5  # conservative: free-tier Flash limits have been as low as 5 requests/minute
MAX_WAIT = 120.0  # total seconds we are willing to wait on 429/5xx per call
RETRYABLE = (429, 500, 502, 503, 504)


class LLMUnavailable(RuntimeError):
    """The LLM can't be used right now (no key, quota exhausted, outage)."""


class LLMBadResponse(ValueError):
    """The LLM answered, but not with valid data for our schema."""


# ---- schemas + validated results ----------------------------------------------
DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "kind": {"type": "string", "enum": [SIMPLE, CORE, ISSUE_ONLY]},
        "root_cause": {"type": "string", "description": "one or two sentences naming the file and the cause"},
        "files": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["category", "kind", "root_cause", "files", "confidence"],
}

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "search": {"type": "string", "description": "exact text copied from the current file"},
                    "replace": {"type": "string"},
                },
                "required": ["file", "search", "replace"],
            },
        },
    },
    "required": ["explanation", "confidence", "edits"],
}


@dataclass
class Diagnosis:
    category: str
    kind: str
    root_cause: str
    files: list[str]
    confidence: float


@dataclass
class Edit:
    file: str
    search: str
    replace: str


@dataclass
class FixProposal:
    explanation: str
    confidence: float
    edits: list[Edit] = field(default_factory=list)


def _confidence(value) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise LLMBadResponse(f"confidence must be a number, got {value!r}")
    return max(0.0, min(1.0, float(value)))


def parse_diagnosis(data: dict) -> Diagnosis:
    if not isinstance(data, dict):
        raise LLMBadResponse("diagnosis is not a JSON object")
    if data.get("category") not in CATEGORIES:
        raise LLMBadResponse(f"unknown category {data.get('category')!r}")
    if data.get("kind") not in (SIMPLE, CORE, ISSUE_ONLY):
        raise LLMBadResponse(f"unknown kind {data.get('kind')!r}")
    if not isinstance(data.get("root_cause"), str) or not data["root_cause"].strip():
        raise LLMBadResponse("root_cause is empty")
    files = data.get("files", [])
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise LLMBadResponse("files must be a list of strings")
    return Diagnosis(data["category"], data["kind"], data["root_cause"].strip(), files,
                     _confidence(data.get("confidence")))


def parse_fix(data: dict) -> FixProposal:
    if not isinstance(data, dict) or not isinstance(data.get("edits"), list):
        raise LLMBadResponse("fix must be an object with an 'edits' list")
    edits = []
    for e in data["edits"]:
        if not isinstance(e, dict) or not all(isinstance(e.get(k), str) for k in ("file", "search", "replace")):
            raise LLMBadResponse(f"bad edit {e!r}: needs string file/search/replace")
        if not e["file"].strip():
            raise LLMBadResponse("edit with empty file name")
        if e["search"] == e["replace"]:
            raise LLMBadResponse(f"edit for {e['file']} changes nothing")
        path = e["file"].strip().replace("\\", "/")
        edits.append(Edit(path[2:] if path.startswith("./") else path, e["search"], e["replace"]))
    return FixProposal(str(data.get("explanation", "")).strip(), _confidence(data.get("confidence")), edits)


# ---- rate limiting ---------------------------------------------------------------
class Throttle:
    """Keeps at least 60/rpm seconds between calls (client side)."""

    def __init__(self, rpm: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self.clock, self.sleep = clock, sleep
        self.last: float | None = None

    def wait(self) -> None:
        if self.last is not None:
            remaining = self.interval - (self.clock() - self.last)
            if remaining > 0:
                self.sleep(remaining)
        self.last = self.clock()


def retry_delay(exc: Exception) -> float | None:
    """The delay Gemini asks for: RetryInfo.retryDelay ("27s") or "retry in 27.3s" in the message."""
    details = getattr(exc, "details", None) or {}
    error = details.get("error", details) if isinstance(details, dict) else {}
    for item in error.get("details", []) if isinstance(error, dict) else []:
        delay = item.get("retryDelay") if isinstance(item, dict) else None
        if isinstance(delay, str) and delay.endswith("s"):
            try:
                return float(delay[:-1])
            except ValueError:
                pass
    m = re.search(r"retry in ([\d.]+)\s*s", str(exc), re.I)
    return float(m.group(1)) if m else None


def is_daily_quota(exc: Exception) -> bool:
    """A per-day quota won't come back within minutes: don't wait for it."""
    return "PerDay" in str(getattr(exc, "details", "")) or "per day" in str(exc).lower()


# ---- prompts -------------------------------------------------------------------
SYSTEM = """You are the incident-response agent of a small FastAPI to-do app (Python 3.12, SQLite,
Docker, deployed on Render). You get CI/CD failure evidence and answer ONLY with JSON for the schema.
Kinds: "simple" = missing/wrong dependency, Dockerfile mistake, config/env default, syntax error,
missing import, obvious typo/NameError. "core" = anything that changes what the app does (endpoint
logic, routing, DB logic, validation/business rules) or a large fix. "issue_only" = no code change can
fix it (missing secrets, provider outage, quota/billing). When in doubt choose "core"."""

FIX_RULES = """Rules for the fix:
- Make the smallest change that fixes the root cause. Do not refactor or reformat.
- Only edit files under app/, requirements*.txt, Dockerfile, .dockerignore. NEVER edit tests/ or .github/;
  if the only possible fix is in a test, return an empty edits list and explain why.
- Each edit: `search` must be copied EXACTLY (including indentation) from the current file and be unique in it;
  `replace` is the new text. To create a new file, use an empty `search`.
- confidence = your probability (0..1) that the fix makes every test and smoke check pass."""


class GeminiClient:
    def __init__(self, api_key: str | None = None, model: str | None = None, rpm: float = DEFAULT_RPM,
                 max_wait: float = MAX_WAIT, client=None, sleep=time.sleep, clock=time.monotonic, log=print):
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.max_wait = max_wait
        self._client = client
        self.sleep, self.clock, self.log = sleep, clock, log
        self.throttle = Throttle(rpm, clock, sleep)
        self.calls = 0  # real API requests made (for the report / eval)

    @classmethod
    def from_env(cls, env=os.environ, **kwargs) -> "GeminiClient":
        return cls(api_key=env.get("GEMINI_API_KEY"), model=env.get("AGENT_MODEL") or None,
                   rpm=float(env.get("AGENT_LLM_RPM") or DEFAULT_RPM), **kwargs)

    def _sdk(self):
        if self._client is None:
            if not self.api_key:
                raise LLMUnavailable("GEMINI_API_KEY is not set")
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def generate_json(self, prompt: str, schema: dict) -> dict:
        """One structured call with throttle + retries. Raises LLMUnavailable / LLMBadResponse."""
        from google.genai import types

        client = self._sdk()
        config = types.GenerateContentConfig(system_instruction=SYSTEM, response_mime_type="application/json",
                                             response_json_schema=schema)
        waited, attempt = 0.0, 0
        while True:
            self.throttle.wait()
            try:
                self.calls += 1
                response = client.models.generate_content(model=self.model, contents=prompt, config=config)
                break
            except genai_errors.APIError as exc:
                if exc.code not in RETRYABLE:
                    raise LLMUnavailable(f"Gemini error {exc.code}: {exc.message or exc}") from exc
                if exc.code == 429 and is_daily_quota(exc):
                    raise LLMUnavailable("Gemini daily quota exhausted") from exc
                delay = retry_delay(exc) or min(60.0, 4.0 * 2 ** attempt)
                reason = f"Gemini {exc.code}"
            except httpx.HTTPError as exc:
                delay, reason = min(60.0, 4.0 * 2 ** attempt), f"network error {type(exc).__name__}"
            if waited + delay > self.max_wait:
                raise LLMUnavailable(f"{reason}: still failing after waiting {waited:.0f}s")
            self.log(f"llm: {reason}, retrying in {delay:.0f}s")
            self.sleep(delay)
            waited += delay
            attempt += 1

        text = getattr(response, "text", None) or ""
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMBadResponse(f"response is not JSON: {text[:200]!r}") from exc

    def diagnose(self, evidence: str, rule: Classification) -> Diagnosis:
        prompt = (f"Rule-based pre-classification: category={rule.category}, kind={rule.kind}, "
                  f"reason={rule.reason}.\nDiagnose the root cause.\n\n{evidence}")
        return parse_diagnosis(self.generate_json(prompt, DIAGNOSIS_SCHEMA))

    def propose_fix(self, evidence: str, diagnosis: Diagnosis | None, files: dict[str, str],
                    previous_errors: list[str] | None = None) -> FixProposal:
        parts = [FIX_RULES, f"\nDiagnosis: {diagnosis.root_cause if diagnosis else 'not available'}",
                 f"\n{evidence}", "\n## Current file contents"]
        parts += [f"\n### {path}\n```\n{content}\n```" for path, content in files.items()]
        for i, err in enumerate(previous_errors or [], 1):
            parts.append(f"\n## Previous attempt {i} FAILED, do not repeat it\n```\n{err[-3000:]}\n```")
        return parse_fix(self.generate_json("\n".join(parts), FIX_SCHEMA))
