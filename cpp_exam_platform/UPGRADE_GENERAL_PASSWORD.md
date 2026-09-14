# General student temporary password update

This update adds a small instructor control to **Admin → Students**:

- Enter one temporary password twice.
- Click **Set for all students**.
- Every student account is reset to that temporary password.
- Every student is marked **Change required**.
- On the next successful login, the student is forced to create a private password before accessing the dashboard, grades, or exams.
- Student IDs are unchanged. Leading zeros remain significant.
- No grades, attempts, monitoring records, Canvas roster information, or other course data is removed.

## Security

The readable general password is not stored in the database. BeaconCode writes a separate one-way password hash to each student account. Because the plaintext is not retained, the instructor should keep the selected temporary password somewhere secure until the class has completed first login.

## Deployment

Upload the contents of this `cpp_exam_platform` folder over the existing `examtool/cpp_exam_platform` folder in GitHub and commit. The existing Render service will redeploy automatically. No new Render service, database, Blueprint, URL, or environment variables are required.
