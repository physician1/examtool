# Clear Recent Activity patch

This small upgrade adds a **Clear activity** button to the instructor dashboard.

- It hides the currently displayed recent activity feed for that admin.
- It does **not** delete monitoring events, student attempt history, grades, or audit records.
- Detailed events remain available in Live Monitor and individual attempt review pages.
- One small table (`dashboard_activity_state`) is created automatically by `db.create_all()` on startup.
- No Render service, database, Blueprint, URL, or environment-variable changes are required.

Deploy by replacing the contents of your existing GitHub `cpp_exam_platform` folder and committing. Render will redeploy the same service automatically.
