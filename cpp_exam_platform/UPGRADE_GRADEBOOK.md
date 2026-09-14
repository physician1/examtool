# BeaconCode Gradebook + Canvas Import Upgrade

This package is designed as an in-place upgrade of the Phase 1 Render deployment.

## Existing Render deployment

Do **not** create a new Render Blueprint, web service, PostgreSQL database, or URL.

1. In GitHub, open the existing `examtool/cpp_exam_platform` directory.
2. Upload/replace the contents of this package's `cpp_exam_platform` directory there.
3. Do not upload a private `.env` file.
4. Commit the change, for example: `Add central gradebook and Canvas roster import`.
5. The existing Render service should redeploy automatically from the same repository and keep using the same PostgreSQL database.

`db.create_all()` adds the new `student_profile` table without replacing existing users, exams, attempts, grades, comments, or monitoring events.

## After deployment

Verify these pages:

- Instructor: **Students → Import from Canvas**
- Instructor: **Grades**
- Student: **Grades**

For a Canvas import, use a Canvas roster or Gradebook CSV. The importer ignores assignment-score columns and uses roster identity fields only.
