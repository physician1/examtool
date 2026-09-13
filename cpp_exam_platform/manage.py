import os
from getpass import getpass
from dotenv import load_dotenv
load_dotenv()

from exam_app import create_app
from exam_app.extensions import db
from exam_app.models import User, Exam, Question, TestCase
from exam_app.grader import compiler_status, find_cpp_compiler

app = create_app()

def init_admin():
    with app.app_context():
        email = os.getenv("ADMIN_EMAIL") or input("Admin email: ").strip()
        name = os.getenv("ADMIN_NAME") or input("Admin name: ").strip() or "Course Instructor"
        password = os.getenv("ADMIN_PASSWORD") or getpass("Admin password: ")
        existing = User.query.filter_by(email=email.lower()).first()
        if existing:
            existing.name, existing.role, existing.active = name, "admin", True
            existing.set_password(password)
            print(f"Updated admin: {email}")
        else:
            u = User(name=name, email=email.lower(), role="admin")
            u.set_password(password)
            db.session.add(u)
            print(f"Created admin: {email}")
        db.session.commit()

def check_compiler():
    with app.app_context():
        ok, message = compiler_status()
        if ok:
            print(f"C++ compiler OK: {message}")
            print(f"Executable: {find_cpp_compiler()}")
        else:
            print(f"C++ compiler NOT READY: {message}")
            raise SystemExit(1)


def seed_demo():
    with app.app_context():
        init_admin()
        if User.query.filter_by(student_id="DEMO001").first():
            print("Demo data already exists.")
            return
        s=User(name="Demo Student", email="student@example.edu", student_id="DEMO001", role="student");s.set_password("Student123!");db.session.add(s)
        exam=Exam(title="C++ Fundamentals Practice",course_code="CS C++",description="A sample assessment showing MCQ and code-question behavior.",duration_minutes=45,published=True,monitoring_enabled=True,require_fullscreen=False,block_copy_paste=False,show_results_immediately=True);db.session.add(exam);db.session.flush()
        q1=Question(exam_id=exam.id,prompt="Which operator is used to access a member through a pointer in C++?",question_type="mcq",points=2,position=1,options_json='[".", "->", "::", "&"]',correct_answer="1");db.session.add(q1)
        q2=Question(exam_id=exam.id,prompt="Write a C++ program that reads two integers and prints their sum.",question_type="code",points=8,position=2,starter_code="#include <iostream>\nusing namespace std;\n\nint main() {\n    // your code\n    return 0;\n}\n",allow_compile=True,allow_run=True,show_compile_errors=True);db.session.add(q2);db.session.flush()
        db.session.add_all([TestCase(question_id=q2.id,input_data="2 3\n",expected_output="5\n",weight=1),TestCase(question_id=q2.id,input_data="-10 4\n",expected_output="-6\n",weight=1)])
        db.session.commit();print("Demo student: DEMO001 / Student123!")

if __name__ == "__main__":
    import argparse
    p=argparse.ArgumentParser();p.add_argument("command",choices=["init-admin","seed-demo","check-compiler"]);args=p.parse_args()
    if args.command == "init-admin":
        init_admin()
    elif args.command == "seed-demo":
        seed_demo()
    else:
        check_compiler()
