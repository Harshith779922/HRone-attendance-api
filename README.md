# Employee Attendance & Analytics API

This is my submission for the HROne assignment. It uses Python, FastAPI and MongoDB to manage employee attendance, record corrections and return attendance summaries. All application code is in `app/main.py`.

## Setup

You need Python 3.11 or newer and a running MongoDB server, version 6.0 or newer.

From the project folder, create a virtual environment:

```bash
python -m venv .venv
```

Activate it with `.venv\Scripts\Activate.ps1` in Windows PowerShell, or `source .venv/bin/activate` on macOS/Linux. Then install the packages:

```bash
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your environment, or create a local `.env` file with:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=hrone_assignment
```

Environment variables take priority over `.env`. The `.env` file is not included in the repository.

Start the app from the project folder:

```bash
uvicorn app.main:app --port 8000
```

Open `http://127.0.0.1:8000/docs` to try the endpoints. `GET /health` checks whether MongoDB is reachable. The app creates its indexes during startup, so they do not need to be added manually.

## What is included

The app supports employee creation and listing, punch-in, punch-out, attendance listing and manual corrections with history. It also includes monthly employee summaries, department summaries, the late leaderboard, daily department trends and the explain endpoint.

Attendance dates and shifts use IST. Punch times are stored in UTC and returned as epoch milliseconds. Analytics use MongoDB aggregation pipelines and read the stored attendance values.

## Checks

I checked the employee list and analytics locally with the sample data. The employee list returned all six employees. Asha's July summary showed 23 working days, two present days and 8.7% attendance. The late leaderboard showed Neha with 35 late minutes, and the Engineering trend returned all seven requested dates, including the weekend.

Additional automated checks on the implementation covered validation, concurrent requests, corrections, rounding and analytics on 100,000 attendance records using MongoDB 7.0.16. The tested explain plans used indexes without collection scans. Those verification scripts are not part of this repository, and the employer's hidden tests have not been run.

For a query plan, try:

```text
GET /admin/explain/attendance_list?page=1&page_size=20
```

All required endpoints are implemented. `REVIEW.md` lists the starter issues and fixes. `DECISIONS.md` explains the main choices.

The app can start without sample data. If you use the original kit's `sample_seed.py` locally, use a separate development database: the script clears the two collections before loading the samples.
