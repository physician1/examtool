import os
import tempfile
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
