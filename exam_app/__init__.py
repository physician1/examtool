import os
from flask import Flask
from .extensions import db, login_manager, csrf, socketio
from .models import User


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object("config.Config")
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    socketio.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from .routes import auth_bp, admin_bp, student_bp, main_bp
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(student_bp, url_prefix="/student")

    import json
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    app.jinja_env.filters["fromjson"] = lambda value: json.loads(value or "[]")
    def eastern_input(value):
        if not value:
            return ""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M")
    app.jinja_env.filters["eastern_input"] = eastern_input
    app.jinja_env.globals["eastern_now_input"] = lambda: datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%dT%H:%M")

    with app.app_context():
        db.create_all()

    return app
