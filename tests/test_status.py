from datetime import date, timedelta

import pytest

from app.status import STATUS_ORDER, compute_status, sort_key, summarize

TODAY = date(2026, 10, 5)


def days(n: int) -> date:
    return TODAY + timedelta(days=n)


@pytest.mark.parametrize(
    ("due", "expected"),
    [
        (days(-30), "overdue"),
        (days(-1), "overdue"),  # yesterday
        (days(0), "due_today"),  # today
        (days(1), "due_tomorrow"),  # tomorrow
        (days(2), "upcoming"),  # first "upcoming" day
        (days(365), "upcoming"),
        (None, "no_deadline"),
    ],
)
def test_status_of_open_todo(due, expected):
    assert compute_status(False, due, TODAY) == expected


@pytest.mark.parametrize("due", [days(-3), days(0), days(1), days(7), None])
def test_done_ignores_the_deadline(due):
    # done with a past deadline is "done", not "overdue"
    assert compute_status(True, due, TODAY) == "done"


def test_status_across_month_and_year_end():
    assert compute_status(False, date(2027, 1, 1), date(2026, 12, 31)) == "due_tomorrow"
    assert compute_status(False, date(2026, 2, 28), date(2026, 3, 1)) == "overdue"


def _todo(id, due, status):
    return {"id": id, "due_date": due, "status": status}


def test_sort_key_orders_by_group_then_date_then_id():
    todos = [
        _todo(1, None, "done"),
        _todo(2, days(-5), "done"),
        _todo(3, None, "no_deadline"),
        _todo(4, days(9), "upcoming"),
        _todo(5, days(3), "upcoming"),
        _todo(6, days(1), "due_tomorrow"),
        _todo(7, days(0), "due_today"),
        _todo(8, days(-1), "overdue"),
        _todo(9, days(-4), "overdue"),
        _todo(10, days(3), "upcoming"),
    ]
    ordered = [t["id"] for t in sorted(todos, key=sort_key)]
    # same date -> lower id first (5 before 10); within done, dated (2) before undated (1)
    assert ordered == [9, 8, 7, 6, 5, 10, 4, 3, 2, 1]


def test_status_order_matches_contract():
    assert STATUS_ORDER == ["overdue", "due_today", "due_tomorrow", "upcoming", "no_deadline", "done"]


def test_summarize():
    statuses = ["overdue", "overdue", "due_today", "due_tomorrow", "upcoming", "no_deadline", "done"]
    assert summarize(statuses) == {"overdue": 2, "due_soon": 2, "open": 6, "done": 1}


def test_summarize_empty():
    assert summarize([]) == {"overdue": 0, "due_soon": 0, "open": 0, "done": 0}
