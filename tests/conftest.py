from datetime import date

import pytest

from app import db

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
