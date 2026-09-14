import os
import tempfile
import io
import pytest

from exam_app import create_app
from exam_app.extensions import db
from exam_app.models import User, Exam, Question

@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    app = create_app({
        'TESTING': True,
        'SECRET_KEY': 'test-secret',
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{path}',
        'WTF_CSRF_ENABLED': False,
        'ALLOW_UNSAFE_LOCAL_RUNNER': False,
    })
    with app.app_context():
        db.drop_all(); db.create_all()
        admin=User(name='Instructor',email='teacher@example.edu',role='admin');admin.set_password('Password123!')
        student=User(name='Student One',email='student@example.edu',student_id='S001',role='student');student.set_password('Password123!')
        exam=Exam(title='Test Exam',course_code='CS101',duration_minutes=30,published=True,monitoring_enabled=True,require_fullscreen=False)
        db.session.add_all([admin,student,exam]);db.session.flush()
        q=Question(exam_id=exam.id,prompt='2 + 2?',question_type='mcq',points=2,position=1,options_json='["3","4","5"]',correct_answer='1')
        db.session.add(q);db.session.commit()
    yield app
    os.unlink(path)

@pytest.fixture()
def client(app): return app.test_client()

def login(client, ident, password='Password123!'):
    return client.post('/login',data={'identifier':ident,'password':password},follow_redirects=True)

def test_admin_login_and_dashboard(client):
    r=login(client,'teacher@example.edu')
    assert r.status_code==200
    assert b'Assessment overview' in r.data

def test_student_can_start_save_and_submit_mcq(client, app):
    r=login(client,'S001')
    assert b'Welcome' in r.data
    with app.app_context(): exam_id=Exam.query.first().id; qid=Question.query.first().id
    r=client.post(f'/student/exams/{exam_id}/start',follow_redirects=True)
    assert r.status_code==200 and b'Test Exam' in r.data
    # Determine attempt id from URL after redirect.
    attempt_id=int(r.request.path.rstrip('/').split('/')[-1])
    r=client.post(f'/student/attempts/{attempt_id}/save',json={'question_id':qid,'answer':'1'})
    assert r.get_json()['ok'] is True
    r=client.post(f'/student/attempts/{attempt_id}/submit',follow_redirects=True)
    assert b'SUBMISSION RECEIVED' in r.data
    assert b'2.0' not in r.data  # score not immediately shown by default

def test_compile_permission_is_enforced(client, app):
    login(client,'S001')
    with app.app_context(): exam_id=Exam.query.first().id; q=Question.query.first(); q.question_type='code'; q.allow_compile=False; db.session.commit(); qid=q.id
    r=client.post(f'/student/exams/{exam_id}/start',follow_redirects=True)
    attempt_id=int(r.request.path.rstrip('/').split('/')[-1])
    r=client.post(f'/student/attempts/{attempt_id}/compile',json={'question_id':qid,'code':'int main(){return 0;}'})
    assert r.status_code==403

def test_admin_gradebook_page(client):
    r = login(client, 'teacher@example.edu')
    assert r.status_code == 200
    r = client.get('/admin/grades')
    assert r.status_code == 200
    assert b'COURSE GRADEBOOK' in r.data
    assert b'Student One' in r.data
    assert b'Test Exam' in r.data


def test_canvas_csv_import_creates_student_and_profile(client, app):
    from exam_app.models import StudentProfile
    login(client, 'teacher@example.edu')
    data = (
        'Student,ID,SIS User ID,SIS Login ID,Section,Quiz 1\n'
        'Canvas Learner,777,UMB0099,canvas.learner@umb.edu,CS110-01,9\n'
    )
    r = client.post(
        '/admin/students/import-canvas',
        data={'csv_file': (io.BytesIO(data.encode('utf-8')), 'gradebook.csv'), 'temporary_password': 'SharedPass2026!', 'reset_existing': 'on'},
        content_type='multipart/form-data',
    )
    assert r.status_code == 200
    assert b'1 created' in r.data
    with app.app_context():
        student = User.query.filter_by(student_id='UMB0099').first()
        assert student is not None
        profile = StudentProfile.query.filter_by(user_id=student.id).first()
        assert profile is not None
        assert profile.section == 'CS110-01'


def test_admin_can_delete_student_and_related_records(client, app):
    from exam_app.models import Attempt, Answer, MonitorEvent, StudentProfile
    login(client, 'teacher@example.edu')
    with app.app_context():
        student = User.query.filter_by(student_id='S001').first()
        exam = Exam.query.first()
        question = Question.query.first()
        attempt = Attempt(user_id=student.id, exam_id=exam.id, status='submitted', score=2.0)
        db.session.add(attempt)
        db.session.flush()
        db.session.add(Answer(attempt_id=attempt.id, question_id=question.id, answer_text='1', score=2.0))
        db.session.add(MonitorEvent(attempt_id=attempt.id, event_type='tab_hidden', details='test'))
        db.session.add(StudentProfile(user_id=student.id, source='manual', section='CS101-01'))
        db.session.commit()
        student_id = student.id
        attempt_id = attempt.id

    r = client.post(f'/admin/students/{student_id}/delete', follow_redirects=True)
    assert r.status_code == 200
    assert b'permanently deleted' in r.data

    with app.app_context():
        assert db.session.get(User, student_id) is None
        assert db.session.get(Attempt, attempt_id) is None
        assert StudentProfile.query.filter_by(user_id=student_id).first() is None


def test_admin_can_grant_post_deadline_exception(client, app):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    from exam_app.models import ExamException

    with app.app_context():
        exam = Exam.query.first()
        exam.start_at = datetime.now(timezone.utc) - timedelta(hours=2)
        exam.end_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.session.commit()
        exam_id = exam.id
        student_id = User.query.filter_by(student_id='S001').first().id

    login(client, 'teacher@example.edu')
    now_et = datetime.now(ZoneInfo('America/New_York'))
    r = client.post(
        f'/admin/exams/{exam_id}/exceptions',
        data={
            'user_id': str(student_id),
            'start_at': (now_et - timedelta(minutes=2)).strftime('%Y-%m-%dT%H:%M'),
            'end_at': (now_et + timedelta(minutes=45)).strftime('%Y-%m-%dT%H:%M'),
            'duration_minutes': '30',
            'reason': 'Approved make-up exam',
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert b'Special exam access saved' in r.data

    with app.app_context():
        exception = ExamException.query.filter_by(exam_id=exam_id, user_id=student_id).first()
        assert exception is not None
        assert exception.duration_minutes == 30

    client.post('/logout', follow_redirects=True)
    r = login(client, 'S001')
    assert b'Special access granted' in r.data
    r = client.post(f'/student/exams/{exam_id}/start', follow_redirects=True)
    assert r.status_code == 200
    assert b'Test Exam' in r.data


def test_admin_can_reopen_submitted_attempt_with_exception(client, app):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from exam_app.models import Attempt, Answer

    with app.app_context():
        exam = Exam.query.first()
        student = User.query.filter_by(student_id='S001').first()
        question = Question.query.first()
        attempt = Attempt(user_id=student.id, exam_id=exam.id, status='submitted', score=2.0, submitted_at=datetime.now(ZoneInfo('UTC')))
        db.session.add(attempt)
        db.session.flush()
        db.session.add(Answer(attempt_id=attempt.id, question_id=question.id, answer_text='1', score=2.0, feedback='Correct.'))
        db.session.commit()
        exam_id, student_id, attempt_id = exam.id, student.id, attempt.id

    login(client, 'teacher@example.edu')
    now_et = datetime.now(ZoneInfo('America/New_York'))
    r = client.post(
        f'/admin/exams/{exam_id}/exceptions',
        data={
            'user_id': str(student_id),
            'start_at': now_et.strftime('%Y-%m-%dT%H:%M'),
            'end_at': (now_et + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
            'reopen_submitted': 'on',
        },
        follow_redirects=True,
    )
    assert r.status_code == 200

    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        answer = Answer.query.filter_by(attempt_id=attempt_id).first()
        assert attempt.status == 'in_progress'
        assert attempt.submitted_at is None
        assert attempt.score == 0.0
        assert answer.answer_text == '1'  # student's work is preserved
        assert answer.score == 0.0


def test_canvas_student_must_change_shared_temp_password_before_dashboard(client, app):
    from exam_app.models import StudentPasswordState
    login(client, 'teacher@example.edu')
    data = (
        'Student,ID,SIS User ID,SIS Login ID,Section\n'
        'First Login Student,888,UMB0100,first.login@umb.edu,CS110-01\n'
    )
    r = client.post(
        '/admin/students/import-canvas',
        data={
            'csv_file': (io.BytesIO(data.encode('utf-8')), 'roster.csv'),
            'temporary_password': 'SharedPass2026!',
            'reset_existing': 'on',
        },
        content_type='multipart/form-data',
    )
    assert r.status_code == 200
    client.post('/logout', follow_redirects=True)

    r = client.post('/login', data={'identifier': 'UMB0100', 'password': 'SharedPass2026!'}, follow_redirects=True)
    assert r.status_code == 200
    assert b'Create your private password' in r.data

    # Student cannot bypass the change screen by navigating directly.
    r = client.get('/student/', follow_redirects=True)
    assert b'Create your private password' in r.data

    r = client.post('/change-password', data={
        'current_password': 'SharedPass2026!',
        'new_password': 'MyPrivatePass2026!',
        'confirm_password': 'MyPrivatePass2026!',
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b'Welcome' in r.data

    with app.app_context():
        user = User.query.filter_by(student_id='UMB0100').first()
        state = db.session.get(StudentPasswordState, user.id)
        assert state is not None
        assert state.must_change_password is False
        assert user.check_password('MyPrivatePass2026!')
        assert not user.check_password('SharedPass2026!')
