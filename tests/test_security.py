"""CSRF, rate limiting, security headers and authorisation boundaries."""
import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_PASSWORD, ADMIN_USER
from warfarin.app_factory import create_app
from warfarin.config import get_settings, reset_settings_cache
from warfarin.db import db
from warfarin.security import hash_password, is_rate_limited, reset_security_state
from warfarin.staff import create_staff


# --- headers ----------------------------------------------------------------
def test_security_headers_present(anon_client):
    response = anon_client.get("/login")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in response.headers
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_request_id_header_present(anon_client):
    assert anon_client.get("/login").headers.get("X-Request-ID")


def test_no_cors_wildcard_by_default(anon_client):
    """The pre-2.0 app sent Access-Control-Allow-Origin: * with credentials."""
    response = anon_client.get(
        "/login", headers={"Origin": "https://evil.example.com"}
    )
    assert response.headers.get("access-control-allow-origin") is None


# --- CSRF -------------------------------------------------------------------
@pytest.fixture
def csrf_client(tmp_path):
    """A separate app instance with CSRF enforcement switched on.

    Settings are process-global, so this must be function scoped: the
    environment is restored immediately after each test rather than at the
    end of the module, which would leave later tests running under it.
    """
    import os

    saved = {key: os.environ.get(key) for key in ("CSRF_ENABLED", "DB_PATH")}
    os.environ["CSRF_ENABLED"] = "1"
    os.environ["DB_PATH"] = str(tmp_path / "csrf.db")
    reset_settings_cache()
    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        reset_settings_cache()


def test_post_without_csrf_token_is_rejected(csrf_client):
    response = csrf_client.post(
        "/login", data={"username": "x", "password": "y"}, follow_redirects=False
    )
    assert response.status_code == 403


def test_post_with_csrf_token_is_accepted(csrf_client):
    page = csrf_client.get("/login")
    token = csrf_client.cookies.get("csrf_token")
    assert token, "the anonymous CSRF cookie must be set on the first page load"
    assert token in page.text, "the form must carry the token the cookie holds"
    response = csrf_client.post(
        "/login",
        data={"username": "nobody", "password": "wrong", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 200  # rejected credentials, not rejected CSRF


def test_webhook_is_exempt_from_csrf(csrf_client):
    assert csrf_client.post("/webhook", content=b"").status_code == 200


# --- rate limiting ----------------------------------------------------------
def test_rate_limiter_blocks_after_threshold():
    reset_security_state()
    key = "unit-test-bucket"
    assert not any(is_rate_limited(key, 3, 60) for _ in range(3))
    assert is_rate_limited(key, 3, 60)


def test_symptom_submission_is_rate_limited(anon_client, patient):
    reset_security_state()
    statuses = []
    for _ in range(12):
        response = anon_client.post(
            f"/report/symptom/{patient['access_token']}", data={"severity": "1"}
        )
        statuses.append(response.status_code)
    assert 429 in statuses


# --- authorisation ----------------------------------------------------------
def _login(client, username, password):
    client.cookies.clear()
    return client.post(
        "/login", data={"username": username, "password": password},
        follow_redirects=False,
    )


def test_nurse_cannot_reach_admin_pages(client):
    with db() as conn:
        create_staff(conn, "nurse-rbac", "nurse-password-1", "พยาบาล", "nurse", "pytest")
    _login(client, "nurse-rbac", "nurse-password-1")
    try:
        for path in ("/staff", "/audit", "/system"):
            assert client.get(path).status_code == 403, path
    finally:
        client.cookies.clear()


def test_readonly_staff_cannot_edit_patients(client, patient):
    with db() as conn:
        create_staff(conn, "viewer-rbac", "viewer-password-1", "เจ้าหน้าที่", "staff", "pytest")
    _login(client, "viewer-rbac", "viewer-password-1")
    try:
        assert client.get(f"/patients/{patient['patient_id']}").status_code == 200
        assert client.get("/patients/new").status_code == 403
        response = client.post(
            f"/patients/{patient['patient_id']}/inventory", data={"pill_inventory": "5"}
        )
        assert response.status_code == 403
    finally:
        client.cookies.clear()


def test_admin_can_reach_admin_pages(admin_client):
    for path in ("/staff", "/audit", "/system"):
        assert admin_client.get(path).status_code == 200, path


def test_patient_json_apis_require_authentication(anon_client, patient):
    """These endpoints were public before 2.0 and leaked INR history by id."""
    pid = patient["patient_id"]
    for path in (
        f"/api/patients/{pid}/inr-data",
        f"/api/patients/{pid}/adherence-data",
        f"/api/patients/{pid}/heatmap-data",
        "/api/dashboard-stats",
    ):
        assert anon_client.get(path).status_code == 401, path


def test_authenticated_apis_return_data(admin_client, patient):
    pid = patient["patient_id"]
    response = admin_client.get(f"/api/patients/{pid}/inr-data")
    assert response.status_code == 200
    assert "points" in response.json()


def test_admin_cannot_deactivate_last_admin_account(admin_client):
    """Losing the last active admin would lock everyone out of the system."""
    from warfarin.db import read_db
    from warfarin.staff import get_by_username

    with read_db() as conn:
        account = get_by_username(conn, ADMIN_USER)
    response = admin_client.post(
        f"/staff/{account['staff_id']}/edit",
        data={"full_name": "ผู้ดูแลระบบ", "role": "admin"},
    )
    assert response.status_code == 400
    assert "ตัวเอง" in response.text or "ผู้ดูแลระบบ" in response.text


def test_password_change_revokes_sessions(client):
    with db() as conn:
        create_staff(conn, "rotate-me", "old-password-123", "ทดสอบ", "nurse", "pytest")
    _login(client, "rotate-me", "old-password-123")
    try:
        response = client.post(
            "/account/password",
            data={
                "current_password": "old-password-123",
                "new_password": "new-password-456",
                "confirm_password": "new-password-456",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert client.get("/dashboard", follow_redirects=False).status_code == 303
    finally:
        client.cookies.clear()


def test_login_next_parameter_cannot_redirect_offsite(anon_client):
    response = anon_client.post(
        "/login",
        data={
            "username": ADMIN_USER, "password": ADMIN_PASSWORD,
            "next": "https://evil.example.com/steal",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    anon_client.cookies.clear()


def test_production_requires_secret_key(monkeypatch):
    from warfarin.config import load_settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        load_settings()
