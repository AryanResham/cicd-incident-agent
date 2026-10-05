# Failure scenarios

One JSON file per scenario (PLAN.md §C1). ⭐ = part of the live demo.

Each file has:
- `breaks`: how to plant the bug, as small edits against the app (`regex`/`replace` = first match,
  `search`/`replace`, `prepend`, `append`). They were written against the API contract in `docs/API.md`
  and a typical implementation of it, **not** the final app code. If a pattern no longer matches,
  `python -m agent.eval --dry-run` shows `needs re-base` in the `bug` column: update the pattern
  (the title says what the bug is meant to be).
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
| S6 ⭐ | Wrong port in the Dockerfile `CMD` | `$PORT`/`${PORT:-8000}` in `CMD` → `9999` | verify | SIMPLE / deploy_failure | **rollback** → auto-fix |
| S7 | Required env var with no default | `os.getenv("DATABASE_PATH", ...)` → `os.environ["DATABASE_PATH"]` in `app/db.py` | verify | SIMPLE / deploy_failure | **rollback** → auto-fix |
| S8 | `tzdata` removed | delete the `tzdata` line from `requirements.txt` | verify | SIMPLE / deploy_failure | **rollback** → auto-fix |
| S9 | `index.html` not in the image | append `app/static/` to `.dockerignore` | verify (`GET /` 404) | SIMPLE / deploy_failure | **rollback** → auto-fix |
| C1 ⭐ | Mark done is broken | `int(done)` → `0` in `app/db.py` (PATCH never stores done) | CI (test) | CORE / test_failure | issue + suggested PR (needs-human) |
| C2 | Overdue off by one | `due_date < today` → `<=` in `app/status.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C3 | Done + past deadline shows Overdue | remove the `if done: return "done"` branch in `app/status.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C4 | PATCH wipes the deadline | `exclude_unset=True` → `False` in `app/main.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C5 | Delete removes the wrong item | `DELETE ... WHERE id = ?", (todo_id,)` → `(todo_id + 1,)` in `app/db.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C6 | Create returns 200 instead of 201 | `status_code=201` → `200` in `app/main.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C7 | Route path renamed | list route `"/api/todos"` → `"/api/todo"` in `app/main.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| C8 | Validation removed | `min_length=1` → `min_length=0` in `app/main.py` | CI (test) | CORE / test_failure | issue + suggested PR |
| N1 | `RENDER_DEPLOY_HOOK_URL` secret missing | manual: delete the secret, push a commit | deploy | ISSUE_ONLY / config_secret | issue naming the secret |
| N2 | App down outside a deploy | manual: suspend the service on Render | health monitor | ISSUE_ONLY / deploy_failure | rollback (redeploy) → issue |
| E1 ⭐ | SIMPLE-looking bug that is hard to fix | `app/main.py` imports `acme_internal_audit` + `requirements.txt` pins it (not on PyPI) | CI (test) | SIMPLE / dependency | 3 attempts → issue + unmerged PR, stable stays live |
| E2 | Only "easy" fix is editing a test | append a syntax error to `tests/conftest.py` | CI (test) | SIMPLE / code_error | patch rejected (tests/ is off-limits) → issue |
| E3 | Random push while an incident is open | manual: push any commit while an `incident:active` issue is open | deploy freeze | – | `deploy=false` |

## Running the eval

```bash
# Rules only, no Gemini, no Docker: classification of every scenario, the freeze rule (E3),
# and whether each bug can still be planted in the current app code.
python -m agent.eval --dry-run

# Real: for each scenario, copy the repo to a temp dir, plant the bug, run the pre-checks
# (pytest; docker build + container smoke checks if Docker is installed) to capture the real
# failure, then rules + Gemini diagnosis + the fix loop (max 3 attempts). PRs are only recorded
# locally. Throttled for the free tier; stops at --max-calls.
GEMINI_API_KEY=... python -m agent.eval --only S1,S2,C1 --rpm 4 --max-calls 20
```

Results land in `scenarios/results.md` / `results-dry-run.md` (+ `.json`), which are git-ignored.
Notes:
- The real eval installs changed requirements into the current Python environment (like the CI runner
  does); use a throwaway venv.
- S6–S9 only fail inside the container, so without Docker they show as `not caught` locally.
- To plant a scenario for the live demo, apply the same edit by hand on a branch and merge it to `main`.
