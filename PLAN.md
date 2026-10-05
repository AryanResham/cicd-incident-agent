# Plan: Autonomous CI/CD Incident Response Agent

**Project title (as registered):** *Autonomous CI/CD Incident Response Agent for Automated Failure Diagnosis and Remediation*

No code yet. This file is the plan. Review it, change what you want, then we build.

---

## 0. The idea

A small FastAPI to-do app with a full pipeline (Git/GitHub → tests → Docker → GitHub Actions → Render). On top of that sits an **incident agent**. When something breaks, its rule is: **first get back to the last stable version, then figure out what broke, then fix it, or hand it to a human with a suggested fix.**

The agent is a set of Python scripts in the repo, run by GitHub Actions workflows on GitHub's machines. It isn't hosted anywhere. The only outside service it calls is the Gemini API, which it uses for diagnosis and for writing fixes.

### How this maps to the course requirements
| Requirement | Where |
|---|---|
| Version control (Git/GitHub) | Repo, structured commits, PRs (the agent opens PRs too) |
| Application build | FastAPI app + Docker image build |
| Automated testing | pytest in CI, plus smoke tests against the live app |
| Containerization (Docker) | `Dockerfile`, image pushed to GHCR |
| CI/CD (GitHub Actions) | `ci-cd.yml`, `incident-agent.yml`, `health-monitor.yml` |
| Deployment | Render |
| Documentation + demo | README, this plan, incident reports, demo script (§F) |

---

# Part A: The target app + pipeline

## A1. Tech
| Thing | Choice |
|---|---|
| App | Python 3.12, FastAPI + Uvicorn |
| Storage | SQLite (`sqlite3`, built into Python), seeded on startup only if the table is empty |
| Page | One static `index.html` + Tailwind (Play CDN) + about 40 lines of vanilla JS `fetch()` |
| Tests | pytest + FastAPI `TestClient` |
| Container | `python:3.12-slim`, non-root user, listens on `$PORT` |
| Registry | GHCR. Every image is tagged with its commit SHA, which is what makes rollback possible |
| Hosting | Render Web Service, running an existing image, deployed through a deploy hook |

> ⚠️ Render's free tier has no persistent disk, so the DB resets to the seed data on every deploy or restart. It also **sleeps after about 15 minutes idle**, and the first request after that can take up to a minute. Every health check accounts for this (a slow warm-up request, then retries).

## A2. Features
A complete (but simple) to-do app:
- **Create** a todo: title (required) + optional deadline
- **Edit** a todo: change the title and/or deadline (or remove the deadline)
- **Mark done / undo**: a checkbox
- **Delete**
- **Deadlines + status**: each todo shows one status badge:

| Status | Rule | Badge |
|---|---|---|
| `done` | marked done (deadline ignored) | grey, strikethrough |
| `overdue` | not done, and the deadline date is before today | red "Overdue" |
| `due_today` / `due_tomorrow` | not done, and the deadline is today / tomorrow | amber "Due today" / "Due tomorrow" |
| `upcoming` | not done, and the deadline is 2+ days away | neutral, shows the date |
| `no_deadline` | not done, no deadline | no badge |

- **Sort order:** overdue → due soon → upcoming → no deadline → done (and by deadline date within each group)
- **Summary line at the top:** e.g. "2 overdue · 1 due soon · 5 open"

**Status is computed on the server** (not in the JS), so it's covered by pytest. That also makes it a good thing to break in a scenario.
"Today" depends on the time zone, so it's set by an env var: `APP_TIMEZONE` (default `Asia/Kolkata`). Python's `zoneinfo` needs the `tzdata` package in the slim Docker image, so that goes in `requirements.txt`.

## A3. Data model (`todos` table)
| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `title` | TEXT NOT NULL | 1–200 chars, trimmed |
| `done` | INTEGER 0/1 | default 0 |
| `due_date` | TEXT `YYYY-MM-DD`, nullable | date only (no time). Keeps it simple |
| `created_at` / `updated_at` | TEXT ISO timestamp | |

Responses also include the computed `status` field (it isn't stored).

## A4. Routes
| Method | Path | Notes |
|---|---|---|
| GET | `/` | Serves `static/index.html` |
| GET | `/api/todos` | Sorted list (order above) + `status` on each item |
| GET | `/api/todos/summary` | `{overdue, due_soon, open, done}` counts |
| GET | `/api/todos/{id}` | `404` if missing |
| POST | `/api/todos` | `{title, due_date?}` → `201`; bad title/date → `422` |
| PATCH | `/api/todos/{id}` | any of `{title, due_date, done}`; `due_date: null` removes the deadline → `200` / `404` / `422` |
| DELETE | `/api/todos/{id}` | `204` / `404` |
| GET | `/health` | `{"status": "ok"}` |

Validation: title 1–200 chars after trimming; `due_date` must be a real date. A past date is **allowed** (you can log something that's already overdue).

**Seed data** is inserted whenever the DB is empty. Render's free tier wipes the DB on every deploy/restart, so **after every redeploy the app comes back with these todos already there**. Todos you add yourself last until the next deploy or restart. Deadlines are set **relative to the day the app starts**, so every badge always shows up:
| Title | Deadline | Shows as |
|---|---|---|
| "Submit project title" | 3 days ago | Overdue |
| "Write pytest tests" | today | Due today |
| "Set up GitHub Actions" | tomorrow | Due tomorrow |
| "Prepare demo slides" | +7 days | Upcoming |
| "Read FastAPI docs" | none | — |
| "Initialize git repo" | 2 days ago, **done** | Done |

## A5. Page (one static `index.html`, simple)

**Keep it small and boring:** a single-user personal to-do page, like a notes app. No login, no accounts, no framework, no components, no build step, no custom CSS file (Tailwind classes only). One HTML file with one small `<script>` (target: under ~120 lines of JS). Simple functions like `load()` and `render()`, plus one handler per button.
```
┌──────────────────────────────────────────────┐
│  To-Do                2 overdue · 1 due soon │
│  [ New todo title......... ] [📅 date] [Add] │
│  All | Open | Done                           │  ← filter tabs
├──────────────────────────────────────────────┤
│ ☐ Submit project title    🔴 Overdue  ✏️ 🗑 │
│ ☐ Write pytest tests      🟠 Due today ✏️ 🗑 │
│ ☐ Prepare demo slides     12 Oct      ✏️ 🗑 │
│ ☑ ~~Initialize git repo~~             ✏️ 🗑 │
└──────────────────────────────────────────────┘
```
- Checkbox = mark done/undo (PATCH)
- ✏️ = inline edit: the row turns into title + date inputs with Save/Cancel
- 🗑 = delete, with a `confirm()` prompt
- Native `<input type="date">` (no date-picker library)
- Tailwind via the Play CDN, plain JS (~120 lines), no framework, no build step
- Errors from the API (`422`) show as a small red message under the form
- Titles are inserted with `textContent` (safe against HTML injection)
- Works on a phone (single column)

## A6. Tests (pytest)
A fresh temporary DB for each test. **The date is frozen in tests** (the "today" function is injected as a dependency), so deadline tests don't depend on the real date.
- **CRUD:** create (with/without a deadline), get, list, edit title, change/remove deadline, delete, and `404`s for each
- **Mark done:** toggle on → `done`; toggle off → back to the correct deadline status
- **Validation:** blank title, title too long, invalid date (`2026-02-30`), wrong types → `422`
- **Status logic:** overdue / due today / due tomorrow / upcoming / no deadline / done-but-past-deadline (= `done`, not overdue). Also the boundaries: yesterday, today, tomorrow, +2 days
- **Sorting + summary counts**
- **Seed:** inserted once, not duplicated, with the expected statuses
- **Time zone:** a deadline near midnight UTC gives the right status in `Asia/Kolkata`
- **Page:** `/` returns HTML; `/health` returns ok

## A7. Pipeline (`ci-cd.yml`), on push to `main`
1. **test**: run pytest and save the JUnit XML results as an artifact
2. **build**: Docker build, then push `ghcr.io/<owner>/<repo>:<sha>`
3. **deploy**: call Render's deploy hook with that exact image
4. **verify**: run the **smoke checks** (§B2) against the live URL

PRs run steps 1–2 only.

---

# Part B: The Incident Agent

## B1. When it wakes up (3 triggers)

| Trigger | What it means | Is production broken? |
|---|---|---|
| **CI failed** (test or build) | The broken code **never got deployed**, so the stable version is still live | No → no rollback needed, go straight to fixing |
| **Deploy/verify failed** | The new version is live and broken | **Yes → roll back first** |
| **Health monitor failed** (scheduled every ~10 min) | The live app has gone unhealthy | **Yes → roll back first** |

So in practice: bugs that the tests catch never reach Render. The rollback path is for bugs that **pass the tests but break the real deployment** (bad start command, wrong port, a crash only in the container, a broken page the tests don't cover, and so on).

## B2. Smoke checks (`agent/smoke.py`)
Used by the `verify` step, by the health monitor, and after every rollback or fix:
| Check | Pass condition |
|---|---|
| `GET /health` | `200` + `{"status":"ok"}` |
| `GET /` | `200`, HTML |
| `GET /api/todos` | `200`, a JSON list |
| Round trip | `POST` a todo with a deadline → `PATCH` it to done → `GET` it shows `status: done` → `DELETE` it (cleans up) |
| Latency | each request under ~3 s after warm-up |

A warm-up request with a 90 s timeout comes first, then 3 retries with backoff before anything is declared "down".

## B3. The flow

```
                 ┌─────────────── failure detected ───────────────┐
                 │                                                 │
        production broken?                                         │
          yes │            no (CI failed, nothing deployed)        │
              ▼                                                    │
   ① ROLLBACK to last stable image ──▶ smoke check ✅               │
     (+ deploy freeze on)                                          │
              │                                                    │
              ▼                                                    ▼
   ② COLLECT evidence: failed logs, smoke results, Render deploy status,
                       diff between the stable commit and the broken commit
              ▼
   ③ DIAGNOSE (rules first, then Gemini) → category + root cause + SIMPLE or CORE
              ▼
     ┌────────┴──────────────────────────┐
   SIMPLE                               CORE
     ▼                                   ▼
   ④ FIX LOOP (max 3 attempts)         ⑤ Issue + suggested-fix PR
     patch → verify in runner             (PR is NOT merged; labelled needs-human)
     → PR → CI → auto-merge               stable version keeps running
     → deploy → smoke check
       ✅ → close incident, lift freeze
       ❌ → rollback again, next attempt
     after 3 failures:
       → Issue + suggested-fix PR (best attempt, not merged)
       → stable version keeps running
```

### ① Rollback
- **"Last stable"** = the commit SHA of the most recent `ci-cd` run on `main` where **deploy + verify passed**. It's looked up through the Actions API, so nothing extra is stored
- Call the Render deploy hook with `imgURL=ghcr.io/...:<stable-sha>`, then run the smoke checks
- If the rollback itself fails its smoke checks → stop and open an urgent issue (`needs-human`). No more automation, since something bigger is wrong (Render down, etc.)
- **Rollback never depends on Gemini.** It's pure rules, so it still works if the LLM is rate-limited or down

### Deploy freeze (important)
After a rollback, `main` still contains the broken commit. Without a freeze, the next unrelated push would deploy the bug again.
- While an issue labelled `incident:active` is open, the `deploy` job **only deploys commits that come from the agent's fix PRs**. Other pushes still get tested and built, but not deployed
- The freeze lifts when the incident is closed (by a successful fix, or by a human)

### ② Collect evidence
- Failed job/step logs (tail ~300 lines per step, **secrets masked**)
- JUnit XML of failing tests
- Smoke-check results (which check failed, status code, response body)
- Render deploy status (if the Render API key is set; optional)
- `git diff <stable-sha>..<broken-sha>`, plus the full contents of the files involved

### ③ Diagnose: SIMPLE vs CORE
Rules decide first. Gemini only refines the decision. **When in doubt → CORE** (the safe side).

| SIMPLE (agent fixes and merges by itself) | CORE (agent only suggests) |
|---|---|
| Missing/wrong dependency in `requirements.txt` | Wrong logic in an API endpoint |
| Dockerfile mistakes (bad `COPY`, wrong `CMD`, port) | Routing broken / wrong paths |
| Config: env var defaults, settings files | DB/storage logic |
| Syntax error, missing import, obvious typo/`NameError` | Validation / business rules |
| | Anything where the fix changes *what the app does* |
| | A fix > 30 changed lines, or one that touches several functions |

On top of the label, the patch itself is checked. If a "SIMPLE" fix ends up changing function logic in `app/` (anything beyond imports/config/constants), it's upgraded to **CORE**.

Things the agent **can't** fix are always CORE/issue-only: missing GitHub/Render secrets (it isn't allowed to set those), Render outages, and quota/billing problems.

### ④ Fix loop (SIMPLE only, max 3 attempts)
Each attempt:
1. Gemini writes a patch (`{file, search, replace}` edits) from the evidence + any errors from earlier attempts
2. **Pre-checks in the runner, before anything gets near production:** apply the patch → pytest → `docker build` → **run the container in the runner + run the smoke checks against it**
   - If a pre-check fails → that counts as an attempt; the new error goes into the next attempt. **Nothing is deployed**
3. Pre-checks pass → open PR `agent/fix-<incident>-<n>` → CI → **auto-merge**
4. Deploy → smoke checks on the live app
   - ✅ → close the incident issue, lift the freeze, done
   - ❌ → **roll back again**, and that counts as an attempt

Because of step 2, most bad fixes fail inside the runner and never touch production.

After **3 failed attempts**: open an issue with the full history (what was tried, why each attempt failed) + open the best attempt as a PR **without merging it** (`needs-human`). The stable version stays live and the freeze stays on.

### ⑤ CORE path
- No auto-fix, no auto-merge
- The agent still has Gemini write a suggested fix and runs the same pre-checks on it
- It opens an **issue** (diagnosis, evidence, root cause) + a **PR** with the suggested fix, linked to the issue and labelled `needs-human`. The PR description says whether the pre-checks passed
- The stable version stays live and the freeze stays on until a human merges or closes it

## B4. Guardrails
- The agent **never pushes directly to `main`**. Everything goes through a PR + required CI checks
- **Never edits `tests/` or `.github/workflows/`**. A patch that touches them is rejected
- Allowed files: `app/`, `requirements*.txt`, `Dockerfile`, config files
- **Loop guard:** failures on `agent/*` branches don't start a new incident. They're part of the current one
- **One incident at a time** (a GitHub Actions `concurrency` group), so two triggers can't fight each other
- **Kill switches:** repo variables `AGENT_ENABLED`, `AGENT_AUTO_MERGE`, `AGENT_ROLLBACK` (`false` = off)
- Secrets are masked before anything is sent to Gemini
- Every incident has one GitHub issue that serves as the full timeline (rollback, attempts, results)

## B5. Gemini + rate limits
- `google-genai` SDK with a Flash-class model, set by an `AGENT_MODEL` env var; secret `GEMINI_API_KEY`
- JSON output is enforced with Gemini's response-schema mode and validated in our code
- **Calls per incident:** 1 diagnosis + at most 3 fix attempts = **at most 4–5 calls**. Logs are trimmed, so each call is small
- **Built for the free tier:**
  - Rules handle classification and rollback, so the LLM is only used where it's actually needed
  - A **client-side throttle** keeps us under the requests-per-minute limit
  - On `429`: wait for the delay Gemini tells us (or use exponential backoff), up to ~2 minutes in total
  - If the quota is still exhausted: **the stable version is already restored** (rollback needs no LLM). The agent opens the issue with the evidence + "diagnosis pending (Gemini quota exhausted)" and can be re-run later with `workflow_dispatch`
- **Expectation:** normal demo use (a handful of incidents a day) should fit comfortably within the free tier. The **eval harness** (Part C) is the only heavy user. It runs slowly on purpose (throttled), and I'll tell you if it doesn't fit. I'll check Google's current free-tier numbers before building, since they change

## B6. Alerts
For now, GitHub only (issues, PRs, comments). All notifications go through a single `notify()` function, so adding Discord/Slack later is a one-function change.

---

# Part C: Evaluation (how we measure "how accurate is it?")

## C1. Failure scenarios (`scenarios/`)
⭐ = part of the live demo. The rest run through the eval harness.

**SIMPLE: the agent should fix these by itself**
| # | Scenario | Caught by | Expected path |
|---|---|---|---|
| S1 ⭐ | Code imports a package missing from `requirements.txt` | CI (test step: `ModuleNotFoundError`) | auto-fix |
| S2 | Missing `import` in the code | CI | auto-fix |
| S3 | Syntax error | CI | auto-fix |
| S4 | Typo in a variable name → `NameError` | CI | auto-fix |
| S5 | Dockerfile `COPY` from a wrong path | CI (build) | auto-fix |
| S6 ⭐ | Wrong port in the Dockerfile `CMD` (tests pass, crashes on Render) | verify | **Rollback** → auto-fix |
| S7 | Required env var with no default → crash at startup | verify | **Rollback** → auto-fix |
| S8 | `tzdata` removed → `zoneinfo` fails only in the slim container | verify | **Rollback** → auto-fix |
| S9 | `index.html` not copied into the image (page 404s live) | verify | **Rollback** → auto-fix |

**CORE: the agent should only suggest a fix (issue + PR, waits for a human)**
| # | Scenario | Caught by |
|---|---|---|
| C1 ⭐ | Mark done is broken (toggles the wrong todo / never flips) | CI |
| C2 | Overdue logic off by one (deadline today shows as Overdue) | CI |
| C3 | Done todos with a past deadline still show as Overdue | CI |
| C4 | Edit (PATCH) wipes the deadline when only the title changes | CI |
| C5 | Delete removes the wrong item | CI |
| C6 | Create returns `200` instead of `201` | CI |
| C7 | Route path renamed (`/api/todo`) | CI |
| C8 | Validation removed (blank titles / invalid dates accepted) | CI |

**No code fix possible: issue only**
| # | Scenario | Expected |
|---|---|---|
| N1 | `RENDER_DEPLOY_HOOK_URL` secret missing | Issue saying which secret to set |
| N2 | The app goes down outside a deploy (suspended on Render) | Health monitor → rollback → issue |

**Guardrail edge cases**
| # | Scenario | Expected |
|---|---|---|
| E1 ⭐ | A SIMPLE bug that's deliberately hard to fix | 3 attempts → issue + unmerged PR, the stable version stays live |
| E2 | The only "easy" fix would be editing a test | Patch rejected |
| E3 | A random push while an incident is open | Deploy freeze blocks it |

## C2. Metrics
- **Rollback**: time from detection to the stable version being live again, and whether it succeeded
- **Classification**: is SIMPLE vs CORE right?
- **Diagnosis**: does the root cause name the right file/cause?
- **Fix success**: SIMPLE fixed within 3 attempts? On which attempt?
- **Suggested-fix quality** (CORE): would the suggested PR have passed the pre-checks?
- **Safety**: no broken code left live, no tests edited, no CORE change auto-merged
- **MTTR**: time from failure to resolved; **Gemini calls per incident**

`agent/eval.py` runs scenarios locally (the pre-check path, no GitHub/Render needed) and outputs a results table. A few live end-to-end runs on the real pipeline are used for the demo.

---

## D. Project layout
```
├── app/                     # FastAPI app
│   ├── main.py  db.py  seed.py
│   └── static/index.html
├── tests/                   # pytest for the app
├── agent/
│   ├── main.py              # entry: trigger type + run id
│   ├── smoke.py             # smoke checks
│   ├── rollback.py          # find last stable SHA, redeploy, verify
│   ├── freeze.py            # deploy freeze check (used by ci-cd.yml)
│   ├── collect.py           # logs, junit, diff
│   ├── classify.py          # rules: category + SIMPLE/CORE
│   ├── llm.py               # Gemini client, throttle, retry, schema
│   ├── fix.py               # patch → pre-checks → PR, attempt loop
│   ├── github_api.py        # issues, PRs, labels, auto-merge
│   ├── notify.py            # alerts (GitHub only for now)
│   ├── eval.py
│   └── tests/               # unit tests for the agent's rules/guardrails
├── scenarios/
├── .github/workflows/
│   ├── ci-cd.yml
│   ├── incident-agent.yml
│   └── health-monitor.yml
├── Dockerfile  .dockerignore  .gitignore
├── requirements.txt  requirements-dev.txt  requirements-agent.txt
├── README.md
└── PLAN.md
```

## E. Build order + commits
Git is set up locally. One commit per logical step (conventional commit messages), and I push after each milestone once the GitHub remote exists.

| # | Commit | Milestone (push) |
|---|---|---|
| 1 | `docs: add project plan` | |
| 2 | `chore: scaffold project (gitignore, requirements)` | |
| 3 | `feat(app): todo CRUD API with SQLite and seed data` | |
| 3b | `feat(app): deadlines, status, sorting and summary` | |
| 4 | `feat(app): todo page (create, edit, done, delete, deadlines)` | |
| 5 | `test(app): pytest suite` | **Push 1: app** |
| 6 | `build: Dockerfile and dockerignore` | |
| 7 | `ci: test, build, deploy and verify pipeline` | **Push 2: Docker + CI/CD** → set up Render, first green deploy |
| 8 | `feat(agent): smoke checks` | |
| 9 | `feat(agent): rollback and deploy freeze` | |
| 10 | `feat(agent): evidence collection and classification` | |
| 11 | `feat(agent): Gemini client with throttling and retries` | |
| 12 | `feat(agent): fix loop, PRs and incident issues` | |
| 13 | `ci: incident agent and health monitor workflows` | **Push 3: agent** → test live with a planted bug |
| 14 | `test(agent): eval harness and failure scenarios` | |
| 15 | `docs: README, architecture and demo guide` | **Push 4: done** |

## F. Demo script
1. Show the app live on Render
2. **Rollback demo (S6, wrong port):** it deploys, verify fails, the agent rolls back within about a minute (the app is back up), diagnoses SIMPLE, fixes, auto-merges, redeploys, and the checks go green
3. **Auto-fix demo (S1, missing package):** CI fails, nothing is deployed, the agent fixes requirements.txt, auto-merges and deploys
4. **Core demo (C1, mark done broken):** CI fails, nothing is deployed, the agent opens an issue + a suggested PR, and it waits for a human
5. **Give-up demo (E1):** 3 attempts → issue + unmerged PR, while the stable version stays live throughout
6. Show the incident issue timeline + the eval results table

## G. Manual setup you'll need to do
- A **GitHub repo** (public is simplest: free Actions minutes + GHCR). Then give me the URL to add as `origin`
- **Render:** Web Service from an existing image (`ghcr.io/<owner>/<repo>:latest` for the first time) → deploy hook → `RENDER_DEPLOY_HOOK_URL` secret; **auto-deploy off**; the GHCR package must be public (or add registry credentials in Render)
- **Gemini key** (Google AI Studio) → `GEMINI_API_KEY` secret
- **Bot token** → `AGENT_TOKEN` secret (fine-grained PAT for this repo: Contents, Pull requests, Issues, Actions = read/write). Needed because PRs and merges made with the built-in `GITHUB_TOKEN` don't trigger other workflows, so CI/deploy would never run on the agent's fixes
- `RENDER_APP_URL` repo variable
- Repo settings: allow auto-merge ✅; allow Actions to create PRs ✅; branch protection on `main` requiring `test` + `build`

## H. Decisions
**Made:** Gemini · rollback-first · SIMPLE auto-fix (max 3) / CORE suggest-only · deploy freeze during incidents · health monitor every ~10 min · GitHub-only alerts for now · git with structured commits

**Still open:**
1. **Deadline:** the sheet said implementation is due 30 Sep – 3 Oct, and today is 5 Oct. When are the demo/submission actually due?
2. **GitHub repo URL:** needed before the first push
