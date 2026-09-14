# Shared Temporary Password + Forced First-Login Change

This update keeps the existing Render service and PostgreSQL database. `db.create_all()` adds a new `student_password_state` table automatically.

## Canvas import
1. Go to **Students → Import from Canvas**.
2. Upload the Canvas CSV.
3. Enter one shared temporary password (minimum 10 characters).
4. Leave **Also reset matching existing students** checked if you want the whole current roster to use that shared temporary password.
5. Import.

Students sign in with their Student/SIS ID and the shared temporary password. Before any dashboard/exam/grade access, they must replace it with a private password.

The temporary password is not stored in readable form in PostgreSQL.

## Deploy
Upload the contents of this `cpp_exam_platform` folder over the existing GitHub `cpp_exam_platform` directory and commit. Render will redeploy the same service and database automatically.
