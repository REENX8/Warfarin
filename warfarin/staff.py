"""Staff account management and authentication."""
from __future__ import annotations

import logging
import re
import sqlite3

from warfarin.audit import log_audit
from warfarin.config import get_settings
from warfarin.db import db, fetch_all, fetch_one, insert_returning_id, read_db, scalar
from warfarin.security import (
    ROLES,
    destroy_sessions_for_user,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)
from warfarin.time_utils import now

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,40}$")


class StaffError(Exception):
    """Raised for user-correctable staff-account problems."""


def get_by_username(conn: sqlite3.Connection, username: str) -> dict | None:
    return fetch_one(conn, "SELECT * FROM staff WHERE username=?", (username,))


def get_by_id(conn: sqlite3.Connection, staff_id: int) -> dict | None:
    return fetch_one(conn, "SELECT * FROM staff WHERE staff_id=?", (staff_id,))


def list_staff(conn: sqlite3.Connection) -> list[dict]:
    return fetch_all(
        conn,
        "SELECT staff_id, username, full_name, role, is_active, created_at, last_login, "
        "must_change_password FROM staff ORDER BY is_active DESC, username",
    )


def authenticate(username: str, password: str) -> dict | None:
    """Verify credentials and upgrade legacy password hashes on the way through."""
    if not username or not password:
        return None
    with read_db() as conn:
        staff = get_by_username(conn, username)
    if staff is None or not staff.get("password_hash"):
        # Still run a hash so a missing user and a wrong password take
        # comparable time, which avoids leaking valid usernames by timing.
        verify_password(
            "pbkdf2:sha256:260000$0000000000000000$" + "0" * 64, password
        )
        return None
    if staff.get("is_active") == 0:
        return None
    if not verify_password(staff["password_hash"], password):
        return None

    updates: list[str] = []
    params: list = []
    if needs_rehash(staff["password_hash"]):
        updates.append("password_hash=?")
        params.append(hash_password(password))
        logger.info("Upgraded legacy password hash for %s", username)
    updates.append("last_login=?")
    params.append(now())
    params.append(staff["staff_id"])
    with db() as conn:
        conn.execute(
            f"UPDATE staff SET {', '.join(updates)} WHERE staff_id=?", tuple(params)
        )
    return staff


def create_staff(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    full_name: str,
    role: str,
    performed_by: str,
    must_change_password: bool = False,
) -> int:
    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise StaffError("ชื่อผู้ใช้ต้องยาว 3–40 ตัว ใช้ได้เฉพาะ a-z 0-9 . _ -")
    if role not in ROLES:
        raise StaffError("บทบาทไม่ถูกต้อง")
    error = validate_password(password)
    if error:
        raise StaffError(error)
    if get_by_username(conn, username):
        raise StaffError("ชื่อผู้ใช้นี้มีอยู่แล้ว")
    staff_id = insert_returning_id(
        conn,
        "INSERT INTO staff (username, password_hash, full_name, role, created_at, "
        "is_active, must_change_password, updated_at) VALUES (?,?,?,?,?,1,?,?)",
        (
            username, hash_password(password), (full_name or username).strip()[:120],
            role, now(), 1 if must_change_password else 0, now(),
        ),
    )
    log_audit(
        conn, "create_staff", "staff", staff_id, performed_by,
        f"username={username} role={role}",
    )
    return staff_id


def update_staff(
    conn: sqlite3.Connection,
    staff_id: int,
    full_name: str,
    role: str,
    is_active: bool,
    performed_by: str,
    current_username: str = "",
) -> None:
    staff = get_by_id(conn, staff_id)
    if staff is None:
        raise StaffError("ไม่พบบัญชีผู้ใช้")
    if role not in ROLES:
        raise StaffError("บทบาทไม่ถูกต้อง")
    if staff["username"] == current_username and not is_active:
        raise StaffError("ไม่สามารถปิดใช้งานบัญชีของตัวเองได้")
    if staff["username"] == current_username and role != "admin":
        raise StaffError("ไม่สามารถลดสิทธิ์ของบัญชีตัวเองได้")
    if (
        staff["role"] == "admin"
        and (role != "admin" or not is_active)
        and count_active_admins(conn) <= 1
    ):
        raise StaffError("ต้องมีผู้ดูแลระบบที่ใช้งานได้อย่างน้อย 1 บัญชี")
    conn.execute(
        "UPDATE staff SET full_name=?, role=?, is_active=?, updated_at=? WHERE staff_id=?",
        ((full_name or staff["username"]).strip()[:120], role, 1 if is_active else 0,
         now(), staff_id),
    )
    log_audit(
        conn, "update_staff", "staff", staff_id, performed_by,
        f"username={staff['username']} role={role} active={is_active}",
    )
    if not is_active:
        destroy_sessions_for_user(staff["username"], conn)


def count_active_admins(conn: sqlite3.Connection) -> int:
    return int(
        scalar(conn, "SELECT COUNT(*) FROM staff WHERE role='admin' AND is_active=1")
    )


def reset_password(
    conn: sqlite3.Connection, staff_id: int, new_password: str, performed_by: str
) -> str:
    """Admin reset — forces the user to choose a new password at next login."""
    staff = get_by_id(conn, staff_id)
    if staff is None:
        raise StaffError("ไม่พบบัญชีผู้ใช้")
    error = validate_password(new_password)
    if error:
        raise StaffError(error)
    conn.execute(
        "UPDATE staff SET password_hash=?, must_change_password=1, updated_at=? "
        "WHERE staff_id=?",
        (hash_password(new_password), now(), staff_id),
    )
    log_audit(
        conn, "reset_staff_password", "staff", staff_id, performed_by,
        f"username={staff['username']}",
    )
    destroy_sessions_for_user(staff["username"], conn)
    return staff["username"]


def change_own_password(
    conn: sqlite3.Connection, username: str, current_password: str, new_password: str
) -> None:
    staff = get_by_username(conn, username)
    if staff is None:
        raise StaffError("ไม่พบบัญชีผู้ใช้")
    if not verify_password(staff["password_hash"], current_password):
        raise StaffError("รหัสผ่านปัจจุบันไม่ถูกต้อง")
    if current_password == new_password:
        raise StaffError("รหัสผ่านใหม่ต้องไม่ซ้ำกับรหัสผ่านเดิม")
    error = validate_password(new_password)
    if error:
        raise StaffError(error)
    conn.execute(
        "UPDATE staff SET password_hash=?, must_change_password=0, updated_at=? "
        "WHERE staff_id=?",
        (hash_password(new_password), now(), staff["staff_id"]),
    )
    log_audit(conn, "change_password", "staff", staff["staff_id"], username, "")


def bootstrap_admin() -> dict | None:
    """Create the first admin account when the staff table is empty.

    The password comes from BOOTSTRAP_ADMIN_PASSWORD when set. Otherwise a
    random one is generated and returned so the operator can read it from the
    startup log once — a fixed default password on an internet-facing clinic
    system is not acceptable.
    """
    import secrets

    settings = get_settings()
    with db() as conn:
        if int(scalar(conn, "SELECT COUNT(*) FROM staff")) > 0:
            return None
        password = settings.bootstrap_admin_password
        generated = False
        if not password or validate_password(password):
            password = secrets.token_urlsafe(12)
            generated = True
        username = settings.bootstrap_admin_user
        # Only a password the operator never chose has to be rotated at first
        # login; an explicitly configured one is already theirs.
        staff_id = insert_returning_id(
            conn,
            "INSERT INTO staff (username, password_hash, full_name, role, created_at, "
            "is_active, must_change_password, updated_at) VALUES (?,?,?,?,?,1,?,?)",
            (
                username, hash_password(password), "ผู้ดูแลระบบ", "admin", now(),
                1 if generated else 0, now(),
            ),
        )
        log_audit(conn, "bootstrap_admin", "staff", staff_id, "system", "")
    return {"username": username, "password": password, "generated": generated}
