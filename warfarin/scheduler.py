"""Scheduled jobs (reminders, missed-dose marking, housekeeping).

Every job claims a per-day row in `job_runs` before doing work, so running
multiple web workers — the normal production setup — cannot send a patient
the same reminder several times.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from warfarin import notifications
from warfarin.appointments import (
    due_within,
    mark_past_appointments_missed,
    mark_reminded,
)
from warfarin.audit import log_audit_standalone
from warfarin.clinical import days_of_stock
from warfarin.config import get_settings
from warfarin.db import db, fetch_all, read_db
from warfarin.doses import current_weekly_mg, mark_overdue_missed
from warfarin.security import purge_expired_sessions, reset_security_state
from warfarin.time_utils import now, now_dt, today

logger = logging.getLogger(__name__)

_scheduler = None


# ---------------------------------------------------------------------------
# Single-flight guard
# ---------------------------------------------------------------------------
def claim_job(job_name: str, key_suffix: str | None = None) -> bool:
    """Claim a job for today. Returns False when another worker already has it."""
    job_key = f"{job_name}:{key_suffix or today()}"
    try:
        with db() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO job_runs (job_key, started_at, status) VALUES (?,?,?)",
                (job_key, now(), "running"),
            )
            return bool(cursor.rowcount)
    except sqlite3.Error:
        logger.exception("Could not claim job %s", job_key)
        return False


def finish_job(job_name: str, detail: str = "", key_suffix: str | None = None) -> None:
    job_key = f"{job_name}:{key_suffix or today()}"
    try:
        with db() as conn:
            conn.execute(
                "UPDATE job_runs SET finished_at=?, status='done', detail=? WHERE job_key=?",
                (now(), detail[:500], job_key),
            )
    except sqlite3.Error:
        logger.exception("Could not finalise job %s", job_key)


def fail_job(job_name: str, detail: str, key_suffix: str | None = None) -> None:
    job_key = f"{job_name}:{key_suffix or today()}"
    try:
        with db() as conn:
            conn.execute(
                "UPDATE job_runs SET finished_at=?, status='failed', detail=? WHERE job_key=?",
                (now(), detail[:500], job_key),
            )
    except sqlite3.Error:
        logger.exception("Could not record job failure %s", job_key)


def _run_guarded(job_name: str, fn) -> None:
    """Claim, run and record a job, swallowing failures so APScheduler survives."""
    if not claim_job(job_name):
        logger.info("Job %s already claimed by another worker today", job_name)
        return
    try:
        detail = fn() or ""
        finish_job(job_name, str(detail))
        logger.info("Job %s finished: %s", job_name, detail)
    except Exception as exc:
        logger.exception("Job %s failed", job_name)
        fail_job(job_name, repr(exc))


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def _patients_with_pending_doses() -> list[dict]:
    with read_db() as conn:
        return fetch_all(
            conn,
            "SELECT DISTINCT p.* FROM patients p "
            "JOIN medication_plan mp ON p.patient_id=mp.patient_id "
            "WHERE mp.scheduled_date=? AND mp.status='planned' AND p.active=1 "
            "AND p.line_user_id IS NOT NULL AND p.line_user_id<>''",
            (today(),),
        )


def job_first_reminder() -> str:
    patients = _patients_with_pending_doses()
    sent = sum(1 for p in patients if notifications.send_dose_reminder(p, attempt=1))
    return f"reminded={sent}/{len(patients)}"


def job_second_reminder() -> str:
    with read_db() as conn:
        patients = fetch_all(
            conn,
            "SELECT DISTINCT p.* FROM patients p "
            "JOIN medication_plan mp ON p.patient_id=mp.patient_id "
            "JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.scheduled_date=? AND mp.status='planned' AND p.active=1 "
            "AND p.line_user_id IS NOT NULL AND p.line_user_id<>'' "
            "AND COALESCE(dt.reminder_count,0) < 2",
            (today(),),
        )
    sent = sum(1 for p in patients if notifications.send_dose_reminder(p, attempt=2))
    return f"reminded={sent}/{len(patients)}"


def job_mark_missed() -> str:
    """Alert patients still unconfirmed, then flip past-due doses to missed."""
    with read_db() as conn:
        pending = fetch_all(
            conn,
            "SELECT mp.dose_id, p.* FROM medication_plan mp "
            "JOIN patients p ON mp.patient_id=p.patient_id "
            "WHERE mp.scheduled_date=? AND mp.status='planned' AND p.active=1",
            (today(),),
        )
    alerted = 0
    for row in pending:
        try:
            if notifications.send_missed_alert(row, row.get("dose_id")):
                alerted += 1
        except Exception:
            logger.exception("Missed alert failed for patient %s", row.get("patient_id"))
    with db() as conn:
        marked = mark_overdue_missed(conn)
    return f"alerted={alerted} marked_missed={marked}"


def job_appointment_reminders() -> str:
    """Remind patients whose INR appointment is coming up."""
    days = get_settings().inr_due_reminder_days
    with read_db() as conn:
        appointments = due_within(conn, days)
    sent = 0
    for appointment in appointments:
        if not appointment.get("line_user_id"):
            continue
        if notifications.send_appointment_reminder(appointment, appointment):
            sent += 1
        with db() as conn:
            mark_reminded(conn, appointment["appointment_id"])
    with db() as conn:
        missed = mark_past_appointments_missed(conn)
    return f"appointment_reminders={sent} marked_missed={missed}"


def job_low_stock_alerts() -> str:
    """Warn patients whose tablet supply will not reach the next refill."""
    settings = get_settings()
    with read_db() as conn:
        patients = fetch_all(
            conn,
            "SELECT * FROM patients WHERE active=1 AND COALESCE(pill_inventory,0) > 0 "
            "AND line_user_id IS NOT NULL AND line_user_id<>''",
        )
        weekly = {
            p["patient_id"]: current_weekly_mg(conn, p["patient_id"]) for p in patients
        }
    sent = 0
    for patient in patients:
        days_left = days_of_stock(
            int(patient.get("pill_inventory") or 0), weekly.get(patient["patient_id"], 0)
        )
        if days_left is None or days_left > settings.low_stock_threshold:
            continue
        if notifications.send_low_stock_alert(patient, days_left):
            sent += 1
    return f"low_stock_alerts={sent}"


def job_housekeeping() -> str:
    """Nightly cleanup: expired sessions, stale job rows, in-memory limiters."""
    purged = purge_expired_sessions()
    cutoff = (now_dt() - timedelta(days=30)).isoformat(timespec="seconds")
    with db() as conn:
        conn.execute("DELETE FROM job_runs WHERE started_at < ?", (cutoff,))
    reset_security_state()
    return f"sessions_purged={purged}"


JOBS = {
    "first_reminder": job_first_reminder,
    "second_reminder": job_second_reminder,
    "mark_missed": job_mark_missed,
    "appointment_reminders": job_appointment_reminders,
    "low_stock_alerts": job_low_stock_alerts,
    "housekeeping": job_housekeeping,
}


def run_job(name: str) -> str:
    """Run a job by name, bypassing the schedule (used by the admin action)."""
    fn = JOBS.get(name)
    if fn is None:
        raise KeyError(name)
    detail = fn() or ""
    log_audit_standalone("run_job", "scheduler", name, "manual", str(detail))
    return str(detail)


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------
def start() -> None:
    """Register cron jobs and start the background scheduler (idempotent)."""
    global _scheduler
    settings = get_settings()
    if not settings.enable_scheduler:
        logger.info("Scheduler disabled by configuration")
        return
    if _scheduler is not None and getattr(_scheduler, "running", False):
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:  # pragma: no cover
        logger.warning("APScheduler not installed; scheduled reminders are disabled")
        return

    _scheduler = BackgroundScheduler(timezone="Asia/Bangkok")
    schedule = [
        ("first_reminder", settings.reminder_first_hour, settings.reminder_first_minute),
        ("second_reminder", settings.reminder_second_hour, settings.reminder_second_minute),
        ("mark_missed", settings.mark_missed_hour, settings.mark_missed_minute),
        ("appointment_reminders", 9, 0),
        ("low_stock_alerts", 10, 0),
        ("housekeeping", 3, 0),
    ]
    for name, hour, minute in schedule:
        _scheduler.add_job(
            _run_guarded, "cron", hour=hour, minute=minute,
            args=[name, JOBS[name]], id=name, replace_existing=True,
            misfire_grace_time=3600, coalesce=True, max_instances=1,
        )
    try:
        _scheduler.start()
        logger.info("Scheduler started with %d jobs", len(schedule))
    except Exception:
        logger.exception("Scheduler failed to start")
        _scheduler = None


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and getattr(_scheduler, "running", False):
        try:
            _scheduler.shutdown(wait=False)
        except Exception:  # pragma: no cover
            logger.warning("Scheduler shutdown raised", exc_info=True)
    _scheduler = None


def job_status() -> list[dict]:
    """Today's job outcomes, shown on the admin system page."""
    with read_db() as conn:
        rows = fetch_all(
            conn,
            "SELECT * FROM job_runs WHERE job_key LIKE ? ORDER BY started_at DESC",
            (f"%:{today()}",),
        )
    for row in rows:
        row["name"] = row["job_key"].rsplit(":", 1)[0]
    return rows
