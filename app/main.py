"""Employee Attendance & Analytics API.

Run: uvicorn app.main:app --port 8000
Environment: MONGO_URI and MONGO_DB (a local .env is loaded for convenience).
"""

import os
import re
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # Existing process environment variables take precedence over .env.

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}

logger = logging.getLogger(__name__)

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=5_000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes() -> None:
    """Create required indexes idempotently; unique indexes enforce race safety."""
    db.employees.create_index([("emp_code", 1)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="ix_employee_joined_department")
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_employee_date"
    )
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], name="ix_attendance_employee_date_desc")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="ix_attendance_status_date_employee")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # The process must still start when MongoDB is temporarily unavailable so
    # /health can correctly report 503; indexes are retried on a successful ping.
    try:
        create_indexes()
    except PyMongoError:
        logger.exception("MongoDB unavailable while creating startup indexes")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Validation and date/time helpers
# --------------------------------------------------------------------------- #
def validate_date_string(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return value


def validate_shift_time(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("shift time must be HH:MM in 24-hour format")
    return value


def truncate_to_seconds(value: datetime) -> datetime:
    """Return an aware UTC instant truncated (not rounded) to whole seconds."""
    if value.tzinfo is None:
        # PyMongo can return naive UTC values for older call sites/data.
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def epoch_ms_to_datetime(value: int) -> datetime:
    """Validate an epoch-millisecond integer and convert it to a truncated UTC instant."""
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError) as exc:
        raise HTTPException(422, "timestamp is out of range") from exc


def datetime_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    value = truncate_to_seconds(value)
    return int(value.timestamp() * 1000)


def shift_datetime(attendance_date: str, shift_time: str) -> datetime:
    day = date.fromisoformat(attendance_date)
    hour, minute = map(int, shift_time.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST).astimezone(UTC)


def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = truncate_to_seconds(punch_in).astimezone(IST)
    shift_date = local.date()
    # R1: early-morning punch-in for an overnight shift belongs to the previous shift day.
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        shift_date -= timedelta(days=1)
    return shift_date.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: Optional[str] = None) -> int:
    punch_in = truncate_to_seconds(punch_in)
    if attendance_date is None:
        attendance_date = punch_in.astimezone(IST).date().isoformat()
    scheduled_start = shift_datetime(attendance_date, shift_start)
    elapsed_seconds = (punch_in - scheduled_start).total_seconds()
    # Strictly more than ten minutes; after that, floor whole minutes since shift start.
    if elapsed_seconds > 600:
        return int(elapsed_seconds // 60)
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (truncate_to_seconds(punch_out) - truncate_to_seconds(punch_in)).total_seconds()
    value = (Decimal(str(seconds)) / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(value)     


def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: Optional[str] = None) -> int:
    punch_out = truncate_to_seconds(punch_out)
    scheduled_end_date = date.fromisoformat(attendance_date)
    if shift_start is not None and shift_end <= shift_start:
        scheduled_end_date += timedelta(days=1)
    scheduled_end = shift_datetime(scheduled_end_date.isoformat(), shift_end)
    elapsed_seconds = (punch_out - scheduled_end).total_seconds()
    whole_minutes = int(elapsed_seconds // 60) if elapsed_seconds > 0 else 0
    return whole_minutes if whole_minutes >= 30 else 0


def serialize_employee(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    if "created_at" in result:
        result["created_at"] = datetime_to_epoch_ms(result["created_at"])
    return result


def serialize_attendance(doc: dict) -> dict:
    result = {key: value for key, value in doc.items() if key != "_id"}
    # The data model deliberately has no public attendance id.
    result["punch_in"] = datetime_to_epoch_ms(result.get("punch_in"))
    result["punch_out"] = datetime_to_epoch_ms(result.get("punch_out"))
    result["late_minutes"] = result.get("late_minutes", 0)
    result["overtime_minutes"] = result.get("overtime_minutes", 0)
    result["half_day"] = result.get("half_day", False)
    result["history"] = result.get("history") or []
    for entry in result["history"]:
        entry["at"] = datetime_to_epoch_ms(entry.get("at"))
        changes = entry.get("changes") or {}
        for field in ("punch_in", "punch_out"):
            if field in changes:
                changes[field]["from"] = datetime_to_epoch_ms(changes[field].get("from"))
                changes[field]["to"] = datetime_to_epoch_ms(changes[field].get("to"))
    return result


def page_parameters(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise HTTPException(422, "page must be at least 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(422, "page_size must be between 1 and 100")
    return (page - 1) * page_size, page_size


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def valid_emp_code(cls, value: str) -> str:
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 100:
            raise ValueError("name must contain 1-100 characters")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if len(value) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email is invalid")
        return value

    @field_validator("department")
    @classmethod
    def valid_department(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 50:
            raise ValueError("department must contain 1-50 characters")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift(cls, value: str) -> str:
        return validate_shift_time(value)

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        return validate_date_string(value)

    @model_validator(mode="after")
    def shifts_must_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds in the supported range")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


# --------------------------------------------------------------------------- #
# Endpoints present in the starter (reviewed and corrected)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        create_indexes()
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    skip, size = page_parameters(page, page_size)
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    ts = truncate_to_seconds(ts)
    attendance_date = attendance_date_for_punch(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], attendance_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    # The unique (emp_code, date) index, not a racy pre-check, arbitrates concurrent requests.
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_attendance(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    skip, size = page_parameters(page, page_size)
    if date_from is not None:
        try:
            validate_date_string(date_from)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if date_to is not None:
        try:
            validate_date_string(date_to)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="invalid attendance status")

    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(size)
    )
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
