import pytest
from pydantic import ValidationError

from app.main import EmployeeIn, PunchInIn


@pytest.fixture
def employee_data():
    return {
        "emp_code": "EMP0001",
        "name": "Sample Employee",
        "email": "employee@example.com",
        "department": "Engineering",
        "joined_on": "2026-07-01",
    }


def test_employee_uses_default_shift(employee_data):
    employee = EmployeeIn(**employee_data)

    assert employee.shift_start == "09:30"
    assert employee.shift_end == "18:30"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("emp_code", "EMP123"),
        ("emp_code", "EMP1234567"),
        ("name", ""),
        ("email", "invalid-email"),
        ("department", ""),
        ("shift_start", "24:00"),
        ("shift_end", "18:60"),
        ("joined_on", "2026-02-30"),
        ("joined_on", "2026-7-01"),
    ],
)
def test_invalid_employee_fields_are_rejected(employee_data, field, value):
    employee_data[field] = value

    with pytest.raises(ValidationError):
        EmployeeIn(**employee_data)


def test_equal_shift_times_are_rejected(employee_data):
    employee_data.update(shift_start="09:30", shift_end="09:30")

    with pytest.raises(ValidationError):
        EmployeeIn(**employee_data)


def test_overnight_shift_is_accepted(employee_data):
    employee_data.update(shift_start="22:00", shift_end="06:00")

    employee = EmployeeIn(**employee_data)

    assert employee.shift_start == "22:00"
    assert employee.shift_end == "06:00"


@pytest.mark.parametrize(
    "timestamp",
    [
        1_783_312_500,
        "1783312500000",
        1_783_312_500_000.0,
        True,
        None,
        99_999_999_999,
        4_102_444_800_001,
    ],
)
def test_invalid_punch_timestamps_are_rejected(timestamp):
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", punched_at=timestamp)


@pytest.mark.parametrize(
    "timestamp",
    [100_000_000_000, 1_783_312_500_000, 4_102_444_800_000],
)
def test_valid_punch_timestamps_are_accepted(timestamp):
    request = PunchInIn(emp_code="EMP0001", punched_at=timestamp)

    assert request.punched_at == timestamp


def test_omitted_timestamp_is_allowed():
    request = PunchInIn(emp_code="EMP0001")

    assert request.punched_at is None
    assert request.status == "PRESENT"


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "present"])
def test_invalid_punch_in_status_is_rejected(status):
    with pytest.raises(ValidationError):
        PunchInIn(emp_code="EMP0001", status=status)


@pytest.mark.parametrize("status", ["PRESENT", "WFH", "ON_DUTY"])
def test_presence_status_is_accepted(status):
    request = PunchInIn(emp_code="EMP0001", status=status)

    assert request.status == status


def test_client_supplied_derived_fields_are_ignored():
    request = PunchInIn(
        emp_code="EMP0001",
        work_hours=99,
        late_minutes=0,
        overtime_minutes=999,
        half_day=True,
    )

    assert set(request.model_dump()) == {"emp_code", "punched_at", "status"}