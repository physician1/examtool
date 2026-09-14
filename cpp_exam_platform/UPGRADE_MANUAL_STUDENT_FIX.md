# Manual student account creation fix

This patch fixes an `Internal Server Error` that could occur when an instructor
created a student manually (and also hardens Generic CSV / Canvas creation).

## Cause
`User.password_hash` is a NOT NULL database column. The previous route flushed a
new `User` to PostgreSQL before assigning the temporary password hash. PostgreSQL
therefore rejected the INSERT.

## Fix
The temporary password is now hashed before the first database flush. The
student password-state record is then created after the new user id is assigned.

## Deploy
Replace the contents of the existing GitHub `cpp_exam_platform` folder with this
version and commit. The existing Render service will redeploy automatically.
No new Blueprint, database, or environment variables are required.
