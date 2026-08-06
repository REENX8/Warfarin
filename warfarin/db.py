"""SQLite access layer.

One short-lived connection per unit of work. SQLite is opened in WAL mode so
readers never block the writer, with a generous busy timeout because the
scheduler thread and web workers can write concurrently.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from warfarin.config import get_settings

logger = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 10_000


def _db_path() -> str:
    return get_settings().db_path


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a configured connection. Caller is responsible for closing it."""
    target = path or _db_path()
    directory = os.path.dirname(os.path.abspath(target))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(target, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """Read/write unit of work: commits on success, rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def read_db() -> Iterator[sqlite3.Connection]:
    """Read-only unit of work — never commits, so it cannot corrupt state."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return plain dicts (safe to hand to Jinja/JSON)."""
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = (), default: Any = 0) -> Any:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return default
    value = row[0]
    return default if value is None else value


def insert_returning_id(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """INSERT and return the new rowid from the same cursor.

    Using the cursor's lastrowid (rather than a follow-up
    `SELECT last_insert_rowid()`) keeps the value correct even if another
    statement runs on the connection in between.
    """
    cur = conn.execute(sql, params)
    return int(cur.lastrowid)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def healthcheck() -> tuple[bool, str]:
    """Cheap liveness probe used by /ping."""
    try:
        with read_db() as conn:
            conn.execute("SELECT 1").fetchone()
        return True, "ok"
    except Exception as exc:  # pragma: no cover - depends on disk failure
        logger.exception("Database healthcheck failed")
        return False, str(exc)
