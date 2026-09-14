# BeaconCode Assess

A Flask-based C++ assessment platform for small university classes (designed around ~50 concurrent students).

> **Brand note:** The interface uses UMass Boston-inspired Beacon Blue and navy colors, but this project is not an official UMass Boston service and does not include or claim the university's logo.

## Included features

### Instructor
- Secure instructor login
- Create/edit/publish exams
- Set duration and exam availability window
- Validate exam schedules so an end time cannot be in the past or earlier than the start time
- MCQ, short-answer, and C++ coding questions
- Per-code-question controls:
  - allow/disallow **Compile**
  - allow/disallow **Run**
  - show/hide compiler/runtime diagnostics
- Hidden C++ test cases with weighted partial credit
- Create students manually or import a CSV roster
- Live monitoring dashboard
- Monitor tab hiding, window focus loss, fullscreen exit, blocked copy/paste, right-click, network loss, and page-leave attempts
- Review answers and override automatic scores
- Export results as CSV

### Student
- Login by email or student ID
- Timed exam interface
- Autosaved answers
- Question navigator and mark-for-review
- C++17 editor with tab indentation
- Compile/Run buttons only when the instructor permits them
- Optional stdin/output area when Run is allowed
- Fullscreen entry for monitored exams
- Automatic final grading against hidden tests
- Optional immediate score display

## Important monitoring limitation

A normal website cannot completely lock a student's Windows/macOS computer or stop use of a phone/second device. This app can detect and log many browser-level events, including a hidden tab, loss of browser focus, and fullscreen exit. For a true lockdown exam, deploy the site together with a managed kiosk environment or a dedicated lockdown browser.

## Important C++ execution security note

The included `local` C++ runner uses subprocess timeouts and Linux resource limits and is useful for development/trusted testing. **Do not treat it as a hardened sandbox for hostile arbitrary code.** Before a high-stakes production deployment where students can submit arbitrary C++, use a dedicated isolated judge/runner service (for example, Judge0 or a container/isolate-based runner) and keep the Flask web process separate from code execution.

The application deliberately requires `ALLOW_UNSAFE_LOCAL_RUNNER=1` before the local runner will execute C++.

---

## Fastest local setup (Windows/macOS/Linux)

### 1. Install prerequisites

Install:
- Python 3.11 or newer
- a C++17 compiler if you want C++ compilation locally

#### Windows compiler setup (MSYS2 / MinGW-w64)

A reliable Windows option is MSYS2. After installing MSYS2, open the **MSYS2 UCRT64** terminal and install GCC:

```bash
pacman -S --needed mingw-w64-ucrt-x86_64-gcc
```

The compiler is normally located at:

```text
C:/msys64/ucrt64/bin/g++.exe
```

Either add `C:\msys64\ucrt64\bin` to your Windows PATH and restart VS Code, or set this in `.env`:

```env
CPP_COMPILER=C:/msys64/ucrt64/bin/g++.exe
```

Then verify the app can see the compiler:

```powershell
python manage.py check-compiler
```

You should see `C++ compiler OK`. If you use Docker instead, the included Docker image already installs `g++`.

### 2. Create a virtual environment

From the project folder:

```bash
python -m venv .venv
```

Activate it.

**Windows PowerShell**

```powershell
.\.venv\Scripts\Activate.ps1
```

**macOS/Linux**

```bash
source .venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create your environment file

Copy `.env.example` to `.env`.

**Windows PowerShell**

```powershell
Copy-Item .env.example .env
```

**macOS/Linux**

```bash
cp .env.example .env
```

Edit `.env`. At minimum change:

```env
SECRET_KEY=use-a-long-random-secret
ADMIN_EMAIL=your-email@umb.edu
ADMIN_PASSWORD=use-a-strong-password
ADMIN_NAME=Your Name
ALLOW_UNSAFE_LOCAL_RUNNER=1
```

### 5. Create the instructor account

```bash
python manage.py init-admin
```

Or create an instructor + sample student + sample exam:

```bash
python manage.py seed-demo
```

The demo student is:

```text
Student ID: DEMO001
Password: Student123!
```

### 6. Start the application

```bash
python run.py
```

Open:

```text
http://127.0.0.1:5000
```

Sign in with the instructor credentials from your `.env` file.

---

## Docker setup

Docker is useful because the included image already installs `g++`.

### 1. Create `.env`

```bash
cp .env.example .env
```

Change the secret and admin password.

### 2. Build/start

```bash
docker compose up --build
```

### 3. Create the instructor

In another terminal:

```bash
docker compose exec web python manage.py init-admin
```

Then browse to:

```text
http://localhost:5000
```

To stop:

```bash
docker compose down
```

---

## How to build an exam

1. Sign in as instructor.
2. Open **Exams → Create exam**.
3. Set title, course code, duration, monitoring settings, and score-release behavior.
4. Save the exam.
5. Choose **Add question**.
6. Select:
   - Multiple choice
   - Short answer
   - C++ code
7. For C++ questions, choose whether students may Compile or Run.
8. Add hidden test cases for automatic code grading.
9. Return to exam settings and turn on **Publish exam** when ready.
10. Use **Live monitor** while students are taking it.
11. Use **Results** to review/override scores and export CSV.

### MCQ correct-answer format

The question builder uses a zero-based option index:
- `0` = first option
- `1` = second option
- `2` = third option

### Coding test example

Prompt:

```text
Write a program that reads two integers and prints their sum.
```

Test case 1:

```text
stdin:     2 3
expected:  5
```

Test case 2:

```text
stdin:     -10 4
expected:  -6
```

The student does not see tests marked **Hidden**.

---

## Student CSV format

```csv
name,email,student_id,password
Ada Student,ada@example.edu,UMB001,TempPass123!
Grace Student,grace@example.edu,UMB002,TempPass456!
```

Duplicate email addresses/student IDs are skipped.

## Production recommendations for a class of ~50

- Put the Flask app behind HTTPS (Nginx/Caddy/your cloud load balancer).
- Use PostgreSQL instead of SQLite for the hosted version.
- Use a dedicated sandboxed C++ judge service for arbitrary student code.
- Keep one Socket.IO-aware web worker unless you configure a shared message queue.
- Set a long random `SECRET_KEY`.
- Do not expose debug mode publicly.
- Back up the database before and after major exams.
- Run a mock exam with several test accounts before the real assessment.
- If you require strict device lockdown, pair the site with managed kiosk/lockdown software.

## PostgreSQL

Set, for example:

```env
DATABASE_URL=postgresql+psycopg://examuser:strong-password@db-host/examdb
```

The app creates its database tables automatically at startup. For long-term production maintenance, add Alembic/Flask-Migrate before making schema changes after students have begun using the system.

## Project structure

```text
cpp_exam_platform/
├── run.py
├── manage.py
├── config.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── exam_app/
│   ├── __init__.py
│   ├── extensions.py
│   ├── models.py
│   ├── routes.py
│   ├── grader.py
│   ├── templates/
│   │   ├── admin/
│   │   └── student/
│   └── static/
│       ├── css/app.css
│       └── js/exam.js
└── tests/
```

## Run tests

```bash
pytest -q
```

---

# Deploying BeaconCode Assess to Render

This repository includes a `render.yaml` Blueprint and a Dockerfile prepared for Render.
The Docker image installs Linux `g++` automatically, so students do **not** install a
compiler on their own machines.

## What the hosted deployment contains

- Flask + Gunicorn web application
- Flask-SocketIO using threaded/simple-websocket support
- `g++` at `/usr/bin/g++`
- Render Postgres through `DATABASE_URL`
- `/healthz` health-check endpoint

## One-click Blueprint deployment

1. Put the contents of `cpp_exam_platform` at the root of a GitHub repository.
2. In Render, choose **New > Blueprint** and connect that repository.
3. Render reads `render.yaml` and creates:
   - `beaconcode-assess` web service
   - `beaconcode-db` Postgres database
4. When Render asks for unsynced environment values, enter:
   - `ADMIN_EMAIL`
   - `ADMIN_PASSWORD`
   - `ADMIN_NAME`
5. Deploy. The first-deploy hook runs `python manage.py init-admin` to create/update
   the instructor account.
6. After deployment, open the Render URL and log in with the admin credentials.

## Verify the hosted compiler

Render's Docker build runs `apt-get install g++`. The application uses:

```text
CPP_COMPILER=/usr/bin/g++
```

You can also open a Render Shell (when available for your plan) and run:

```bash
python manage.py check-compiler
```

A successful check reports the GNU C++ compiler and `/usr/bin/g++`.

## Important free-tier limitations

Render Free is suitable for testing and demos, not a high-stakes live exam. Free web
services can spin down and have limited CPU/RAM. Free Render Postgres databases also
expire after 30 days. Before an actual class exam, load-test the deployment and use a
paid web/database plan if you need reliability beyond the free-tier limits.

## C++ execution security warning

`ALLOW_UNSAFE_LOCAL_RUNNER=1` makes the hosted web container compile and execute
student C++ locally with time/resource limits. This is acceptable for controlled
prototype testing, but it is **not equivalent to a hardened multi-tenant code sandbox**.
For a real high-stakes deployment, move code execution into a dedicated isolated judge
service/container before allowing untrusted students to run arbitrary programs.

---

## Phase 1 upgrade

If this project is already deployed on Render, read `UPGRADE_PHASE1.md`. The Phase 1 package is intentionally backward-compatible with the existing PostgreSQL deployment and adds extension tables automatically at startup, so you do not need to create a new Render service or database.

## Gradebook and Canvas roster import

This build adds a course-wide **Grades** page for instructors and an expanded **Grades** page for students.

### Instructor Gradebook

Open **Grades** in the instructor sidebar. The matrix combines each student's submitted BeaconCode assessments into one row, includes per-test scores, completed-test counts, current percentage, letter grade, and grade-point equivalent, and can be exported to CSV. Work that has not been submitted is not silently counted as zero.

### Canvas roster import

Open **Students → Import from Canvas** and upload a Canvas Gradebook/roster CSV. BeaconCode recognizes common fields such as `Student`, `ID`, `SIS User ID`, `SIS Login ID`, and `Section`; assignment columns are ignored. New accounts receive random temporary passwords and the post-import screen can download a credentials CSV. Existing students are detected and not duplicated.

Canvas metadata is stored in a separate `student_profile` table so an existing PostgreSQL deployment can be upgraded with the same database via `db.create_all()`.

## Per-student exam exceptions

Instructors can grant one student a custom exam start/end window after the normal exam has closed. Open **Exams -> Exceptions** (or **Student exceptions** from the exam settings/results page), choose the student, set the special window, and optionally reopen an already submitted attempt while preserving the student's answers. This uses the existing Render/PostgreSQL deployment; no hosting changes are required.
