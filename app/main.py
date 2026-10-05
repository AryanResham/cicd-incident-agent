"""FastAPI app: the to-do JSON API (docs/API.md) plus the static page at /."""

import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, StrictBool, StringConstraints

from app import clock, db
from app.seed import seed_if_empty
from app.status import compute_status, sort_key, summarize

INDEX_HTML = Path(__file__).parent / "static" / "index.html"


# ---------- request / response models ----------

def parse_due_date(value):
    """Accept only null or a real 'YYYY-MM-DD' date (2026-02-30 is rejected)."""
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("due_date must be a date string like 2026-10-05")
    return date.fromisoformat(value)  # raises ValueError for impossible dates


Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
DueDate = Annotated[date | None, BeforeValidator(parse_due_date)]


class TodoCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title
    due_date: DueDate = None


class TodoUpdate(BaseModel):
    """PATCH body: every field is optional; only the fields sent are changed."""

    model_config = ConfigDict(extra="forbid")

    # A default of None means "not sent"; an explicit null title/done is still a 422.
    title: Title = None
    due_date: DueDate = None  # null clears the deadline
    done: StrictBool = None


class Todo(BaseModel):
    id: int
    title: str
    done: bool
    due_date: date | None
    status: str
    created_at: str
    updated_at: str


class Summary(BaseModel):
    overdue: int
    due_soon: int
    open: int
    done: int


# ---------- dependencies ----------

def get_today() -> date:
    """Today's date; tests replace this with app.dependency_overrides."""
    return clock.today()


def get_conn(request: Request):
    """One SQLite connection per request."""
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Today = Annotated[date, Depends(get_today)]


# ---------- helpers ----------

def with_status(todo: dict, today: date) -> dict:
    due = date.fromisoformat(todo["due_date"]) if todo["due_date"] else None
    return {**todo, "due_date": due, "status": compute_status(todo["done"], due, today)}


def all_with_status(conn: sqlite3.Connection, today: date) -> list[dict]:
    return [with_status(todo, today) for todo in db.list_todos(conn)]


def not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Todo not found")


# ---------- routes ----------

router = APIRouter()


@router.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX_HTML)


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/api/todos", response_model=list[Todo])
def list_todos(conn: Conn, today: Today):
    return sorted(all_with_status(conn, today), key=sort_key)


# Declared before /api/todos/{todo_id} so "summary" isn't read as an id.
@router.get("/api/todos/summary", response_model=Summary)
def todo_summary(conn: Conn, today: Today):
    return summarize([todo["status"] for todo in all_with_status(conn, today)])


@router.get("/api/todos/{todo_id}", response_model=Todo)
def get_todo(todo_id: int, conn: Conn, today: Today):
    todo = db.get_todo(conn, todo_id)
    if todo is None:
        raise not_found()
    return with_status(todo, today)


@router.post("/api/todos", response_model=Todo, status_code=201)
def create_todo(body: TodoCreate, conn: Conn, today: Today):
    due = body.due_date.isoformat() if body.due_date else None
    return with_status(db.create_todo(conn, body.title, due_date=due), today)


@router.patch("/api/todos/{todo_id}", response_model=Todo)
def update_todo(todo_id: int, body: TodoUpdate, conn: Conn, today: Today):
    changes = body.model_dump(mode="json", exclude_unset=True)  # dates become "YYYY-MM-DD"
    todo = db.update_todo(conn, todo_id, changes)
    if todo is None:
        raise not_found()
    return with_status(todo, today)


@router.delete("/api/todos/{todo_id}", status_code=204)
def delete_todo(todo_id: int, conn: Conn):
    if not db.delete_todo(conn, todo_id):
        raise not_found()
    return Response(status_code=204)


# ---------- app ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the table and add the demo todos if the DB is empty.
    path = db.database_path()
    db.init_db(path)
    conn = db.connect(path)
    try:
        seed_if_empty(conn, clock.today())
    finally:
        conn.close()
    app.state.db_path = path
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="To-Do", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
