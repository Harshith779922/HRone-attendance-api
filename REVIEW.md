# Review of the starter code

The first issue I saw when running the starter was the employee list. It returned `total: 6`, but `items` was empty on page 1. The offset was skipping the first 20 records.

The table below covers that issue and the other problems in the starter. I have used function names to identify the locations, since the line numbers changed after the fixes. The examples show inputs or situations that expose each problem.

| # | Where (function / line) | What is wrong | How to notice it (test, input, or symptom) | Fix in the updated code |
|---|---|---|---|---|
| 1 | `health` | Returns success without checking MongoDB. | A running app still reports healthy after MongoDB becomes unavailable. | Ping MongoDB; database failures return 503. |
| 2 | `EmployeeIn` | Employee codes, emails and text lengths are not checked. | Submit `emp_code="BAD"`, an invalid email, or an empty name. | Add the contract's patterns and length limits. |
| 3 | `EmployeeIn` | Shift strings and equal start/end times are accepted. | Submit `25:90`, or identical shift times. | Validate HH:MM and reject equal start/end times. Overnight shifts remain supported. |
| 4 | `EmployeeIn.joined_on` | Any string is accepted as a date. | Submit `2026-02-30` or `tomorrow`. | Require exact YYYY-MM-DD syntax and a real calendar date. |
| 5 | `create_employee` | Check-then-insert does not prevent concurrent duplicate employees. | Send simultaneous creates with the same code. | Create a unique `emp_code` index at startup; catch duplicate-key errors as 409. |
| 6 | `create_employee` | `datetime.now()` is naive local time, although BSON naive datetimes are interpreted as UTC. | Run the server outside UTC and inspect the stored creation instant. | Use an aware UTC server time, truncated to whole seconds. |
| 7 | Employee responses | `created_at` is returned as a datetime/ISO string rather than epoch milliseconds. | Inspect POST/GET employee JSON. | Explicitly serialize instants to integer epoch milliseconds. |
| 8 | `list_employees` | `page * page_size` skips the first page. | Six seeded employees give `total=6`, `items=[]` on page 1. | Use `(page - 1) * page_size`. |
| 9 | `list_employees` | The total ignores the department filter. | Filter Engineering: two items but a total of six. | Count using the same filter as the listing query. |
| 10 | `list_employees` | There is no required employee-code sort. | Insert employees out of code order, then paginate. | Sort by `emp_code` ascending before skip/limit. |
| 11 | Both list endpoints | Pagination bounds are not validated. | Try page 0, page size 0, or page size 101. | Query validation enforces page >= 1 and page size 1–100. |
| 12 | `PunchInIn.punched_at` | The timestamp field accepts converted values and does not check the allowed range. | Submit a numeric string, integral float, boolean, seconds timestamp, or an excessive timestamp. | Strict integers with the contract's millisecond bounds; explicit null is rejected and omission uses now. |
| 13 | `PunchInIn.status` | Arbitrary statuses, including ABSENT/LEAVE, are accepted for punch-in. | Submit `status="INVALID"` or `"LEAVE"`. | Restrict punch-in to PRESENT, WFH and ON_DUTY. |
| 14 | `punch_in` | The code uses the employee result without checking whether it exists. | Punch in with an unknown code. | Check employee existence before accessing shift fields. |
| 15 | `punch_in` | Timestamp conversion and attendance date depend on the host timezone. | Use an instant whose UTC and IST dates differ. | Decode as aware UTC and explicitly derive the IST attendance date. |
| 16 | `punch_in` | Overnight early-morning punches are assigned to the wrong day. | For a 22:00–06:00 shift, punch in at 01:00 IST. | Assign punches strictly before shift end to the previous attendance date. |
| 17 | `punch_in` and helpers | Milliseconds are kept even though R1 requires whole seconds. | Send a timestamp ending in 900 milliseconds. | Truncate before calculations, storage and responses. |
| 18 | `compute_late_minutes` | Flooring before comparing with the grace threshold loses late seconds. | At 09:40:01 for a 09:30 shift, the starter returns 0 instead of 10. | Compare elapsed seconds with 600 first, then floor minutes. |
| 19 | `compute_late_minutes` | Shift start uses the punch date and timezone, which can be wrong for overnight shifts. | Check an early-morning overnight punch or run the host in UTC. | Build aware IST shift bounds from the resolved attendance day. |
| 20 | `punch_in` | Check-then-insert permits duplicate attendance under concurrency. | Send parallel punches for the same employee/day. | Unique `(emp_code, date)` index; exactly one insert succeeds and duplicates return 409. |
| 21 | Attendance responses | MongoDB IDs are exposed as `id`, and punch instants are not serialized as epoch milliseconds. | Inspect punch-in and list-attendance response fields. | Return only the required fields and convert all dates to milliseconds, including dates inside history. |
| 22 | `compute_work_hours` | Built-in `round` uses ties-to-even and binary floating-point arithmetic. | 4.125 hours (14,850 seconds) must round half-up to 4.13; built-in round returns 4.12. | Use Decimal arithmetic with ROUND_HALF_UP. |
| 23 | `compute_overtime` | Overtime below 30 whole minutes is counted. | Punch out 29 minutes after shift end. | Return zero unless floored overtime is at least 30 minutes. |
| 24 | `compute_overtime` | Overnight shift end is placed on the start date. | A 22:00–06:00 shift ending next morning produces excessive overtime. | Move the shift end to the next date for overnight shifts. |
| 25 | Datetime handling | PyMongo's default naive dates can be mixed with aware IST dates. | Pass a fetched naive punch-out into the overtime helper. | Configure timezone-aware decoding and normalize datetime arithmetic to aware instants. |
| 26 | `list_attendance` | Dates, range order and status are unvalidated. | Send an impossible date, reversed range, or unknown status. | Validate real dates/status values and return 422 for reversed ranges. |
| 27 | `list_attendance` | Loads every matching document and sorts/paginates in Python. | Large result sets consume memory and slow small-page requests. | Filter, sort, skip and limit in MongoDB; use a separate filtered count. |
| 28 | `list_attendance` | Sorting only by date gives unstable same-day ordering. | Insert multiple employee records on one date in reverse code order. | Sort by date descending, then employee code ascending. |
| 29 | `list_attendance` | Older records can produce responses without the required default fields. | Read a seeded record without `history` or `half_day`. | Return an empty history, false half-day, and documented defaults for missing derived fields. |
| 30 | Startup / database queries | Required unique and performance indexes are never created. | Inspect indexes or explain a large attendance query. | Create indexes at startup without duplicating them. Use the same query builders for the API and explain endpoint. |

## Things I kept

- `load_dotenv()` already lets environment variables take priority over the file. I kept this and made `override=False` explicit.
- Dates such as `joined_on` and shift times stay as strings because that is what the data model requires. Punch times are stored as BSON dates.
- The shared MongoClient works with the synchronous FastAPI endpoints. There was no need to change every endpoint to async.
- `model_dump()` is valid for Pydantic v2. Extra request fields are still ignored, so a client cannot set the calculated attendance values.
- An open attendance record should have null work hours, zero overtime, false half-day and an empty history. Punch-in and punch-out do not add audit entries.
- The attendance list already used the right page offset in its Python slice. The issue there was loading and sorting all matching records in memory.

## Missing endpoints

The updated code also includes the seven endpoints marked TODO in the starter. Punch-out calculates hours, overtime and half-day status. Corrections recalculate these values and append one history entry. Both use conditional updates so concurrent requests cannot silently overwrite a change.

The analytics run in MongoDB and use the stored calculated values. The leaderboard ranks employees before applying the limit. The trend fills missing dates and calculates the moving average inside the database. The explain endpoint runs the same query or pipeline used by the endpoint being inspected.
