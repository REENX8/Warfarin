"""Scheduled jobs and their single-flight guard."""
import pytest

from warfarin import scheduler
from warfarin.db import db, read_db
from warfarin.time_utils import days_ago, now, today


def test_job_can_only_be_claimed_once_per_day():
    """Multiple workers must not each send the same reminder batch."""
    key = "unit-claim-test"
    with db() as conn:
        conn.execute("DELETE FROM job_runs WHERE job_key LIKE ?", (f"{key}:%",))
    assert scheduler.claim_job(key) is True
    assert scheduler.claim_job(key) is False


def test_finish_job_records_detail():
    key = "unit-finish-test"
    with db() as conn:
        conn.execute("DELETE FROM job_runs WHERE job_key LIKE ?", (f"{key}:%",))
    scheduler.claim_job(key)
    scheduler.finish_job(key, "sent=3")
    with read_db() as conn:
        row = conn.execute(
            "SELECT status, detail FROM job_runs WHERE job_key=?", (f"{key}:{today()}",)
        ).fetchone()
    assert row["status"] == "done"
    assert row["detail"] == "sent=3"


def test_failed_job_is_recorded():
    key = "unit-fail-test"
    with db() as conn:
        conn.execute("DELETE FROM job_runs WHERE job_key LIKE ?", (f"{key}:%",))
    scheduler.claim_job(key)
    scheduler.fail_job(key, "boom")
    with read_db() as conn:
        row = conn.execute(
            "SELECT status FROM job_runs WHERE job_key=?", (f"{key}:{today()}",)
        ).fetchone()
    assert row["status"] == "failed"


def test_run_guarded_swallows_job_failures(caplog):
    """An exploding job must not take down the scheduler thread."""
    key = "unit-guard-test"
    with db() as conn:
        conn.execute("DELETE FROM job_runs WHERE job_key LIKE ?", (f"{key}:%",))

    def explode():
        raise RuntimeError("kaboom")

    scheduler._run_guarded(key, explode)
    with read_db() as conn:
        row = conn.execute(
            "SELECT status FROM job_runs WHERE job_key=?", (f"{key}:{today()}",)
        ).fetchone()
    assert row["status"] == "failed"


def test_unknown_job_name_raises():
    with pytest.raises(KeyError):
        scheduler.run_job("no-such-job")


def test_mark_missed_job_updates_overdue_doses(patient):
    with db() as conn:
        conn.execute(
            "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, "
            "warfarin_mg, status, created_at) VALUES (?,?,?,?,'planned',?)",
            (patient["patient_id"], days_ago(2), "18:00", 3.0, now()),
        )
    detail = scheduler.job_mark_missed()
    assert "marked_missed" in detail
    with read_db() as conn:
        statuses = [
            row[0] for row in conn.execute(
                "SELECT status FROM medication_plan WHERE patient_id=? AND scheduled_date=?",
                (patient["patient_id"], days_ago(2)),
            ).fetchall()
        ]
    assert statuses == ["missed"]


def test_reminder_jobs_are_safe_without_line(patient):
    """Without LINE credentials the jobs must no-op rather than raise."""
    assert "reminded=" in scheduler.job_first_reminder()
    assert "reminded=" in scheduler.job_second_reminder()


def test_appointment_reminder_job_runs(patient):
    from warfarin.appointments import create_appointment

    with db() as conn:
        create_appointment(
            conn, patient["patient_id"], {"appointment_date": today()}, "pytest"
        )
    assert "appointment_reminders=" in scheduler.job_appointment_reminders()


def test_low_stock_job_runs(patient):
    with db() as conn:
        conn.execute(
            "UPDATE patients SET pill_inventory=2 WHERE patient_id=?",
            (patient["patient_id"],),
        )
    assert "low_stock_alerts=" in scheduler.job_low_stock_alerts()


def test_housekeeping_purges_expired_sessions():
    from warfarin.security import create_session

    session_id, _ = create_session(
        {"staff_id": 1, "username": "gone", "full_name": "G", "role": "nurse"}
    )
    with db() as conn:
        conn.execute(
            "UPDATE sessions SET expires_at='2000-01-01T00:00:00' WHERE session_id=?",
            (session_id,),
        )
    assert "sessions_purged=" in scheduler.job_housekeeping()
    with read_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()[0] == 0


def test_scheduler_is_disabled_in_tests():
    from warfarin.config import get_settings

    assert get_settings().enable_scheduler is False
    scheduler.start()  # must be a no-op, not a crash
    scheduler.shutdown()


def test_every_registered_job_is_callable():
    for name, function in scheduler.JOBS.items():
        assert callable(function), name
