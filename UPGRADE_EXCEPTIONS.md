# BeaconCode update: per-student exam exceptions

This update adds instructor-controlled special exam access for individual students without reopening an exam for the whole class.

## What is new

- `Exams -> Exceptions` / `Student exceptions`
- custom start and end time for one student
- optional duration override
- instructor reason/note
- ability to reopen a previously submitted attempt while preserving the student's answer text
- student dashboard notice when special access is active
- exception removal when it is no longer needed

The normal exam must still be **published** and not **archived**. Only the selected student's date/time window is overridden.

## Existing Render deployment

No new Render service, database, Blueprint, URL, or environment variables are required.

Upload the contents of this `cpp_exam_platform` directory into the existing GitHub `cpp_exam_platform` directory and commit. Render will redeploy the same service. On startup, `db.create_all()` creates the new `exam_exception` table in the existing PostgreSQL database.

## Reopening a submitted attempt

When granting special access, check **Reopen submitted attempt**. BeaconCode will:

1. keep the student's answer text/code,
2. set the attempt back to `in_progress`,
3. clear the previous grading state,
4. restart the attempt timer for the exception window,
5. regrade when the student submits again.

If the student never started the exam, simply grant the exception and they can start normally during their individual window.
