# Failure scenarios

One JSON file per scenario (PLAN.md §C1). ⭐ = part of the live demo.

Each file has:
- `breaks`: how to plant the bug, as small edits against the app (`regex`/`replace` = first match,
  `search`/`replace`, `prepend`, `append`). They are based on the current app, `Dockerfile` and
  `requirements.txt`; a unit test (`test_every_bug_applies_to_the_real_app`) fails if a change to the app
  makes a pattern stop matching. `python -m agent.eval --dry-run` then shows `needs re-base` in the `bug`
  column: update the pattern (the title says what the bug is meant to be).
- `simulated`: the failure evidence the scenario should produce in CI (log lines, failing tests,
  failed smoke checks, diff). The dry run classifies this without Gemini.
- `expected`: kind (`simple` / `core` / `issue_only`), category and the path the agent should take.
- `root_cause_keywords`: words a correct diagnosis should mention (used by the real eval).

| ID | Scenario | How to apply | Caught by | Expected kind | Expected path |
|---|---|---|---|---|---|
| S1 ⭐ | Code imports a package missing from `requirements.txt` | prepend `import humanize` to `app/main.py` | CI (test) | SIMPLE / dependency | auto-fix |
| S2 | Missing import | delete the `from fastapi import ...` line in `app/main.py` | CI (test) | SIMPLE / code_error | auto-fix |
| S3 | Syntax error | remove the `:` of the first `def` in `app/status.py` | CI (test) | SIMPLE / code_error | auto-fix |
| S4 | Typo in a variable name (`NameError`) | `due_date < today` → `due_dte < today` in `app/status.py` | CI (test) | SIMPLE / code_error | auto-fix |
| S5 | Dockerfile `COPY` from a wrong path | `COPY app` → `COPY src/app` | CI (build) | SIMPLE / docker_build | auto-fix |
| S6 ⭐ | Wrong host/port in the Dockerfile `CMD` | `--host 0.0.0.0 --port ${PORT:-8000}` → `--host 127.0.0.1 --port 9999` | verify | SIMPLE / deploy_failure | **rollback** → auto-fix |
| S7 | Required env var with no default | `os.getenv("DATABASE_PATH", ...)` → `os.environ["DATABASE_PATH"]` in `app/db.py` (the tests set it, the image and Render don't) | verify | SIMPLE / deploy_failure | **rollback** → auto-fix |
| S8 | `tzdata` removed | delete the `tzdata` line from `requirements.txt` (CI runners have system time zones; the image uses only the package) | verify | SIMPLE / deploy_failure | **rollback** → auto-fix |
| S9 | `index.html` not in the image | append `app/static/` to `.dockerignore` | verify (`GET /` 404) | SIMPLE / deploy_failure | **rollback** → auto-fix |
| C1 ⭐ | Mark done is broken | `fields["done"] = int(fields["done"])` → `= 0` in `update_todo()`, `app/db.py` (PATCH never stores done) | CI (test) | CORE / test_failure | issue + suggested PR (needs-human) |
| C2 | Overdue off by one | `due_date < today` → `<=` in `app/status.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C3 | Done + past deadline shows Overdue | remove the `if done: return "done"` branch in `app/status.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C4 | PATCH wipes the deadline | `exclude_unset=True` → `False` in `app/main.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C5 | Delete removes the wrong item | `DELETE ... WHERE id = ?", (todo_id,)` → `(todo_id + 1,)` in `app/db.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C6 | Create returns 200 instead of 201 | `status_code=201` → `200` in `app/main.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C7 | Route path renamed | list route `"/api/todos"` → `"/api/todo"` in `app/main.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C8 | Validation removed | delete the empty-title check (`if not value: raise ...`) in `check_title()`, `app/main.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| N1 | `RENDER_DEPLOY_HOOK_URL` secret missing | manual: delete the secret, push a commit | deploy | ISSUE_ONLY / config_secret | issue naming the secret |
| N2 | App down outside a deploy | manual: suspend the service on Render | health monitor | ISSUE_ONLY / deploy_failure | rollback (redeploy) → issue |
| E1 ⭐ | SIMPLE-looking bug that is hard to fix | `app/main.py` imports `acme_internal_audit` and calls it in `create_todo()` + `requirements.txt` pins it (not on PyPI). Removing the call changes behaviour, so any working fix is upgraded to CORE | CI (test) | SIMPLE / dependency | up to 3 attempts → issue + unmerged PR (never auto-merged), stable stays live |
| E2 | Only "easy" fix is editing a test | append a syntax error to `tests/conftest.py` | CI (test) | SIMPLE / code_error | patch rejected (tests/ is off-limits) → issue |
| E3 | Random push while an incident is open | manual: push any commit while an `incident:active` issue is open | deploy freeze | – | `deploy=false` |

## Running the eval

```bash
# Rules only, no Gemini, no Docker: classification of every scenario, the freeze rule (E3),
# and whether each bug can still be planted in the current app code.
python -m agent.eval --dry-run

# No Gemini: plant each bug in a temp copy and check it fails where the scenario says
# (CI (test) bugs fail pytest; S5 passes pytest and fails `docker build`; S6-S9 pass pytest and the
# build, and fail the container smoke checks). Without Docker only the pytest half is checked.
python -m agent.eval --plant-check

# Real: for each scenario, copy the repo to a temp dir, plant the bug, run the pre-checks
# (pytest; docker build + container smoke checks if Docker is installed) to capture the real
# failure, then rules + Gemini diagnosis + the fix loop (max 3 attempts). PRs are only recorded
# locally. Throttled for the free tier; stops at --max-calls.
GEMINI_API_KEY=... python -m agent.eval --only S1,S2,C1 --rpm 4 --max-calls 20
```

Results land in `scenarios/results.md` / `results-dry-run.md` / `results-plant-check.md` (+ `.json`),
which are git-ignored.
Notes:
- The real eval installs changed requirements into the current Python environment (like the CI runner
  does); use a throwaway venv. `--plant-check` never installs anything.
- S6–S9 only fail inside the container, so without Docker they show as `not caught` locally.
- Why S6–S9 pass CI but break on Render: S6 binds 127.0.0.1, so Render finds no open port (Render
  auto-detects a wrong port on 0.0.0.0, which is why the host is wrong too); S7 needs `DATABASE_PATH`,
  which `tests/conftest.py` sets but the image and Render don't; S8 the image reads time zones only from
  the `tzdata` package (`PYTHONTZPATH=""`), while CI runners have system time zones; S9 `index.html` is
  excluded from the image by `.dockerignore`, while the tests read it from the checkout. In S6–S8 the new
  container never starts, so Render keeps the old version: `verify` catches it because `/health` never
  reports the new commit ("new version did not go live").
- To plant a scenario for the live demo: `python -m agent.eval --plant S6` (applies the bug to this
  checkout), then commit and push to `main` (see the demo script in the main README).
