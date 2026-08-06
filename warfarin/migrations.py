"""Versioned schema migrations for SQLite.

Every migration is applied exactly once and recorded in `schema_migrations`.
Migrations must be idempotent at the statement level too, because databases
created by the pre-2.0 `init_db()` already contain most of the baseline
objects — those installs are adopted by stamping the baseline as applied.
"""
from __future__ import annotations

import logging
import sqlite3

from warfarin.db import column_names, connect, table_exists

logger = logging.getLogger(__name__)

BASELINE = """
CREATE TABLE IF NOT EXISTS staff (
    staff_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT,
    role TEXT DEFAULT 'staff',
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS patients (
    patient_id INTEGER PRIMARY KEY AUTOINCREMENT,
    hn TEXT UNIQUE,
    full_name TEXT NOT NULL,
    birth_date TEXT,
    age_years INTEGER,
    weight_kg REAL,
    phone TEXT,
    line_user_id TEXT,
    chronic_conditions TEXT,
    diagnosis TEXT,
    target_inr_min REAL DEFAULT 2.0,
    target_inr_max REAL DEFAULT 3.0,
    active INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    pill_inventory INTEGER DEFAULT 0,
    registration_code TEXT
);
CREATE TABLE IF NOT EXISTS caregivers (
    caregiver_id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(patient_id),
    name TEXT,
    phone TEXT,
    line_user_id TEXT,
    relationship TEXT,
    notify_enabled INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS medication_plan (
    dose_id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(patient_id),
    scheduled_date TEXT,
    scheduled_time TEXT DEFAULT '18:00',
    warfarin_mg REAL,
    pill_description TEXT,
    status TEXT DEFAULT 'planned',
    confirmed_at TEXT,
    confirmed_by TEXT,
    late_minutes INTEGER DEFAULT 0,
    notes TEXT,
    created_at TEXT,
    confirm_source TEXT DEFAULT 'patient'
);
CREATE TABLE IF NOT EXISTS dose_tokens (
    token_id TEXT PRIMARY KEY,
    dose_id INTEGER UNIQUE REFERENCES medication_plan(dose_id),
    created_at TEXT,
    expires_at TEXT,
    is_used INTEGER DEFAULT 0,
    used_at TEXT,
    reminder_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lab_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(patient_id),
    lab_name TEXT DEFAULT 'INR',
    value REAL,
    test_date TEXT,
    in_range INTEGER DEFAULT 0,
    notes TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS test_scores (
    score_id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(patient_id),
    test_type TEXT DEFAULT 'pre',
    score REAL,
    max_score REAL DEFAULT 100,
    taken_at TEXT
);
CREATE TABLE IF NOT EXISTS notification_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(patient_id),
    dose_id INTEGER,
    channel TEXT DEFAULT 'line',
    message_type TEXT,
    message_text TEXT,
    sent_at TEXT,
    delivered INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT,
    entity_type TEXT,
    entity_id TEXT,
    performed_by TEXT,
    details TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS satisfaction_surveys (
    survey_id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(patient_id),
    survey_date TEXT NOT NULL,
    ease_of_use INTEGER,
    line_satisfaction INTEGER,
    reminder_helpful INTEGER,
    comments TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS symptom_reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER REFERENCES patients(patient_id),
    report_date TEXT NOT NULL,
    bleeding INTEGER DEFAULT 0,
    bruising INTEGER DEFAULT 0,
    headache INTEGER DEFAULT 0,
    dizziness INTEGER DEFAULT 0,
    nausea INTEGER DEFAULT 0,
    other TEXT,
    severity INTEGER DEFAULT 1,
    source TEXT DEFAULT 'patient',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_medplan_patient_date ON medication_plan(patient_id, scheduled_date);
CREATE INDEX IF NOT EXISTS idx_medplan_status_date ON medication_plan(status, scheduled_date);
CREATE INDEX IF NOT EXISTS idx_dose_tokens_dose ON dose_tokens(dose_id);
CREATE INDEX IF NOT EXISTS idx_lab_patient_date ON lab_results(patient_id, test_date);
CREATE INDEX IF NOT EXISTS idx_patients_line ON patients(line_user_id);
CREATE INDEX IF NOT EXISTS idx_symptom_patient ON symptom_reports(patient_id, report_date);
"""


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """ALTER TABLE ... ADD COLUMN, skipping columns that already exist."""
    if not table_exists(conn, table):
        return
    if column in column_names(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
def m001_baseline(conn: sqlite3.Connection) -> None:
    conn.executescript(BASELINE)


def m002_sessions(conn: sqlite3.Connection) -> None:
    """Server-side sessions so logins survive restarts and multiple workers."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            staff_id INTEGER,
            username TEXT,
            full_name TEXT,
            role TEXT,
            csrf_token TEXT,
            ip TEXT,
            user_agent TEXT,
            must_change_password INTEGER DEFAULT 0,
            created_at TEXT,
            last_seen_at TEXT,
            expires_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
        """
    )


def m003_staff_accounts(conn: sqlite3.Connection) -> None:
    """Roles, activation and forced password rotation for staff accounts."""
    _add_column(conn, "staff", "is_active", "INTEGER DEFAULT 1")
    _add_column(conn, "staff", "last_login", "TEXT")
    _add_column(conn, "staff", "must_change_password", "INTEGER DEFAULT 0")
    _add_column(conn, "staff", "updated_at", "TEXT")
    conn.execute("UPDATE staff SET is_active=1 WHERE is_active IS NULL")


def m004_symptom_workflow(conn: sqlite3.Connection) -> None:
    """Triage workflow + short ticket codes for LINE replies (ported from TB)."""
    _add_column(conn, "symptom_reports", "status", "TEXT DEFAULT 'new'")
    _add_column(conn, "symptom_reports", "ticket_code", "TEXT")
    _add_column(conn, "symptom_reports", "auto_response", "TEXT")
    _add_column(conn, "symptom_reports", "pharmacist_reply", "TEXT")
    _add_column(conn, "symptom_reports", "replied_by", "TEXT")
    _add_column(conn, "symptom_reports", "replied_at", "TEXT")
    conn.execute("UPDATE symptom_reports SET status='new' WHERE status IS NULL")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_symptom_status ON symptom_reports(status);
        CREATE INDEX IF NOT EXISTS idx_symptom_ticket ON symptom_reports(ticket_code);
        CREATE INDEX IF NOT EXISTS idx_symptom_created ON symptom_reports(created_at);
        """
    )


def m005_line_recipients(conn: sqlite3.Connection) -> None:
    """Pharmacist LINE accounts that receive symptom alerts."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS line_recipients (
            recipient_id INTEGER PRIMARY KEY AUTOINCREMENT,
            line_user_id TEXT UNIQUE NOT NULL,
            display_name TEXT,
            is_active INTEGER DEFAULT 1,
            registered_at TEXT
        );
        """
    )


def m006_patient_clinical_fields(conn: sqlite3.Connection) -> None:
    """Fields the warfarin clinic actually needs on the patient record."""
    _add_column(conn, "patients", "sex", "TEXT")
    _add_column(conn, "patients", "indication", "TEXT")
    _add_column(conn, "patients", "warfarin_start_date", "TEXT")
    _add_column(conn, "patients", "allergies", "TEXT")
    _add_column(conn, "patients", "notes", "TEXT")
    _add_column(conn, "patients", "access_token", "TEXT")
    _add_column(conn, "patients", "next_inr_date", "TEXT")
    _add_column(conn, "patients", "inr_interval_days", "INTEGER DEFAULT 28")
    conn.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_patients_access_token
            ON patients(access_token) WHERE access_token IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_patients_next_inr ON patients(next_inr_date);
        CREATE INDEX IF NOT EXISTS idx_patients_active_name ON patients(active, full_name);
        """
    )


def m007_appointments(conn: sqlite3.Connection) -> None:
    """INR / clinic appointments with their own reminder state."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS appointments (
            appointment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER REFERENCES patients(patient_id),
            appointment_date TEXT NOT NULL,
            appointment_time TEXT,
            appointment_type TEXT DEFAULT 'inr',
            location TEXT,
            notes TEXT,
            status TEXT DEFAULT 'scheduled',
            reminded_at TEXT,
            created_by TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_appt_patient_date
            ON appointments(patient_id, appointment_date);
        CREATE INDEX IF NOT EXISTS idx_appt_date_status
            ON appointments(appointment_date, status);
        """
    )


def m008_dose_adjustments(conn: sqlite3.Connection) -> None:
    """Warfarin titration history — who changed the weekly dose, and why."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS dose_adjustments (
            adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER REFERENCES patients(patient_id),
            effective_date TEXT NOT NULL,
            previous_weekly_mg REAL,
            new_weekly_mg REAL,
            inr_value REAL,
            reason TEXT,
            adjusted_by TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dose_adj_patient
            ON dose_adjustments(patient_id, effective_date);
        """
    )


def m009_job_runs(conn: sqlite3.Connection) -> None:
    """Single-flight guard so scheduled jobs run once across all workers."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_runs (
            job_key TEXT PRIMARY KEY,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            detail TEXT
        );
        """
    )


def m010_lab_provenance(conn: sqlite3.Connection) -> None:
    _add_column(conn, "lab_results", "recorded_by", "TEXT")
    _add_column(conn, "lab_results", "action_taken", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notification_patient "
        "ON notification_log(patient_id, sent_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)"
    )


def m011_dose_confirm_index(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_medplan_confirmed
            ON medication_plan(confirmed_at);
        CREATE INDEX IF NOT EXISTS idx_medplan_date_status
            ON medication_plan(scheduled_date, status);
        """
    )


def m012_caregiver_index(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_caregivers_patient ON caregivers(patient_id)"
    )
    _add_column(conn, "caregivers", "created_at", "TEXT")


MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "baseline_schema", m001_baseline),
    (2, "server_side_sessions", m002_sessions),
    (3, "staff_account_fields", m003_staff_accounts),
    (4, "symptom_triage_workflow", m004_symptom_workflow),
    (5, "line_recipients", m005_line_recipients),
    (6, "patient_clinical_fields", m006_patient_clinical_fields),
    (7, "appointments", m007_appointments),
    (8, "dose_adjustments", m008_dose_adjustments),
    (9, "job_runs", m009_job_runs),
    (10, "lab_provenance", m010_lab_provenance),
    (11, "dose_confirm_indexes", m011_dose_confirm_index),
    (12, "caregiver_index", m012_caregiver_index),
]

LATEST_VERSION = max(v for v, _, _ in MIGRATIONS)


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_version_table(conn)
    return {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}


def run_migrations(path: str | None = None) -> list[str]:
    """Apply every pending migration. Returns the names that ran."""
    from warfarin.time_utils import now

    conn = connect(path)
    applied: list[str] = []
    try:
        _ensure_version_table(conn)
        done = applied_versions(conn)
        for version, name, fn in MIGRATIONS:
            if version in done:
                continue
            try:
                conn.execute("BEGIN")
                fn(conn)  # type: ignore[operator]
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?,?,?)",
                    (version, name, now()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("Migration %03d_%s failed", version, name)
                raise
            applied.append(f"{version:03d}_{name}")
            logger.info("Applied migration %03d_%s", version, name)
    finally:
        conn.close()
    return applied


def schema_version(path: str | None = None) -> int:
    conn = connect(path)
    try:
        done = applied_versions(conn)
        return max(done) if done else 0
    finally:
        conn.close()
