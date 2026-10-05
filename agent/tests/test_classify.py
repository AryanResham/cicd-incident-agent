import pytest

from agent import classify
from agent.classify import CORE, ISSUE_ONLY, RERUN, SIMPLE
from agent.collect import Evidence, FailedJob, TestFailure


def ev(trigger="ci_failure", log="", jobs=("test",), junit=(), smoke_failed=(), changed=(), diff=""):
    e = Evidence(trigger=trigger, changed_files=list(changed), diff=diff)
    e.failed_jobs = [FailedJob(j, "failure", [], log) for j in jobs]
    e.junit_failures = [TestFailure(n, "assert False", "") for n in junit]
    if smoke_failed is not None:
        checks = ["GET /health", "GET /", "GET /api/todos", "round trip", "latency"]
        e.smoke = {"checks": [{"name": c, "ok": c not in smoke_failed, "status_code": None, "detail": ""}
                              for c in checks]} if trigger != "ci_failure" else None
    return e


def diff_for(path, *lines):
    return f"--- {path} (modified)\n@@ -1,1 +1,1 @@\n" + "\n".join(lines)


# ---- category + kind for each scenario family ----------------------------------
@pytest.mark.parametrize("log, category, kind", [
    ("ModuleNotFoundError: No module named 'requests'", "dependency", SIMPLE),  # S1
    ("ERROR: No matching distribution found for fastapi==99", "dependency", SIMPLE),
    ("NameError: name 'datetime' is not defined", "code_error", SIMPLE),  # S2
    ('  File "app/main.py", line 4\n    def f(\nSyntaxError: invalid syntax', "code_error", SIMPLE),  # S3
    ("NameError: name 'todo_idd' is not defined", "code_error", SIMPLE),  # S4
    ("ModuleNotFoundError: No module named 'app.utils'", "code_error", SIMPLE),
    ("ImportError: cannot import name 'today' from 'app.clock'", "code_error", SIMPLE),
    ("FAILED tests/test_todos.py::test_mark_done - AssertionError", "test_failure", CORE),  # C1-C8
    ("toomanyrequests: rate limit exceeded pulling python:3.12-slim", "flaky", RERUN),
    ("Error: Input required and not supplied: token", "config_secret", ISSUE_ONLY),
])
def test_ci_failures(log, category, kind):
    result = classify.classify(ev(log=log))
    assert (result.category, result.kind) == (category, kind)
    assert result.signals


def test_docker_build_failure():  # S5
    log = 'ERROR: failed to solve: failed to compute cache key: "/src/app": not found'
    result = classify.classify(ev(log=log, jobs=("build",)))
    assert (result.category, result.kind) == ("docker_build", SIMPLE)


def test_build_job_failure_without_known_message_is_docker_build():
    assert classify.classify(ev(log="exit code 1", jobs=("build",))).category == "docker_build"


def test_junit_failures_are_core_even_without_log_text():
    result = classify.classify(ev(log="", junit=["tests.test_status::test_overdue"]))
    assert result.kind == CORE and result.signals == ["tests.test_status::test_overdue"]


def test_unknown_ci_failure_defaults_to_core():
    result = classify.classify(ev(log="something weird happened", jobs=("lint",)))
    assert result.kind == CORE and "doubt" in result.reason


def test_missing_secret_wins_over_everything():  # N1
    log = "RENDER_DEPLOY_HOOK_URL is not set\nNameError: name 'x' is not defined"
    result = classify.classify(ev(trigger="deploy_failure", log=log, jobs=("deploy",)))
    assert (result.category, result.kind) == ("config_secret", ISSUE_ONLY)


def test_flaky_patterns_only_count_for_ci():
    result = classify.classify(ev(trigger="deploy_failure", log="Connection reset by peer", jobs=("verify",),
                                  smoke_failed=("GET /health",), changed=["app/status.py"],
                                  diff=diff_for("app/status.py", "+    return 'done'")))
    assert result.kind == CORE


# ---- production failures --------------------------------------------------------
ALL_DOWN = ("GET /health", "GET /", "GET /api/todos", "round trip")


def test_wrong_port_in_dockerfile_is_simple():  # S6
    e = ev(trigger="deploy_failure", jobs=("verify",), smoke_failed=ALL_DOWN, changed=["Dockerfile"],
           diff=diff_for("Dockerfile", '-CMD uvicorn app.main:app --port $PORT', '+CMD uvicorn app.main:app --port 9999'))
    assert (classify.classify(e).category, classify.classify(e).kind) == ("deploy_failure", SIMPLE)


def test_env_var_without_default_is_simple():  # S7
    e = ev(trigger="deploy_failure", jobs=("verify",), smoke_failed=ALL_DOWN, changed=["app/db.py"],
           diff=diff_for("app/db.py", '-DATABASE_PATH = os.getenv("DATABASE_PATH", "./todos.db")',
                         '+DATABASE_PATH = os.environ["DATABASE_PATH"]'))
    assert classify.classify(e).kind == SIMPLE


def test_tzdata_removed_is_simple():  # S8
    e = ev(trigger="deploy_failure", jobs=("verify",), smoke_failed=ALL_DOWN, changed=["requirements.txt"],
           diff=diff_for("requirements.txt", "-tzdata==2026.5"))
    assert classify.classify(e).kind == SIMPLE


def test_page_missing_from_image_is_simple():  # S9
    e = ev(trigger="deploy_failure", jobs=("verify",), smoke_failed=("GET /",), changed=[".dockerignore"],
           diff=diff_for(".dockerignore", "+app/static"))
    assert classify.classify(e).kind == SIMPLE


def test_api_logic_broken_in_production_is_core():
    e = ev(trigger="deploy_failure", jobs=("verify",), smoke_failed=("round trip",), changed=["app/main.py"],
           diff=diff_for("app/main.py", "-    todo.done = body.done", "+    todo.done = False"))
    assert classify.classify(e).kind == CORE


def test_unhealthy_after_app_logic_change_is_core():
    e = ev(trigger="deploy_failure", jobs=("verify",), smoke_failed=ALL_DOWN, changed=["app/main.py"],
           diff=diff_for("app/main.py", "+    raise RuntimeError()"))
    assert classify.classify(e).kind == CORE


def test_deploy_job_failure_is_issue_only():
    e = ev(trigger="deploy_failure", log="curl: (22) The requested URL returned error: 404", jobs=("deploy",))
    assert classify.classify(e).kind == ISSUE_ONLY


def test_health_failure_without_code_change_is_issue_only():  # N2
    e = ev(trigger="health", jobs=(), smoke_failed=ALL_DOWN)
    result = classify.classify(e)
    assert (result.category, result.kind) == ("deploy_failure", ISSUE_ONLY)


def test_latency_only_is_issue_only():
    e = ev(trigger="deploy_failure", jobs=("verify",), smoke_failed=("latency",), changed=["app/main.py"],
           diff=diff_for("app/main.py", "+x = 1"))
    assert classify.classify(e).kind == ISSUE_ONLY


def test_combine_only_makes_things_safer():
    simple = classify.Classification("dependency", SIMPLE, "")
    core = classify.Classification("test_failure", CORE, "")
    assert classify.combine(simple, CORE) == CORE
    assert classify.combine(core, SIMPLE) == CORE
    assert classify.combine(simple, SIMPLE) == SIMPLE
    assert classify.combine(simple, None) == SIMPLE


# ---- patch-based upgrade --------------------------------------------------------
MAIN = '''import os
from fastapi import FastAPI

app = FastAPI()
LIMIT = 200


@app.post("/api/todos", status_code=201)
def create(title: str):
    clean = title.strip()
    return {"title": clean}


@app.patch("/api/todos/{todo_id}")
def update(todo_id: int, done: bool):
    item = {"id": todo_id}
    item["done"] = done
    return item
'''


def kind_of(old, new, path="app/main.py"):
    return classify.patch_kind({path: (old, new)})


def test_requirements_and_dockerfile_fixes_stay_simple():
    assert kind_of("fastapi\n", "fastapi\nrequests\n", "requirements.txt")[0] == SIMPLE
    assert kind_of("CMD x --port 1\n", "CMD x --port $PORT\n", "Dockerfile")[0] == SIMPLE


def test_missing_import_fix_is_simple():
    old = MAIN.replace("import os\n", "")
    assert kind_of(old, MAIN)[0] == SIMPLE


def test_typo_name_fix_inside_function_is_simple():
    old = MAIN.replace('return {"title": clean}', 'return {"title": claen}')
    assert kind_of(old, MAIN)[0] == SIMPLE


def test_syntax_error_fix_is_simple():
    old = MAIN.replace("def create(title: str):", "def create(title: str)")
    assert kind_of(old, MAIN)[0] == SIMPLE


def test_constant_and_env_default_fixes_are_simple():
    assert kind_of(MAIN.replace("LIMIT = 200", "LIMIT = 20"), MAIN)[0] == SIMPLE
    old = MAIN.replace("LIMIT = 200", 'DB = os.environ["DATABASE_PATH"]')
    new = MAIN.replace("LIMIT = 200", 'DB = os.getenv("DATABASE_PATH", "./todos.db")')
    assert kind_of(old, new)[0] == SIMPLE


def test_logic_change_is_core():
    new = MAIN.replace('item["done"] = done', 'item["done"] = not done')
    kind, reason = kind_of(MAIN, new)
    assert kind == CORE and "update()" in reason


def test_status_code_or_route_change_is_core():
    assert kind_of(MAIN, MAIN.replace("status_code=201", "status_code=200"))[0] == CORE
    assert kind_of(MAIN, MAIN.replace('"/api/todos"', '"/api/todo"'))[0] == CORE


def test_adding_a_function_is_core():
    assert kind_of(MAIN, MAIN + "\n\ndef helper():\n    return 1\n")[0] == CORE


def test_big_fix_is_core():
    new = MAIN + "".join(f"\nX{i} = {i}" for i in range(31))
    kind, reason = kind_of(MAIN, new)
    assert kind == CORE and "lines" in reason


def test_module_level_logic_is_core():
    assert kind_of(MAIN, MAIN.replace("app = FastAPI()", "app = FastAPI(debug=True)"))[0] == CORE


def test_fix_that_still_has_syntax_error_is_core():
    assert kind_of(MAIN, MAIN.replace("def update(", "def update"))[0] == CORE


def test_renames_in_several_functions_is_core():
    funcs = "".join(f"\ndef f{i}():\n    return value{i}\n" for i in range(3))
    fixed = "".join(f"\ndef f{i}():\n    return valu{i}\n" for i in range(3))
    kind, reason = kind_of(funcs, fixed)
    assert kind == CORE and "several functions" in reason
