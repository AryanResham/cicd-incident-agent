from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import clock, db
from app.main import create_app, get_today

# Every test runs on this frozen "today", whatever the real date is.
FROZEN_TODAY = date(2026, 10, 5)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A fresh, empty SQLite file per test (also exported as DATABASE_PATH)."""
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Kolkata")
    return path


@pytest.fixture
def conn(db_path):
    db.init_db(db_path)
    connection = db.connect(db_path)
    yield connection
    connection.close()


@pytest.fixture
def client(db_path, monkeypatch):
    """API client on a temp DB, seeded at startup, with "today" frozen."""
    monkeypatch.setattr(clock, "today", lambda moment=None: FROZEN_TODAY)  # used by the startup seed
    app = create_app()
    app.dependency_overrides[get_today] = lambda: FROZEN_TODAY  # used by the routes
    with TestClient(app) as test_client:  # "with" runs the lifespan (init DB + seed)
        yield test_client


@pytest.fixture
def empty_client(client, db_path):
    """Same as `client`, but with the seed rows removed."""
    connection = db.connect(db_path)
    connection.execute("DELETE FROM todos")
    connection.commit()
    connection.close()
    return client
