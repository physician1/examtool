# BeaconCode Assess — Phase 1 Upgrade

This release is designed to replace the currently deployed Render version **without creating a new Render service or database**.

## What this release adds

- Archive, restore, and permanent exam deletion with typed confirmation.
- Per-student integrity monitoring ON/OFF from the live monitor.
- Automatic student warning modal for tab/window/fullscreen/copy/paste violations.
- Instructor-to-student live warning messages using the existing Socket.IO connection.
- Expanded activity timeline: exam start, question views, answer saves, compile/run attempts, integrity events, warnings, grading, and submission.
- Student My Grades / Performance area.
- Released past-test results, instructor comments, question-level feedback, and question score breakdown.
- Anonymous class comparison: average, median, high/low, percentile, top percentage, optional exact rank, and score distribution.
- Running course percentage, letter grade, and 4.0 grade-point equivalent estimate.
- Instructor Student View page to turn each student-facing metric ON/OFF.
- Instructor-controlled result release/hold per exam.

## Why the existing Render database can stay

Phase 1 does **not modify the columns of the original User, Exam, Question, Attempt, Answer, or MonitorEvent tables**. It adds a few new extension tables. The application already runs `db.create_all()` at startup, so PostgreSQL creates only the missing tables and preserves all existing students, exams, attempts, grades, and monitoring history.

New extension tables:

- `portal_settings`
- `exam_portal_settings`
- `exam_archive`
- `monitoring_override`
- `attempt_comment`

## Upgrade the hosted app

### Safest method: update the existing GitHub repository

1. Keep the existing Render service `beaconcode-assess` and `beaconcode-db` exactly as they are.
2. Back up the current GitHub repository by creating a branch or downloading the current source.
3. Replace the files inside the existing `cpp_exam_platform/` folder with the files from this release.
4. Do **not** delete or recreate Render environment variables.
5. Commit and push to the same `main` branch.
6. Render's existing auto-deploy will rebuild the same service.
7. The same `DATABASE_URL` points to the same PostgreSQL database, so old data remains.
8. When the app starts, the new tables are created automatically.

No new Render Blueprint, domain, database, or URL is required.

## Environment variables

Keep the existing values in Render:

- `SECRET_KEY`
- `DATABASE_URL`
- `CPP_COMPILER=/usr/bin/g++`
- `ALLOW_UNSAFE_LOCAL_RUNNER=1`
- `MAX_CODE_OUTPUT_BYTES`
- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`
- `ADMIN_NAME`

The Docker startup command also runs `python manage.py init-admin` before Gunicorn so the configured admin account remains usable after redeploys.

## After Render shows Live

Verify in this order:

1. `/healthz` returns `{"status":"ok"}`.
2. Admin login works.
3. Existing students and exams still appear.
4. Open **Student View** and select which grades/comparison fields students may see.
5. Open an exam's **Results** page and release or hold results.
6. Open **Live Monitor**, start an exam as a student in an incognito browser, and test:
   - tab switch → automatic warning
   - fullscreen exit → automatic warning
   - integrity monitoring ON/OFF for that student
   - instructor **Send warning** → instant student message
7. Test C++ Compile and Run again.

## Grade-point wording

The displayed 4.0 value is an **assessment-based grade-point equivalent**, not an official UMass Boston cumulative GPA. The portal labels it accordingly.

## Production note

The current Render compiler remains suitable for development and controlled testing. Before using arbitrary student C++ in a high-stakes production exam, move execution to a more strongly isolated judge/container service and load-test the system for the expected class concurrency.
