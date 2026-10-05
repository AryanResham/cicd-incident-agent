import json

import pytest

from agent import eval as ev_mod
from agent import fix
from agent.llm import Diagnosis, Edit, FixProposal

# A plausible version of the app (docs/API.md), used to check that every scenario's bug can be planted.
SAMPLE = {
    "app/main.py": '''from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field

app = FastAPI(title="To-Do")


def check_title(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("Title can't be empty")
    return value


class TodoCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)


@app.get("/api/todos")
def list_todos():
    return []


@app.post("/api/todos", status_code=201)
def create_todo(body: TodoCreate):
    return body


@app.patch("/api/todos/{todo_id}")
def update_todo(todo_id: int, body: TodoCreate):
    fields = body.model_dump(exclude_unset=True)
    return fields
''',
    "app/status.py": '''import datetime as dt


def compute_status(done: bool, due_date, today: dt.date) -> str:
    if done:
        return "done"
    if due_date is None:
        return "no_deadline"
    if due_date < today:
        return "overdue"
    return "upcoming"
''',
    "app/db.py": '''import os
import sqlite3

DATABASE_PATH = os.getenv("DATABASE_PATH", "./todos.db")


def update(conn, todo_id, fields):
    if "done" in fields:
        fields["done"] = int(fields["done"])
    conn.execute("UPDATE todos SET done = ? WHERE id = ?", (fields["done"], todo_id))


def delete(conn, todo_id):
    conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
''',
    "requirements.txt": "fastapi==0.142.2\nuvicorn==0.54.0\ntzdata==2026.5\n",
    "Dockerfile": 'FROM python:3.12-slim\nWORKDIR /srv\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n'
                  'COPY app ./app\nCMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}\n'
                  'HEALTHCHECK CMD ["python", "-c", "print(${PORT})"]\n',
    "tests/conftest.py": "import pytest\n",
}


@pytest.fixture
def app_repo(tmp_path):
    for rel, text in SAMPLE.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


def test_every_scenario_is_well_formed():
    scenarios = ev_mod.load_scenarios()
    ids = {s["id"] for s in scenarios}
    assert ids == {f"S{i}" for i in range(1, 10)} | {f"C{i}" for i in range(1, 9)} | {"N1", "N2", "E1", "E2", "E3"}
    for s in scenarios:
        assert {"id", "group", "title", "caught_by", "expected", "breaks", "simulated"} <= set(s), s["id"]
        assert s["breaks"] or s.get("manual"), s["id"]


def test_dry_run_classifies_every_scenario_as_expected():
    rows = [ev_mod.dry_run(s) for s in ev_mod.load_scenarios()]
    wrong = [(r["id"], r["got"], r["category"]) for r in rows if not r["correct"]]
    assert wrong == []


@pytest.mark.parametrize("scenario", [s for s in ev_mod.load_scenarios() if s["breaks"]], ids=lambda s: s["id"])
def test_every_bug_can_be_planted_in_a_typical_app(app_repo, scenario):
    changes = ev_mod.apply_breaks(app_repo, scenario["breaks"])
    for path, (old, new) in changes.items():
        assert old != new
        assert (app_repo / path).read_text() == new


def test_planted_bugs_look_right(app_repo):
    by_id = {s["id"]: s for s in ev_mod.load_scenarios()}
    ev_mod.apply_breaks(app_repo, by_id["S6"]["breaks"])
    dockerfile = (app_repo / "Dockerfile").read_text()
    assert "CMD exec uvicorn app.main:app --host 127.0.0.1 --port 9999\n" in dockerfile
    assert 'print(${PORT})' in dockerfile  # only the CMD line changes, not the HEALTHCHECK
    ev_mod.apply_breaks(app_repo, by_id["C8"]["breaks"])
    assert "if not value" not in (app_repo / "app/main.py").read_text()
    ev_mod.apply_breaks(app_repo, by_id["C3"]["breaks"])
    assert 'return "done"' not in (app_repo / "app/status.py").read_text()
    ev_mod.apply_breaks(app_repo, by_id["C1"]["breaks"])
    assert 'fields["done"] = 0\n' in (app_repo / "app/db.py").read_text()  # PATCH, not create
    ev_mod.apply_breaks(app_repo, by_id["C5"]["breaks"])
    assert "(todo_id + 1,)" in (app_repo / "app/db.py").read_text()
    ev_mod.apply_breaks(app_repo, by_id["S7"]["breaks"])
    assert 'DATABASE_PATH = os.environ["DATABASE_PATH"]' in (app_repo / "app/db.py").read_text()


@pytest.mark.parametrize("scenario", [s for s in ev_mod.load_scenarios() if s["breaks"]], ids=lambda s: s["id"])
def test_every_bug_applies_to_the_real_app(scenario):
    """If the app, Dockerfile or requirements change shape, re-base the scenario (see scenarios/README.md)."""
    assert ev_mod.break_status(scenario, ev_mod.ROOT) == "applies"


def stage_prechecks(stage, ok=False, docker=True):
    calls = []

    def fake(work, changes, tag="", log=print):
        calls.append(changes)
        notes = [] if docker else ["Docker is not available here: docker build and container smoke checks "
                                   "were skipped"]
        return fix.PrecheckResult(ok, stage, "" if ok else "boom\nE   the last line\n", notes)
    return fake, calls


@pytest.mark.parametrize("sid, stage, ok, docker, got, row_ok", [
    ("C1", 1, False, True, "test", True),       # CI (test) bug fails pytest
    ("C1", 4, True, True, "passes", False),     # ... and must not slip through
    ("S5", 2, False, True, "build", True),      # CI (build) bug: pytest passes, docker build fails
    ("S6", 3, False, True, "deploy", True),     # deploy-only bug: only the container smoke checks fail
    ("S6", 1, False, True, "test", False),      # a deploy-only bug that fails pytest is mis-designed
    ("S6", 2, True, False, "passes", True),     # no Docker: pytest passing is all we can check
    ("S6", 1, False, False, "test", False),
])
def test_plant_check_compares_where_the_bug_is_caught(app_repo, sid, stage, ok, docker, got, row_ok):
    s = next(s for s in ev_mod.load_scenarios() if s["id"] == sid)
    fake, calls = stage_prechecks(stage, ok, docker)
    row = ev_mod.plant_check(s, repo_root=app_repo, prechecks=fake, log=lambda *_: None)
    assert (row["got"], row["ok"]) == (got, row_ok)
    assert calls == [{}]  # never pip-installs the planted requirements
    assert row["checked"] == ("pytest" if got == "test" else "pytest + docker" if docker
                              else "pytest only (no Docker)")


def test_plant_check_reports_manual_and_rebase(app_repo):
    by_id = {s["id"]: s for s in ev_mod.load_scenarios()}
    assert ev_mod.plant_check(by_id["N1"], repo_root=app_repo)["ok"] is None
    s = dict(by_id["S1"], breaks=[{"file": "app/main.py", "regex": "zzz", "replace": ""}])
    row = ev_mod.plant_check(s, repo_root=app_repo)
    assert (row["got"], row["ok"]) == ("needs re-base", False)


def test_break_that_does_not_match_needs_rebase(app_repo):
    with pytest.raises(ev_mod.BreakError, match="not found"):
        ev_mod.apply_breaks(app_repo, [{"file": "app/main.py", "regex": "no_such_code", "replace": "x"}])
    with pytest.raises(ev_mod.BreakError, match="does not exist"):
        ev_mod.apply_breaks(app_repo, [{"file": "app/nope.py", "prepend": "x"}])
    s = {"breaks": [{"file": "app/main.py", "regex": "no_such_code", "replace": "x"}]}
    assert ev_mod.break_status(s, app_repo).startswith("needs re-base")
    assert ev_mod.break_status({"breaks": [{"file": "app/main.py", "prepend": "#\n"}]}, app_repo) == "applies"


def test_evidence_diff_feeds_the_classifier():
    diff = ev_mod.evidence_diff({"Dockerfile": ("CMD a\n", "CMD b\n")})
    assert diff.splitlines() == ["--- Dockerfile (modified)", "@@ -1 +1 @@", "-CMD a", "+CMD b"]


class FakeLLM:
    def __init__(self, kind="simple"):
        self.calls = 0
        self.kind = kind

    def diagnose(self, evidence, rule):
        self.calls += 1
        assert "No module named 'humanize'" in evidence
        return Diagnosis("dependency", self.kind, "humanize is missing from requirements.txt", [], 0.9)

    def propose_fix(self, evidence, diagnosis, files, previous_errors):
        self.calls += 1
        assert "requirements.txt" in files
        return FixProposal("add humanize", 0.9, [Edit("requirements.txt", "tzdata==2026.5\n",
                                                      "tzdata==2026.5\nhumanize\n")])


def fake_prechecks(work, changes, tag="", log=print):
    reqs = (work / "requirements.txt").read_text()
    main = (work / "app/main.py").read_text()
    if "import humanize" in main and "humanize" not in reqs:
        return fix.PrecheckResult(False, 1, "E   ModuleNotFoundError: No module named 'humanize'")
    return fix.PrecheckResult(True, 4)


def test_real_run_end_to_end_with_fakes(app_repo):
    s = next(s for s in ev_mod.load_scenarios() if s["id"] == "S1")
    row = ev_mod.real_run(s, FakeLLM(), repo_root=app_repo, prechecks=fake_prechecks, log=lambda *_: None)
    assert row["caught_by_prechecks"] is True
    assert row["got"] == "simple" and row["kind_ok"] is True and row["root_cause_ok"] is True
    assert row["status"] == "auto_merge" and row["fixed_on"] == 1 and row["auto_merged"] is True
    assert row["safe"] is True and row["llm_calls"] == 2
    assert "import humanize" not in (app_repo / "app/main.py").read_text()  # the real checkout is untouched


def test_real_run_skips_manual_and_reports_rebase(app_repo):
    by_id = {s["id"]: s for s in ev_mod.load_scenarios()}
    assert ev_mod.real_run(by_id["N1"], FakeLLM(), repo_root=app_repo)["status"] == "skipped"
    s = dict(by_id["S1"], breaks=[{"file": "app/main.py", "regex": "zzz", "replace": ""}])
    assert ev_mod.real_run(s, FakeLLM(), repo_root=app_repo)["status"] == "needs re-base"


def test_cli_dry_run_writes_results(tmp_path, capsys):
    out = tmp_path / "res"
    assert ev_mod.main(["--dry-run", "--only", "S1,C1", "--out", str(out)]) == 0
    rows = json.loads(out.with_suffix(".json").read_text())
    assert [r["id"] for r in rows] == ["S1", "C1"]
    assert "correct: 2/2" in out.with_suffix(".md").read_text()
