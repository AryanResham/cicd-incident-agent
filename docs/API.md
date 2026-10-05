# API Contract (frozen)

The backend implements this exactly. The frontend and the incident agent's smoke checks rely on it.
Don't change it without updating every consumer.

## Todo object

```json
{
  "id": 1,
  "title": "Write pytest tests",
  "done": false,
  "due_date": "2026-10-05",
  "status": "due_today",
  "created_at": "2026-10-05T10:15:00+05:30",
  "updated_at": "2026-10-05T10:15:00+05:30"
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | int | |
| `title` | string | Trimmed, 1–200 chars |
| `done` | bool | |
| `due_date` | `"YYYY-MM-DD"` or `null` | Date only, no time |
| `status` | string | **Computed by the server, not stored.** See below |
| `created_at`, `updated_at` | ISO 8601 string | Timezone-aware, in `APP_TIMEZONE` |

### `status` values
"Today" means the current date in `APP_TIMEZONE` (default `Asia/Kolkata`).

| Value | Rule (checked in this order) |
|---|---|
| `done` | `done == true` (the deadline is ignored) |
| `no_deadline` | `due_date == null` |
| `overdue` | `due_date < today` |
| `due_today` | `due_date == today` |
| `due_tomorrow` | `due_date == today + 1` |
| `upcoming` | `due_date >= today + 2` |

## Endpoints

| Method | Path | Request | Success | Errors |
|---|---|---|---|---|
| GET | `/` | – | `200` HTML (`app/static/index.html`) | |
| GET | `/health` | – | `200` `{"status": "ok"}` | |
| GET | `/api/todos` | – | `200` `Todo[]`, sorted (see below) | |
| GET | `/api/todos/summary` | – | `200` `{"overdue": n, "due_soon": n, "open": n, "done": n}` | |
| GET | `/api/todos/{id}` | – | `200` `Todo` | `404` |
| POST | `/api/todos` | `{"title": str, "due_date"?: "YYYY-MM-DD" \| null}` | `201` `Todo` | `422` |
| PATCH | `/api/todos/{id}` | any subset of `{"title", "due_date", "done"}` | `200` `Todo` | `404`, `422` |
| DELETE | `/api/todos/{id}` | – | `204`, empty body | `404` |

- **Summary:** `due_soon` = `due_today` + `due_tomorrow`; `open` = every todo that isn't done (including overdue); `done` = done todos.
- **PATCH:** only the fields sent are changed. `"due_date": null` **removes** the deadline; leaving `due_date` out keeps it. `updated_at` is refreshed.
- **Validation (`422`):** title is blank after trimming or longer than 200 chars; `due_date` isn't a real calendar date (e.g. `2026-02-30`); wrong types; unknown fields in a POST/PATCH body (rejected). Past deadlines **are allowed**.
- **Error bodies:** `404` → `{"detail": "Todo not found"}`. `422` → FastAPI's default validation format (`{"detail": [{"loc": [...], "msg": "...", ...}]}`).
- Route order matters: `/api/todos/summary` must be matched before `/api/todos/{id}`.

### Sort order for `GET /api/todos`
1. By status group: `overdue`, `due_today`, `due_tomorrow`, `upcoming`, `no_deadline`, `done`
2. Within a group: `due_date` ascending (nulls last), then `id` ascending

## Configuration (env vars)

| Var | Default | Used for |
|---|---|---|
| `DATABASE_PATH` | `./todos.db` | SQLite file |
| `APP_TIMEZONE` | `Asia/Kolkata` | What "today" means |
| `PORT` | `8000` | Container only (uvicorn `--port`) |

## Seed data
Inserted at startup **only if the table is empty**. Deadlines are relative to "today" at startup:

| Title | `due_date` | `done` | Expected status |
|---|---|---|---|
| Submit project title | today − 3 | false | `overdue` |
| Write pytest tests | today | false | `due_today` |
| Set up GitHub Actions | today + 1 | false | `due_tomorrow` |
| Prepare demo slides | today + 7 | false | `upcoming` |
| Read FastAPI docs | null | false | `no_deadline` |
| Initialize git repo | today − 2 | true | `done` |

Seed summary: `{"overdue": 1, "due_soon": 2, "open": 5, "done": 1}`

## Testability hook
"Today" comes from a single function, `app.clock.today()`, which returns a `datetime.date` in `APP_TIMEZONE`. The API reads it through a FastAPI dependency (`get_today`), so tests can freeze the date with `app.dependency_overrides[get_today]`. The seed function takes `today` as a parameter.
