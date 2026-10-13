"""
Focused live-MongoDB analytics verification for the HROne assignment.

Run only this file:
    $env:MONGO_DB = "attendance_verification_20261013"
    python -m pytest -v tests/test_populated_analytics_manual.py

The fixture drops ONLY the named verification database before/after the run.
It never drops attendance_db unless you deliberately change the database name.
"""

import os
from datetime import UTC, datetime

# Set the environment before importing app.main.
# MONGO_URI still comes from the real environment or local .env file.
os.environ["MONGO_DB"] = "attendance_verification_20261013"

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main

VERIFICATION_DB_NAME = "attendance_verification_20261013"


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def seed_data(db) -> None:
    db.employees.insert_many(
        [
            {
                "emp_code": "EMP1001",
                "name": "Asha Rao",
                "email": "asha1001@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-07-01",
                "created_at": utc("2026-07-01T04:00:00"),
            },
            {
                "emp_code": "EMP1002",
                "name": "Bharat Shah",
                "email": "bharat1002@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-07-09",
                "created_at": utc("2026-07-09T04:00:00"),
            },
            {
                "emp_code": "EMP1003",
                "name": "Chitra Das",
                "email": "chitra1003@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-08-01",
                "created_at": utc("2026-08-01T04:00:00"),
            },
            {
                "emp_code": "EMP1004",
                "name": "Dev Mehta",
                "email": "dev1004@example.com",
                "department": "HR",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-07-01",
                "created_at": utc("2026-07-01T04:00:00"),
            },
            {
                "emp_code": "EMP1005",
                "name": "Esha Jain",
                "email": "esha1005@example.com",
                "department": "Sales",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-07-01",
                "created_at": utc("2026-07-01T04:00:00"),
            },
        ]
    )

    # Derived fields are deliberately inserted as stored values: analytics
    # must read these values instead of recalculating them from punch times.
    db.attendance_logs.insert_many(
        [
            {
                "emp_code": "EMP1001",
                "date": "2026-07-06",
                "status": "PRESENT",
                "punch_in": utc("2026-07-06T04:05:00"),
                "punch_out": utc("2026-07-06T13:10:00"),
                "work_hours": 9.12,
                "late_minutes": 35,
                "overtime_minutes": 40,
                "half_day": False,
            },
            {
                "emp_code": "EMP1001",
                "date": "2026-07-07",
                "status": "WFH",
                "punch_in": utc("2026-07-07T04:00:00"),
                "punch_out": utc("2026-07-07T07:30:00"),
                "work_hours": 3.50,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": True,
            },
            {
                "emp_code": "EMP1001",
                "date": "2026-07-08",
                "status": "ABSENT",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            },
            {
                "emp_code": "EMP1001",
                "date": "2026-07-09",
                "status": "LEAVE",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
            },
            {
                "emp_code": "EMP1001",
                "date": "2026-07-11",
                "status": "PRESENT",
                "punch_in": utc("2026-07-11T04:00:00"),
                "punch_out": utc("2026-07-11T11:57:36"),
                "work_hours": 7.96,
                "late_minutes": 20,
                "overtime_minutes": 30,
                "half_day": False,
            },
            {
                "emp_code": "EMP1002",
                "date": "2026-07-09",
                "status": "ON_DUTY",
                "punch_in": utc("2026-07-09T04:00:00"),
                "punch_out": utc("2026-07-09T12:00:00"),
                "work_hours": 8.00,
                "late_minutes": 55,
                "overtime_minutes": 0,
                "half_day": False,
            },
            {
                "emp_code": "EMP1005",
                "date": "2026-07-06",
                "status": "PRESENT",
                "punch_in": utc("2026-07-06T04:00:00"),
                "punch_out": utc("2026-07-06T12:00:00"),
                "work_hours": 8.00,
                "late_minutes": 40,
                "overtime_minutes": 0,
                "half_day": False,
            },
        ]
    )


@pytest.fixture(scope="module")
def api():
    mongo_uri = os.getenv(
        "MONGO_URI",
        "mongodb://localhost:27017",
    )

    # Connect explicitly to the isolated verification database.
    verification_client = MongoClient(mongo_uri)
    db = verification_client[VERIFICATION_DB_NAME]

    # Safety check: never run these tests against attendance_db.
    assert db.name == VERIFICATION_DB_NAME, (
        f"Safety check failed: expected isolated verification DB, "
        f"got {db.name!r}"
    )

    original_db = main.db
    original_client = main.client

    try:
        # Override the application's database references for these tests.
        main.client = verification_client
        main.db = db

        db.client.drop_database(db.name)
        seed_data(db)

        with TestClient(main.app) as client:
            yield client

    finally:
        # Only the isolated verification database is dropped.
        verification_client.drop_database(VERIFICATION_DB_NAME)

        # Restore the application's original references.
        main.db = original_db
        main.client = original_client

        verification_client.close()


def test_employee_monthly_expected_values(api):
    response = api.get(
        "/analytics/employees/EMP1001/monthly",
        params={"month": "2026-07"},
    )

    assert response.status_code == 200, response.text

    body = response.json()

    assert body == {
        "emp_code": "EMP1001",
        "month": "2026-07",
        "working_days": 23,
        "present_days": 1.5,
        "leave_days": 1,
        "late_count": 2,
        "total_late_minutes": 55,
        "total_overtime_minutes": 70,
        # R8 says rates use 4 decimal places, half-up.
        "attendance_pct": 6.5217,
    }


def test_department_summary_includes_zero_log_and_midmonth_employees(api):
    response = api.get(
        "/analytics/departments/summary",
        params={"month": "2026-07"},
    )

    assert response.status_code == 200, response.text

    items = {
        item["department"]: item
        for item in response.json()["items"]
    }

    engineering = items["Engineering"]

    assert engineering["headcount"] == 2  # EMP1003 joins after July.
    assert engineering["present_days"] == 2.5

    # Mean is (9.12 + 3.50 + 7.96 + 8.00) / 4 = 7.145.
    # R8 requires half-up => 7.15 (MongoDB $round uses ties-to-even).
    assert engineering["avg_work_hours"] == 7.15

    assert engineering["late_count"] == 3
    assert engineering["total_late_minutes"] == 110
    assert engineering["leave_count"] == 1
    assert engineering["on_duty_count"] == 1

    hr = items["HR"]

    assert hr["headcount"] == 1  # EMP1004 has no attendance logs.
    assert hr["present_days"] == 0
    assert hr["avg_work_hours"] is None
    assert hr["late_count"] == 0
    assert hr["leave_count"] == 0


def test_leaderboard_tie_at_limit_returns_both_rank_one_rows(api):
    response = api.get(
        "/analytics/leaderboard/late",
        params={"month": "2026-07", "limit": 1},
    )

    assert response.status_code == 200, response.text

    items = response.json()["items"]

    # Asha: 35 + 20 = 55; Bharat: 55; Esha: 40.
    assert [
        (item["emp_code"], item["rank"], item["total_late_minutes"])
        for item in items
    ] == [
        ("EMP1001", 1, 55),
        ("EMP1002", 1, 55),
    ]


def test_department_trend_gap_fill_weekends_headcount_and_moving_average(api):
    response = api.get(
        "/analytics/departments/Engineering/trend",
        params={
            "from": "2026-07-06",
            "to": "2026-07-12",
        },
    )

    assert response.status_code == 200, response.text

    items = response.json()["items"]

    assert len(items) == 7

    assert [item["date"] for item in items] == [
        "2026-07-06",
        "2026-07-07",
        "2026-07-08",
        "2026-07-09",
        "2026-07-10",
        "2026-07-11",
        "2026-07-12",
    ]

    assert [item["headcount"] for item in items] == [
        1, 1, 1, 2, 2, 2, 2,
    ]

    assert [item["present_count"] for item in items] == [
        1, 0.5, 0, 1, 0, 1, 0,
    ]

    assert [item["late_count"] for item in items] == [
        1, 0, 0, 1, 0, 1, 0,
    ]

    assert [item["attendance_rate"] for item in items] == [
        1.0, 0.5, 0.0, 0.5, 0.0, None, None,
    ]

    assert [item["moving_avg_7d"] for item in items] == [
        1.0, 0.75, 0.5, 0.5, 0.4, 0.4, 0.4,
    ]

    assert [item["is_working_day"] for item in items] == [
        True, True, True, True, True, False, False,
    ]


def test_unknown_department_returns_404(api):
    response = api.get(
        "/analytics/departments/DoesNotExist/trend",
        params={
            "from": "2026-07-06",
            "to": "2026-07-12",
        },
    )

    assert response.status_code == 404, response.text