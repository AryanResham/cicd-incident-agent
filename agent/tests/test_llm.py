import json

import httpx
import pytest
from google.genai import errors as genai_errors

from agent import llm
from agent.classify import Classification

GOOD_DIAGNOSIS = {"category": "dependency", "kind": "simple", "root_cause": "requests missing from requirements.txt",
                  "files": ["requirements.txt"], "confidence": 0.9}
GOOD_FIX = {"explanation": "add requests", "confidence": 0.95,
            "edits": [{"file": "./requirements.txt", "search": "fastapi\n", "replace": "fastapi\nrequests\n"}]}


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModels:
    def __init__(self, answers):
        self.answers = list(answers)  # each: dict (JSON answer), str (raw text) or Exception
        self.requests = []

    def generate_content(self, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(answer if isinstance(answer, str) else json.dumps(answer))


class FakeSDK:
    def __init__(self, *answers):
        self.models = FakeModels(answers)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def client(*answers, rpm=1000, max_wait=120, fallback_models=()):
    clock = Clock()
    c = llm.GeminiClient(client=FakeSDK(*answers), model="test-flash", rpm=rpm, max_wait=max_wait,
                         sleep=clock.sleep, clock=clock, log=lambda *_: None, fallback_models=fallback_models)
    return c, clock


def rate_limited(delay="7s", quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier"):
    return genai_errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota exceeded",
        "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                     "violations": [{"quotaId": quota_id}]},
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})


RULE = Classification("dependency", "simple", "missing module")


def test_diagnose_uses_schema_and_parses():
    c, _ = client(GOOD_DIAGNOSIS)
    d = c.diagnose("evidence text", RULE)
    assert d == llm.Diagnosis("dependency", "simple", "requests missing from requirements.txt",
                              ["requirements.txt"], 0.9)
    req = c._client.models.requests[0]
    assert req["model"] == "test-flash"
    assert req["config"].response_mime_type == "application/json"
    assert req["config"].response_json_schema == llm.DIAGNOSIS_SCHEMA
    assert "evidence text" in req["contents"] and "kind=simple" in req["contents"]
    assert c.calls == 1


def test_propose_fix_includes_files_and_previous_errors():
    c, _ = client(GOOD_FIX)
    fix = c.propose_fix("ev", None, {"requirements.txt": "fastapi\n"}, ["pytest: 1 failed"])
    assert fix.edits == [llm.Edit("requirements.txt", "fastapi\n", "fastapi\nrequests\n")]
    prompt = c._client.models.requests[0]["contents"]
    assert "### requirements.txt" in prompt and "Previous attempt 1 FAILED" in prompt and "NEVER edit tests/" in prompt


def test_429_honours_retry_delay_then_succeeds():
    c, clock = client(rate_limited("7s"), GOOD_DIAGNOSIS)
    assert c.diagnose("e", RULE).kind == "simple"
    assert clock.sleeps == [7.0]
    assert c.calls == 2


def test_server_errors_use_exponential_backoff():
    err = genai_errors.ServerError(503, {"error": {"code": 503, "message": "overloaded"}})
    c, clock = client(err, err, httpx.ConnectError("down"), GOOD_DIAGNOSIS)
    c.diagnose("e", RULE)
    assert clock.sleeps == [4.0, 8.0, 16.0]


def test_overloaded_model_falls_back_to_the_next_one():
    err = genai_errors.ServerError(503, {"error": {"code": 503, "message": "overloaded"}})
    c, clock = client(err, GOOD_DIAGNOSIS, fallback_models=["test-lite"])
    assert c.diagnose("e", RULE).kind == "simple"
    assert [r["model"] for r in c._client.models.requests] == ["test-flash", "test-lite"]
    assert all(s < 1 for s in clock.sleeps)  # switched straight away: only the throttle spacing, no backoff


def test_missing_model_and_daily_quota_fall_back():
    missing = genai_errors.ClientError(404, {"error": {"code": 404, "message": "model not found"}})
    c, _ = client(missing, rate_limited(quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier"),
                  GOOD_DIAGNOSIS, fallback_models=["test-lite", "test-other"])
    c.diagnose("e", RULE)
    assert [r["model"] for r in c._client.models.requests] == ["test-flash", "test-lite", "test-other"]


def test_last_model_still_retries_with_backoff():
    err = genai_errors.ServerError(503, {"error": {"code": 503, "message": "overloaded"}})
    c, clock = client(err, err, GOOD_DIAGNOSIS, fallback_models=["test-lite"])
    c.diagnose("e", RULE)
    assert [r["model"] for r in c._client.models.requests] == ["test-flash", "test-lite", "test-lite"]
    assert [s for s in clock.sleeps if s >= 1] == [4.0]  # one backoff (ignoring throttle spacing)


def test_fallback_models_from_env():
    assert llm.GeminiClient.from_env({}).models == [llm.DEFAULT_MODEL, *llm.DEFAULT_FALLBACK_MODELS]
    env = {"AGENT_MODEL": "a", "AGENT_FALLBACK_MODELS": "b, a ,c"}
    assert llm.GeminiClient.from_env(env).models == ["a", "b", "c"]
    assert llm.GeminiClient.from_env({"AGENT_FALLBACK_MODELS": ""}).models == [llm.DEFAULT_MODEL]


def test_gives_up_after_about_two_minutes():
    c, clock = client(*[rate_limited("50s") for _ in range(5)])
    with pytest.raises(llm.LLMUnavailable, match="still failing"):
        c.diagnose("e", RULE)
    assert sum(clock.sleeps) <= 120


def test_daily_quota_fails_fast():
    c, clock = client(rate_limited("30s", quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
    with pytest.raises(llm.LLMUnavailable, match="daily quota"):
        c.diagnose("e", RULE)
    assert clock.sleeps == []


def test_non_retryable_error_and_missing_key():
    c, _ = client(genai_errors.ClientError(400, {"error": {"code": 400, "message": "API key not valid"}}))
    with pytest.raises(llm.LLMUnavailable, match="400"):
        c.diagnose("e", RULE)
    with pytest.raises(llm.LLMUnavailable, match="GEMINI_API_KEY"):
        llm.GeminiClient(api_key=None).diagnose("e", RULE)


def test_retry_delay_from_message():
    assert llm.retry_delay(RuntimeError("Please retry in 12.5s.")) == 12.5
    assert llm.retry_delay(RuntimeError("nothing")) is None


def test_throttle_spaces_calls():
    c, clock = client(GOOD_DIAGNOSIS, GOOD_DIAGNOSIS, rpm=6)  # one call per 10 s
    c.diagnose("e", RULE)
    clock.t += 3
    c.diagnose("e", RULE)
    assert clock.sleeps == [7.0]


def test_from_env():
    c = llm.GeminiClient.from_env({"GEMINI_API_KEY": "k", "AGENT_MODEL": "gemini-x", "AGENT_LLM_RPM": "2"})
    assert (c.api_key, c.model, c.throttle.interval) == ("k", "gemini-x", 30.0)
    assert llm.GeminiClient.from_env({}).model == llm.DEFAULT_MODEL


@pytest.mark.parametrize("bad", [
    "not json",
    {**GOOD_DIAGNOSIS, "category": "astrology"},
    {**GOOD_DIAGNOSIS, "kind": "maybe"},
    {**GOOD_DIAGNOSIS, "root_cause": " "},
    {**GOOD_DIAGNOSIS, "confidence": "high"},
    {**GOOD_DIAGNOSIS, "files": "app/main.py"},
])
def test_invalid_diagnosis_is_rejected(bad):
    c, _ = client(bad)
    with pytest.raises(llm.LLMBadResponse):
        c.diagnose("e", RULE)


@pytest.mark.parametrize("bad", [
    {"explanation": "x", "confidence": 1},
    {"explanation": "x", "confidence": 1, "edits": [{"file": "a.py", "search": "x"}]},
    {"explanation": "x", "confidence": 1, "edits": [{"file": "", "search": "a", "replace": "b"}]},
    {"explanation": "x", "confidence": 1, "edits": [{"file": "a.py", "search": "a", "replace": "a"}]},
])
def test_invalid_fix_is_rejected(bad):
    with pytest.raises(llm.LLMBadResponse):
        llm.parse_fix(bad)


def test_confidence_is_clamped():
    assert llm.parse_fix({"explanation": "", "confidence": 3, "edits": []}).confidence == 1.0
