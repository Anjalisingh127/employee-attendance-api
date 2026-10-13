
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app import main


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = deepcopy(documents or [])
        self.update_calls = 0
        self.fail_next_update = False

    def create_index(self, *args, **kwargs):
        return kwargs.get("name", "fake_index")

    def find_one(self, query):
        for document in self.documents:
            if self._matches(document, query):
                return deepcopy(document)
        return None

    def find_one_and_update(
        self, query, update, return_document=None
    ):
        self.update_calls += 1

        if self.fail_next_update:
            self.fail_next_update = False
            return None

        for document in self.documents:
            if self._matches(document, query):
                document.update(deepcopy(update.get("$set", {})))

                for field, value in update.get("$push", {}).items():
                    document.setdefault(field, []).append(deepcopy(value))

                return deepcopy(document)

        return None

    @staticmethod
    def _matches(document, query):
        for field, expected in query.items():
            if isinstance(expected, dict) and "$exists" in expected:
                if (field in document) != expected["$exists"]:
                    return False
            elif field not in document or document[field] != expected:
                return False
        return True


class FakeDatabase:
    def __init__(self, employee, attendance):
        self.employees = FakeCollection([employee])
        self.attendance_logs = FakeCollection([attendance])

    def command(self, command):
        return {"ok": 1}


@pytest.fixture
def correction_client(monkeypatch):
    employee = {
        "_id": "employee-id",
        "emp_code": "EMP0001",
        "name": "Sample Employee",
        "email": "employee@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-01",
        "shift_start": "09:30",
        "shift_end": "18:30",
    }

    attendance = {
        "_id": "attendance-id",
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 6, 4, 0, tzinfo=UTC),
        "punch_out": datetime(2026, 7, 6, 13, 0, tzinfo=UTC),
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    fake_db = FakeDatabase(employee, attendance)
    monkeypatch.setattr(main, "db", fake_db)

    with TestClient(main.app) as client:
        yield client, fake_db


def test_successful_correction_appends_one_history_entry(
    correction_client,
):
    client, fake_db = correction_client

    response = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "WFH",
            "reason": "Correct work status",
            "regularized_by": "admin",
        },
    )

    assert response.status_code == 200
    result = response.json()

    assert result["status"] == "WFH"
    assert len(result["history"]) == 1
    assert result["history"][0]["by"] == "admin"
    assert result["history"][0]["reason"] == "Correct work status"
    assert result["history"][0]["changes"] == {
        "status": {"from": "PRESENT", "to": "WFH"}
    }
    assert "_id" not in result
    assert len(fake_db.attendance_logs.documents[0]["history"]) == 1


def test_no_op_correction_returns_422_without_updating(
    correction_client,
):
    client, fake_db = correction_client

    response = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "PRESENT",
            "reason": "No actual change",
            "regularized_by": "admin",
        },
    )

    assert response.status_code == 422
    assert fake_db.attendance_logs.update_calls == 0
    assert fake_db.attendance_logs.documents[0]["history"] == []


def test_concurrent_change_returns_409(correction_client):
    client, fake_db = correction_client
    fake_db.attendance_logs.fail_next_update = True

    response = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "WFH",
            "reason": "Correct work status",
            "regularized_by": "admin",
        },
    )

    assert response.status_code == 409
    assert fake_db.attendance_logs.documents[0]["status"] == "PRESENT"
    assert fake_db.attendance_logs.documents[0]["history"] == []


def test_correction_handles_legacy_record_without_history(
    correction_client,
):
    client, fake_db = correction_client
    document = fake_db.attendance_logs.documents[0]
    document.pop("history")

    response = client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "WFH",
            "reason": "Correct work status",
            "regularized_by": "admin",
        },
    )

    assert response.status_code == 200
    assert len(response.json()["history"]) == 1
    assert len(fake_db.attendance_logs.documents[0]["history"]) == 1
