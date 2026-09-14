import csv
import io
import json
import random
import statistics
import secrets
import string
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import wraps

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, jsonify,
    abort, Response
)
from flask_login import login_user, logout_user, login_required, current_user
from flask_socketio import join_room

from .extensions import db, socketio
from .models import (
    User, Exam, Question, TestCase, Attempt, Answer, MonitorEvent, utcnow,
    PortalSettings, ExamPortalSettings, ExamArchive, MonitoringOverride,
    AttemptComment, StudentProfile, ExamException, DashboardActivityState,
    StudentPasswordState,
)
from .grader import compile_cpp, run_cpp, grade_code

main_bp = Blueprint("main", __name__)
auth_bp = Blueprint("auth", __name__)
admin_bp = Blueprint("admin", __name__)
student_bp = Blueprint("student", __name__)

VIOLATION_TYPES = {
    "tab_hidden", "window_blur", "fullscreen_exit", "copy_attempt",
    "paste_attempt", "context_menu", "page_leave",
}

# A conventional 4.0 conversion for the *assessment estimate* shown in the
# portal. It is deliberately labelled as an estimate, not an official UMass GPA.
GRADE_SCALE = [
    (93, "A", 4.0), (90, "A-", 3.7), (87, "B+", 3.3), (83, "B", 3.0),
    (80, "B-", 2.7), (77, "C+", 2.3), (73, "C", 2.0), (70, "C-", 1.7),
    (67, "D+", 1.3), (63, "D", 1.0), (60, "D-", 0.7), (0, "F", 0.0),
]


def student_must_change_password(user_id):
    state = db.session.get(StudentPasswordState, user_id)
    return bool(state and state.must_change_password)


def set_temporary_password(user, password):
    """Assign a temporary password and force a change before student access."""
    user.set_password(password)
    state = db.session.get(StudentPasswordState, user.id)
    if state is None:
        state = StudentPasswordState(user_id=user.id)
        db.session.add(state)
    state.must_change_password = True
    state.assigned_at = utcnow()
    state.changed_at = None
    return state


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if current_user.role != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def student_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if current_user.role != "student":
            abort(403)
        if student_must_change_password(current_user.id):
            return redirect(url_for("auth.change_password"))
        return fn(*args, **kwargs)
    return wrapper


def parse_local_datetime(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_portal_settings():
    settings = db.session.get(PortalSettings, 1)
    if not settings:
        settings = PortalSettings(id=1)
        db.session.add(settings)
        db.session.commit()
    return settings


def is_archived(exam_id):
    return db.session.get(ExamArchive, exam_id) is not None


def results_released(exam):
    if exam.show_results_immediately:
        return True
    cfg = db.session.get(ExamPortalSettings, exam.id)
    return bool(cfg and cfg.results_released)


def effective_monitoring(exam, user_id):
    override = MonitoringOverride.query.filter_by(exam_id=exam.id, user_id=user_id).first()
    if override is not None:
        return bool(override.enabled)
    return bool(exam.monitoring_enabled)


def get_exam_exception(exam_id, user_id):
    if not user_id:
        return None
    return ExamException.query.filter_by(
        exam_id=exam_id, user_id=user_id, enabled=True
    ).first()


def exam_is_open(exam, user_id=None):
    """Return whether the exam is currently available for this student.

    A per-student exception overrides the normal start/end window, but it does
    not override an archived or unpublished exam.
    """
    if is_archived(exam.id) or not exam.published:
        return False

    exception = get_exam_exception(exam.id, user_id) if user_id else None
    start_at = exception.start_at if exception else exam.start_at
    end_at = exception.end_at if exception else exam.end_at
    now = utcnow()

    if start_at and now < aware(start_at):
        return False
    if end_at and now > aware(end_at):
        return False
    return True


def remaining_seconds(attempt):
    exception = get_exam_exception(attempt.exam_id, attempt.user_id)
    duration_minutes = (
        exception.duration_minutes
        if exception and exception.duration_minutes
        else attempt.exam.duration_minutes
    )
    duration_end = aware(attempt.started_at) + timedelta(minutes=duration_minutes)
    hard_end = (
        aware(exception.end_at) if exception and exception.end_at
        else (aware(attempt.exam.end_at) if attempt.exam.end_at else duration_end)
    )
    end = min(duration_end, hard_end)
    return max(0, int((end - utcnow()).total_seconds()))


def get_or_make_answer(attempt, question):
    answer = Answer.query.filter_by(attempt_id=attempt.id, question_id=question.id).first()
    if not answer:
        initial = (question.starter_code or "") if question.question_type == "code" else ""
        answer = Answer(attempt_id=attempt.id, question_id=question.id, answer_text=initial)
        db.session.add(answer)
        db.session.flush()
    return answer


def grade_band(percent):
    pct = float(percent or 0)
    for threshold, letter, gp in GRADE_SCALE:
        if pct >= threshold:
            return letter, gp
    return "F", 0.0


def total_points(exam):
    return float(sum(q.points for q in exam.questions))


def attempt_percent(attempt):
    possible = total_points(attempt.exam)
    return round((attempt.score / possible) * 100, 1) if possible > 0 else 0.0


def record_event(attempt, event_type, details="", *, violation=False, emit=True):
    event = MonitorEvent(
        attempt_id=attempt.id,
        event_type=str(event_type)[:80],
        details=str(details or "")[:500],
    )
    db.session.add(event)
    if violation:
        attempt.violations += 1
    db.session.flush()
    if emit:
        socketio.emit("monitor_event", {
            "student": attempt.user.name,
            "student_id": attempt.user.student_id,
            "attempt_id": attempt.id,
            "type": event.event_type,
            "details": event.details,
            "violation": bool(violation),
            "violations": attempt.violations,
            "time": event.created_at.isoformat() if event.created_at else utcnow().isoformat(),
        }, room="admins")
    return event


def grade_attempt(attempt):
    total = 0.0
    for question in attempt.exam.questions:
        answer = get_or_make_answer(attempt, question)
        text = (answer.answer_text or "").strip()
        if question.question_type == "mcq":
            answer.score = question.points if text == (question.correct_answer or "") else 0.0
            answer.feedback = "Correct." if answer.score == question.points else "Incorrect."
        elif question.question_type == "short":
            expected = (question.correct_answer or "").strip().casefold()
            answer.score = question.points if expected and text.casefold() == expected else 0.0
            answer.feedback = "Correct." if answer.score == question.points else "Instructor review may be required."
        elif question.question_type == "code":
            answer.score, answer.feedback = grade_code(question, answer.answer_text or "")
        answer.graded_at = utcnow()
        total += answer.score
    attempt.score = round(total, 2)
    attempt.status = "submitted"
    attempt.submitted_at = utcnow()
    record_event(attempt, "exam_submitted", f"Final score recorded: {attempt.score}", emit=True)
    db.session.commit()


def exam_class_stats(exam, student_attempt=None):
    possible = total_points(exam)
    submitted = Attempt.query.filter_by(exam_id=exam.id, status="submitted").all()
    scores = [round((a.score / possible) * 100, 1) if possible else 0.0 for a in submitted]
    if not scores:
        return None
    avg = round(sum(scores) / len(scores), 1)
    median = round(statistics.median(scores), 1)
    stats = {
        "count": len(scores), "average": avg, "median": median,
        "highest": max(scores), "lowest": min(scores),
        "distribution": [
            sum(1 for x in scores if x < 60),
            sum(1 for x in scores if 60 <= x < 70),
            sum(1 for x in scores if 70 <= x < 80),
            sum(1 for x in scores if 80 <= x < 90),
            sum(1 for x in scores if x >= 90),
        ],
    }
    if student_attempt is not None:
        mine = attempt_percent(student_attempt)
        below_or_equal = sum(1 for x in scores if x <= mine)
        stats["percentile"] = min(100, max(1, round(100 * below_or_equal / len(scores))))
        sorted_desc = sorted(scores, reverse=True)
        stats["rank"] = sorted_desc.index(mine) + 1 if mine in sorted_desc else None
        stats["top_percent"] = max(1, round(100 * (stats["rank"] or len(scores)) / len(scores)))
    return stats


def released_attempts_for_user(user_id):
    attempts = (Attempt.query.filter_by(user_id=user_id, status="submitted")
                .order_by(Attempt.submitted_at.asc()).all())
    return [a for a in attempts if results_released(a.exam) and not is_archived(a.exam_id)]


def course_summaries_for_user(user_id):
    attempts = released_attempts_for_user(user_id)
    grouped = {}
    for a in attempts:
        grouped.setdefault(a.exam.course_code, []).append(a)
    summaries = []
    for course_code, items in grouped.items():
        earned = sum(a.score for a in items)
        possible = sum(total_points(a.exam) for a in items)
        pct = round((earned / possible) * 100, 1) if possible else 0.0
        letter, gp = grade_band(pct)

        # Anonymous comparison of current released-course performance.
        student_ids = [u.id for u in User.query.filter_by(role="student", active=True).all()]
        class_pcts = []
        for sid in student_ids:
            other = [a for a in released_attempts_for_user(sid) if a.exam.course_code == course_code]
            if not other:
                continue
            e = sum(a.score for a in other)
            p = sum(total_points(a.exam) for a in other)
            if p:
                class_pcts.append((e / p) * 100)
        class_avg = round(sum(class_pcts) / len(class_pcts), 1) if class_pcts else None
        percentile = None
        if class_pcts:
            percentile = min(100, max(1, round(100 * sum(1 for x in class_pcts if x <= pct) / len(class_pcts))))
        summaries.append({
            "course_code": course_code, "earned": round(earned, 2), "possible": round(possible, 2),
            "percent": pct, "letter": letter, "grade_points": gp,
            "class_average": class_avg, "percentile": percentile, "completed": len(items),
        })
    return summaries


def performance_rows_for_user(user_id):
    rows = []
    for a in released_attempts_for_user(user_id):
        stats = exam_class_stats(a.exam, a)
        rows.append({
            "attempt": a,
            "percent": attempt_percent(a),
            "class_average": stats["average"] if stats else None,
            "comment": a.comment_record.comment if a.comment_record else "",
        })
    return rows


def _active_exam_list(course_code=None):
    """Return non-archived exams for the course gradebook."""
    archived_ids = {x.exam_id for x in ExamArchive.query.all()}
    query = Exam.query.order_by(Exam.created_at.asc())
    if course_code:
        query = query.filter(Exam.course_code == course_code)
    return [e for e in query.all() if e.id not in archived_ids]


def build_admin_gradebook(course_code=None):
    """Build a Canvas-style matrix of students x exams.

    Current grade is points earned divided by points possible for submitted
    attempts only. Work that has not yet been submitted is not silently treated
    as a zero.
    """
    exams = _active_exam_list(course_code)
    students = User.query.filter_by(role="student").order_by(User.name.asc()).all()
    exam_ids = [e.id for e in exams]
    user_ids = [u.id for u in students]
    attempts = []
    if exam_ids and user_ids:
        attempts = (Attempt.query.filter(Attempt.exam_id.in_(exam_ids), Attempt.user_id.in_(user_ids))
                    .order_by(Attempt.started_at.asc()).all())
    amap = {(a.user_id, a.exam_id): a for a in attempts}
    rows = []
    for student in students:
        earned = 0.0
        possible = 0.0
        completed = 0
        cells = []
        for exam in exams:
            attempt = amap.get((student.id, exam.id))
            exam_possible = total_points(exam)
            cell = {
                "exam": exam,
                "attempt": attempt,
                "possible": exam_possible,
                "percent": None,
                "score": None,
                "status": "not_started",
            }
            if attempt:
                cell["status"] = attempt.status
                if attempt.status == "submitted":
                    cell["score"] = attempt.score
                    cell["percent"] = attempt_percent(attempt)
                    earned += float(attempt.score or 0)
                    possible += exam_possible
                    completed += 1
            cells.append(cell)
        current_pct = round((earned / possible) * 100, 1) if possible else None
        letter, gp = grade_band(current_pct or 0)
        rows.append({
            "student": student,
            "profile": student.student_profile,
            "cells": cells,
            "earned": round(earned, 2),
            "possible": round(possible, 2),
            "percent": current_pct,
            "letter": letter if current_pct is not None else "—",
            "grade_points": gp if current_pct is not None else None,
            "completed": completed,
        })
    return exams, rows


def _norm_header(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").strip().lower()).strip()


def _row_lookup(row, *candidates):
    normalized = {_norm_header(k): (v or "").strip() for k, v in (row or {}).items() if k is not None}
    for candidate in candidates:
        value = normalized.get(_norm_header(candidate), "")
        if value:
            return value
    return ""


def _temporary_password(length=14):
    # Avoid ambiguous characters while still including upper/lower/digits/symbols.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _placeholder_email(student_id, canvas_id=""):
    token = re.sub(r"[^A-Za-z0-9._-]+", "-", (student_id or canvas_id or secrets.token_hex(4))).strip("-.")
    token = token[:80] or secrets.token_hex(4)
    return f"{token.lower()}@beaconcode.local"


def _canvas_student_from_row(row):
    """Extract the roster identity fields from common Canvas CSV exports.

    Canvas gradebook/roster exports commonly include Student, ID, SIS User ID,
    SIS Login ID and Section. Extra assignment columns are intentionally ignored.
    """
    name = _row_lookup(row, "Student", "Student Name", "Name", "Full Name")
    lowered = name.casefold()
    if not name or lowered in {"points possible", "student, test", "test student"} or lowered.startswith("points possible"):
        return None

    canvas_id = _row_lookup(row, "ID", "Canvas User ID", "Canvas ID")
    sis_user_id = _row_lookup(row, "SIS User ID", "SIS ID", "Student ID", "Student Number")
    sis_login_id = _row_lookup(row, "SIS Login ID", "Login ID", "Login", "Username")
    explicit_email = _row_lookup(row, "Email", "Email Address", "University Email")
    section = _row_lookup(row, "Section", "Sections", "Course Section")

    student_id = sis_user_id or canvas_id or sis_login_id
    if not student_id:
        return None
    email = explicit_email or (sis_login_id if "@" in sis_login_id else "")
    return {
        "name": name,
        "student_id": student_id[:40],
        "email": email.lower()[:255] if email else "",
        "canvas_user_id": canvas_id[:80],
        "sis_user_id": sis_user_id[:120],
        "sis_login_id": sis_login_id[:255],
        "section": section[:255],
    }


# -----------------------------------------------------------------------------
# Main/auth
# -----------------------------------------------------------------------------

@main_bp.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        if current_user.role == "student" and student_must_change_password(current_user.id):
            return redirect(url_for("auth.change_password"))
        return redirect(url_for("admin.dashboard" if current_user.role == "admin" else "student.dashboard"))
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.index"))
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter((User.email.ilike(identifier)) | (User.student_id == identifier)).first()
        if user and user.check_password(password) and user.active:
            login_user(user)
            if user.role == "student" and student_must_change_password(user.id):
                return redirect(url_for("auth.change_password"))
            return redirect(url_for("main.index"))
        flash("Invalid login details.", "danger")
    return render_template("login.html")


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if current_user.role != "student":
        return redirect(url_for("main.index"))

    state = db.session.get(StudentPasswordState, current_user.id)
    forced = bool(state and state.must_change_password)

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not current_user.check_password(current_password):
            flash("Your current temporary password is incorrect.", "danger")
        elif len(new_password) < 10:
            flash("Your new password must be at least 10 characters.", "danger")
        elif new_password != confirm_password:
            flash("The new passwords do not match.", "danger")
        elif current_user.check_password(new_password):
            flash("Choose a new password that is different from the temporary password.", "danger")
        else:
            current_user.set_password(new_password)
            if state is None:
                state = StudentPasswordState(user_id=current_user.id, must_change_password=False)
                db.session.add(state)
            state.must_change_password = False
            state.changed_at = utcnow()
            db.session.commit()
            flash("Password changed successfully. Welcome to BeaconCode.", "success")
            return redirect(url_for("student.dashboard"))

    return render_template("change_password.html", forced=forced)


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


# -----------------------------------------------------------------------------
# Admin: overview/students/settings
# -----------------------------------------------------------------------------

@admin_bp.route("/")
@admin_required
def dashboard():
    archived_ids = {x.exam_id for x in ExamArchive.query.all()}
    exams = [e for e in Exam.query.order_by(Exam.created_at.desc()).all() if e.id not in archived_ids]
    students = User.query.filter_by(role="student").count()
    active_attempts = Attempt.query.filter_by(status="in_progress").count()
    submitted = Attempt.query.filter_by(status="submitted").count()
    activity_state = db.session.get(DashboardActivityState, current_user.id)
    events_query = MonitorEvent.query
    if activity_state and activity_state.cleared_before:
        events_query = events_query.filter(MonitorEvent.created_at > activity_state.cleared_before)
    recent_events = events_query.order_by(MonitorEvent.created_at.desc()).limit(10).all()
    return render_template(
        "admin/dashboard.html", exams=exams, students=students,
        active_attempts=active_attempts, submitted=submitted, recent_events=recent_events,
    )


@admin_bp.route("/dashboard/activity/clear", methods=["POST"])
@admin_required
def clear_dashboard_activity():
    state = db.session.get(DashboardActivityState, current_user.id)
    if state is None:
        state = DashboardActivityState(admin_user_id=current_user.id)
        db.session.add(state)
    state.cleared_before = utcnow()
    db.session.commit()
    flash("Recent dashboard activity cleared. Monitoring and audit records were preserved.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/students", methods=["GET", "POST"])
@admin_required
def students():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        student_id = request.form.get("student_id", "").strip()
        password = request.form.get("password", "").strip()
        if not all([name, email, student_id, password]):
            flash("All student fields are required.", "danger")
        elif len(password) < 10:
            flash("Temporary password must be at least 10 characters.", "danger")
        elif User.query.filter((User.email == email) | (User.student_id == student_id)).first():
            flash("Email or student ID already exists.", "danger")
        else:
            user = User(name=name, email=email, student_id=student_id, role="student")
            db.session.add(user)
            db.session.flush()
            set_temporary_password(user, password)
            db.session.commit()
            flash("Student account created. They must change the temporary password at first login.", "success")
        return redirect(url_for("admin.students"))
    items = User.query.filter_by(role="student").order_by(User.name).all()
    states = {s.user_id: s for s in StudentPasswordState.query.all()}
    return render_template("admin/students.html", students=items, password_states=states)


@admin_bp.route("/students/import", methods=["POST"])
@admin_required
def import_students():
    uploaded = request.files.get("csv_file")
    if not uploaded:
        flash("Choose a CSV file first.", "danger")
        return redirect(url_for("admin.students"))
    text = uploaded.stream.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    added, skipped = 0, 0
    for row in reader:
        name = (row.get("name") or "").strip()
        email = (row.get("email") or "").strip().lower()
        sid = (row.get("student_id") or "").strip()
        password = (row.get("password") or "").strip()
        if not all([name, email, sid, password]) or User.query.filter((User.email == email) | (User.student_id == sid)).first():
            skipped += 1
            continue
        u = User(name=name, email=email, student_id=sid, role="student")
        db.session.add(u)
        db.session.flush()
        set_temporary_password(u, password)
        added += 1
    db.session.commit()
    flash(f"Imported {added} student(s); skipped {skipped}.", "success")
    return redirect(url_for("admin.students"))


@admin_bp.route("/students/import-canvas", methods=["GET", "POST"])
@admin_required
def import_canvas_students():
    if request.method == "GET":
        return render_template("admin/canvas_import.html", report=None, credentials=[], shared_password="")

    shared_password = request.form.get("temporary_password", "").strip()
    reset_existing = request.form.get("reset_existing") == "on"
    if len(shared_password) < 10:
        flash("Choose a shared temporary password with at least 10 characters.", "danger")
        return redirect(url_for("admin.import_canvas_students"))

    uploaded = request.files.get("csv_file")
    if not uploaded or not uploaded.filename:
        flash("Choose the Canvas CSV file first.", "danger")
        return redirect(url_for("admin.import_canvas_students"))

    try:
        raw = uploaded.stream.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("No CSV headers were found.")
    except Exception as exc:
        flash(f"Could not read that CSV file: {exc}", "danger")
        return redirect(url_for("admin.import_canvas_students"))

    credentials = []
    created = 0
    existing_count = 0
    reset_count = 0
    skipped = 0
    sections = set()
    errors = []

    for line_no, raw_row in enumerate(reader, start=2):
        data = _canvas_student_from_row(raw_row)
        if not data:
            skipped += 1
            continue
        if data["section"]:
            sections.add(data["section"])

        existing = User.query.filter_by(student_id=data["student_id"]).first()
        if not existing and data["email"]:
            existing = User.query.filter(User.email.ilike(data["email"])).first()
        if not existing and (data["sis_user_id"] or data["sis_login_id"]):
            profile = None
            if data["sis_user_id"]:
                profile = StudentProfile.query.filter_by(sis_user_id=data["sis_user_id"]).first()
            if not profile and data["sis_login_id"]:
                profile = StudentProfile.query.filter_by(sis_login_id=data["sis_login_id"]).first()
            existing = profile.user if profile else None

        if existing:
            if existing.role != "student":
                skipped += 1
                errors.append(f"Line {line_no}: {data['name']} matches a non-student account and was skipped.")
                continue
            profile = existing.student_profile
            if not profile:
                profile = StudentProfile(user_id=existing.id, source="canvas")
                db.session.add(profile)
            profile.canvas_user_id = data["canvas_user_id"] or profile.canvas_user_id
            profile.sis_user_id = data["sis_user_id"] or profile.sis_user_id
            profile.sis_login_id = data["sis_login_id"] or profile.sis_login_id
            profile.section = data["section"] or profile.section
            profile.source = "canvas"
            if reset_existing:
                set_temporary_password(existing, shared_password)
                credentials.append({
                    "name": existing.name,
                    "student_id": existing.student_id,
                    "login": existing.student_id,
                    "email": "" if existing.email.endswith("@beaconcode.local") else existing.email,
                    "password": shared_password,
                    "section": data["section"],
                })
                reset_count += 1
            existing_count += 1
            continue

        email = data["email"] or _placeholder_email(data["student_id"], data["canvas_user_id"])
        # Placeholder addresses are internal only. Ensure uniqueness without
        # changing the student's Canvas/SIS ID used for login.
        if User.query.filter(User.email.ilike(email)).first():
            email = _placeholder_email(f"{data['student_id']}-{secrets.token_hex(2)}", data["canvas_user_id"])

        password = shared_password
        user = User(
            name=data["name"][:120],
            email=email,
            student_id=data["student_id"],
            role="student",
            active=True,
        )
        db.session.add(user)
        db.session.flush()
        set_temporary_password(user, password)
        db.session.add(StudentProfile(
            user_id=user.id,
            canvas_user_id=data["canvas_user_id"] or None,
            sis_user_id=data["sis_user_id"] or None,
            sis_login_id=data["sis_login_id"] or None,
            section=data["section"] or None,
            source="canvas",
        ))
        credentials.append({
            "name": user.name,
            "student_id": user.student_id,
            "login": user.student_id,
            "email": "" if user.email.endswith("@beaconcode.local") else user.email,
            "password": password,
            "section": data["section"],
        })
        created += 1

    db.session.commit()
    report = {
        "created": created,
        "existing": existing_count,
        "reset": reset_count,
        "skipped": skipped,
        "sections": sorted(sections),
        "headers": reader.fieldnames or [],
        "errors": errors[:20],
    }
    return render_template("admin/canvas_import.html", report=report, credentials=credentials, shared_password=shared_password)


@admin_bp.route("/grades")
@admin_required
def gradebook():
    course_codes = [x[0] for x in db.session.query(Exam.course_code).distinct().order_by(Exam.course_code).all() if x[0]]
    selected = (request.args.get("course") or "").strip()
    if selected and selected not in course_codes:
        selected = ""
    exams, rows = build_admin_gradebook(selected or None)
    return render_template(
        "admin/gradebook.html",
        exams=exams,
        rows=rows,
        course_codes=course_codes,
        selected_course=selected,
        total_points=total_points,
    )


@admin_bp.route("/grades.csv")
@admin_required
def gradebook_csv():
    selected = (request.args.get("course") or "").strip()
    exams, rows = build_admin_gradebook(selected or None)
    output = io.StringIO()
    writer = csv.writer(output)
    header = ["student_id", "student_name", "email", "section"]
    header.extend([f"{exam.title} ({total_points(exam):g} pts)" for exam in exams])
    header.extend(["earned_points", "graded_points_possible", "current_percent", "letter_grade", "grade_point_equivalent"])
    writer.writerow(header)
    for row in rows:
        student = row["student"]
        values = [student.student_id, student.name, student.email, row["profile"].section if row["profile"] else ""]
        for cell in row["cells"]:
            if cell["attempt"] and cell["attempt"].status == "submitted":
                values.append(f"{cell['score']:g}/{cell['possible']:g} ({cell['percent']:g}%)")
            elif cell["attempt"]:
                values.append("In progress")
            else:
                values.append("")
        values.extend([
            row["earned"], row["possible"],
            "" if row["percent"] is None else row["percent"],
            row["letter"], "" if row["grade_points"] is None else row["grade_points"],
        ])
        writer.writerow(values)
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "_", selected or "all_courses")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=beaconcode_gradebook_{suffix}.csv"},
    )


@admin_bp.route("/students/<int:user_id>/toggle", methods=["POST"])
@admin_required
def toggle_student(user_id):
    user = db.get_or_404(User, user_id)
    if user.role != "student":
        abort(400)
    user.active = not user.active
    db.session.commit()
    flash("Student access updated.", "success")
    return redirect(url_for("admin.students"))


@admin_bp.route("/students/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_student(user_id):
    """Permanently remove a student and their BeaconCode assessment records.

    This is intentionally a small admin-only patch. Related attempts are
    deleted through SQLAlchemy so their answers, monitoring events, and
    instructor comments follow the existing cascade rules.
    """
    user = db.get_or_404(User, user_id)
    if user.role != "student":
        abort(400)

    student_name = user.name

    # Delete per-student extension records first because they reference user.id.
    MonitoringOverride.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    ExamException.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    StudentProfile.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    StudentPasswordState.query.filter_by(user_id=user.id).delete(synchronize_session=False)

    # Deleting each Attempt via the ORM triggers the existing cascades for
    # answers, monitoring events, and the attempt comment record.
    for attempt in Attempt.query.filter_by(user_id=user.id).all():
        db.session.delete(attempt)

    db.session.flush()
    db.session.delete(user)
    db.session.commit()

    flash(f"{student_name} and their BeaconCode records were permanently deleted.", "success")
    return redirect(url_for("admin.students"))


@admin_bp.route("/student-view", methods=["GET", "POST"])
@admin_required
def portal_settings():
    settings = get_portal_settings()
    if request.method == "POST":
        fields = [
            "show_current_grade", "show_letter_grade", "show_grade_points",
            "show_class_comparison", "show_percentile", "show_distribution",
            "show_past_results", "show_comments", "show_question_breakdown",
            "show_exact_rank",
        ]
        for field in fields:
            setattr(settings, field, field in request.form)
        db.session.commit()
        flash("Student visibility settings saved.", "success")
        return redirect(url_for("admin.portal_settings"))
    return render_template("admin/portal_settings.html", settings=settings)


# -----------------------------------------------------------------------------
# Admin: exams/questions
# -----------------------------------------------------------------------------

@admin_bp.route("/exams")
@admin_required
def exams():
    show_archived = request.args.get("archived") == "1"
    archived_ids = {x.exam_id for x in ExamArchive.query.all()}
    items = Exam.query.order_by(Exam.created_at.desc()).all()
    if show_archived:
        items = [e for e in items if e.id in archived_ids]
    else:
        items = [e for e in items if e.id not in archived_ids]
    return render_template("admin/exams.html", exams=items, show_archived=show_archived, archived_ids=archived_ids)


@admin_bp.route("/exams/new", methods=["GET", "POST"])
@admin_required
def exam_new():
    exam = Exam()
    if request.method == "POST":
        errors = _populate_exam(exam)
        if errors:
            for error in errors:
                flash(error, "danger")
            return render_template("admin/exam_form.html", exam=exam, is_new=True), 400
        db.session.add(exam)
        db.session.commit()
        flash("Exam created. Add your questions next.", "success")
        return redirect(url_for("admin.exam_edit", exam_id=exam.id))
    return render_template("admin/exam_form.html", exam=exam, is_new=True)


def _populate_exam(exam):
    raw_start = (request.form.get("start_at") or "").strip()
    raw_end = (request.form.get("end_at") or "").strip()
    start_at = parse_local_datetime(raw_start)
    end_at = parse_local_datetime(raw_end)
    errors = []

    if raw_start and start_at is None:
        errors.append("Start time is invalid.")
    if raw_end and end_at is None:
        errors.append("End time is invalid.")

    now = utcnow()
    existing_end = aware(exam.end_at) if getattr(exam, "end_at", None) else None
    if end_at and end_at <= now:
        unchanged_historical_end = existing_end and abs((end_at - existing_end).total_seconds()) < 60
        if not unchanged_historical_end:
            errors.append("End time must be in the future. You cannot schedule an exam to end in the past.")
    if start_at and end_at and end_at <= start_at:
        errors.append("End time must be later than the start time.")

    try:
        duration_minutes = max(1, int(request.form.get("duration_minutes", 60)))
    except (TypeError, ValueError):
        duration_minutes = 60
        errors.append("Duration must be a valid number of minutes.")

    if errors:
        return errors

    exam.title = request.form.get("title", "Untitled Exam").strip()
    exam.course_code = request.form.get("course_code", "C++").strip()
    exam.description = request.form.get("description", "").strip()
    exam.duration_minutes = duration_minutes
    exam.start_at = start_at
    exam.end_at = end_at
    exam.published = "published" in request.form
    exam.monitoring_enabled = "monitoring_enabled" in request.form
    exam.require_fullscreen = "require_fullscreen" in request.form
    exam.block_copy_paste = "block_copy_paste" in request.form
    exam.shuffle_questions = "shuffle_questions" in request.form
    exam.show_results_immediately = "show_results_immediately" in request.form
    return []


@admin_bp.route("/exams/<int:exam_id>/edit", methods=["GET", "POST"])
@admin_required
def exam_edit(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    if request.method == "POST":
        errors = _populate_exam(exam)
        if errors:
            for error in errors:
                flash(error, "danger")
            return render_template("admin/exam_form.html", exam=exam, is_new=False), 400
        db.session.commit()
        flash("Exam settings saved.", "success")
        return redirect(url_for("admin.exam_edit", exam_id=exam.id))
    return render_template("admin/exam_form.html", exam=exam, is_new=False)


@admin_bp.route("/exams/<int:exam_id>/archive", methods=["POST"])
@admin_required
def exam_archive(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    record = db.session.get(ExamArchive, exam.id)
    if record:
        db.session.delete(record)
        flash("Exam restored from archive.", "success")
    else:
        db.session.add(ExamArchive(exam_id=exam.id))
        flash("Exam archived. Student access is disabled, but results are preserved.", "success")
    db.session.commit()
    return redirect(url_for("admin.exams", archived=1 if not record else 0))


@admin_bp.route("/exams/<int:exam_id>/delete", methods=["POST"])
@admin_required
def exam_delete(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    confirmation = (request.form.get("confirmation") or "").strip()
    if confirmation not in {"DELETE", exam.title}:
        flash('Permanent deletion cancelled. Type "DELETE" or the exact exam title to confirm.', "danger")
        return redirect(url_for("admin.exams", archived=1 if is_archived(exam.id) else 0))

    # Delete extension records first, then attempts (whose answers/events/comments
    # cascade), then the exam and questions/test cases.
    MonitoringOverride.query.filter_by(exam_id=exam.id).delete(synchronize_session=False)
    ExamException.query.filter_by(exam_id=exam.id).delete(synchronize_session=False)
    db.session.query(ExamPortalSettings).filter_by(exam_id=exam.id).delete(synchronize_session=False)
    db.session.query(ExamArchive).filter_by(exam_id=exam.id).delete(synchronize_session=False)
    for attempt in Attempt.query.filter_by(exam_id=exam.id).all():
        db.session.delete(attempt)
    db.session.flush()
    db.session.delete(exam)
    db.session.commit()
    flash("Exam and all associated attempts, grades, and monitoring records were permanently deleted.", "success")
    return redirect(url_for("admin.exams"))


@admin_bp.route("/exams/<int:exam_id>/exceptions", methods=["GET", "POST"])
@admin_required
def exam_exceptions(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    students = User.query.filter_by(role="student", active=True).order_by(User.name.asc()).all()

    if request.method == "POST":
        try:
            user_id = int(request.form.get("user_id", "0"))
        except ValueError:
            user_id = 0
        student = db.session.get(User, user_id)
        if not student or student.role != "student":
            flash("Choose a valid student.", "danger")
            return redirect(url_for("admin.exam_exceptions", exam_id=exam.id))

        raw_start = (request.form.get("start_at") or "").strip()
        raw_end = (request.form.get("end_at") or "").strip()
        start_at = parse_local_datetime(raw_start) if raw_start else utcnow()
        end_at = parse_local_datetime(raw_end)
        errors = []
        if raw_start and start_at is None:
            errors.append("Exception start time is invalid.")
        if not raw_end or end_at is None:
            errors.append("A valid exception end time is required.")
        if end_at and end_at <= utcnow():
            errors.append("Exception end time must be in the future.")
        if start_at and end_at and end_at <= start_at:
            errors.append("Exception end time must be later than the exception start time.")

        duration_raw = (request.form.get("duration_minutes") or "").strip()
        duration_minutes = None
        if duration_raw:
            try:
                duration_minutes = int(duration_raw)
                if duration_minutes < 1:
                    raise ValueError
            except ValueError:
                errors.append("Extra-access duration must be at least 1 minute.")

        if errors:
            for error in errors:
                flash(error, "danger")
            return redirect(url_for("admin.exam_exceptions", exam_id=exam.id))

        exception = ExamException.query.filter_by(exam_id=exam.id, user_id=student.id).first()
        if not exception:
            exception = ExamException(exam_id=exam.id, user_id=student.id)
            db.session.add(exception)
        exception.start_at = start_at
        exception.end_at = end_at
        exception.duration_minutes = duration_minutes
        exception.reason = (request.form.get("reason") or "").strip()[:500]
        exception.enabled = True

        attempt = Attempt.query.filter_by(exam_id=exam.id, user_id=student.id).order_by(Attempt.id.desc()).first()
        if "reopen_submitted" in request.form and attempt and attempt.status == "submitted":
            attempt.status = "in_progress"
            attempt.submitted_at = None
            attempt.score = 0.0
            attempt.started_at = start_at if start_at and start_at > utcnow() else utcnow()
            for answer in attempt.answers:
                answer.score = 0.0
                answer.feedback = ""
                answer.graded_at = None
            record_event(
                attempt, "exam_reopened",
                f"Instructor reopened exam with special access until {end_at.isoformat()}",
            )

        db.session.commit()
        flash(f"Special exam access saved for {student.name}.", "success")
        return redirect(url_for("admin.exam_exceptions", exam_id=exam.id))

    exceptions = (ExamException.query.filter_by(exam_id=exam.id)
                  .order_by(ExamException.updated_at.desc()).all())
    attempts = {
        a.user_id: a for a in Attempt.query.filter_by(exam_id=exam.id)
        .order_by(Attempt.id.asc()).all()
    }
    return render_template(
        "admin/exceptions.html", exam=exam, students=students, exceptions=exceptions,
        attempts=attempts, now_eastern=datetime.now(ZoneInfo("America/New_York")),
    )


@admin_bp.route("/exam-exceptions/<int:exception_id>/delete", methods=["POST"])
@admin_required
def exam_exception_delete(exception_id):
    exception = db.get_or_404(ExamException, exception_id)
    exam_id = exception.exam_id
    student_name = exception.user.name
    db.session.delete(exception)
    db.session.commit()
    flash(f"Special access removed for {student_name}.", "success")
    return redirect(url_for("admin.exam_exceptions", exam_id=exam_id))


@admin_bp.route("/exams/<int:exam_id>/questions/new", methods=["GET", "POST"])
@admin_required
def question_new(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    question = Question(exam_id=exam.id, position=(len(exam.questions) + 1))
    if request.method == "POST":
        _populate_question(question)
        db.session.add(question)
        db.session.commit()
        flash("Question added.", "success")
        return redirect(url_for("admin.question_edit", question_id=question.id))
    return render_template("admin/question_form.html", exam=exam, question=question, options_text="", is_new=True)


@admin_bp.route("/questions/<int:question_id>/edit", methods=["GET", "POST"])
@admin_required
def question_edit(question_id):
    question = db.get_or_404(Question, question_id)
    if request.method == "POST":
        _populate_question(question)
        db.session.commit()
        flash("Question saved.", "success")
        return redirect(url_for("admin.question_edit", question_id=question.id))
    options_text = "\n".join(json.loads(question.options_json or "[]"))
    return render_template("admin/question_form.html", exam=question.exam, question=question, options_text=options_text, is_new=False)


def _populate_question(question):
    question.prompt = request.form.get("prompt", "").strip()
    question.question_type = request.form.get("question_type", "mcq")
    question.points = max(0.0, float(request.form.get("points", 1)))
    question.position = max(1, int(request.form.get("position", 1)))
    opts = [x.strip() for x in request.form.get("options_text", "").splitlines() if x.strip()]
    question.options_json = json.dumps(opts)
    question.correct_answer = request.form.get("correct_answer", "").strip()
    question.starter_code = request.form.get("starter_code", "")
    question.allow_compile = "allow_compile" in request.form
    question.allow_run = "allow_run" in request.form
    question.show_compile_errors = "show_compile_errors" in request.form


@admin_bp.route("/questions/<int:question_id>/delete", methods=["POST"])
@admin_required
def question_delete(question_id):
    question = db.get_or_404(Question, question_id)
    exam_id = question.exam_id
    db.session.delete(question)
    db.session.commit()
    flash("Question deleted.", "success")
    return redirect(url_for("admin.exam_edit", exam_id=exam_id))


@admin_bp.route("/questions/<int:question_id>/tests", methods=["POST"])
@admin_required
def test_add(question_id):
    question = db.get_or_404(Question, question_id)
    if question.question_type != "code":
        abort(400)
    test = TestCase(
        question_id=question.id,
        input_data=request.form.get("input_data", ""),
        expected_output=request.form.get("expected_output", ""),
        hidden="hidden" in request.form,
        weight=max(0.0, float(request.form.get("weight", 1))),
    )
    db.session.add(test)
    db.session.commit()
    flash("Test case added.", "success")
    return redirect(url_for("admin.question_edit", question_id=question.id))


@admin_bp.route("/tests/<int:test_id>/delete", methods=["POST"])
@admin_required
def test_delete(test_id):
    test = db.get_or_404(TestCase, test_id)
    qid = test.question_id
    db.session.delete(test)
    db.session.commit()
    return redirect(url_for("admin.question_edit", question_id=qid))


# -----------------------------------------------------------------------------
# Admin: monitoring, messaging, results
# -----------------------------------------------------------------------------

@admin_bp.route("/exams/<int:exam_id>/live")
@admin_required
def live_monitor(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    attempts = Attempt.query.filter_by(exam_id=exam.id).order_by(Attempt.started_at.desc()).all()
    recent_events = (MonitorEvent.query.join(Attempt).filter(Attempt.exam_id == exam.id)
                     .order_by(MonitorEvent.created_at.desc()).limit(150).all())
    return render_template("admin/live.html", exam=exam, attempts=attempts, recent_events=recent_events)


@admin_bp.route("/exams/<int:exam_id>/live-data")
@admin_required
def live_data(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    attempts = Attempt.query.filter_by(exam_id=exam.id).all()
    rows = []
    for a in attempts:
        answered = Answer.query.filter_by(attempt_id=a.id).filter(Answer.answer_text != "").count()
        rows.append({
            "id": a.id, "name": a.user.name, "student_id": a.user.student_id,
            "status": a.status, "answered": answered, "total": len(exam.questions),
            "violations": a.violations, "score": a.score,
            "remaining": remaining_seconds(a) if a.status == "in_progress" else 0,
            "monitoring": effective_monitoring(exam, a.user_id),
        })
    return jsonify(rows)


@admin_bp.route("/attempts/<int:attempt_id>/monitoring-toggle", methods=["POST"])
@admin_required
def monitoring_toggle(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    current = effective_monitoring(attempt.exam, attempt.user_id)
    override = MonitoringOverride.query.filter_by(exam_id=attempt.exam_id, user_id=attempt.user_id).first()
    if not override:
        override = MonitoringOverride(exam_id=attempt.exam_id, user_id=attempt.user_id, enabled=not current)
        db.session.add(override)
    else:
        override.enabled = not current
    record_event(attempt, "monitoring_changed", f"Instructor set integrity monitoring to {'ON' if override.enabled else 'OFF'}")
    db.session.commit()
    socketio.emit("monitoring_status", {"enabled": override.enabled}, room=f"user:{attempt.user_id}")
    return jsonify({"ok": True, "enabled": override.enabled})


@admin_bp.route("/attempts/<int:attempt_id>/warning", methods=["POST"])
@admin_required
def send_warning(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    data = request.get_json(silent=True) or {}
    message = str(data.get("message") or "Please remain on the examination page. Your exam activity is being monitored.").strip()[:500]
    record_event(attempt, "instructor_warning", message)
    db.session.commit()
    socketio.emit("student_warning", {
        "message": message,
        "violations": attempt.violations,
        "source": "Instructor",
    }, room=f"user:{attempt.user_id}")
    return jsonify({"ok": True})


@admin_bp.route("/exams/<int:exam_id>/results")
@admin_required
def results(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    attempts = Attempt.query.filter_by(exam_id=exam.id).order_by(Attempt.submitted_at.desc()).all()
    return render_template(
        "admin/results.html", exam=exam, attempts=attempts,
        total_points=total_points(exam), released=results_released(exam),
    )


@admin_bp.route("/exams/<int:exam_id>/release-toggle", methods=["POST"])
@admin_required
def release_toggle(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    cfg = db.session.get(ExamPortalSettings, exam.id)
    if not cfg:
        cfg = ExamPortalSettings(exam_id=exam.id, results_released=not exam.show_results_immediately)
        db.session.add(cfg)
    else:
        cfg.results_released = not results_released(exam)
    # Immediate-release setting itself always wins, so turn it off if instructor
    # explicitly holds results from the Results page.
    if results_released(exam) and request.form.get("action") == "hold":
        exam.show_results_immediately = False
        cfg.results_released = False
    elif request.form.get("action") == "release":
        cfg.results_released = True
    db.session.commit()
    flash("Student result visibility updated.", "success")
    return redirect(url_for("admin.results", exam_id=exam.id))


@admin_bp.route("/attempts/<int:attempt_id>/review", methods=["GET", "POST"])
@admin_required
def review_attempt(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    if request.method == "POST":
        total = 0.0
        for ans in attempt.answers:
            key = f"score_{ans.id}"
            if key in request.form:
                try:
                    ans.score = max(0.0, min(ans.question.points, float(request.form[key])))
                except ValueError:
                    pass
            feedback_key = f"feedback_{ans.id}"
            if feedback_key in request.form:
                ans.feedback = request.form.get(feedback_key, "").strip()[:5000]
            total += ans.score
        attempt.score = round(total, 2)
        comment_text = request.form.get("instructor_comment", "").strip()[:10000]
        comment = attempt.comment_record
        if not comment:
            comment = AttemptComment(attempt_id=attempt.id, comment=comment_text)
            db.session.add(comment)
        else:
            comment.comment = comment_text
        record_event(attempt, "grade_reviewed", f"Instructor updated score to {attempt.score}")
        db.session.commit()
        flash("Scores and feedback updated.", "success")
    events = MonitorEvent.query.filter_by(attempt_id=attempt.id).order_by(MonitorEvent.created_at.desc()).limit(100).all()
    return render_template("admin/review.html", attempt=attempt, events=events)


@admin_bp.route("/exams/<int:exam_id>/results.csv")
@admin_required
def results_csv(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    attempts = Attempt.query.filter_by(exam_id=exam.id).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["student_id", "name", "email", "status", "score", "percent", "violations", "started_at", "submitted_at", "comment"])
    for a in attempts:
        writer.writerow([
            a.user.student_id, a.user.name, a.user.email, a.status, a.score,
            attempt_percent(a) if a.status == "submitted" else "", a.violations,
            a.started_at, a.submitted_at or "", a.comment_record.comment if a.comment_record else "",
        ])
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=exam_{exam.id}_results.csv"},
    )


# -----------------------------------------------------------------------------
# Student dashboard/results/exam
# -----------------------------------------------------------------------------

@student_bp.route("/")
@student_required
def dashboard():
    archived_ids = {x.exam_id for x in ExamArchive.query.all()}
    exams = [e for e in Exam.query.filter_by(published=True).order_by(Exam.created_at.desc()).all() if e.id not in archived_ids]
    attempts = {a.exam_id: a for a in Attempt.query.filter_by(user_id=current_user.id).all()}
    exceptions = {
        x.exam_id: x for x in ExamException.query.filter_by(
            user_id=current_user.id, enabled=True
        ).all()
    }
    settings = get_portal_settings()
    summaries = course_summaries_for_user(current_user.id)
    performance = performance_rows_for_user(current_user.id)
    return render_template(
        "student/dashboard.html", exams=exams, attempts=attempts,
        exam_is_open=lambda exam: exam_is_open(exam, current_user.id),
        exceptions=exceptions, settings=settings, summaries=summaries,
        performance=performance, results_released=results_released,
    )


@student_bp.route("/grades")
@student_required
def grades():
    settings = get_portal_settings()
    summaries = course_summaries_for_user(current_user.id)
    performance = performance_rows_for_user(current_user.id)
    all_attempts = (Attempt.query.filter_by(user_id=current_user.id, status="submitted")
                    .order_by(Attempt.submitted_at.desc()).all())
    return render_template(
        "student/grades.html", settings=settings, summaries=summaries,
        performance=performance, attempts=all_attempts, results_released=results_released,
        attempt_percent=attempt_percent,
    )


@student_bp.route("/exams/<int:exam_id>/start", methods=["POST"])
@student_required
def start_exam(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    if not exam_is_open(exam, current_user.id):
        flash("This exam is not currently open for your account.", "danger")
        return redirect(url_for("student.dashboard"))
    attempt = Attempt.query.filter_by(user_id=current_user.id, exam_id=exam.id).first()
    if attempt and attempt.status == "submitted":
        flash("You have already submitted this exam.", "warning")
        return redirect(url_for("student.result", attempt_id=attempt.id))
    if not attempt:
        attempt = Attempt(user_id=current_user.id, exam_id=exam.id)
        db.session.add(attempt)
        db.session.flush()
        for q in exam.questions:
            db.session.add(Answer(
                attempt_id=attempt.id, question_id=q.id,
                answer_text=(q.starter_code or "") if q.question_type == "code" else "",
            ))
        record_event(attempt, "exam_started", "Student started the exam")
        db.session.commit()
    return redirect(url_for("student.take_exam", attempt_id=attempt.id))


@student_bp.route("/attempts/<int:attempt_id>")
@student_required
def take_exam(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    if attempt.user_id != current_user.id:
        abort(403)
    if attempt.status == "submitted":
        return redirect(url_for("student.result", attempt_id=attempt.id))
    if not exam_is_open(attempt.exam, current_user.id):
        flash("This exam is not currently open for your account.", "warning")
        return redirect(url_for("student.dashboard"))
    if remaining_seconds(attempt) <= 0:
        grade_attempt(attempt)
        return redirect(url_for("student.result", attempt_id=attempt.id))
    questions = list(attempt.exam.questions)
    if attempt.exam.shuffle_questions:
        rnd = random.Random(attempt.id)
        rnd.shuffle(questions)
    answers = {a.question_id: a for a in attempt.answers}
    monitoring_active = effective_monitoring(attempt.exam, current_user.id)
    return render_template(
        "student/exam.html", attempt=attempt, questions=questions, answers=answers,
        remaining_seconds=remaining_seconds(attempt), monitoring_active=monitoring_active,
    )


@student_bp.route("/attempts/<int:attempt_id>/save", methods=["POST"])
@student_required
def save_answer(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    if attempt.user_id != current_user.id or attempt.status != "in_progress":
        abort(403)
    if not exam_is_open(attempt.exam, current_user.id):
        return jsonify({"ok": False, "expired": True, "error": "Exam access window is closed."}), 409
    if remaining_seconds(attempt) <= 0:
        grade_attempt(attempt)
        return jsonify({"ok": False, "expired": True}), 409
    data = request.get_json(silent=True) or {}
    question = db.get_or_404(Question, int(data.get("question_id", 0)))
    if question.exam_id != attempt.exam_id:
        abort(400)
    answer = get_or_make_answer(attempt, question)
    answer.answer_text = str(data.get("answer", ""))[:100000]
    record_event(attempt, "answer_saved", f"Question {question.position}", emit=True)
    db.session.commit()
    return jsonify({"ok": True, "saved_at": utcnow().isoformat()})


@student_bp.route("/attempts/<int:attempt_id>/compile", methods=["POST"])
@student_required
def compile_code(attempt_id):
    attempt, question, code = _execution_request(attempt_id)
    if not question.allow_compile:
        return jsonify({"ok": False, "error": "Compilation is disabled for this question."}), 403
    record_event(attempt, "compile_requested", f"Question {question.position}")
    db.session.commit()
    ok, message, temp_dir = compile_cpp(code)
    try:
        record_event(attempt, "compile_result", f"Question {question.position}: {'success' if ok else 'failed'}")
        db.session.commit()
        if ok:
            return jsonify({"ok": True, "message": "Compilation successful."})
        return jsonify({"ok": False, "error": message if question.show_compile_errors else "Compilation failed."})
    finally:
        if temp_dir:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)


@student_bp.route("/attempts/<int:attempt_id>/run", methods=["POST"])
@student_required
def run_code(attempt_id):
    attempt, question, code = _execution_request(attempt_id)
    if not question.allow_run:
        return jsonify({"ok": False, "error": "Running code is disabled for this question."}), 403
    data = request.get_json(silent=True) or {}
    record_event(attempt, "run_requested", f"Question {question.position}")
    db.session.commit()
    result = run_cpp(code, str(data.get("stdin", ""))[:20000])
    record_event(attempt, "run_result", f"Question {question.position}: {'success' if result.ok else 'failed'}")
    db.session.commit()
    payload = {"ok": result.ok, "stdout": result.stdout}
    if result.stderr and question.show_compile_errors:
        payload["stderr"] = result.stderr
    elif not result.ok:
        payload["stderr"] = "Program could not be executed successfully."
    return jsonify(payload)


def _execution_request(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    if (attempt.user_id != current_user.id or attempt.status != "in_progress"
            or not exam_is_open(attempt.exam, current_user.id)
            or remaining_seconds(attempt) <= 0):
        abort(403)
    data = request.get_json(silent=True) or {}
    question = db.get_or_404(Question, int(data.get("question_id", 0)))
    if question.exam_id != attempt.exam_id or question.question_type != "code":
        abort(400)
    return attempt, question, str(data.get("code", ""))[:100000]


@student_bp.route("/attempts/<int:attempt_id>/monitor", methods=["POST"])
@student_required
def monitor_event(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    if attempt.user_id != current_user.id or attempt.status != "in_progress":
        abort(403)
    data = request.get_json(silent=True) or {}
    event_type = str(data.get("type", "unknown"))[:80]
    details = str(data.get("details", ""))[:500]

    # Browser integrity events obey the instructor's per-student ON/OFF switch.
    # Non-violation acknowledgements are still useful in the activity timeline.
    active = effective_monitoring(attempt.exam, attempt.user_id)
    if event_type in VIOLATION_TYPES and not active:
        return jsonify({"ok": True, "ignored": True, "monitoring": False, "violations": attempt.violations})

    violation = event_type in VIOLATION_TYPES
    record_event(attempt, event_type, details, violation=violation)
    db.session.commit()
    return jsonify({
        "ok": True,
        "monitoring": active,
        "violation": violation,
        "violations": attempt.violations,
        "warning": violation,
        "message": "You left or interacted outside the permitted exam environment. This event has been recorded and reported to your instructor." if violation else "",
    })


@student_bp.route("/attempts/<int:attempt_id>/submit", methods=["POST"])
@student_required
def submit_exam(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    if attempt.user_id != current_user.id:
        abort(403)
    if attempt.status == "in_progress":
        grade_attempt(attempt)
    flash("Your exam has been submitted.", "success")
    return redirect(url_for("student.result", attempt_id=attempt.id))


@student_bp.route("/attempts/<int:attempt_id>/result")
@student_required
def result(attempt_id):
    attempt = db.get_or_404(Attempt, attempt_id)
    if attempt.user_id != current_user.id:
        abort(403)
    if attempt.status != "submitted":
        return redirect(url_for("student.take_exam", attempt_id=attempt.id))
    settings = get_portal_settings()
    released = results_released(attempt.exam)
    stats = exam_class_stats(attempt.exam, attempt) if released and settings.show_class_comparison else None
    letter, gp = grade_band(attempt_percent(attempt))
    return render_template(
        "student/result.html", attempt=attempt, total_points=total_points(attempt.exam),
        released=released, settings=settings, stats=stats, percent=attempt_percent(attempt),
        letter=letter, grade_points=gp,
    )


# -----------------------------------------------------------------------------
# Socket rooms
# -----------------------------------------------------------------------------

@socketio.on("connect")
def socket_connect():
    if not current_user.is_authenticated:
        return
    if current_user.role == "admin":
        join_room("admins")
    elif current_user.role == "student":
        join_room(f"user:{current_user.id}")
