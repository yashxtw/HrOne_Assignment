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
from pydantic import BaseModel, ConfigDict, StrictInt, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
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
employees = db.employees
attendance_logs = db.attendance_logs
indexes_ready = False


def create_indexes() -> None:
    """Create required indexes idempotently; unique indexes enforce race safety."""
    employees.create_index([("emp_code", 1)], unique=True, name="uq_employee_code")
    employees.create_index([("department", 1), ("emp_code", 1)], name="ix_employee_department_code")
    employees.create_index([("joined_on", 1), ("department", 1)], name="ix_employee_joined_department")
    attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_employee_date"
    )
    attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="ix_attendance_date_employee")
    attendance_logs.create_index([("emp_code", 1), ("date", -1)], name="ix_attendance_employee_date_desc")
    attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="ix_attendance_status_date_employee")
    attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="ix_attendance_employee_punchin")
    attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)], name="ix_attendance_date_late_employee")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global indexes_ready
    try:
        create_indexes()
        indexes_ready = True
    except PyMongoError:
        logger.exception("Failed to create MongoDB indexes during startup")
    yield
    client.close()


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


def round_half_up(value: Any, places: int = 2) -> Optional[float]:
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def validate_month(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("month must be YYYY-MM")
    return value


def month_bounds(month: str) -> tuple[str, str, date, date]:
    validate_month(month)
    year, mon = map(int, month.split("-"))
    start = date(year, mon, 1)
    end = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat(), start, end


def working_days(start: date, end: date) -> int:
    if start > end:
        return 0
    return sum(1 for offset in range((end - start).days + 1) if (start + timedelta(days=offset)).weekday() < 5)

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


def present_day_expr(date_field: str = "$date") -> dict:
    parsed = {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}
    weekday = {"$dayOfWeek": parsed}  # Sunday=1, Saturday=7
    is_weekday = {"$and": [{"$gte": [weekday, 2]}, {"$lte": [weekday, 6]}]}
    is_present = {"$in": ["$status", list(PRESENCE_STATUSES)]}
    amount = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]}
    return {"$cond": [{"$and": [is_weekday, is_present]}, amount, 0.0]}


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end, _, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_day_expr()},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    start, end, _, _ = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_day_expr()},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list[dict]:
    start, end, _, _ = month_bounds(month)
    match: dict[str, Any] = {"joined_on": {"$lte": end}}
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "let": {"code": "$emp_code"},
            "pipeline": [{"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start]}, {"$lte": ["$date", end]}
            ]}}}], "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"emp_code": "$emp_code", "department": "$department"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$ne": [{"$ifNull": ["$logs.date", None]}, None]},
                    {"$in": ["$logs.status", list(PRESENCE_STATUSES)]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1970-01-01"]}}}}, 6]},
                ]},
                {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]}, 0.0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]},
                "$logs.work_hours", 0
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE_STATUSES)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0
            ]}},
        }},
        {"$group": {
            "_id": "$_id.department", "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"}, "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"}, "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"}, "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$work_hours_count", 0]}, {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None]},
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, department: Optional[str]) -> list[dict]:
    start, end, _, _ = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline    


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
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
        if not value:
            raise ValueError("name must not be blank")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("email is invalid")
        return value

    @field_validator("department")
    @classmethod
    def valid_department(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("department must not be blank")
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

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, data):
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at must be an integer epoch-millisecond value")
        return data

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds in the supported range")
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, data):
        if isinstance(data, dict) and "punched_at" in data and data["punched_at"] is None:
            raise ValueError("punched_at must be an integer epoch-millisecond value")
        return data

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at must be epoch milliseconds in the supported range")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_null_punch_times(cls, data):
        if isinstance(data, dict):
            for key in ("punch_in", "punch_out", "status"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must not be null")
        return data

    @field_validator("status")
    @classmethod
    def valid_status(cls, value):
        if value is not None and value not in ALL_STATUSES:
            raise ValueError("invalid attendance status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("timestamp must be epoch milliseconds in the supported range")
        return value

    @field_validator("regularized_by")
    @classmethod
    def nonblank_by(cls, value):
        if not value.strip():
            raise ValueError("regularized_by must not be blank")
        return value


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    global indexes_ready
    try:
        db.command("ping")
        if not indexes_ready:
            create_indexes()
            indexes_ready = True
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable or not initialized") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(UTC))
    try:
        employees.insert_one(doc)
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
    total = employees.count_documents(query)
    cursor = employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else truncate_to_seconds(datetime.now(UTC))
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
        attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return serialize_attendance(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else truncate_to_seconds(datetime.now(UTC))
    record = attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", -1), ("date", -1)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")
    punch_in_dt = truncate_to_seconds(record["punch_in"])
    elapsed = (ts - punch_in_dt).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(422, "punched_at must be after punch_in and no more than 24 hours later")
    hours = compute_work_hours(punch_in_dt, ts)
    overtime = compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"])
    half_day = hours < 4.50
    updated = attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half_day}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record already punched out")
    return serialize_attendance(updated)


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

    total = attendance_logs.count_documents(query)
    cursor = (
        attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(size)
    )
    items = [serialize_attendance(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    try:
        validate_date_string(date)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")
    record = attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")
    emp = employees.find_one({"emp_code": emp_code})
    supplied = body.model_fields_set
    current_status = record.get("status")
    final_status = body.status if "status" in supplied else current_status
    current_in = record.get("punch_in")
    current_out = record.get("punch_out")
    final_in = epoch_ms_to_datetime(body.punch_in) if "punch_in" in supplied and body.punch_in is not None else current_in
    final_out = epoch_ms_to_datetime(body.punch_out) if "punch_out" in supplied and body.punch_out is not None else current_out

    if final_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(422, "punch times cannot be supplied with ABSENT or LEAVE")
        final_in = final_out = None
    elif final_status in PRESENCE_STATUSES and final_in is None:
        raise HTTPException(422, "presence status requires punch_in")

    if final_in is not None:
        final_in = truncate_to_seconds(final_in)
        if attendance_date_for_punch(final_in, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
    if final_out is not None:
        final_out = truncate_to_seconds(final_out)
        if final_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        seconds = (final_out - final_in).total_seconds()
        if seconds <= 0 or seconds > 24 * 60 * 60:
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if final_status in {"ABSENT", "LEAVE"}:
        late, hours, overtime, half_day = 0, None, 0, False
    else:
        late = compute_late_minutes(final_in, emp["shift_start"], date)
        if final_out is None:
            hours, overtime, half_day = None, 0, False
        else:
            hours = compute_work_hours(final_in, final_out)
            overtime = compute_overtime(final_out, emp["shift_end"], date, emp["shift_start"])
            half_day = hours < 4.50

    before = {
        "status": current_status, "punch_in": current_in, "punch_out": current_out,
        "work_hours": record.get("work_hours"), "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0), "half_day": record.get("half_day", False),
    }
    after = {
        "status": final_status, "punch_in": final_in, "punch_out": final_out,
        "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day,
    }
    changes = {}
    for field in before:
        old, new = before[field], after[field]
        if field in {"punch_in", "punch_out"}:
            old_cmp = truncate_to_seconds(old) if old is not None else None
            new_cmp = truncate_to_seconds(new) if new is not None else None
        else:
            old_cmp, new_cmp = old, new
        if old_cmp != new_cmp:
            if field in {"punch_in", "punch_out"}:
                changes[field] = {"from": old_cmp, "to": new_cmp}
            else:
                changes[field] = {"from": old, "to": new}
    if not changes:
        raise HTTPException(422, "request does not change the record")

    at = truncate_to_seconds(datetime.now(UTC))
    history_entry = {"at": at, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history_filter = {"history": record["history"]} if "history" in record else {"history": {"$exists": False}}
    update_filter = {"_id": record["_id"], **history_filter}
    updated = attendance_logs.find_one_and_update(
        update_filter,
        {"$set": after, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed by another request; retry")
    return serialize_attendance(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    try:
        start_s, end_s, start, end = month_bounds(month)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    emp = employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    joined = date.fromisoformat(emp["joined_on"])
    working = working_days(max(joined, start), end)
    agg = list(attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    stats = agg[0] if agg else {}
    present = float(stats.get("present_days", 0.0))
    pct = round_half_up((present / working) * 100, 4) if working else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working,
        "present_days": round_half_up(present, 2), "leave_days": int(stats.get("leave_days", 0)),
        "late_count": int(stats.get("late_count", 0)), "total_late_minutes": int(stats.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)), "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None,
):
    try:
        month_bounds(month)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    rows = list(employees.aggregate(department_summary_pipeline(month, department), allowDiskUse=True))
    items = []
    for row in rows:
        if row.get("headcount", 0) <= 0:
            continue
        row["present_days"] = round_half_up(row.get("present_days", 0), 2)
        row["avg_work_hours"] = round_half_up(row.get("avg_work_hours"), 2)
        for field in ("headcount", "late_count", "total_late_minutes", "leave_count", "on_duty_count"):
            row[field] = int(row.get(field, 0))
        items.append(row)
    return {"month": month, "items": items}    


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50), department: Optional[str] = None,
):
    try:
        month_bounds(month)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    pipeline = leaderboard_pipeline(month, department)
    pipeline.append({"$match": {"rank": {"$lte": limit}}})
    rows = list(attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": [{
        "rank": int(row["rank"]), "emp_code": row["emp_code"], "name": row["name"],
        "department": row["department"], "total_late_minutes": int(row["total_late_minutes"]),
        "late_count": int(row["late_count"]),
    } for row in rows]}


