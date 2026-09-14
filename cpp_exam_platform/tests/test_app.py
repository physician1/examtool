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
        data={'csv_file': (io.BytesIO(data.encode('utf-8')), 'gradebook.csv')},
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
