from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin
from .extensions import db


def utcnow():
    return datetime.now(timezone.utc)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    student_id = db.Column(db.String(40), unique=True, nullable=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="student")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.active

class Exam(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    course_code = db.Column(db.String(60), nullable=False, default="C++")
    description = db.Column(db.Text, default="")
    duration_minutes = db.Column(db.Integer, nullable=False, default=60)
    start_at = db.Column(db.DateTime(timezone=True), nullable=True)
    end_at = db.Column(db.DateTime(timezone=True), nullable=True)
    published = db.Column(db.Boolean, nullable=False, default=False)
    monitoring_enabled = db.Column(db.Boolean, nullable=False, default=True)
    require_fullscreen = db.Column(db.Boolean, nullable=False, default=True)
    block_copy_paste = db.Column(db.Boolean, nullable=False, default=True)
    shuffle_questions = db.Column(db.Boolean, nullable=False, default=False)
    show_results_immediately = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    questions = db.relationship("Question", backref="exam", lazy=True, cascade="all, delete-orphan", order_by="Question.position")

class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"), nullable=False, index=True)
    prompt = db.Column(db.Text, nullable=False)
    question_type = db.Column(db.String(20), nullable=False)  # mcq, short, code
    points = db.Column(db.Float, nullable=False, default=1.0)
    position = db.Column(db.Integer, nullable=False, default=1)
    options_json = db.Column(db.Text, nullable=True)
    correct_answer = db.Column(db.Text, nullable=True)
    starter_code = db.Column(db.Text, nullable=True)
    allow_compile = db.Column(db.Boolean, nullable=False, default=False)
    allow_run = db.Column(db.Boolean, nullable=False, default=False)
    show_compile_errors = db.Column(db.Boolean, nullable=False, default=True)

    tests = db.relationship("TestCase", backref="question", lazy=True, cascade="all, delete-orphan")

class TestCase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("question.id"), nullable=False, index=True)
    input_data = db.Column(db.Text, default="")
    expected_output = db.Column(db.Text, nullable=False)
    hidden = db.Column(db.Boolean, nullable=False, default=True)
    weight = db.Column(db.Float, nullable=False, default=1.0)

class Attempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"), nullable=False, index=True)
    started_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    submitted_at = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="in_progress")
    score = db.Column(db.Float, nullable=False, default=0.0)
    violations = db.Column(db.Integer, nullable=False, default=0)

    user = db.relationship("User")
    exam = db.relationship("Exam")
    answers = db.relationship("Answer", backref="attempt", lazy=True, cascade="all, delete-orphan")
    events = db.relationship("MonitorEvent", backref="attempt", lazy=True, cascade="all, delete-orphan")

class Answer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempt.id"), nullable=False, index=True)
    question_id = db.Column(db.Integer, db.ForeignKey("question.id"), nullable=False, index=True)
    answer_text = db.Column(db.Text, default="")
    score = db.Column(db.Float, nullable=False, default=0.0)
    feedback = db.Column(db.Text, default="")
    graded_at = db.Column(db.DateTime(timezone=True), nullable=True)
    question = db.relationship("Question")
    __table_args__ = (db.UniqueConstraint("attempt_id", "question_id", name="uq_attempt_question"),)

class MonitorEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempt.id"), nullable=False, index=True)
    event_type = db.Column(db.String(80), nullable=False)
    details = db.Column(db.String(500), default="")
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
