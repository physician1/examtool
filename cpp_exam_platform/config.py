import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _database_url() -> str:
    """Return a SQLAlchemy URL that works locally and on Render.

    Render Postgres exposes a standard postgresql:// URL. This project uses
    psycopg 3, so SQLAlchemy needs the explicit +psycopg driver suffix.
    """
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{BASE_DIR / 'exam.db'}"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024
    WTF_CSRF_TIME_LIMIT = None

    # C++ execution. On Render, the Dockerfile installs /usr/bin/g++.
    RUNNER_BACKEND = os.getenv("RUNNER_BACKEND", "local")
    CPP_COMPILER = os.getenv("CPP_COMPILER", "")
    ALLOW_UNSAFE_LOCAL_RUNNER = os.getenv("ALLOW_UNSAFE_LOCAL_RUNNER", "0") == "1"
    MAX_CODE_OUTPUT_BYTES = int(os.getenv("MAX_CODE_OUTPUT_BYTES", "12000"))
