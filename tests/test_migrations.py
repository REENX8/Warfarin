"""Schema migrations: idempotency and adoption of pre-2.0 databases."""
import sqlite3

from warfarin.db import column_names, connect, table_exists
from warfarin.migrations import (
    BASELINE,
    LATEST_VERSION,
    MIGRATIONS,
    applied_versions,
    run_migrations,
    schema_version,
)


def test_migrations_are_numbered_consecutively():
    versions = [version for version, _, _ in MIGRATIONS]
    assert versions == sorted(versions)
    assert versions == list(range(1, len(versions) + 1))
    assert versions[-1] == LATEST_VERSION


def test_migrations_run_to_head_on_a_fresh_database(tmp_path):
    path = str(tmp_path / "fresh.db")
    applied = run_migrations(path)
    assert len(applied) == LATEST_VERSION
    assert schema_version(path) == LATEST_VERSION


def test_migrations_are_idempotent(tmp_path):
    path = str(tmp_path / "twice.db")
    run_migrations(path)
    assert run_migrations(path) == []


def test_expected_tables_exist(tmp_path):
    path = str(tmp_path / "tables.db")
    run_migrations(path)
    conn = connect(path)
    try:
        for table in (
            "patients", "medication_plan", "dose_tokens", "lab_results",
            "sessions", "appointments", "dose_adjustments", "job_runs",
            "line_recipients", "symptom_reports", "audit_log", "staff",
        ):
            assert table_exists(conn, table), table
    finally:
        conn.close()


def test_new_columns_are_added(tmp_path):
    path = str(tmp_path / "columns.db")
    run_migrations(path)
    conn = connect(path)
    try:
        patient_columns = column_names(conn, "patients")
        for column in ("access_token", "next_inr_date", "indication", "sex"):
            assert column in patient_columns, column
        symptom_columns = column_names(conn, "symptom_reports")
        for column in ("status", "ticket_code", "pharmacist_reply"):
            assert column in symptom_columns, column
        staff_columns = column_names(conn, "staff")
        for column in ("is_active", "must_change_password", "last_login"):
            assert column in staff_columns, column
    finally:
        conn.close()


def test_legacy_database_is_upgraded_without_data_loss(tmp_path):
    """A pre-2.0 database (baseline tables, no version table) must migrate."""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    try:
        conn.executescript(BASELINE)
        conn.execute(
            "INSERT INTO patients (hn, full_name, target_inr_min, target_inr_max, active) "
            "VALUES ('OLD-1', 'ผู้ป่วยเดิม', 2.0, 3.0, 1)"
        )
        conn.commit()
    finally:
        conn.close()

    run_migrations(path)

    conn = connect(path)
    try:
        assert schema_version(path) == LATEST_VERSION
        row = conn.execute(
            "SELECT full_name FROM patients WHERE hn='OLD-1'"
        ).fetchone()
        assert row[0] == "ผู้ป่วยเดิม"
        assert "access_token" in column_names(conn, "patients")
    finally:
        conn.close()


def test_access_token_backfill_for_legacy_rows(tmp_path):
    from warfarin.patients import backfill_access_tokens

    path = str(tmp_path / "backfill.db")
    run_migrations(path)
    conn = connect(path)
    try:
        conn.execute(
            "INSERT INTO patients (hn, full_name, active) VALUES ('B-1', 'ไม่มีโทเคน', 1)"
        )
        conn.commit()
        assert backfill_access_tokens(conn) == 1
        token = conn.execute(
            "SELECT access_token FROM patients WHERE hn='B-1'"
        ).fetchone()[0]
        assert token
        assert backfill_access_tokens(conn) == 0
    finally:
        conn.close()


def test_version_table_tracks_each_migration(tmp_path):
    path = str(tmp_path / "versions.db")
    run_migrations(path)
    conn = connect(path)
    try:
        assert applied_versions(conn) == {version for version, _, _ in MIGRATIONS}
    finally:
        conn.close()
