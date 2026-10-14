
"""Real MongoDB concurrency tests using an isolated verification database.

Run:
    python -m pytest -v tests/test_mongodb_concurrency.py

This file drops ONLY its dedicated verification database.
It never drops attendance_db or the analytics verification database.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Configure the database before importing the application.
VERIFICATION_DB_NAME = "attendance_concurrency_verification_20261014"
os.environ["MONGO_DB"] = VERIFICATION_DB_NAME

from app import main


def epoch_ms(value: str) -> int:
    """Convert an ISO UTC timestamp to epoch milliseconds."""
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


@pytest.fixture(scope="module")
def concurrency_api():
    mongo_uri = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    verification_client = MongoClient(
        mongo_uri,
        serverSelectionTimeoutMS=5000,
    )
    verification_db = verification_client[VERIFICATION_DB_NAME]

    # Confirm MongoDB is reachable and the database name is correct.
    verification_client.admin.command("ping")
    assert verification_db.name == VERIFICATION_DB_NAME

    original_db = main.db
    original_client = main.client

    try:
        # Only this dedicated verification database is cleared.
        verification_client.drop_database(VERIFICATION_DB_NAME)

        main.client = verification_client
        main.db = verification_db
        main.ensure_indexes()

        verification_db.employees.insert_one(
            {
                "emp_code": "CONC001",
                "name": "Concurrency Test Employee",
                "email": "conc001@example.com",
                "department": "Engineering",
                "shift_start": "09:30",
                "shift_end": "18:30",
                "joined_on": "2026-01-01",
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        )

        with TestClient(main.app) as api:
            yield api, verification_db

    finally:
        # Drop only this test's dedicated database.
        verification_client.drop_database(VERIFICATION_DB_NAME)
        main.db = original_db
        main.client = original_client
        verification_client.close()


def concurrent_requests(api, requests):
    """Issue independent HTTP requests concurrently."""

    def send(request):
        method, url, payload = request
        return api.request(method, url, json=payload)

    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        return list(executor.map(send, requests))


def test_concurrent_punch_ins_create_exactly_one_record(concurrency_api):
    api, db = concurrency_api
    db.attendance_logs.delete_many({"emp_code": "CONC001"})

    punch_time = epoch_ms("2026-10-14T04:00:00")

    requests = [
        (
            "POST",
            "/attendance/punch-in",
            {"emp_code": "CONC001", "punched_at": punch_time},
        )
        for _ in range(10)
    ]

    responses = concurrent_requests(api, requests)
    status_codes = [response.status_code for response in responses]

    assert status_codes.count(201) == 1, status_codes
    assert status_codes.count(409) == 9, status_codes
    assert (
        db.attendance_logs.count_documents(
            {"emp_code": "CONC001", "date": "2026-10-14"}
        )
        == 1
    )


def test_concurrent_punch_out_succeeds_exactly_once(concurrency_api):
    api, db = concurrency_api
    db.attendance_logs.delete_many({"emp_code": "CONC001"})

    punch_in_time = epoch_ms("2026-10-14T04:00:00")
    punch_out_time = epoch_ms("2026-10-14T13:00:00")

    punch_in_response = api.post(
        "/attendance/punch-in",
        json={"emp_code": "CONC001", "punched_at": punch_in_time},
    )
    assert punch_in_response.status_code == 201, punch_in_response.text

    requests = [
        (
            "POST",
            "/attendance/punch-out",
            {"emp_code": "CONC001", "punched_at": punch_out_time},
        )
        for _ in range(2)
    ]

    responses = concurrent_requests(api, requests)
    status_codes = [response.status_code for response in responses]

    response_details = [(r.status_code, r.text) for r in responses]
    assert status_codes.count(200) == 1, response_details
    assert status_codes.count(409) == 1, response_details

    record = db.attendance_logs.find_one(
        {"emp_code": "CONC001", "date": "2026-10-14"}
    )
    assert record is not None
    assert record["punch_out"] is not None
    assert record["work_hours"] is not None


def test_concurrent_regularizations_do_not_lose_history(concurrency_api):
    api, db = concurrency_api
    db.attendance_logs.delete_many({"emp_code": "CONC001"})

    punch_in_time = epoch_ms("2026-10-14T04:00:00")

    punch_in_response = api.post(
        "/attendance/punch-in",
        json={"emp_code": "CONC001", "punched_at": punch_in_time},
    )
    assert punch_in_response.status_code == 201, punch_in_response.text

    requests = [
        (
            "PATCH",
            "/attendance/CONC001/2026-10-14",
            {
                "status": "WFH",
                "reason": "Concurrent correction A",
                "regularized_by": "test-admin-A",
            },
        ),
        (
            "PATCH",
            "/attendance/CONC001/2026-10-14",
            {
                "status": "ON_DUTY",
                "reason": "Concurrent correction B",
                "regularized_by": "test-admin-B",
            },
        ),
    ]

    responses = concurrent_requests(api, requests)
    status_codes = [response.status_code for response in responses]

    response_details = [(r.status_code, r.text) for r in responses]
    assert status_codes.count(200) == 1, response_details
    assert status_codes.count(409) == 1, response_details

    record = db.attendance_logs.find_one(
        {"emp_code": "CONC001", "date": "2026-10-14"}
    )
    assert record is not None

    # The initial record has empty history; the one successful correction
    # must append exactly one history entry.
    assert len(record.get("history", [])) == 1
    assert record["history"][0]["by"] in {
        "test-admin-A",
        "test-admin-B",
    }
