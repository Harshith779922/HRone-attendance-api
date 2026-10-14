"""Employee Attendance & Analytics API. Run: uvicorn app.main:app --port 8000.

All application code is intentionally in this file, as required by the kit.
MongoDB 6.0+; configuration comes from MONGO_URI and MONGO_DB.
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import Decimal128, ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENT = ['PRESENT', 'WFH', 'ON_DUTY']
Status = Literal['PRESENT', 'ABSENT', 'LEAVE', 'WFH', 'ON_DUTY']
PresenceStatus = Literal['PRESENT', 'WFH', 'ON_DUTY']
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]


def calendar_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value):
        raise ValueError('Use a calendar date in YYYY-MM-DD format')
    Date.fromisoformat(value)
    return value


def calendar_month(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', value):
        raise ValueError('Use a month in YYYY-MM format')
    Date.fromisoformat(value + '-01')
    return value


DateString = Annotated[str, BeforeValidator(calendar_date)]
MonthString = Annotated[str, BeforeValidator(calendar_month)]
ShiftTime = Annotated[str, Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]
client = MongoClient(os.getenv('MONGO_URI', 'mongodb://localhost:27017'),
                     tz_aware=True, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
db = client[os.getenv('MONGO_DB', 'attendance_db')]


def create_indexes():
    db.employees.create_index('emp_code', unique=True, name='employee_code_unique')
    db.employees.create_index([('department', 1), ('emp_code', 1)], name='department_code')
    db.employees.create_index([('joined_on', 1), ('department', 1)], name='joined_department')
    db.employees.create_index([('department', 1), ('joined_on', 1)], name='department_joined')
    db.attendance_logs.create_index([('emp_code', 1), ('date', -1)], unique=True,
                                    name='employee_day_unique')
    db.attendance_logs.create_index([('date', -1), ('emp_code', 1)], name='day_employee')
    db.attendance_logs.create_index([('status', 1), ('date', -1), ('emp_code', 1)],
                                    name='status_day_employee')
    db.attendance_logs.create_index([('emp_code', 1), ('punch_in', -1)], name='employee_punch')


@asynccontextmanager
async def lifespan(app):
    create_indexes()
    yield
    client.close()


app = FastAPI(title='Employee Attendance & Analytics API', version='2.0.0', lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def database_error(request: Request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={'detail': 'Database unavailable'})


def invalid(message, field='body', location='body'):
    raise HTTPException(422, detail=[{'type': 'value_error', 'loc': [location, field],
                                      'msg': message}])


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def now():
    return datetime.now(UTC).replace(microsecond=0)


def instant(value):
    return now() if value is None else EPOCH + timedelta(seconds=value // 1000)


def json_value(value):
    """Convert only API-bound values, including dates nested inside history."""
    if isinstance(value, datetime):
        delta = utc(value) - EPOCH
        return delta.days * 86400000 + delta.seconds * 1000 + delta.microseconds // 1000
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


EMPLOYEE_FIELDS = ('emp_code', 'name', 'email', 'department', 'shift_start', 'shift_end',
                   'joined_on', 'created_at')
DEFAULTS = {'punch_in': None, 'punch_out': None, 'work_hours': None, 'late_minutes': 0,
            'overtime_minutes': 0, 'half_day': False, 'history': []}
EDITABLE = ('status', 'punch_in', 'punch_out', 'work_hours', 'late_minutes',
            'overtime_minutes', 'half_day')


def employee_json(doc):
    return json_value({key: doc[key] for key in EMPLOYEE_FIELDS})


def record_json(doc):
    return json_value({'emp_code': doc['emp_code'], 'date': doc['date'],
                       'status': doc['status'],
                       **{k: doc.get(k, v) for k, v in DEFAULTS.items()}})


def get_employee(code):
    employee = db.employees.find_one({'emp_code': code})
    if employee is None:
        raise HTTPException(404, 'Employee not found')
    return employee


def attendance_day(ts, employee):
    local = utc(ts).astimezone(IST).replace(microsecond=0)
    day = local.date()
    if employee['shift_end'] <= employee['shift_start']:
        end_h, end_m = map(int, employee['shift_end'].split(':'))
        if (local.hour, local.minute, local.second) < (end_h, end_m, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(employee, day):
    d = Date.fromisoformat(day)
    sh, sm = map(int, employee['shift_start'].split(':'))
    eh, em = map(int, employee['shift_end'].split(':'))
    start = datetime(d.year, d.month, d.day, sh, sm, tzinfo=IST)
    end = datetime(d.year, d.month, d.day, eh, em, tzinfo=IST)
    if employee['shift_end'] <= employee['shift_start']:
        end += timedelta(days=1)
    return start, end


def computed(employee, day, status, punch_in, punch_out):
    if status not in PRESENT:
        return {'punch_in': None, 'punch_out': None, 'work_hours': None,
                'late_minutes': 0, 'overtime_minutes': 0, 'half_day': False}
    if punch_in is None:
        invalid('Presence status requires punch_in', 'punch_in')
    punch_in = utc(punch_in).replace(microsecond=0)
    if attendance_day(punch_in, employee) != day:
        invalid('punch_in must remain on the attendance date', 'punch_in')
    start, end = shift_bounds(employee, day)
    delay = int((punch_in - start).total_seconds())
    result = {'punch_in': punch_in, 'punch_out': None, 'work_hours': None,
              'late_minutes': delay // 60 if delay > 600 else 0,
              'overtime_minutes': 0, 'half_day': False}
    if punch_out is not None:
        punch_out = utc(punch_out).replace(microsecond=0)
        seconds = int((punch_out - punch_in).total_seconds())
        if not 0 < seconds <= 86400:
            invalid('Punch-out must be after punch-in and within 24 hours', 'punch_out')
        hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        overtime = int((punch_out - end).total_seconds() // 60)
        result.update(punch_out=punch_out, work_hours=float(hours),
                      overtime_minutes=overtime if overtime >= 30 else 0,
                      half_day=hours < Decimal('4.50'))
    return result


class InputModel(BaseModel):
    model_config = ConfigDict(extra='ignore')  # Client-derived fields are ignored.


class EmployeeIn(InputModel):
    emp_code: Annotated[str, Field(pattern=r'^EMP[0-9]{4,6}$')]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: ShiftTime = '09:30'
    shift_end: ShiftTime = '18:30'
    joined_on: DateString

    @model_validator(mode='after')
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError('shift_start and shift_end must differ')
        return self


class PunchOutIn(InputModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None

    @model_validator(mode='before')
    @classmethod
    def omitted_not_null(cls, data):
        if isinstance(data, dict) and 'punched_at' in data and data['punched_at'] is None:
            raise ValueError('Omit punched_at to use now; explicit null is not allowed')
        return data


class PunchInIn(PunchOutIn):
    status: PresenceStatus = 'PRESENT'


class RegularizeIn(InputModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]

    @model_validator(mode='before')
    @classmethod
    def reject_null(cls, data):
        if isinstance(data, dict):
            for key in ('status', 'punch_in', 'punch_out'):
                if key in data and data[key] is None:
                    raise ValueError(f'{key} may be omitted but cannot be null')
        return data


def snapshot_filter(doc):
    """Compare-and-set all mutable values and history; works with seeded legacy records.

    Matching the complete previous history also prevents an ABA edit from passing.
    $push and $set occur in the same atomic MongoDB document update.
    """
    result = {'_id': doc['_id']}
    for key in (*EDITABLE, 'history'):
        result[key] = {'$eq': doc[key]} if key in doc else {'$exists': False}
    return result


@app.get('/health')
def health():
    client.admin.command('ping')
    return {'status': 'ok'}


@app.post('/employees', status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc['created_at'] = now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, 'emp_code already exists')
    return employee_json(doc)


@app.get('/employees')
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {'department': department}
    cursor = db.employees.find(q).sort('emp_code', 1).skip((page - 1) * page_size).limit(page_size)
    return {'items': [employee_json(d) for d in cursor], 'total': db.employees.count_documents(q),
            'page': page, 'page_size': page_size}


@app.post('/attendance/punch-in', status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = instant(body.punched_at)
    day = attendance_day(ts, emp)
    doc = {'emp_code': body.emp_code, 'date': day, 'status': body.status, 'history': [],
           **computed(emp, day, body.status, ts, None)}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, 'Already punched in for this date')
    return record_json(doc)


@app.post('/attendance/punch-out')
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = db.attendance_logs.find_one(
        {'emp_code': body.emp_code, 'punch_in': {'$ne': None, '$lte': ts}},
        sort=[('punch_in', -1)], hint='employee_punch')
    if doc is None:
        raise HTTPException(404, 'No punch-in found')
    if doc.get('punch_out') is not None:
        raise HTTPException(409, 'Already punched out')
    values = computed(emp, doc['date'], doc['status'], doc['punch_in'], ts)
    updated = db.attendance_logs.find_one_and_update(snapshot_filter(doc), {'$set': values},
                                                     return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, 'Attendance changed concurrently; reload and retry')
    return record_json(updated)


@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code: str, date: DateString, body: RegularizeIn):
    emp = get_employee(emp_code)
    doc = db.attendance_logs.find_one({'emp_code': emp_code, 'date': date})
    if doc is None:
        raise HTTPException(404, 'Attendance record not found')
    fields = body.model_fields_set
    status = body.status if 'status' in fields else doc['status']
    if status not in PRESENT and fields.intersection({'punch_in', 'punch_out'}):
        invalid('ABSENT and LEAVE cannot have punch times', 'status')
    pi = instant(body.punch_in) if 'punch_in' in fields else doc.get('punch_in')
    po = instant(body.punch_out) if 'punch_out' in fields else doc.get('punch_out')
    values = {'status': status, **computed(emp, date, status, pi, po)}
    changes = {key: {'from': doc.get(key, DEFAULTS.get(key)), 'to': value}
               for key, value in values.items() if doc.get(key, DEFAULTS.get(key)) != value}
    if not changes:
        invalid('The correction changes nothing')
    entry = {'at': now(), 'by': body.regularized_by, 'reason': body.reason, 'changes': changes}
    updated = db.attendance_logs.find_one_and_update(snapshot_filter(doc),
        {'$set': values, '$push': {'history': entry}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, 'Attendance changed concurrently; reload and retry')
    return record_json(updated)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    if date_from is not None and date_to is not None and date_from > date_to:
        invalid('date_from must not be after date_to', 'date_to', 'query')
    q = {}
    if emp_code is not None:
        q['emp_code'] = emp_code
    if date_from is not None or date_to is not None:
        q['date'] = {}
        if date_from is not None:
            q['date']['$gte'] = date_from
        if date_to is not None:
            q['date']['$lte'] = date_to
    if status is not None:
        q['status'] = status
    hint = 'employee_day_unique' if emp_code is not None else (
        'status_day_employee' if status is not None else 'day_employee')
    return q, hint


@app.get('/attendance')
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[DateString] = None,
                    date_to: Optional[DateString] = None, status: Optional[Status] = None,
                    page: Page = 1, page_size: PageSize = 20):
    q, hint = attendance_query(emp_code, date_from, date_to, status)
    cursor = db.attendance_logs.find(q).hint(hint).sort([('date', -1), ('emp_code', 1)])
    cursor = cursor.skip((page - 1) * page_size).limit(page_size)
    return {'items': [record_json(d) for d in cursor],
            'total': db.attendance_logs.count_documents(q, hint=hint),
            'page': page, 'page_size': page_size}


# Aggregation expression builders: calculations and gap filling stay in MongoDB.
def month_bounds(month):
    d = Date.fromisoformat(month + '-01')
    last = d.replace(day=calendar.monthrange(d.year, d.month)[1])
    return d.isoformat(), last.isoformat(), last.day


def mongo_date(expr):
    return {'$dateFromString': {'dateString': expr, 'format': '%Y-%m-%d', 'timezone': '+05:30'}}


def weekday(expr):
    return {'$in': [{'$isoDayOfWeek': {'date': expr, 'timezone': '+05:30'}}, [1, 2, 3, 4, 5]]}


def round_half_up(expr, digits=2):
    """Decimal arithmetic; MongoDB $round uses a different tie rule."""
    scale = 10 ** digits
    return {'$let': {'vars': {'v': {'$toDecimal': expr}}, 'in': {
        '$cond': [{'$eq': ['$$v', None]}, None, {'$multiply': [
            {'$cond': [{'$lt': ['$$v', 0]}, -1, 1]},
            {'$divide': [{'$floor': {'$add': [
                {'$multiply': [{'$abs': '$$v'}, scale]}, Decimal128('0.5')]}}, scale]}
        ]}]}}}


def present_value(weekdays_only=False):
    condition = {'$in': ['$status', PRESENT]}
    if weekdays_only:
        condition = {'$and': [condition, weekday(mongo_date('$date'))]}
    return {'$cond': [condition, {'$cond': [{'$ifNull': ['$half_day', False]}, 0.5, 1]}, 0]}


def stats_group():
    valid_hours = {'$and': [{'$in': ['$status', PRESENT]},
                            {'$ne': [{'$ifNull': ['$work_hours', None]}, None]}]}
    late = {'$ifNull': ['$late_minutes', 0]}
    return {'$group': {
        '_id': None,
        'present_days': {'$sum': present_value(True)},
        'leave_days': {'$sum': {'$cond': [{'$eq': ['$status', 'LEAVE']}, 1, 0]}},
        'late_count': {'$sum': {'$cond': [{'$gt': [late, 0]}, 1, 0]}},
        'total_late_minutes': {'$sum': late},
        'total_overtime_minutes': {'$sum': {'$ifNull': ['$overtime_minutes', 0]}},
        'on_duty_count': {'$sum': {'$cond': [{'$eq': ['$status', 'ON_DUTY']}, 1, 0]}},
        'hours_sum': {'$sum': {'$cond': [valid_hours, {'$toDecimal': '$work_hours'}, 0]}},
        'hours_count': {'$sum': {'$cond': [valid_hours, 1, 0]}},
    }}


def employee_logs_lookup(first, last):
    return {'$lookup': {'from': 'attendance_logs', 'localField': 'emp_code',
        'foreignField': 'emp_code', 'pipeline': [
            {'$match': {'date': {'$gte': first, '$lte': last}}}, stats_group()], 'as': 'stats'}}


def stat(name):
    return {'$ifNull': [{'$arrayElemAt': ['$stats.' + name, 0]}, 0]}


def monthly_pipeline(emp_code, month):
    first, last, days = month_bounds(month)
    day_expr = {'$dateAdd': {'startDate': mongo_date({'$literal': first}), 'unit': 'day',
                            'amount': '$$n', 'timezone': '+05:30'}}
    working = {'$size': {'$filter': {'input': {'$range': [0, days]}, 'as': 'n', 'cond': {
        '$and': [weekday(day_expr), {'$gte': [day_expr, mongo_date('$joined_on')]}]}}}}
    return [
        {'$match': {'emp_code': emp_code}}, employee_logs_lookup(first, last),
        {'$project': {'_id': 0, 'emp_code': 1, 'month': {'$literal': month},
            'working_days': working, **{key: stat(key) for key in (
                'present_days', 'leave_days', 'late_count', 'total_late_minutes', 'total_overtime_minutes')}}},
        {'$set': {'attendance_pct': {'$cond': [{'$gt': ['$working_days', 0]},
            round_half_up({'$divide': [{'$multiply': [{'$toDecimal': '$present_days'}, 100]},
                                        '$working_days']}), None]}}},
    ]


def department_pipeline(month, department):
    first, last, _ = month_bounds(month)
    match = {'joined_on': {'$lte': last}}
    if department is not None:
        match['department'] = department
    sums = ('present_days', 'late_count', 'total_late_minutes', 'on_duty_count', 'hours_sum', 'hours_count')
    return [
        {'$match': match}, employee_logs_lookup(first, last),
        {'$group': {'_id': '$department', 'headcount': {'$sum': 1},
            'leave_count': {'$sum': stat('leave_days')},
            **{key: {'$sum': stat(key)} for key in sums}}},
        {'$project': {'_id': 0, 'department': '$_id', 'headcount': 1, 'present_days': 1,
            'late_count': 1, 'total_late_minutes': 1, 'leave_count': 1, 'on_duty_count': 1,
            'avg_work_hours': {'$cond': [{'$gt': ['$hours_count', 0]},
                round_half_up({'$divide': ['$hours_sum', '$hours_count']}), None]}}},
        {'$sort': {'department': 1}},
    ]


def leaderboard_pipeline(month, department, limit):
    first, last, _ = month_bounds(month)
    pipeline = [
        {'$match': {'date': {'$gte': first, '$lte': last}}},
        {'$group': {'_id': '$emp_code',
            'total_late_minutes': {'$sum': {'$ifNull': ['$late_minutes', 0]}},
            'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}}},
        {'$match': {'total_late_minutes': {'$gt': 0}}},
        {'$lookup': {'from': 'employees', 'localField': '_id', 'foreignField': 'emp_code', 'as': 'employee'}},
        {'$unwind': '$employee'},
    ]
    if department is not None:
        pipeline.append({'$match': {'employee.department': department}})
    pipeline.extend([
        {'$setWindowFields': {'sortBy': {'total_late_minutes': -1}, 'output': {'rank': {'$rank': {}}}}},
        {'$match': {'rank': {'$lte': limit}}},
        {'$project': {'_id': 0, 'emp_code': '$_id', 'name': '$employee.name',
            'department': '$employee.department', 'rank': 1, 'total_late_minutes': 1, 'late_count': 1}},
        {'$sort': {'total_late_minutes': -1, 'emp_code': 1}},
    ])
    return pipeline


def check_range(first, last):
    if first > last or (Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1 > 92:
        invalid('Range must be ordered and contain at most 92 calendar days', 'to', 'query')


def trend_pipeline(department, first, last):
    check_range(first, last)
    start = datetime.combine(Date.fromisoformat(first), datetime.min.time(), IST).astimezone(UTC)
    stop = datetime.combine(Date.fromisoformat(last), datetime.min.time(), IST).astimezone(UTC) + timedelta(days=1)
    return [
        # Anchor one real employee so even an entirely empty log range gets all dates.
        {'$match': {'department': department}}, {'$limit': 1},
        {'$project': {'_id': 0, 'day': {'$literal': start}}},
        {'$densify': {'field': 'day', 'range': {'step': 1, 'unit': 'day', 'bounds': [start, stop]}}},
        {'$set': {'date': {'$dateToString': {'date': '$day', 'format': '%Y-%m-%d', 'timezone': '+05:30'}},
                  'is_working_day': weekday('$day')}},
        {'$lookup': {'from': 'employees', 'let': {'day_string': '$date'}, 'pipeline': [
            {'$match': {'department': department, '$expr': {'$lte': ['$joined_on', '$$day_string']}}},
            {'$count': 'n'}], 'as': 'people'}},
        {'$lookup': {'from': 'attendance_logs', 'let': {'day_string': '$date'}, 'pipeline': [
            {'$match': {'$expr': {'$eq': ['$date', '$$day_string']}}},
            {'$lookup': {'from': 'employees', 'localField': 'emp_code', 'foreignField': 'emp_code',
                         'pipeline': [{'$match': {'department': department}}], 'as': 'employee'}},
            {'$match': {'employee.0': {'$exists': True}}},
            {'$group': {'_id': None, 'present_count': {'$sum': present_value()},
                'late_count': {'$sum': {'$cond': [{'$gt': [{'$ifNull': ['$late_minutes', 0]}, 0]}, 1, 0]}}}}
        ], 'as': 'logs'}},
        {'$set': {'headcount': {'$ifNull': [{'$arrayElemAt': ['$people.n', 0]}, 0]},
                  'present_count': {'$ifNull': [{'$arrayElemAt': ['$logs.present_count', 0]}, 0]},
                  'late_count': {'$ifNull': [{'$arrayElemAt': ['$logs.late_count', 0]}, 0]}}},
        {'$set': {'attendance_rate': {'$cond': [
            {'$and': ['$is_working_day', {'$gt': ['$headcount', 0]}]},
            round_half_up({'$divide': [{'$toDecimal': '$present_count'}, '$headcount']}, 4), None]}}},
        {'$setWindowFields': {'sortBy': {'day': 1}, 'output': {'moving_avg_7d': {
            '$avg': '$attendance_rate', 'window': {'documents': [-6, 0]}}}}},
        {'$set': {'moving_avg_7d': round_half_up('$moving_avg_7d', 4)}},
        {'$sort': {'day': 1}},
        {'$project': {'_id': 0, 'date': 1, 'is_working_day': 1, 'headcount': 1,
                      'present_count': 1, 'late_count': 1, 'attendance_rate': 1, 'moving_avg_7d': 1}},
    ]


def analytics_spec(endpoint, *, emp_code=None, month=None, department=None, limit=10,
                   first=None, last=None):
    """One source for the actual aggregation and /admin/explain."""
    if endpoint == 'employee_monthly':
        return 'employees', monthly_pipeline(emp_code, month), 'employee_code_unique'
    if endpoint == 'department_summary':
        return 'employees', department_pipeline(month, department), (
            'department_joined' if department is not None else 'joined_department')
    if endpoint == 'late_leaderboard':
        return 'attendance_logs', leaderboard_pipeline(month, department, limit), 'day_employee'
    return 'employees', trend_pipeline(department, first, last), 'department_joined'


def run_analytics(endpoint, **params):
    collection, pipeline, hint = analytics_spec(endpoint, **params)
    return json_value(list(db[collection].aggregate(pipeline, hint=hint, allowDiskUse=True)))


@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code: str, month: MonthString):
    rows = run_analytics('employee_monthly', emp_code=emp_code, month=month)
    if not rows:
        raise HTTPException(404, 'Employee not found')
    return rows[0]


@app.get('/analytics/departments/summary')
def department_summary(month: MonthString, department: Optional[str] = None):
    return {'month': month, 'items': run_analytics('department_summary', month=month, department=department)}


@app.get('/analytics/leaderboard/late')
def late_leaderboard(month: MonthString, limit: Limit = 10, department: Optional[str] = None):
    return {'month': month, 'items': run_analytics('late_leaderboard', month=month,
                                                  department=department, limit=limit)}


@app.get('/analytics/departments/{department}/trend')
def department_trend(department: str, first: Annotated[DateString, Query(alias='from')],
                     last: Annotated[DateString, Query(alias='to')]):
    check_range(first, last)
    if db.employees.find_one({'department': department}, {'_id': 1}) is None:
        raise HTTPException(404, 'Department not found')
    return {'department': department, 'items': run_analytics('department_trend',
                department=department, first=first, last=last)}


ExplainEndpoint = Literal['attendance_list', 'employee_monthly', 'department_summary',
                          'late_leaderboard', 'department_trend']


@app.get('/admin/explain/{endpoint}')
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: Optional[str] = None,
    month: Optional[MonthString] = None, department: Optional[str] = None, limit: Limit = 10,
    date_from: Optional[DateString] = None, date_to: Optional[DateString] = None,
    status: Optional[Status] = None,
    first: Annotated[Optional[DateString], Query(alias='from')] = None,
    last: Annotated[Optional[DateString], Query(alias='to')] = None,
    page: Page = 1, page_size: PageSize = 20):
    if endpoint == 'attendance_list':
        q, hint = attendance_query(emp_code, date_from, date_to, status)
        collection = 'attendance_logs'
        command = {'find': collection, 'filter': q, 'sort': {'date': -1, 'emp_code': 1},
                   'skip': (page - 1) * page_size, 'limit': page_size, 'hint': hint}
    else:
        required = {'employee_monthly': {'emp_code': emp_code, 'month': month},
                    'department_summary': {'month': month}, 'late_leaderboard': {'month': month},
                    'department_trend': {'department': department, 'from': first, 'to': last}}[endpoint]
        for key, value in required.items():
            if value is None:
                invalid('Field required', key, 'query')
        collection, pipeline, hint = analytics_spec(endpoint, emp_code=emp_code, month=month,
                                    department=department, limit=limit, first=first, last=last)
        command = {'aggregate': collection, 'pipeline': pipeline, 'cursor': {},
                   'hint': hint, 'allowDiskUse': True}
    result = db.command('explain', command, verbosity='executionStats')
    return {'endpoint': endpoint, 'collection': collection, 'explain': json_value(result)}
