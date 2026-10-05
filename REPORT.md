# Build Report: CI/CD Incident Response Agent

**Date:** 5 Oct 2026 · **Repo:** https://github.com/AryanResham/cicd-incident-agent · **Live app:** https://cicd-incident-agent.onrender.com

This report covers what was built, how it was built, how each part was checked, what went wrong along the way, and what's left.

---

## 1. Result at a glance

| | |
|---|---|
| **Pipeline** | First run fully green on GitHub: test ✅ → build ✅ → deploy ✅ → verify ✅ |
| **Live app** | Running on Render; `/health` reports the exact commit SHA that was deployed |
| **Tests** | **337 passing**: 92 app tests + 245 agent tests (run locally and on GitHub) |
| **Live incident** | Scenario S1 detected, diagnosed by Gemini, fixed, auto-merged, deployed and verified **with no human input** (§4b) |
| **Failure scenarios** | 22 scenarios; rule-based classification **22/22 correct**; planted-bug check **19/19** behave as designed |
| **Commits** | 43 on `main` (38 work commits + 5 merges), all small and conventional (`feat`, `fix`, `test`, `ci`, `build`, `docs`, `chore`) |
| **Code size** | App ~580 lines · App tests ~630 · Agent ~2,870 · Agent tests ~2,200 · Workflows ~415 |

---

## 2. How it was built (the flow)

The work was split into phases, run by separate AI agents. Each agent worked in **its own isolated copy of the repo on its own branch**, so they couldn't overwrite each other's work. I (the coordinating session) merged each branch into `main` only after re-running all tests.

```
Phase 0  me           plan → skeleton → frozen API contract (docs/API.md)
Phase 1  in parallel  🧠 Backend agent · 🎨 Frontend agent · 🤖 Incident-agent builder
Phase 2  🔗 Integration agent  (backend + frontend tested together for real)
Phase 3  🐳 DevOps agent       (Dockerfile, CI/CD pipeline, scenario fixes, README)
Phase 4  me           push → first pipeline run → Render setup → green deploy → this report
```

**Rules every agent followed:** build one module at a time → check it (tests, or running it for real) → commit only when green → never push. Every commit is small and describes one step.

**Key decision: freeze the API contract first.** Before any code was written, `docs/API.md` fixed the exact JSON fields, status values, status codes, sort order and seed data. That let the backend, the frontend and the incident agent be built **at the same time** without waiting for each other, and it's why they fit together with almost no rework.

---

## 3. What each part does and how it was checked

### 3.1 The to-do app (backend: `app/*.py`)
**Built by:** Backend agent · **6 commits**
- FastAPI + SQLite (Python's built-in `sqlite3`, no ORM)
- Create, edit, mark done/undo, delete; optional deadline per todo
- The server works out each todo's **status**: `overdue`, `due_today`, `due_tomorrow`, `upcoming`, `no_deadline`, `done`
- Sorting (most urgent first, done last) and a summary (`overdue`, `due_soon`, `open`, `done`)
- 6 seed todos with deadlines **relative to the start date**, so every badge always shows; added only when the DB is empty, so they come back after every redeploy
- Time zone is configurable (`APP_TIMEZONE`, default `Asia/Kolkata`)
- `/health` returns `{"status":"ok","version":"<commit sha>"}`

**Checked with:** 87 unit tests (status boundaries like yesterday/today/tomorrow, the time zone near midnight, CRUD, validation, sorting, seeding once only), plus a real server run.

### 3.2 The page (`app/static/index.html`)
**Built by:** Frontend agent · **3 commits**
- One HTML file, Tailwind via CDN, ~140 lines of plain JavaScript. No framework, no build step, no login
- Add form with a date picker, filter tabs (All / Open / Done), a checkbox to mark done, ✏️ inline edit, 🗑 delete with confirmation, colour badges, a summary line, readable error messages
- All user text is inserted safely (`textContent`), so typing HTML into a title can't break the page

**Checked with:** clicking through every feature in a real browser against a temporary fake API, at desktop and phone width, with no JavaScript errors.

### 3.3 Integration (frontend + backend together)
**Built by:** Integration agent · **3 commits**
Ran the real app and clicked through every feature against the real backend. **Found and fixed 2 bugs:**
1. **Unclear validation messages**: users saw "String should have at least 1 character". Now: "Title can't be empty" / "Title is too long (201 characters, max 200)".
2. **Long titles broke the layout**: a long word with no spaces widened the page and caused sideways scrolling. It now wraps.

It also confirmed that a restart doesn't duplicate the seed todos, and that a fresh DB (which is what a redeploy looks like) brings them back. It added **4 end-to-end tests**, including one full user journey (create → done → undo → edit → clear deadline → delete).

### 3.4 The incident agent (`agent/`): the actual project
**Built by:** Incident-agent builder · **11 commits** · Python scripts run by GitHub Actions. Nothing is hosted separately.

| Module | What it does |
|---|---|
| `smoke.py` | Checks the live app: health, page, list, and a create → mark done → delete round trip, plus a latency check. Tolerates Render cold starts; can wait until a specific version is live |
| `rollback.py` | Finds the last version that passed `verify`, redeploys that exact image, and waits until it's live. **Doesn't use Gemini**, so it works even when the LLM is down |
| `freeze.py` | Deploy freeze: while an incident is open, only the agent's own fix commits get deployed |
| `collect.py` | Gathers evidence: failed log lines, failing tests, smoke results, the code diff. **Masks secrets** before anything is sent to Gemini |
| `classify.py` | Rules decide the failure type and **SIMPLE vs CORE**. When in doubt → CORE. It also inspects the patch itself: a "simple" fix that changes app logic gets upgraded to CORE |
| `llm.py` | Gemini client: structured JSON output, a built-in rate limiter, retries on 429 using Gemini's own retry delay, and a clean "diagnosis pending" fallback |
| `patching.py` | Applies fixes behind guardrails: may **never** edit `tests/` or workflow files; only `app/`, `requirements`, `Dockerfile`; size limits |
| `fix.py` | Up to **3 attempts**. Each fix is pre-checked in the runner (tests, plus a Docker build + run + smoke check) **before** a PR is opened |
| `main.py` | Runs the whole flow for each trigger (CI failure, deploy failure, health monitor) |
| `notify.py` / `report.py` | GitHub issues/comments with a full incident timeline and the time it took to recover |
| `eval.py` + `scenarios/` | 22 failure scenarios, a dry-run check, a planted-bug check, and a real Gemini eval |

**The flow when something breaks:**
1. **Is production broken?** If yes → **roll back first** (no AI needed) → turn on the deploy freeze.
2. Collect the evidence → classify it.
3. **SIMPLE** (dependency, Dockerfile, config, syntax/import error): fix → pre-check → PR → auto-merge → deploy → verify. Up to 3 tries. If all 3 fail → open an issue + an unmerged suggested PR, and keep the stable version live.
4. **CORE** (app logic, endpoints, routing): open an issue + a suggested-fix PR, and **wait for a human**.
5. Can't be fixed with code (e.g. a missing secret): open an issue that says exactly what to set.
6. Flaky: re-run once.

**Checked with:** 200 unit tests with GitHub, Gemini and Render all faked (no network), later grown to 240.

### 3.5 Docker, CI/CD, README
**Built by:** DevOps agent · **11 commits**
- **Dockerfile:** `python:3.12-slim`, non-root user, dependency layer cached, listens on `$PORT`, built-in health check, commit SHA baked in as `APP_VERSION`
- **`ci-cd.yml`:** test → build (pushed to GHCR, tagged with the commit SHA) → deploy (deploy-freeze check, then Render's deploy hook) → verify (wait until the new version is actually live, then smoke-check it)
- **README:** architecture diagram, setup guide, demo guide, limitations

**Bugs it caught in the earlier work:**
1. **Every deploy failure would have been misread as a test failure.** A smoke-check log line ("round 1 failed") looked like pytest's "N failed", so rollback-worthy incidents would have been treated as code bugs. Fixed, with a test using real output.
2. **The smoke check would have tested the *old* version.** Render keeps the old version serving until the new one is up, and forever if the new one crashes. Fix: `/health` reports the commit SHA, and `verify` waits for that exact SHA.
3. **The "wrong port" scenario wouldn't have broken anything** (Render detects other ports by itself). It now binds to a setting that really fails.
4. **The "mark done is broken" scenario broke the wrong function.** Fixed, and now the right test fails.
5. **The "too hard to fix" scenario could have been auto-merged.** Fixed so it always needs a human.
6. **Removing `tzdata` wouldn't have broken the container** (the base image has its own copy). The image now uses only the pinned package, so the scenario is real.

---

## 4. Problems along the way

| Problem | What happened | Fix |
|---|---|---|
| API overloads (529) | Twice, Anthropic's API was overloaded and killed running agents mid-task | The incident-agent builder resumed from its last commit with nothing lost. The integration agent was relaunched (it hadn't committed yet). After that, agents were told to commit early |
| Locked folder | A crashed agent left a test server running that locked its folder | Stopped the process and removed the leftover folder |
| No Docker on this PC | The image couldn't be built locally | The DevOps agent imitated the container (same files, same command) locally. **The first real build ran on GitHub and passed** |
| First pipeline run | Deploy failed on purpose (Render wasn't connected yet) | `AGENT_ENABLED=false` during setup, so the agent didn't open a false incident. After Render was set up, the failed jobs were re-run → fully green |

---

## 4b. First live incident: scenario S1 (real GitHub + Gemini + Render)

A real bug was pushed to `main`: `app/main.py` imported `humanize`, which wasn't in `requirements.txt`.

| Time (UTC) | What happened |
|---|---|
| 16:17 | CI failed at `test` (`ModuleNotFoundError`). Deploy was skipped, so **the live app was never affected** |
| 16:17 | The agent opened incident #1; the rules classified it as **dependency / SIMPLE** |
| 16:20 | Gemini's main model answered **503 (overloaded)** for 2 minutes → the agent degraded safely to "diagnosis pending" |
| 16:25 | A manual retry showed **bug #1**: the retry was treated as a new failure and only noted |
| 16:27 | Two fixes pushed (below), then the incident was re-run |
| 16:28 | The main model was still 503 → **switched to `gemini-3.5-flash-lite`** → diagnosis: "humanize is imported but missing from requirements.txt" (SIMPLE, confidence 1.00) |
| 16:28 | Attempt 1: a one-line fix (`humanize==4.11.0`) **passed the pre-checks** (deps, pytest, docker build, container smoke) → PR #2 with auto-merge |
| 16:30 | CI passed on the PR → **auto-merged** → deployed (allowed through the deploy freeze as an agent fix) |
| 16:33 | `verify` passed on the live app → **incident #1 closed by itself**; the freeze lifted |

**Time from detection to verified fix: ~16 minutes**, including ~10 minutes of finding and fixing the two bugs below. The agent's own fix cycle (diagnose → PR → merged → live and verified) took ~5 minutes.

**Bugs the live test found (both fixed, with tests):**
1. **No fallback when Gemini is overloaded:** the agent now tries the next model in `AGENT_FALLBACK_MODELS` (default `gemini-3.5-flash-lite`) on 5xx, 404 or an exhausted daily quota, before backing off.
2. **"Re-run later" didn't resume the incident:** re-running the same failed run now picks up the pending incident instead of treating it as a second failure.

---

## 5. Current live state

- `main` is pushed to GitHub; the latest pipeline run is **all green**
- The app is live on Render, serving the latest commit on `main` (which includes `6af0dbb`, **the agent's own fix from incident #1**)
- Commit history was rewritten on 5 Oct to remove tool attribution lines from commit messages (code unchanged). Issue #1 and PR #2 still mention the old commit IDs: `d62faac` = `ce28848` (the planted bug), `1bc20c9` = `6af0dbb` (the agent's fix)
- **Tests: 337 passing** (92 app + 245 agent)
- Branch protection on `main` requires `test` + `build` ✅
- Secrets: `AGENT_TOKEN`, `GEMINI_API_KEY`, `RENDER_DEPLOY_HOOK_URL` ✅
- Variables: `RENDER_APP_URL`, `AGENT_ENABLED=true` ✅
- The health monitor runs every ~10 minutes against the live app
- No open incidents

---

## 6. What's left / not yet verified

| Item | Status |
|---|---|
| A real SIMPLE incident end to end (S1) | ✅ **Done**: see §4b |
| A real rollback on Render (S6) | Not run yet. It's the next rehearsal |
| A real CORE incident (C1) and give-up path (E1) | Not run yet |
| Gemini availability | The main model (`gemini-3.8-flash`) was overloaded during the whole test; the fallback did the work. If that continues, set the variable `AGENT_MODEL=gemini-3.5-flash-lite` to use the lighter model directly |
| Real Gemini eval over all scenarios | Not run. The free tier is probably too small for it in one day (Google may limit to ~20 requests/day); split it over days, use Flash-Lite, or enable billing. Check your limits at aistudio.google.com/rate-limit |
| Gemini model name | Default `gemini-3.8-flash` (picked from Google's docs by the builder agent). Change it with the `AGENT_MODEL` variable if needed |

**Known limitations** (also in the README): the free Render tier resets the DB on every deploy and sleeps after ~15 minutes idle (first load is slow); Tailwind via CDN isn't meant for production; GitHub's scheduled runs can be a few minutes late.

---

## 7. Demo (rehearse once before presenting)

```bash
python -m agent.eval --plant S6   # wrong port: deploys, verify fails → rollback → auto-fix
git commit -am "demo: S6" && git push
```

| Scenario | What the audience sees |
|---|---|
| **S6**: wrong port | Deploy → verify fails → **automatic rollback** → agent fixes it → PR auto-merges → redeploys green |
| **S1**: missing package | CI fails, nothing deployed → agent adds it to `requirements.txt` → auto-merge → deploy |
| **C1**: mark done broken | CI fails → **issue + suggested-fix PR, waits for a human** (core logic) |
| **E1**: too hard to fix | Up to 3 attempts → gives up → issue + unmerged PR, stable version stays live |

Run one scenario at a time, and wait for the incident to close (or close it) before the next one.
