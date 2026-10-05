import io
import json
import zipfile

from agent import collect
from agent.collect import Masker
from agent.tests.fakes import FakeGitHub

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="3" failures="1" errors="1">
  <testcase classname="tests.test_todos" name="test_create" time="0.01"/>
  <testcase classname="tests.test_todos" name="test_mark_done" time="0.01">
    <failure message="assert 'upcoming' == 'done'">tests/test_todos.py:42: AssertionError</failure>
  </testcase>
  <testcase classname="tests.test_seed" name="test_seed" time="0.01">
    <error message="fixture error">E   KeyError: 'DATABASE_PATH'</error>
  </testcase>
</testsuite></testsuites>"""


def zip_bytes(name: str, text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, text)
    return buf.getvalue()


# ---- masking ---------------------------------------------------------------
def test_masks_known_secret_values():
    m = Masker(["super-secret-value", "", "abc"])  # empty/short values are ignored
    assert m("token is super-secret-value!") == "token is ***!"
    assert m("abc stays") == "abc stays"


def test_masks_token_like_patterns():
    m = Masker()
    text = ("ghp_" + "a" * 36 + " github_pat_" + "B" * 30 + " AIza" + "x" * 35 +
            " Authorization: Bearer eyJhbGciOi.payload.sig"
            " https://api.render.com/deploy/srv-abc?key=Zz9xYy8w"
            " GEMINI_API_KEY=hunter2hunter2 password: 'letmein123'")
    masked = m(text)
    for secret in ("a" * 36, "B" * 30, "x" * 35, "eyJhbGciOi", "Zz9xYy8w", "hunter2hunter2", "letmein123"):
        assert secret not in masked
    assert "?key=***" in masked and "GEMINI_API_KEY=***" in masked and "Bearer ***" in masked


def test_from_env_and_non_aggressive_mode_keeps_code_intact():
    m = Masker.from_env({"GEMINI_API_KEY": "AIzaREALKEYVALUE123", "GITHUB_TOKEN": "tok-abcdef"})
    assert "AIzaREALKEYVALUE123" not in m("key AIzaREALKEYVALUE123")
    code = 'token = request.headers.get("x-token")'
    assert m(code, aggressive=False) == code


def test_masks_private_keys():
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    assert Masker()(f"x {key} y") == "x *** y"


# ---- logs + junit ------------------------------------------------------------
def test_clean_log_and_tail_around_first_error():
    raw = "\n".join(f"2026-10-05T10:00:{i:02d}.1234567Z line {i}" for i in range(50))
    raw += "\n2026-10-05T10:01:00.0000000Z ##[error]Process completed with exit code 1."
    raw += "\n" + "\n".join(f"post {i}" for i in range(40))
    lines = collect.clean_log(raw)
    assert lines[0] == "line 0"
    tail = collect.tail_failed_step(lines, n=30)
    assert len(tail) == 30
    assert "##[error]Process completed with exit code 1." in tail
    assert tail[-1] == "post 18"


def test_tail_without_error_marker_is_log_tail():
    assert collect.tail_failed_step([str(i) for i in range(10)], n=3) == ["7", "8", "9"]


def test_parse_junit_failures_and_errors():
    failures = collect.parse_junit(JUNIT)
    assert [f.name for f in failures] == ["tests.test_todos::test_mark_done", "tests.test_seed::test_seed"]
    assert failures[0].message == "assert 'upcoming' == 'done'"
    assert "KeyError" in failures[1].details
    assert collect.junit_from_zip(zip_bytes("junit.xml", JUNIT))[0].name.endswith("test_mark_done")


def test_paths_from_tracebacks():
    text = ('File "/home/runner/work/todo/todo/app/main.py", line 3, in <module>\n'
            "tests/test_todos.py:42: AssertionError\n COPY failed: see Dockerfile")
    assert collect.paths_from_text(text) == {"app/main.py", "tests/test_todos.py", "Dockerfile"}


# ---- collect ------------------------------------------------------------------
def test_collect_gathers_and_masks_everything(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("import requests\n")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "README.md").write_text("not evidence\n")

    gh = FakeGitHub()
    run = gh.add_run(7, "broken", conclusion="failure", jobs={"test": "failure", "build": "skipped"})
    gh.jobs[7][0]["steps"] = [{"name": "Checkout", "conclusion": "success"},
                              {"name": "Run tests", "conclusion": "failure"}]
    gh.logs[700] = ("2026-10-05T10:00:00.0Z ModuleNotFoundError: No module named 'requests'\n"
                    "  File \"/home/runner/work/t/t/app/main.py\", line 1\n"
                    "GEMINI_API_KEY=AIzaSyLEAKEDLEAKEDLEAKEDLEAKEDLEAKED12\n"
                    "##[error]Process completed with exit code 2.")
    gh.artifacts[7] = [{"id": 1, "name": "junit-results"}]
    gh.artifact_zips[1] = zip_bytes("junit.xml", JUNIT)
    gh.compares[("good", "broken")] = {"files": [
        {"filename": "app/main.py", "status": "modified", "patch": "+import requests"},
        {"filename": "README.md", "status": "modified", "patch": "+docs"}]}

    ev = collect.collect(gh, trigger="ci_failure", run=run, stable_sha="good", repo_root=tmp_path,
                         masker=Masker())
    assert ev.failed_job_names == ["test"]
    assert ev.failed_jobs[0].failed_steps == ["Run tests"]
    assert "LEAKED" not in ev.failed_jobs[0].log_tail
    assert len(ev.junit_failures) == 2
    assert ev.changed_files == ["app/main.py", "README.md"]
    assert "+import requests" in ev.diff
    assert set(ev.files) == {"app/main.py", "requirements.txt"}  # README isn't evidence
    prompt = ev.to_prompt()
    assert "ModuleNotFoundError" in prompt and "Diff stable..broken" in prompt
    assert "LEAKED" not in prompt


def test_collect_reads_the_verify_smoke_report(tmp_path):
    gh = FakeGitHub()
    run = gh.add_run(8, "broken", conclusion="failure", jobs={"verify": "failure"})
    gh.artifacts[8] = [{"id": 2, "name": "smoke-report"}]
    report = {"healthy": False, "checks": [
        {"name": "GET /", "ok": False, "status_code": 500, "detail": "token=abcdefSECRET123 boom"},
        {"name": "GET /health", "ok": True, "status_code": 200, "detail": ""}]}
    gh.artifact_zips[2] = zip_bytes("smoke.json", json.dumps(report))
    ev = collect.collect(gh, trigger="deploy_failure", run=run, repo_root=tmp_path, masker=Masker())
    assert [c["name"] for c in ev.smoke["checks"] if not c["ok"]] == ["GET /"]
    assert "SECRET" not in ev.failure_text() and "smoke GET / [500]" in ev.failure_text()


def test_collect_survives_api_errors(tmp_path):
    class Flaky(FakeGitHub):
        def list_jobs(self, run_id):
            raise RuntimeError("API down")

    gh = Flaky()
    run = gh.add_run(1, "x", conclusion="failure")
    ev = collect.collect(gh, trigger="deploy_failure", run=run, repo_root=tmp_path, masker=Masker(),
                         smoke={"checks": [{"name": "GET /health", "ok": False, "status_code": 502,
                                            "detail": "bad gateway"}]})
    assert any("could not read job logs" in n for n in ev.notes)
    assert "smoke GET /health [502]: bad gateway" in ev.failure_text()


def test_prompt_is_trimmed():
    ev = collect.Evidence(trigger="ci_failure", diff="x" * 50_000)
    assert len(ev.to_prompt(max_chars=2_000)) < 2_100
