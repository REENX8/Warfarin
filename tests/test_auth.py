"""Authentication, sessions and access control."""
import pytest

from tests.conftest import ADMIN_PASSWORD, ADMIN_USER
from warfarin.db import db, read_db
from warfarin.security import (
    SESSION_COOKIE,
    create_session,
    destroy_session,
    hash_password,
    load_session,
    needs_rehash,
    purge_expired_sessions,
    validate_password,
    verify_password,
)


def test_login_page_renders(anon_client):
    response = anon_client.get("/login")
    assert response.status_code == 200
    assert "เข้าสู่ระบบ" in response.text


def test_root_redirects_to_login_when_signed_out(anon_client):
    response = anon_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_sets_httponly_session_cookie(anon_client):
    response = anon_client.post(
        "/login",
        data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    cookie_header = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE in cookie_header
    assert "HttpOnly" in cookie_header


def test_login_rejects_wrong_password(anon_client):
    response = anon_client.post(
        "/login",
        data={"username": ADMIN_USER, "password": "definitely-wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "ไม่ถูกต้อง" in response.text


def test_dashboard_requires_login(anon_client):
    response = anon_client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_dashboard_renders_for_admin(admin_client):
    response = admin_client.get("/dashboard")
    assert response.status_code == 200
    assert "แดชบอร์ด" in response.text


def test_logout_clears_session(admin_client):
    admin_client.get("/logout", follow_redirects=False)
    response = admin_client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303


def test_login_locks_out_after_repeated_failures(anon_client):
    for _ in range(5):
        anon_client.post(
            "/login", data={"username": ADMIN_USER, "password": "nope"},
            follow_redirects=False,
        )
    response = anon_client.post(
        "/login", data={"username": ADMIN_USER, "password": ADMIN_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "รอ" in response.text


# --- password hashing -------------------------------------------------------
def test_pbkdf2_roundtrip():
    stored = hash_password("correct horse battery")
    assert stored.startswith("pbkdf2:sha256:")
    assert verify_password(stored, "correct horse battery")
    assert not verify_password(stored, "wrong")
    assert not needs_rehash(stored)


def test_legacy_sha256_hash_still_verifies_and_is_flagged():
    import hashlib

    from warfarin.config import get_settings

    legacy = hashlib.sha256(("secret123" + get_settings().secret_key).encode()).hexdigest()
    assert verify_password(legacy, "secret123")
    assert not verify_password(legacy, "other")
    assert needs_rehash(legacy)


@pytest.mark.parametrize(
    "password,expected_error",
    [("short", True), ("12345678901", True), ("admin123", True), ("clinic-warfarin-2026", False)],
)
def test_password_policy(password, expected_error):
    assert (validate_password(password) is not None) is expected_error


# --- session store ----------------------------------------------------------
def test_session_lifecycle():
    session_id, csrf = create_session(
        {"staff_id": 1, "username": "tester", "full_name": "T", "role": "nurse"},
        ip="127.0.0.1",
    )
    loaded = load_session(session_id)
    assert loaded is not None
    assert loaded["username"] == "tester"
    assert loaded["csrf_token"] == csrf
    destroy_session(session_id)
    assert load_session(session_id) is None


def test_expired_sessions_are_rejected_and_purged():
    session_id, _ = create_session(
        {"staff_id": 1, "username": "expired-user", "full_name": "T", "role": "nurse"}
    )
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at=? WHERE session_id=?",
            ("2000-01-01T00:00:00", session_id),
        )
    assert load_session(session_id) is None
    purge_expired_sessions()
    with read_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()[0] == 0


def test_legacy_hash_is_upgraded_on_login(anon_client):
    """A pre-2.0 unsalted hash must keep working and be rehashed after login."""
    import hashlib

    from warfarin import staff
    from warfarin.config import get_settings

    password = "legacy-user-password"
    legacy = hashlib.sha256((password + get_settings().secret_key).encode()).hexdigest()
    with db() as conn:
        conn.execute(
            "INSERT INTO staff (username, password_hash, full_name, role, created_at, is_active) "
            "VALUES (?,?,?,?,?,1)",
            ("legacyuser", legacy, "Legacy", "nurse", "2024-01-01T00:00:00"),
        )

    account = staff.authenticate("legacyuser", password)
    assert account is not None

    with read_db() as conn:
        stored = conn.execute(
            "SELECT password_hash FROM staff WHERE username=?", ("legacyuser",)
        ).fetchone()[0]
    assert stored.startswith("pbkdf2:")


def test_inactive_account_cannot_authenticate():
    from warfarin import staff

    with db() as conn:
        conn.execute(
            "INSERT INTO staff (username, password_hash, full_name, role, created_at, is_active) "
            "VALUES (?,?,?,?,?,0)",
            ("disabled", hash_password("some-good-password"), "D", "nurse", "2024-01-01T00:00:00"),
        )
    assert staff.authenticate("disabled", "some-good-password") is None
