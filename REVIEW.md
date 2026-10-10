# REVIEW.md — Starter Code Review

This review covers the helpers and endpoints that existed in the supplied starter `app/main.py`.

| # | Where (starter function / lines) | What is wrong | How you'd notice it (test, input, or symptom) | How it was fixed |
|---:|---|---|---|---|

| 1 | MongoClient initialization (22) | PyMongo's default datetime decoding is naive, which makes timezone arithmetic error-prone and can cause aware/naive comparison exceptions. | Read a stored punch timestamp and compare it with an aware UTC timestamp; behavior differs or raises `TypeError`. | Configure `MongoClient(tz_aware=True, tzinfo=UTC)` and normalize instants at helper boundaries. |

| 2 | MongoClient initialization / startup (22–23) | Required indexes were never created. Uniqueness and the expected query performance were therefore not guaranteed. | Start against a fresh database and inspect indexes; concurrent duplicate writes may both pass an application-side check. | Added an idempotent startup index-creation function, including unique employee-code and employee/date attendance indexes plus query-supporting indexes. |

| 3 | `health` (72–74) | It always returns 200 without checking whether MongoDB is available, contrary to the readiness contract. | Stop MongoDB and call `GET /health`; starter still reports healthy. | Run MongoDB `ping`; return 503 if PyMongo raises a connection/command error. |

| 4 | `compute_late_minutes` (33–37) | It builds shift start on the punch-in's calendar date and does not account for the previous-day shift date of early punches for overnight shifts. | For a 22:00–06:00 shift, punch in at 05:00 IST and compare with the shift that started the previous day. | Compute the R1 attendance date for the punch and anchor shift start to that date. The 10-minute threshold remains strict, and elapsed minutes are floored from shift start. |

| 5 | `compute_late_minutes` (36–37) | The helper did not explicitly truncate instants to whole seconds before calculations as R1 requires. | Use a timestamp such as 09:40:00.900; the stored value/calculation can include milliseconds. | Normalize timestamps to UTC whole seconds before calculations and persistence. |

| 6 | `compute_work_hours` (40–41) | Python `round()` uses ties-to-even for midpoint cases and binary floating-point can make exact decimal rounding unreliable. Contract requires half-up rounding. | Test a duration whose hours are exactly at a two-decimal midpoint. | Calculate with `Decimal` and `ROUND_HALF_UP`, using second-truncated instants. |

| 7 | `compute_overtime` (44–47) | Shift end is always anchored to `date_str`, so overnight shifts use the wrong end date. It also fails to apply the 30-minute minimum threshold. | For a 22:00–06:00 shift, punch out at 06:45 next day; starter compares against 06:00 on the starting day. Test 29 vs 30 overtime minutes too. | Anchor end to the following day when `shift_end <= shift_start`; floor elapsed whole minutes and return overtime only at 30 minutes or more. |

| 8 | `EmployeeIn` (53–60) | Fields were plain strings without contract validation: employee-code format, lengths, email format, ISO date, shift-time format, and differing shift start/end were not enforced. | Submit `emp_code="abc"`, `joined_on="tomorrow"`, `shift_start="25:80"`, or equal shift times; starter accepts them. | Added Pydantic validators for the contract's patterns, lengths, dates, and shift-time constraints. |

| 9 | `PunchInIn` (63–66) | `punched_at` accepted/coerced values outside the contract's strict epoch-millisecond integer requirements; status was not constrained to PRESENT/WFH/ON_DUTY. | Send seconds, a float, a string timestamp, an out-of-range integer, or `status="LEAVE"`. | Use a strict integer field, enforce epoch-ms bounds, and validate presence status values. |

| 10 | `create_employee` (79–84) | The check-then-insert duplicate check is race-prone: two simultaneous requests can both see no employee and insert the same `emp_code`. | Send two concurrent POSTs with the same code; both may pass the read check. | Removed the pre-check as the uniqueness mechanism; rely on the unique `emp_code` index and translate `DuplicateKeyError` to 409. |

| 11 | `create_employee` (82) | `datetime.now()` creates a naive local timestamp rather than a UTC BSON instant, and the API contract returns `created_at` as epoch milliseconds. | Run under a non-UTC host timezone or inspect the response type. | Store an aware UTC timestamp truncated to whole seconds and serialize it to epoch milliseconds. |

| 12 | `create_employee` (83–85) | A concurrent duplicate-key exception was not caught, so a race could return 500 instead of 409. The API also needed to ensure MongoDB `_id` never leaks. | Concurrent duplicate POSTs; inspect response for `_id`. | Catch `DuplicateKeyError` and return 409; use a response serializer that omits `_id`. |

| 13 | `list_employees` (93) | `skip = page * page_size` is off by one page because pages start at 1. | Request page 1 with page size 20; the first 20 employees are skipped. | Use `(page - 1) * page_size`; validate page >= 1 and page_size 1–100. |

| 14 | `list_employees` (94) | `total` counts every employee instead of the filtered department query. | Filter by a department when other departments exist; total is too high. | Count with the same query used by `find`. |

| 15 | `list_employees` (95) | Results were not sorted by `emp_code` as the API contract requires. | Insert employees in non-code order and list them. | Add ascending `emp_code` sort before pagination. |

| 16 | `list_employees` (89–96) | No explicit page/page_size bounds were applied by the endpoint. | Request page 0, page_size 0, or page_size 101. | Added FastAPI query constraints and a shared pagination helper. |

| 17 | `punch_in` (101–105) | It did not check whether the employee lookup returned `None`; unknown codes caused an attempt to subscript `None`, returning 500. | Punch in with an unknown `emp_code`. | Return 404 before reading the employee's shift fields. |

| 18 | `punch_in` (102) | `datetime.fromtimestamp()` and `datetime.now()` use the host's local timezone when no timezone is provided; the result may not be IST/UTC-consistent. | Run on a host configured outside UTC and punch in near midnight. | Convert epoch milliseconds explicitly to aware UTC and use aware UTC for the current instant; derive the attendance date in IST. |

| 19 | `punch_in` (102) | `if body.punched_at` treats the valid integer 0 as “not supplied” rather than validating it; more generally, timestamps were not checked for strict type/range. | Send `punched_at=0`, a float, or an out-of-range value. | Distinguish omitted (`is None`) from supplied values; validate strict integer epoch milliseconds and return 422 for invalid input. |

| 20 | `punch_in` (103) | Attendance date was calculated directly from the host-local timestamp date and ignored R1's IST and overnight-shift rule. | Punch at 01:00 IST for a 22:00–06:00 shift or near the IST/UTC date boundary. | Convert to IST, apply the overnight rule, and use the resulting attendance date for storage and lateness calculations. |

| 21 | `punch_in` (104–105, 118) | The check-then-insert pattern does not safely enforce one record per employee/date under concurrency. | Send simultaneous punch-in requests for the same employee/date; duplicate records or a 500 can occur. | Use a unique compound index on `(emp_code, date)` and map duplicate-key errors to 409; no pre-check is used as a lock. |

| 22 | `punch_in` (109) | The status value was not validated, allowing unsupported statuses such as ABSENT or LEAVE to be created by the punch-in endpoint. | POST with `status="ABSENT"`. | Restrict punch-in status to PRESENT, WFH, or ON_DUTY through model validation. |

| 23 | `punch_in` (110–120) | The response added an `id` field from MongoDB `_id`, contrary to the contract: attendance records are identified by `(emp_code, date)` and expose no id. | Inspect a successful punch-in response. | Removed the public `id`; response serialization excludes `_id` and converts instants to epoch milliseconds. |

| 24 | `list_attendance` (136–143) | Invalid dates, reversed date ranges, and invalid status values were not rejected as 422. | Send `date_from=2026-07-10&date_to=2026-07-01` or an unknown status. | Validate ISO dates, enforce `date_from <= date_to`, and validate status against the documented enum. |

| 25 | `list_attendance` (144–147) | It loaded every matching document into Python, sorted in memory, then sliced. This is inefficient and violates the 100k-record requirement. | Run the endpoint on a large collection; memory and latency grow with all matches, not page size. | Count with the filter and push sorting, skip, and limit into MongoDB. Added indexes to support date/employee/status query patterns. |

| 26 | `list_attendance` (145) | Sorting used only `date`, leaving same-date ordering unspecified; contract requires date descending then employee code ascending. | Query two or more employees with attendance on the same date. | Use a compound sort `[('date', -1), ('emp_code', 1)]`. |

| 27 | `list_attendance` (146) | `total` was derived by materializing all matching records rather than using MongoDB's filtered count. | Query a large date range and inspect resource use/latency. | Use `count_documents(query)` so total reflects the exact applied filters without loading records. |

| 28 | `list_attendance` (148–150) | Response serialization leaked raw BSON datetimes, which are not the specified epoch-millisecond API representation, and did not normalize missing legacy fields. | Inspect `punch_in`, `punch_out`, or a seeded record missing `history`/`half_day`. | Added a serializer converting punch instants and history instants to epoch milliseconds and defaulting missing `history`, `half_day`, `late_minutes`, and `overtime_minutes` as required. |

## Examined and not considered defects

- **The default shift values (`09:30` and `18:30`) are correct.** The contract defines these defaults; validation was missing, not the defaults themselves.
- **`status` defaulting to `PRESENT` is correct.** The contract explicitly specifies this behavior, although allowed values needed validation.
- **`punch_out=None`, `work_hours=None`, `overtime_minutes=0`, `half_day=False`, and `history=[]` at punch-in are correct.** These match the data model for an open attendance record.
- **`history` starts empty.** Punch-in is not a manual correction, so it must not append a history entry.
- **The `date` field is stored as a string.** This is intentional: it is an IST calendar date, not an instant. Only actual timestamps are BSON datetimes in MongoDB.
- **Loading `.env` with `load_dotenv()` is acceptable.** Existing environment variables take precedence by default, so injected `MONGO_URI` and `MONGO_DB` values win.


