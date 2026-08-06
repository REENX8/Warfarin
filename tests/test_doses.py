"""Medication plan generation and dose confirmation."""
import pytest

from warfarin.db import db, read_db
from warfarin.doses import (
    DoseError,
    confirm_dose,
    create_plan,
    current_weekly_mg,
    lookup_token,
    mark_overdue_missed,
    override_status,
)
from warfarin.time_utils import days_ago, today


def test_create_plan_creates_one_dose_per_day(patient):
    with db() as conn:
        created = create_plan(
            conn, patient["patient_id"], today(), today(),
            dict.fromkeys(range(7), 3.0), performed_by="pytest",
        )
    assert created == 1
    with read_db() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM medication_plan WHERE patient_id=?",
            (patient["patient_id"],),
        ).fetchone()[0]
    assert rows == 1


def test_create_plan_skips_zero_milligram_days(patient):
    """A 0 mg weekday is a deliberate rest day and gets no dose row."""
    from datetime import timedelta

    from warfarin.time_utils import now_dt

    start = now_dt().date()
    end = start + timedelta(days=6)
    day_doses = {index: (0.0 if index in (5, 6) else 3.0) for index in range(7)}
    with db() as conn:
        created = create_plan(
            conn, patient["patient_id"], start.isoformat(), end.isoformat(),
            day_doses, performed_by="pytest",
        )
    assert created == 5


def test_create_plan_rejects_reversed_dates(patient):
    with pytest.raises(DoseError), db() as conn:
        create_plan(
            conn, patient["patient_id"], "2026-02-10", "2026-02-01",
            dict.fromkeys(range(7), 3.0), performed_by="pytest",
        )


def test_create_plan_rejects_all_zero_doses(patient):
    with pytest.raises(DoseError), db() as conn:
        create_plan(
            conn, patient["patient_id"], today(), today(),
            dict.fromkeys(range(7), 0), performed_by="pytest",
        )


def test_create_plan_rejects_excessive_range(patient):
    with pytest.raises(DoseError), db() as conn:
        create_plan(
            conn, patient["patient_id"], "2020-01-01", "2026-01-01",
            dict.fromkeys(range(7), 3.0), performed_by="pytest",
        )


def test_create_plan_does_not_duplicate_existing_days(patient, dose_token):
    """Re-running a plan over a day that already has a dose must be a no-op."""
    with db() as conn:
        created = create_plan(
            conn, patient["patient_id"], today(), today(),
            dict.fromkeys(range(7), 5.0), performed_by="pytest",
        )
    assert created == 0


# --- confirmation -----------------------------------------------------------
def test_confirm_dose_marks_taken(dose_token):
    with db() as conn:
        token = lookup_token(conn, dose_token["token_id"])
        result = confirm_dose(conn, token, "patient")
    assert result["ok"] is True
    assert result["status"] in ("taken", "late")
    with read_db() as conn:
        status = conn.execute(
            "SELECT status FROM medication_plan WHERE dose_id=?", (dose_token["dose_id"],)
        ).fetchone()[0]
    assert status in ("taken", "late")


def test_confirm_dose_is_idempotent(dose_token):
    """The second confirmation must fail — otherwise inventory double-decrements."""
    with db() as conn:
        token = lookup_token(conn, dose_token["token_id"])
        assert confirm_dose(conn, token, "patient")["ok"] is True
    with db() as conn:
        token = lookup_token(conn, dose_token["token_id"])
        second = confirm_dose(conn, token, "patient")
    assert second["ok"] is False
    assert second["reason"] == "already_used"


def test_confirm_dose_decrements_inventory_once(dose_token):
    pid = dose_token["patient_id"]
    with db() as conn:
        conn.execute(
            "UPDATE patients SET pill_inventory=10 WHERE patient_id=?", (pid,)
        )
    for _ in range(3):
        with db() as conn:
            token = lookup_token(conn, dose_token["token_id"])
            confirm_dose(conn, token, "patient")
    with read_db() as conn:
        remaining = conn.execute(
            "SELECT pill_inventory FROM patients WHERE patient_id=?", (pid,)
        ).fetchone()[0]
    assert remaining == 9


def test_expired_token_is_refused(dose_token):
    with db() as conn:
        conn.execute(
            "UPDATE dose_tokens SET expires_at=? WHERE token_id=?",
            ("2000-01-01T00:00:00", dose_token["token_id"]),
        )
        token = lookup_token(conn, dose_token["token_id"])
        result = confirm_dose(conn, token, "patient")
    assert result["ok"] is False
    assert result["reason"] == "expired"


def test_confirm_page_and_post_over_http(anon_client, dose_token):
    page = anon_client.get(f"/dose/{dose_token['token_id']}")
    assert page.status_code == 200
    assert "ยืนยันว่ากินยาแล้ว" in page.text

    posted = anon_client.post(f"/dose/{dose_token['token_id']}/confirm", data={})
    assert posted.status_code == 200
    assert "ยืนยันสำเร็จ" in posted.text


def test_unknown_token_returns_404(anon_client):
    assert anon_client.get("/dose/nope-not-a-token").status_code == 404


def test_confirm_source_is_validated(dose_token):
    with db() as conn:
        token = lookup_token(conn, dose_token["token_id"])
        confirm_dose(conn, token, "hacker")
    with read_db() as conn:
        source = conn.execute(
            "SELECT confirm_source FROM medication_plan WHERE dose_id=?",
            (dose_token["dose_id"],),
        ).fetchone()[0]
    assert source == "patient"


# --- staff override and housekeeping ---------------------------------------
def test_override_status_round_trip(dose_token):
    with db() as conn:
        assert override_status(conn, dose_token["dose_id"], "missed", "nurse1")
    with read_db() as conn:
        assert conn.execute(
            "SELECT status FROM medication_plan WHERE dose_id=?", (dose_token["dose_id"],)
        ).fetchone()[0] == "missed"

    with db() as conn:
        override_status(conn, dose_token["dose_id"], "taken", "nurse1")
    with read_db() as conn:
        row = conn.execute(
            "SELECT status, confirm_source FROM medication_plan WHERE dose_id=?",
            (dose_token["dose_id"],),
        ).fetchone()
    assert row[0] == "taken"
    assert row[1] == "staff"


def test_override_rejects_unknown_status(dose_token):
    with pytest.raises(DoseError), db() as conn:
        override_status(conn, dose_token["dose_id"], "eaten-twice", "nurse1")


def test_mark_overdue_missed_only_touches_past_days(patient, dose_token):
    with db() as conn:
        conn.execute(
            "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, "
            "warfarin_mg, status, created_at) VALUES (?,?,?,?,'planned',?)",
            (patient["patient_id"], days_ago(3), "18:00", 3.0, "2026-01-01T00:00:00"),
        )
    with db() as conn:
        changed = mark_overdue_missed(conn)
    assert changed >= 1
    with read_db() as conn:
        today_status = conn.execute(
            "SELECT status FROM medication_plan WHERE dose_id=?", (dose_token["dose_id"],)
        ).fetchone()[0]
    assert today_status == "planned"


def test_current_weekly_mg_sums_forward_week(patient):
    from datetime import timedelta

    from warfarin.time_utils import now_dt

    start = now_dt().date()
    with db() as conn:
        create_plan(
            conn, patient["patient_id"], start.isoformat(),
            (start + timedelta(days=6)).isoformat(),
            dict.fromkeys(range(7), 2.0), performed_by="pytest",
        )
        total = current_weekly_mg(conn, patient["patient_id"])
    assert total == 14.0
