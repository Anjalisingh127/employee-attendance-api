
# Employee Attendance & Analytics Backend

A REST API built with FastAPI, Pydantic v2, PyMongo, and MongoDB for employee management, attendance tracking, attendance corrections, and analytics.

## Prerequisites

- Python 3.12 or a compatible version
- MongoDB 6.0 or newer (MongoDB 7.0 is recommended)

## Local Setup

### 1. Create and activate a virtual environment

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**macOS / Linux**

```bash
python -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 3. Configure MongoDB

Ensure MongoDB is running and accessible.

The application reads these environment variables:

- `MONGO_URI` — defaults to `mongodb://localhost:27017`
- `MONGO_DB` — defaults to `attendance_db`

Set them in your shell or deployment environment when using a different MongoDB instance or database. Do not commit credentials, secrets, or a populated `.env` file.

For Windows PowerShell, for example:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

These values apply to the current PowerShell session.

### 4. Start the API

From the project root:

```bash
python -m uvicorn app.main:app --port 8000
```

The API should be available at:

- Health check: `http://127.0.0.1:8000/health`
- Interactive API documentation: `http://127.0.0.1:8000/docs`
- OpenAPI schema: `http://127.0.0.1:8000/openapi.json`

## Running Tests and Checks

Run the automated test suite:

```bash
python -m pytest -q
```

Run lint checks:

```bash
python -m ruff check .
```

Check Python compilation:

```bash
python -m compileall -q app tests
```

MongoDB integration tests use dedicated verification databases. Run them only against the intended test environment, and do not point test configuration at a database containing data you need to preserve.

## Implementation Notes

- MongoDB indexes are created by the application at startup.
- Unique indexes enforce employee-code and employee/date attendance constraints.
- Analytics are implemented using MongoDB aggregation pipelines.
- MongoDB must be available for the application to operate correctly.

## Submission Contents

The repository should include `app/main.py`, `requirements.txt`, `README.md`, `REVIEW.md`, and `DECISIONS.md`.

Do not commit virtual environments, credentials, secrets, or unnecessary database dumps.
