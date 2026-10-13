from datetime import datetime

from fastapi.testclient import TestClient

from app.main import app


def test_status_correction_handles_naive_mongodb_datetimes(
    monkeypatch,
):
    """A status-only correction should preserve stored punch timestamps."""
    punch_in = datetime(2026, 7, 6, 4, 0)
    punch_out = datetime(2026, 7, 6, 13, 0)

    original = {
        "_id": "regression-test-record",
        "emp_code": "EMP9001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": 9.0,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    employee = {
        "emp_code": "EMP9001",
        "name": "Regression Test",
        "email": "regression@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05",
    }

    class FakeCollection:
        def __init__(self, document=None):
            self.document = document

        def create_index(self, *args, **kwargs):
            """Simulate index creation during FastAPI startup."""
            return kwargs.get("name", "test_index")

        def find_one(self, query):
            if self.document is None:
                return None

            for key, value in query.items():
                if key.startswith("$"):
                    continue

                if self.document.get(key) != value:
                    return None

            return self.document

        def find_one_and_update(
            self,
            query,
            update,
            return_document=None,
        ):
            self.document.update(update["$set"])

            history_update = update["$push"]["history"]

            self.document.setdefault("history", []).extend(
                history_update
                if isinstance(history_update, list)
                else [history_update]
            )

            return self.document

    employee_collection = FakeCollection(employee)
    attendance_collection = FakeCollection(original)

    monkeypatch.setattr(
        "app.main.db.employees",
        employee_collection,
    )
    monkeypatch.setattr(
        "app.main.db.attendance_logs",
        attendance_collection,
    )

    with TestClient(app) as client:
        response = client.patch(
            "/attendance/EMP9001/2026-07-06",
            json={
                "status": "WFH",
                "reason": "Correct status",
                "regularized_by": "tester",
            },
        )

    assert response.status_code == 200, response.text

    result = response.json()

    assert result["status"] == "WFH"
    assert result["punch_in"] == 1783310400000
    assert result["punch_out"] == 1783342800000

    assert len(result["history"]) == 1
    assert set(result["history"][0]["changes"]) == {"status"}