"""Pytest fixtures.

The environment must be configured before `warfarin.config` is first imported,
because settings are cached for the process lifetime.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP_DIR = tempfile.mkdtemp(prefix="warfarin-test-")
os.environ["APP_ENV"] = "testing"
os.environ["DB_PATH"] = os.path.join(_TMP_DIR, "test.db")
os.environ["SECRET_KEY"] = "test-secret-key-do-not-use-in-production"
os.environ["BASE_URL"] = "http://testserver"
os.environ["ENABLE_SCHEDULER"] = "0"
os.environ["CSRF_ENABLED"] = "0"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "test-admin-password"
os.environ.pop("LINE_CHANNEL_SECRET", None)
os.environ.pop("LINE_CHANNEL_ACCESS_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

from warfarin.app_factory import create_app  # noqa: E402
from warfarin.config import get_settings  # noqa: E402
from warfarin.db import connect, db  # noqa: E402
from warfarin.migrations import run_migrations  # noqa: E402
from warfarin.security import reset_security_state  # noqa: E402
from warfarin.time_utils import now  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASSWORD = "test-admin-password"


@pytest.fixture(scope="session")
def app():
    return create_app()


@pytest.fixture(scope="session")
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _clean_limiter_state():
    """Rate limiters are process-global; reset between tests."""
    reset_security_state()
    yield
    reset_security_state()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def conn(client):
    """Raw connection for seeding fixtures (client ensures migrations ran)."""
    connection = connect()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def admin_client(client):
    """TestClient carrying an admin session cookie."""
    client.cookies.clear()
    response = client.post(
        "/login",
        data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return client


@pytest.fixture
def anon_client(client):
    client.cookies.clear()
    return client


@pytest.fixture
def patient(conn):
    """Create a patient and return its full row as a dict."""
    import uuid

    from warfarin.patients import create_patient

    hn = "T" + uuid.uuid4().hex[:8].upper()
    data = {
        "full_name": "ผู้ทดสอบ ใจดี",
        "hn": hn,
        "target_inr_min": 2.0,
        "target_inr_max": 3.0,
        "age_years": 60,
        "indication": "af",
    }
    with db() as write_conn:
        patient_id = create_patient(write_conn, data, "pytest")
        row = write_conn.execute(
            "SELECT * FROM patients WHERE patient_id=?", (patient_id,)
        ).fetchone()
    return dict(row)


@pytest.fixture
def dose_token(patient):
    """Seed one planned dose for today and return its token row."""
    import uuid

    from warfarin.time_utils import today

    token_id = uuid.uuid4().hex[:24]
    with db() as write_conn:
        cursor = write_conn.execute(
            "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, "
            "warfarin_mg, pill_description, status, created_at) VALUES (?,?,?,?,?,'planned',?)",
            (patient["patient_id"], today(), "18:00", 3.0, "เม็ดสีชมพู", now()),
        )
        dose_id = cursor.lastrowid
        write_conn.execute(
            "INSERT INTO dose_tokens (token_id, dose_id, created_at, expires_at, is_used) "
            "VALUES (?,?,?,?,0)",
            (token_id, dose_id, now(), "2099-12-31T23:59:59"),
        )
    return {"token_id": token_id, "dose_id": dose_id, "patient_id": patient["patient_id"]}


@pytest.fixture(scope="session", autouse=True)
def _migrate_once():
    run_migrations()
