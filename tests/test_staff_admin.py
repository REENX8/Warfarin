"""Staff account administration."""
import pytest

from warfarin.db import db, read_db
from warfarin.security import ROLES, verify_password
from warfarin.staff import (
    StaffError,
    change_own_password,
    count_active_admins,
    create_staff,
    get_by_username,
    list_staff,
    reset_password,
    update_staff,
)


def test_staff_list_page(admin_client):
    response = admin_client.get("/staff")
    assert response.status_code == 200
    assert "บัญชีผู้ใช้งาน" in response.text


def test_create_staff_over_http(admin_client):
    response = admin_client.post(
        "/staff/new",
        data={
            "username": "pharm-http",
            "password": "pharmacy-password-1",
            "full_name": "ภก.ทดสอบ",
            "role": "pharmacist",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with read_db() as conn:
        account = get_by_username(conn, "pharm-http")
    assert account["role"] == "pharmacist"
    assert account["password_hash"].startswith("pbkdf2:")


def test_create_staff_rejects_weak_password(admin_client):
    response = admin_client.post(
        "/staff/new",
        data={"username": "weak-user", "password": "12345", "role": "nurse"},
    )
    assert response.status_code == 400
    assert "รหัสผ่าน" in response.text


def test_create_staff_rejects_bad_username():
    with pytest.raises(StaffError), db() as conn:
        create_staff(conn, "no", "a-good-password-1", "X", "nurse", "pytest")
    with pytest.raises(StaffError), db() as conn:
        create_staff(conn, "bad user!", "a-good-password-1", "X", "nurse", "pytest")


def test_create_staff_rejects_unknown_role():
    with pytest.raises(StaffError), db() as conn:
        create_staff(conn, "role-test", "a-good-password-1", "X", "wizard", "pytest")


def test_duplicate_username_is_rejected():
    with db() as conn:
        create_staff(conn, "dupe-user", "a-good-password-1", "X", "nurse", "pytest")
    with pytest.raises(StaffError), db() as conn:
        create_staff(conn, "dupe-user", "a-good-password-2", "X", "nurse", "pytest")


def test_reset_password_forces_rotation():
    with db() as conn:
        create_staff(conn, "reset-target", "old-password-111", "X", "nurse", "pytest")
        reset_password(
            conn,
            get_by_username(conn, "reset-target")["staff_id"],
            "temp-password-222",
            "admin",
        )
        account = get_by_username(conn, "reset-target")
    assert account["must_change_password"] == 1
    assert verify_password(account["password_hash"], "temp-password-222")


def test_change_own_password_requires_current():
    with db() as conn:
        create_staff(conn, "self-change", "first-password-1", "X", "nurse", "pytest")
    with pytest.raises(StaffError, match="ปัจจุบัน"), db() as conn:
        change_own_password(conn, "self-change", "wrong", "second-password-2")


def test_change_own_password_rejects_reuse():
    with db() as conn:
        create_staff(conn, "reuse-check", "same-password-1", "X", "nurse", "pytest")
    with pytest.raises(StaffError), db() as conn:
        change_own_password(conn, "reuse-check", "same-password-1", "same-password-1")


def test_cannot_demote_last_admin():
    with db() as conn:
        admins = count_active_admins(conn)
        assert admins >= 1
        account = get_by_username(conn, "admin")
        if admins == 1:
            with pytest.raises(StaffError):
                update_staff(
                    conn, account["staff_id"], "ผู้ดูแล", "nurse", True, "someone-else"
                )


def test_deactivating_account_revokes_its_sessions(client):
    from warfarin.security import load_session

    with db() as conn:
        create_staff(conn, "kickme", "kickme-password-1", "X", "nurse", "pytest")
    client.cookies.clear()
    client.post(
        "/login", data={"username": "kickme", "password": "kickme-password-1"},
        follow_redirects=False,
    )
    session_id = client.cookies.get("session_id")
    assert load_session(session_id) is not None

    with db() as conn:
        account = get_by_username(conn, "kickme")
        update_staff(conn, account["staff_id"], "X", "nurse", False, "admin")

    assert load_session(session_id) is None
    client.cookies.clear()


def test_list_staff_does_not_expose_password_hashes():
    with read_db() as conn:
        accounts = list_staff(conn)
    assert accounts
    assert all("password_hash" not in account for account in accounts)


def test_all_roles_have_thai_labels():
    for key, label in ROLES.items():
        assert label and label != key
