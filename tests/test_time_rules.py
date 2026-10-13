
from datetime import UTC, datetime

import pytest

from app.main import (
    IST,
    epoch_ms_to_utc,
    get_attendance_date,
    get_shift_bounds,
)


def test_epoch_conversion_truncates_partial_seconds():
    result = epoch_ms_to_utc(1_000_000_000_999)

    assert result == datetime(2001, 9, 9, 1, 46, 40, tzinfo=UTC)
    assert result.microsecond == 0


def test_attendance_date_uses_ist_instead_of_utc_date():
    punch_in = datetime(2026, 7, 6, 20, 0, tzinfo=UTC)

    result = get_attendance_date(punch_in, "09:30", "18:30")

    assert result == "2026-07-07"


@pytest.mark.parametrize(
    ("punch_text", "expected_date"),
    [
        ("2026-07-06T22:00:00+05:30", "2026-07-06"),
        ("2026-07-07T02:00:00+05:30", "2026-07-06"),
        ("2026-07-07T05:59:59+05:30", "2026-07-06"),
        ("2026-07-07T06:00:00+05:30", "2026-07-07"),
        ("2026-08-01T02:00:00+05:30", "2026-07-31"),
    ],
)
def test_overnight_attendance_date(punch_text, expected_date):
    punch_in = datetime.fromisoformat(punch_text)

    assert get_attendance_date(punch_in, "22:00", "06:00") == expected_date


def test_attendance_date_rejects_naive_datetime():
    punch_in = datetime(2026, 7, 6, 9, 30)

    with pytest.raises(ValueError, match="timezone"):
        get_attendance_date(punch_in, "09:30", "18:30")


def test_day_shift_bounds():
    start, end = get_shift_bounds("2026-07-06", "09:30", "18:30")

    assert start == datetime(2026, 7, 6, 9, 30, tzinfo=IST)
    assert end == datetime(2026, 7, 6, 18, 30, tzinfo=IST)


def test_overnight_shift_ends_on_next_day():
    start, end = get_shift_bounds("2026-07-06", "22:00", "06:00")

    assert start == datetime(2026, 7, 6, 22, 0, tzinfo=IST)
    assert end == datetime(2026, 7, 7, 6, 0, tzinfo=IST)
