import csv
import io
import json
import random
import statistics
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
    AttemptComment,
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


def exam_is_open(exam):
    if is_archived(exam.id):
        return False
    now = utcnow()
    if not exam.published:
        return False
    if exam.start_at and now < aware(exam.start_at):
        return False
    if exam.end_at and now > aware(exam.end_at):
        return False
    return True


def remaining_seconds(attempt):
    duration_end = aware(attempt.started_at) + timedelta(minutes=attempt.exam.duration_minutes)
    hard_end = aware(attempt.exam.end_at) if attempt.exam.end_at else duration_end
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


# -----------------------------------------------------------------------------
# Main/auth
# -----------------------------------------------------------------------------

@main_bp.route("/healthz")
def healthz():
    return {"status": "ok"}, 200


@main_bp.route("/")
def index():
    if current_user.is_authenticated:
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
            return redirect(url_for("main.index"))
        flash("Invalid login details.", "danger")
    return render_template("login.html")


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
    recent_events = MonitorEvent.query.order_by(MonitorEvent.created_at.desc()).limit(10).all()
    return render_template(
        "admin/dashboard.html", exams=exams, students=students,
        active_attempts=active_attempts, submitted=submitted, recent_events=recent_events,
    )


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
        elif User.query.filter((User.email == email) | (User.student_id == student_id)).first():
            flash("Email or student ID already exists.", "danger")
        else:
            user = User(name=name, email=email, student_id=student_id, role="student")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("Student account created.", "success")
        return redirect(url_for("admin.students"))
    items = User.query.filter_by(role="student").order_by(User.name).all()
    return render_template("admin/students.html", students=items)


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
        u.set_password(password)
        db.session.add(u)
        added += 1
    db.session.commit()
    flash(f"Imported {added} student(s); skipped {skipped}.", "success")
    return redirect(url_for("admin.students"))


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
    db.session.query(ExamPortalSettings).filter_by(exam_id=exam.id).delete(synchronize_session=False)
    db.session.query(ExamArchive).filter_by(exam_id=exam.id).delete(synchronize_session=False)
    for attempt in Attempt.query.filter_by(exam_id=exam.id).all():
        db.session.delete(attempt)
    db.session.flush()
    db.session.delete(exam)
    db.session.commit()
    flash("Exam and all associated attempts, grades, and monitoring records were permanently deleted.", "success")
    return redirect(url_for("admin.exams"))


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
    settings = get_portal_settings()
    summaries = course_summaries_for_user(current_user.id)
    performance = performance_rows_for_user(current_user.id)
    return render_template(
        "student/dashboard.html", exams=exams, attempts=attempts, exam_is_open=exam_is_open,
        settings=settings, summaries=summaries, performance=performance,
        results_released=results_released,
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
    if not exam_is_open(exam):
        flash("This exam is not currently open.", "danger")
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
    if attempt.user_id != current_user.id or attempt.status != "in_progress" or remaining_seconds(attempt) <= 0:
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
