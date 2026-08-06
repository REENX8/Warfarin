"""Audit trail.

Audit failures must never break the operation being audited, so every write
is wrapped: a lost log line is bad, a 500 in the middle of a clinical action
is worse.
"""
from __future__ import annotations

import logging
import sqlite3

from warfarin.db import db, fetch_all, read_db, scalar
from warfarin.time_utils import now

logger = logging.getLogger(__name__)


def log_audit(
    conn: sqlite3.Connection,
    action: str,
    entity_type: str,
    entity_id,
    performed_by,
    details: str = "",
) -> None:
    """Write an audit row on an existing connection (same transaction)."""
    try:
        conn.execute(
            "INSERT INTO audit_log (action, entity_type, entity_id, performed_by, details, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (action, entity_type, str(entity_id), str(performed_by), details[:2000], now()),
        )
    except sqlite3.Error:
        logger.exception("Audit write failed (action=%s)", action)


def log_audit_standalone(
    action: str, entity_type: str, entity_id, performed_by, details: str = ""
) -> None:
    """Write an audit row in its own transaction (background jobs, webhooks)."""
    try:
        with db() as conn:
            log_audit(conn, action, entity_type, entity_id, performed_by, details)
    except Exception:
        logger.exception("Standalone audit write failed (action=%s)", action)


def recent_entries(limit: int = 100, offset: int = 0, action: str = "") -> list[dict]:
    sql = "SELECT * FROM audit_log"
    params: list = []
    if action:
        sql += " WHERE action = ?"
        params.append(action)
    sql += " ORDER BY created_at DESC, audit_id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with read_db() as conn:
        return fetch_all(conn, sql, tuple(params))


def count_entries(action: str = "") -> int:
    sql = "SELECT COUNT(*) FROM audit_log"
    params: tuple = ()
    if action:
        sql += " WHERE action = ?"
        params = (action,)
    with read_db() as conn:
        return int(scalar(conn, sql, params))


def distinct_actions() -> list[str]:
    with read_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT action FROM audit_log ORDER BY action"
        ).fetchall()
    return [r[0] for r in rows if r[0]]
