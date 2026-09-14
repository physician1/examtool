# Delete Student patch

This patch adds a small admin-only permanent delete action to **Students**.

- No new Render service is needed.
- No database schema migration is needed.
- The existing PostgreSQL database and public URL remain unchanged.
- Deleting a student also removes their BeaconCode attempts, answers, grades, comments, monitoring events, Canvas profile metadata, and monitoring overrides.

## Deploy
Upload the contents of this `cpp_exam_platform` folder over the existing GitHub `cpp_exam_platform` folder and commit. Render will redeploy the same service automatically.
