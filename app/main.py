"""
Employee Attendance & Analytics API
Run: uvicorn app.main:app --port 8000
Configuration: MONGO_URI, MONGO_DB
"""

import calendar
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Self

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")


# --------------------------------------------------------------------------- #
# Database setup
# --------------------------------------------------------------------------- #

def ensure_indexes() -> None:
    """Create required indexes idempotently."""
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employees_department_joined_on",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1), ("late_minutes", 1)],
        name="ix_attendance_employee_date_late",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create required MongoDB indexes when the application starts."""
    ensure_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


@app.get("/health", tags=["system"])
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable",
        ) from None
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Shared date, time, rounding and serialization helpers
# --------------------------------------------------------------------------- #

def epoch_ms_to_utc(timestamp_ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to seconds."""
    return datetime.fromtimestamp(timestamp_ms // 1000, tz=UTC)


def to_epoch_ms(value: datetime | None) -> int | None:
    """Convert a BSON/Python datetime to epoch milliseconds."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def normalize_utc_datetime(value: datetime | None) -> datetime | None:
    """Treat naive datetimes read from BSON as UTC; return an aware UTC value."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_calendar_date(value: str, field_name: str = "date") -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be a valid YYYY-MM-DD date",
        ) from None

    if parsed.isoformat() != value:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must use YYYY-MM-DD format",
        )
    return parsed


def parse_month(value: str) -> tuple[date, date]:
    try:
        if len(value) != 7 or value[4] != "-":
            raise ValueError
        year, month = map(int, value.split("-"))
        first = date(year, month, 1)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        ) from None

    last = date(year, month, calendar.monthrange(year, month)[1])
    return first, last


def round_half_up(value: float | Decimal, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def get_attendance_date(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """Return the IST attendance date, including the overnight exception."""
    if punch_in.utcoffset() is None:
        raise ValueError("punch_in must include timezone information")

    local_punch = punch_in.astimezone(IST).replace(microsecond=0)
    attendance_day = local_punch.date()
    start_time = time.fromisoformat(shift_start)
    end_time = time.fromisoformat(shift_end)

    if end_time <= start_time and local_punch.time() < end_time:
        attendance_day -= timedelta(days=1)

    return attendance_day.isoformat()


def get_shift_bounds(
    attendance_date: str,
    shift_start: str,
    shift_end: str,
) -> tuple[datetime, datetime]:
    """Build timezone-aware IST shift boundaries for an attendance date."""
    attendance_day = date.fromisoformat(attendance_date)
    start_time = time.fromisoformat(shift_start)
    end_time = time.fromisoformat(shift_end)

    start = datetime.combine(attendance_day, start_time, tzinfo=IST)
    end = datetime.combine(attendance_day, end_time, tzinfo=IST)

    if end_time <= start_time:
        end += timedelta(days=1)

    return start, end


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    punch_in = punch_in.replace(microsecond=0)
    attendance_day = get_attendance_date(
        punch_in,
        shift_start,
        shift_end,
    )
    shift_start_dt, _ = get_shift_bounds(
        attendance_day,
        shift_start,
        shift_end,
    )
    elapsed_seconds = int((punch_in - shift_start_dt).total_seconds())

    if elapsed_seconds <= 600:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    punch_in = punch_in.replace(microsecond=0)
    punch_out = punch_out.replace(microsecond=0)
    elapsed_seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(elapsed_seconds) / Decimal(3600)
    return float(
        hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    punch_out = punch_out.replace(microsecond=0)
    _, shift_end_dt = get_shift_bounds(
        attendance_date,
        shift_start,
        shift_end,
    )
    overtime_seconds = int((punch_out - shift_end_dt).total_seconds())

    if overtime_seconds < 1800:
        return 0
    return overtime_seconds // 60


def compute_half_day(work_hours: float | None) -> bool:
    return (
        work_hours is not None
        and Decimal(str(work_hours)) < Decimal("4.50")
    )


def employee_response(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"]),
    }


def attendance_response(doc: dict) -> dict:
    """Return only public contract fields; never expose MongoDB _id."""
    history = []

    for entry in doc.get("history", []):
        changes = {}
        for field, values in entry.get("changes", {}).items():
            old_value = values.get("from")
            new_value = values.get("to")

            if field in ("punch_in", "punch_out"):
                old_value = to_epoch_ms(old_value)
                new_value = to_epoch_ms(new_value)

            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

        history.append({
            "at": to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history,
    }


def mongo_json(value):
    """Convert BSON values in explain output to JSON-compatible values."""
    return json.loads(json_util.dumps(value))


def month_filter(first: date, last: date) -> dict:
    return {
        "date": {
            "$gte": first.isoformat(),
            "$lte": last.isoformat(),
        }
    }


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

EpochMillis = Annotated[
    int,
    Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000),
]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AttendanceStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def validate_shift(self) -> Self:
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis | None = None
    status: PresenceStatus = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def reject_explicit_null(cls, value: int | None) -> int:
        if value is None:
            raise ValueError(
                "punched_at must be epoch milliseconds when supplied"
            )
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: EpochMillis | None = None

    @field_validator("punched_at")
    @classmethod
    def reject_explicit_null(cls, value: int | None) -> int:
        if value is None:
            raise ValueError(
                "punched_at must be epoch milliseconds when supplied"
            )
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: AttendanceStatus | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def reject_explicit_null_punches(
        cls,
        value: int | None,
    ) -> int | None:
        if value is None:
            raise ValueError(
                "punch times must be epoch milliseconds when supplied"
            )
        return value


# --------------------------------------------------------------------------- #
# Employee endpoints
# --------------------------------------------------------------------------- #

@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now_seconds()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None

    return employee_response(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}
    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)
    docs = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [employee_response(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #

def attendance_list_query(
    emp_code: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> dict:
    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from is not None:
        parse_calendar_date(date_from, "date_from")
    if date_to is not None:
        parse_calendar_date(date_to, "date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            422,
            "date_from must be on or before date_to",
        )

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    if status is not None:
        if status not in (*PRESENCE_STATUSES, *NON_PRESENCE_STATUSES):
            raise HTTPException(422, "Invalid attendance status")
        query["status"] = status

    return query


def attendance_list_find(
    query: dict,
    page: int,
    page_size: int,
):
    """Shared find operation for listing and explain."""
    return (
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Unknown employee")

    punch_in_at = (
        epoch_ms_to_utc(body.punched_at)
        if body.punched_at is not None
        else utc_now_seconds()
    )
    attendance_day = get_attendance_date(
        punch_in_at,
        emp["shift_start"],
        emp["shift_end"],
    )

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_day,
        "status": body.status,
        "punch_in": punch_in_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punch_in_at,
            emp["shift_start"],
            emp["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409,
            "Already punched in for this date",
        ) from None

    return attendance_response(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Unknown employee")

    punch_out_at = (
        epoch_ms_to_utc(body.punched_at)
        if body.punched_at is not None
        else utc_now_seconds()
    )

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_at, "$ne": None},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(404, "No qualifying punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "That record is already punched out")

    punch_in_at = record["punch_in"]
    if punch_in_at.tzinfo is None:
        punch_in_at = punch_in_at.replace(tzinfo=UTC)

    elapsed_seconds = (punch_out_at - punch_in_at).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 3600:
        raise HTTPException(
            422,
            "punch-out must be after punch-in and within 24 hours",
        )

    work_hours = compute_work_hours(punch_in_at, punch_out_at)
    overtime_minutes = compute_overtime(
        punch_out_at,
        emp["shift_start"],
        emp["shift_end"],
        record["date"],
    )
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            409,
            "That record has already been punched out",
        )

    return attendance_response(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = attendance_list_query(
        emp_code,
        date_from,
        date_to,
        status,
    )
    total = db.attendance_logs.count_documents(query)
    docs = attendance_list_find(query, page, page_size)

    return {
        "items": [attendance_response(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    parse_calendar_date(date, "date")

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(
            404,
            "Employee or attendance record not found",
        )

    original = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })
    if original is None:
        raise HTTPException(
            404,
            "Employee or attendance record not found",
        )

    status = (
        body.status
        if body.status is not None
        else original["status"]
    )

    punch_in_at = (
        epoch_ms_to_utc(body.punch_in)
        if "punch_in" in body.model_fields_set
        else normalize_utc_datetime(original.get("punch_in"))
    )
    punch_out_at = (
        epoch_ms_to_utc(body.punch_out)
        if "punch_out" in body.model_fields_set
        else normalize_utc_datetime(original.get("punch_out"))
    )

    if status in NON_PRESENCE_STATUSES:
        if (
            "punch_in" in body.model_fields_set
            or "punch_out" in body.model_fields_set
        ):
            raise HTTPException(
                422,
                "ABSENT and LEAVE cannot be supplied with punch times",
            )
        punch_in_at = None
        punch_out_at = None
    elif punch_in_at is None:
        raise HTTPException(
            422,
            "Presence status requires punch_in",
        )

    if punch_in_at is not None:
        computed_date = get_attendance_date(
            punch_in_at,
            emp["shift_start"],
            emp["shift_end"],
        )
        if computed_date != date:
            raise HTTPException(
                422,
                "punch_in must belong to the record attendance date",
            )

    if punch_out_at is not None:
        if punch_in_at is None:
            raise HTTPException(422, "punch_out requires punch_in")

        elapsed = (punch_out_at - punch_in_at).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 3600:
            raise HTTPException(
                422,
                "punch_out must be after punch_in and within 24 hours",
            )

    if status in NON_PRESENCE_STATUSES:
        work_hours = None
        late_minutes = 0
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(
            punch_in_at,
            emp["shift_start"],
            emp["shift_end"],
        )

        if punch_out_at is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            work_hours = compute_work_hours(
                punch_in_at,
                punch_out_at,
            )
            overtime_minutes = compute_overtime(
                punch_out_at,
                emp["shift_start"],
                emp["shift_end"],
                date,
            )
            half_day = compute_half_day(work_hours)

    final_values = {
        "status": status,
        "punch_in": punch_in_at,
        "punch_out": punch_out_at,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    changes = {}
    for field, new_value in final_values.items():
        old_value = original.get(field)

        if field in ("punch_in", "punch_out"):
            old_value = normalize_utc_datetime(old_value)
        elif field in ("late_minutes", "overtime_minutes"):
            old_value = original.get(field, 0)
        elif field == "half_day":
            old_value = original.get(field, False)

        if old_value != new_value:
            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(
            422,
            "The request does not change the record",
        )

    history_entry = {
        "at": utc_now_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: match the values read above as well as the record.
    concurrency_filter = {
        "_id": original["_id"],
        "status": original["status"],
    }

    for field in ("punch_in", "punch_out"):
        if field in original:
            concurrency_filter[field] = original[field]
        else:
            concurrency_filter[field] = {"$exists": False}

    if "history" in original:
        concurrency_filter["history"] = original["history"]
    else:
        concurrency_filter["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        concurrency_filter,
        {
            "$set": final_values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            409,
            "Record changed concurrently; retry the correction",
        )

    return attendance_response(updated)


# --------------------------------------------------------------------------- #
# Employee monthly analytics
# --------------------------------------------------------------------------- #

def employee_monthly_pipeline(
    emp_code: str,
    first: date,
    last: date,
) -> list[dict]:
    return [
        {"$match": {
            "emp_code": emp_code,
            "date": {
                "$gte": first.isoformat(),
                "$lte": last.isoformat(),
            },
        }},
        {"$addFields": {
            "_parsed_date": {
                "$dateFromString": {
                    "dateString": "$date",
                    "format": "%Y-%m-%d",
                }
            },
        }},
        {"$addFields": {
            "_weekday": {"$dayOfWeek": "$_parsed_date"},
            "_presence": {
                "$in": ["$status", list(PRESENCE_STATUSES)]
            },
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    "$_presence",
                    {"$in": ["$_weekday", [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [
                    {"$eq": ["$half_day", True]},
                    0.5,
                    1,
                ]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [
                {"$eq": ["$status", "LEAVE"]},
                1,
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                1,
                0,
            ]}},
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            },
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "Unknown employee")

    first, last = parse_month(month)
    joined = date.fromisoformat(employee["joined_on"])
    effective_start = max(first, joined)

    working_days = (
        sum(
            1
            for offset in range((last - effective_start).days + 1)
            if is_weekday(effective_start + timedelta(days=offset))
        )
        if effective_start <= last
        else 0
    )

    result = list(
        db.attendance_logs.aggregate(
            employee_monthly_pipeline(emp_code, first, last)
        )
    )
    summary = result[0] if result else {}

    present_days = summary.get("present_days", 0)
    attendance_pct = (
        round_half_up(
            Decimal(str(present_days)) / Decimal(working_days) * 100,
            2,
        )
        if working_days
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# --------------------------------------------------------------------------- #
# Department summary
# --------------------------------------------------------------------------- #

def department_summary_pipeline(
    first: date,
    last: date,
    department: str | None,
) -> list[dict]:
    employee_match = {"joined_on": {"$lte": last.isoformat()}}
    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"employee_code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", first.isoformat()]},
                    {"$lte": ["$date", last.isoformat()]},
                ]}},
                },
            ],
            "as": "logs",
        }},
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True,
        }},
        {"$addFields": {
            "_parsed_date": {"$cond": [
                {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                {"$dateFromString": {
                    "dateString": "$logs.date",
                    "format": "%Y-%m-%d",
                }},
                None,
            ]},
        }},
        {"$addFields": {
            "_weekday": {"$cond": [
                {"$ne": ["$_parsed_date", None]},
                {"$dayOfWeek": "$_parsed_date"},
                None,
            ]},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                    {"$in": ["$_weekday", [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [
                    {"$eq": ["$logs.half_day", True]},
                    0.5,
                    1,
                ]},
                0,
            ]}},
            "avg_work_hours": {"$avg": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                    {"$ne": [
                        {"$ifNull": ["$logs.work_hours", None]},
                        None,
                    ]},
                ]},
                "$logs.work_hours",
                None,
            ]}},
            "late_count": {"$sum": {"$cond": [
                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                1,
                0,
            ]}},
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
            },
            "leave_count": {"$sum": {"$cond": [
                {"$eq": ["$logs.status", "LEAVE"]},
                1,
                0,
            ]}},
            "on_duty_count": {"$sum": {"$cond": [
                {"$eq": ["$logs.status", "ON_DUTY"]},
                1,
                0,
            ]}},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$headcount"},
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$eq": ["$avg_work_hours", None]},
                None,
                {"$round": ["$avg_work_hours", 2]},
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: str | None = None,
):
    first, last = parse_month(month)
    items = list(
        db.employees.aggregate(
            department_summary_pipeline(first, last, department)
        )
    )
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Late leaderboard
# --------------------------------------------------------------------------- #

def leaderboard_pipeline(
    first: date,
    last: date,
    limit: int,
    department: str | None,
) -> list[dict]:
    pipeline = [
        {"$match": {
            "date": {
                "$gte": first.isoformat(),
                "$lte": last.isoformat(),
            },
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1},
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append({
            "$match": {"employee.department": department}
        })

    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {
            "total_late_minutes": -1,
            "_id": 1,
        }},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$employee.name",
            "department": "$employee.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])

    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    limit: int = Query(default=10, ge=1, le=50),
    department: str | None = None,
):
    first, last = parse_month(month)
    items = list(
        db.attendance_logs.aggregate(
            leaderboard_pipeline(first, last, limit, department)
        )
    )
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Department trend
# --------------------------------------------------------------------------- #

def _round_nonnegative_half_up(expression: dict, places: int) -> dict:
    """
    MongoDB expression for ROUND_HALF_UP on a non-negative number.

    Trend rates are non-negative. Scaling, adding 0.5, flooring and
    scaling back avoids MongoDB $round's ties-to-even behavior.
    """
    scale = 10 ** places
    return {
        "$divide": [
            {
                "$floor": {
                    "$add": [
                        {"$multiply": [expression, scale]},
                        0.5,
                    ]
                }
            },
            scale,
        ]
    }


def department_trend_pipeline(
    department: str,
    first: date,
    last: date,
) -> list[dict]:
    """
    Generate every requested calendar day and calculate the trend entirely
    in MongoDB, including when no attendance records exist in the date range.
    """
    first_datetime = datetime.combine(first, time.min)
    day_count = (last - first).days + 1

    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0}},
        {"$set": {
            "days": {
                "$map": {
                    "input": {"$range": [0, day_count]},
                    "as": "offset",
                    "in": {
                        "$dateAdd": {
                            "startDate": first_datetime,
                            "unit": "day",
                            "amount": "$$offset",
                        }
                    },
                }
            }
        }},
        {"$unwind": "$days"},
        {"$lookup": {
            "from": "employees",
            "let": {"trend_date": "$days"},
            "pipeline": [
                {"$match": {"department": department}},
                {"$match": {"$expr": {
                    "$lte": [
                        "$joined_on",
                        {"$dateToString": {
                            "date": "$$trend_date",
                            "format": "%Y-%m-%d",
                            "timezone": "UTC",
                        }},
                    ]
                }}},
                {"$count": "count"},
            ],
            "as": "_headcount",
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"trend_date": "$days"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$eq": [
                        "$date",
                        {"$dateToString": {
                            "date": "$$trend_date",
                            "format": "%Y-%m-%d",
                            "timezone": "UTC",
                        }},
                    ]
                }}},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "_employee",
                }},
                {"$unwind": "$_employee"},
                {"$match": {
                    "_employee.department": department,
                }},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {
                            "$in": [
                                "$status",
                                list(PRESENCE_STATUSES),
                            ]
                        },
                        {"$cond": [
                            {
                                "$eq": [
                                    {"$ifNull": ["$half_day", False]},
                                    True,
                                ]
                            },
                            0.5,
                            1,
                        ]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [
                        {
                            "$gt": [
                                {"$ifNull": ["$late_minutes", 0]},
                                0,
                            ]
                        },
                        1,
                        0,
                    ]}},
                }},
            ],
            "as": "_daily",
        }},
        {"$set": {
            "date": {"$dateToString": {
                "date": "$days",
                "format": "%Y-%m-%d",
                "timezone": "UTC",
            }},
            "headcount": {"$ifNull": [
                {"$arrayElemAt": ["$_headcount.count", 0]},
                0,
            ]},
            "present_count": {"$ifNull": [
                {"$arrayElemAt": ["$_daily.present_count", 0]},
                0,
            ]},
            "late_count": {"$ifNull": [
                {"$arrayElemAt": ["$_daily.late_count", 0]},
                0,
            ]},
            "is_working_day": {"$in": [
                {"$dayOfWeek": "$days"},
                [2, 3, 4, 5, 6],
            ]},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": [
                    "$is_working_day",
                    {"$gt": ["$headcount", 0]},
                ]},
                _round_nonnegative_half_up(
                    {"$divide": ["$present_count", "$headcount"]},
                    4,
                ),
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"days": 1},
            "output": {
                "_moving_sum": {
                    "$sum": {
                        "$ifNull": ["$attendance_rate", 0]
                    },
                    "window": {"documents": [-6, "current"]},
                },
                "_moving_count": {
                    "$sum": {"$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        1,
                        0,
                    ]},
                    "window": {"documents": [-6, "current"]},
                },
            },
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": {"$cond": [
                {"$gt": ["$_moving_count", 0]},
                _round_nonnegative_half_up(
                    {
                        "$divide": [
                            "$_moving_sum",
                            "$_moving_count",
                        ]
                    },
                    4,
                ),
                None,
            ]},
        }},
        {"$sort": {"date": 1}},
    ]


@app.get(
    "/analytics/departments/{department}/trend",
    tags=["analytics"],
)
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    first = parse_calendar_date(from_date, "from")
    last = parse_calendar_date(to_date, "to")

    if last < first:
        raise HTTPException(422, "to must be on or after from")
    if (last - first).days + 1 > 92:
        raise HTTPException(
            422,
            "The requested range cannot exceed 92 days",
        )

    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "Unknown department")

    items = list(
        db.employees.aggregate(
            department_trend_pipeline(department, first, last)
        )
    )

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain endpoint
# --------------------------------------------------------------------------- #

@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str,
    emp_code: str | None = None,
    month: str | None = Query(
        default=None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    from_date: str | None = Query(default=None, alias="from"),
    to_date: str | None = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(422, "Unsupported endpoint")

    if endpoint == "attendance_list":
        query = attendance_list_query(
            emp_code,
            date_from,
            date_to,
            status,
        )

        explain = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        })
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                422,
                "emp_code and month are required",
            )

        first, last = parse_month(month)
        pipeline = employee_monthly_pipeline(
            emp_code,
            first,
            last,
        )

        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")

        first, last = parse_month(month)
        pipeline = department_summary_pipeline(
            first,
            last,
            department,
        )

        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        collection = "employees"

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")

        first, last = parse_month(month)
        pipeline = leaderboard_pipeline(
            first,
            last,
            limit,
            department,
        )

        explain = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        collection = "attendance_logs"

    else:
        if not department or not from_date or not to_date:
            raise HTTPException(
                422,
                "department, from, and to are required",
            )

        first = parse_calendar_date(from_date, "from")
        last = parse_calendar_date(to_date, "to")

        if last < first or (last - first).days + 1 > 92:
            raise HTTPException(422, "Invalid trend date range")

        if db.employees.count_documents({"department": department}) == 0:
            raise HTTPException(404, "Unknown department")

        pipeline = department_trend_pipeline(
            department,
            first,
            last,
        )

        explain = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        collection = "employees"

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": mongo_json(explain),
    }