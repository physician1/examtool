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
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
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


def test_released_submission_review(client, app):
    from exam_app.models import Attempt, Answer, ExamPortalSettings, AttemptComment, TestCase
    login(client, 'S001')
    with app.app_context():
        exam = Exam.query.first()
        exam_id = exam.id
        short = Question(exam_id=exam_id, prompt='Explain your reasoning', question_type='short', points=3, position=2)
        code = Question(exam_id=exam_id, prompt='Write a program', question_type='code', points=5, position=3)
        db.session.add_all([short, code]); db.session.flush()
        db.session.add(TestCase(question_id=code.id, input_data='SECRET_INPUT', expected_output='SECRET_OUTPUT', hidden=True))
        db.session.commit()
    client.post(f'/student/exams/{exam_id}/start')
    with app.app_context():
        attempt = Attempt.query.first()
        attempt_id = attempt.id
        for answer in attempt.answers:
            answer.answer_text = {'mcq': '1', 'short': '<script>reason</script>', 'code': 'int main() { return 0; }'}[answer.question.question_type]
            answer.feedback = 'Reviewed carefully'
            answer.score = 1
        attempt.status = 'submitted'
        attempt.score = 3
        db.session.add(AttemptComment(attempt_id=attempt_id, comment='Overall review'))
        db.session.commit()
    url = f'/student/attempts/{attempt_id}/result'
    pending = client.get(url)
    assert b'Explain your reasoning' not in pending.data
    assert b'Reviewed carefully' not in pending.data
    with app.app_context():
        db.session.add(ExamPortalSettings(exam_id=exam_id, results_released=True)); db.session.commit()
    released = client.get(url)
    assert released.status_code == 200
    for text in [b'2 + 2?', b'B. 4', b'Explain your reasoning', b'&lt;script&gt;reason&lt;/script&gt;', b'int main() { return 0; }', b'Reviewed carefully', b'Overall review']:
        assert text in released.data
    assert b'SECRET_INPUT' not in released.data and b'SECRET_OUTPUT' not in released.data
    client.post('/logout')
    login(client, 'teacher@example.edu')
    review = client.get(f'/admin/attempts/{attempt_id}/review')
    assert review.status_code == 200 and b'B. 4' in review.data


def test_mcq_display_labels(app):
    display = app.jinja_env.filters['mcq_answer']
    for index, letter in enumerate('ABCD'):
        assert display(str(index), '["one", "two", "three", "four"]').startswith(letter + '. ')
    assert display('', '[]') == '(blank)'
    assert display('-1', '["one"]') == 'Invalid choice'
    assert display('bad', '["one"]') == 'Invalid choice'


def test_access_code_gate_and_resume(client, app):
    from exam_app.models import ExamAccessCode, Attempt, Answer
    with app.app_context():
        exam = Exam.query.first()
        exam_id = exam.id
        record = ExamAccessCode(exam=exam)
        record.set_code('Class2026')
        db.session.add(record); db.session.commit()
        assert record.code_hash != 'Class2026'
    login(client, 'S001')
    url = f'/student/exams/{exam_id}/start'
    page = client.get(url)
    assert page.status_code == 200 and b'Exam access code' in page.data
    assert b'Class2026' not in page.data and b'2 + 2?' not in page.data
    for code in ['', 'wrong', 'class2026', 'x' * 65]:
        assert client.post(url, data={'access_code': code}).status_code == 400
    with app.app_context():
        assert Attempt.query.count() == 0
        assert Answer.query.count() == 0
    assert client.post(url, data={'access_code': 'Class2026'}).status_code == 302
    with app.app_context():
        attempt = Attempt.query.one()
        attempt_id, started = attempt.id, attempt.started_at
        ExamAccessCode.query.one().set_code('NewCode2026')
        db.session.commit()
    assert client.post(url).location.endswith(f'/attempts/{attempt_id}')
    assert client.get(url).location.endswith(f'/attempts/{attempt_id}')
    with app.app_context():
        assert Attempt.query.count() == 1
        assert Attempt.query.one().started_at == started
        Attempt.query.one().status = 'submitted'; db.session.commit()
    assert client.post(url).location.endswith(f'/attempts/{attempt_id}/result')


def test_access_code_admin_create_update_and_delete(client, app):
    from exam_app.models import ExamAccessCode
    login(client, 'teacher@example.edu')
    form = {'title': 'Code protected', 'course_code': 'CS101', 'duration_minutes': '30', 'published': 'on'}
    for code in ['', 'abc', 'x' * 65]:
        assert client.post('/admin/exams/new', data={**form, 'access_code': code}).status_code == 400
    assert client.post('/admin/exams/new', data={**form, 'access_code': 'Open2026'}).status_code == 302
    with app.app_context():
        exam = Exam.query.filter_by(title='Code protected').one()
        exam_id = exam.id
        assert exam.access_code.check_code('Open2026')
        original_hash = exam.access_code.code_hash
    edit = f'/admin/exams/{exam_id}/edit'
    assert b'Open2026' not in client.get(edit).data
    assert client.post(edit, data=form).status_code == 302
    with app.app_context():
        assert db.session.get(ExamAccessCode, exam_id).code_hash == original_hash
    assert client.post(edit, data={**form, 'access_code': 'Next2026'}).status_code == 302
    with app.app_context():
        assert db.session.get(ExamAccessCode, exam_id).check_code('Next2026')
    assert client.post(f'/admin/exams/{exam_id}/delete', data={'confirmation': 'DELETE'}).status_code == 302
    with app.app_context():
        assert db.session.get(ExamAccessCode, exam_id) is None


def test_access_code_does_not_override_schedule(client, app):
    from datetime import timedelta
    from exam_app.models import ExamAccessCode, Attempt, utcnow
    with app.app_context():
        exam = Exam.query.first(); exam_id = exam.id
        exam.start_at = utcnow() + timedelta(days=1)
        record = ExamAccessCode(exam=exam); record.set_code('Class2026')
        db.session.add(record); db.session.commit()
    login(client, 'S001')
    assert client.post(f'/student/exams/{exam_id}/start', data={'access_code': 'Class2026'}).status_code == 302
    with app.app_context():
        assert Attempt.query.count() == 0


def test_access_code_additive_upgrade_preserves_records(app):
    from exam_app.models import ExamAccessCode
    from sqlalchemy import inspect
    with app.app_context():
        original_users = [(u.id, u.email, u.password_hash) for u in User.query.order_by(User.id)]
        original_exams = [(e.id, e.title) for e in Exam.query.order_by(Exam.id)]
        original_columns = [c['name'] for c in inspect(db.engine).get_columns('exam')]
        ExamAccessCode.__table__.drop(db.engine)
        db.create_all()
        assert [(u.id, u.email, u.password_hash) for u in User.query.order_by(User.id)] == original_users
        assert [(e.id, e.title) for e in Exam.query.order_by(Exam.id)] == original_exams
        assert [c['name'] for c in inspect(db.engine).get_columns('exam')] == original_columns
        assert ExamAccessCode.query.count() == 0


def test_formatted_prompt_on_exam_and_reviews(client, app):
    from exam_app.models import Attempt, ExamPortalSettings
    prompt = 'Explain this code:\n```cpp\nint main() {\n    return 0;\n}\n```\n<script>alert(1)</script>'
    with app.app_context():
        question = Question.query.first(); question.prompt = prompt
        exam_id = question.exam_id; question_id = question.id
        db.session.commit()
    login(client, 'teacher@example.edu')
    editor = client.get(f'/admin/questions/{question_id}/edit')
    assert editor.status_code == 200
    assert b'Insert C++ code' in editor.data and b'promptPreview' in editor.data
    client.post('/logout'); login(client, 'S001')
    exam = client.post(f'/student/exams/{exam_id}/start', follow_redirects=True)
    assert exam.status_code == 200 and b'data-question-prompt' in exam.data
    assert b'&lt;script&gt;' in exam.data and b'<script>alert(1)</script>' not in exam.data
    assert b'js/prompt.js' in exam.data
    with app.app_context():
        attempt_id = Attempt.query.one().id
        db.session.add(ExamPortalSettings(exam_id=exam_id, results_released=True)); db.session.commit()
    result = client.post(f'/student/attempts/{attempt_id}/submit', follow_redirects=True)
    assert result.status_code == 200 and b'data-question-prompt' in result.data
    client.post('/logout'); login(client, 'teacher@example.edu')
    review = client.get(f'/admin/attempts/{attempt_id}/review')
    assert review.status_code == 200 and b'data-question-prompt' in review.data


@pytest.mark.parametrize('monitoring', [False, True])
@pytest.mark.parametrize('fullscreen', [False, True])
@pytest.mark.parametrize('clipboard', [False, True])
def test_exam_controls_independent(client, app, monitoring, fullscreen, clipboard):
    with app.app_context():
        exam = Exam.query.first(); exam_id = exam.id
        exam.monitoring_enabled = monitoring
        exam.require_fullscreen = fullscreen
        exam.block_copy_paste = clipboard
        db.session.commit()
    login(client, 'S001')
    page = client.post(f'/student/exams/{exam_id}/start', follow_redirects=True)
    assert page.status_code == 200
    assert (b'id="secureGate"' in page.data) == fullscreen
    assert f'data-monitor="{int(monitoring)}"'.encode() in page.data
    assert f'data-fullscreen="{int(fullscreen)}"'.encode() in page.data
    assert f'data-block-copy="{int(clipboard)}"'.encode() in page.data


def test_monitoring_override_keeps_exam_controls(client, app):
    from exam_app.models import MonitoringOverride
    with app.app_context():
        exam = Exam.query.first(); exam_id = exam.id
        exam.require_fullscreen = True; exam.block_copy_paste = True
        student = User.query.filter_by(student_id='S001').one()
        db.session.add(MonitoringOverride(exam_id=exam_id, user_id=student.id, enabled=False))
        db.session.commit()
    login(client, 'S001')
    page = client.post(f'/student/exams/{exam_id}/start', follow_redirects=True)
    assert page.status_code == 200
    assert b'data-monitor="0"' in page.data
    assert b'data-block-copy="1"' in page.data
    assert b'id="secureGate"' in page.data
