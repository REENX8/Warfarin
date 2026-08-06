"""Appointments, lab results and dose adjustment history."""
import pytest

from warfarin.appointments import (
    AppointmentError,
    add_lab_result,
    clinic_schedule,
    create_appointment,
    due_within,
    latest_lab,
    mark_past_appointments_missed,
    record_dose_adjustment,
    set_appointment_status,
    upcoming_for_patient,
)
from warfarin.db import db, read_db
from warfarin.time_utils import days_ago, today


def _tomorrow():
    from datetime import timedelta

    from warfarin.time_utils import now_dt

    return (now_dt() + timedelta(days=1)).strftime("%Y-%m-%d")


def test_create_appointment_sets_next_inr_date(patient):
    with db() as conn:
        create_appointment(
            conn, patient["patient_id"],
            {"appointment_date": _tomorrow(), "appointment_type": "inr"}, "pytest",
        )
        row = conn.execute(
            "SELECT next_inr_date FROM patients WHERE patient_id=?",
            (patient["patient_id"],),
        ).fetchone()
    assert row[0] == _tomorrow()


def test_non_inr_appointment_does_not_touch_next_inr_date(patient):
    with db() as conn:
        create_appointment(
            conn, patient["patient_id"],
            {"appointment_date": _tomorrow(), "appointment_type": "doctor"}, "pytest",
        )
        row = conn.execute(
            "SELECT next_inr_date FROM patients WHERE patient_id=?",
            (patient["patient_id"],),
        ).fetchone()
    assert row[0] is None


def test_create_appointment_rejects_bad_date(patient):
    with pytest.raises(AppointmentError), db() as conn:
        create_appointment(
            conn, patient["patient_id"], {"appointment_date": "not-a-date"}, "pytest"
        )


def test_upcoming_excludes_past(patient):
    with db() as conn:
        create_appointment(
            conn, patient["patient_id"], {"appointment_date": days_ago(5)}, "pytest"
        )
        create_appointment(
            conn, patient["patient_id"], {"appointment_date": _tomorrow()}, "pytest"
        )
        upcoming = upcoming_for_patient(conn, patient["patient_id"])
    assert len(upcoming) == 1
    assert upcoming[0]["appointment_date"] == _tomorrow()


def test_past_appointments_are_marked_missed(patient):
    with db() as conn:
        create_appointment(
            conn, patient["patient_id"], {"appointment_date": days_ago(3)}, "pytest"
        )
        changed = mark_past_appointments_missed(conn)
    assert changed >= 1


def test_status_update_is_validated(patient):
    with db() as conn:
        appointment_id = create_appointment(
            conn, patient["patient_id"], {"appointment_date": _tomorrow()}, "pytest"
        )
        assert set_appointment_status(conn, appointment_id, "attended", "pytest")
        with pytest.raises(AppointmentError):
            set_appointment_status(conn, appointment_id, "teleported", "pytest")


def test_due_within_only_returns_unreminded(patient):
    with db() as conn:
        appointment_id = create_appointment(
            conn, patient["patient_id"], {"appointment_date": _tomorrow()}, "pytest"
        )
        assert len(due_within(conn, 7)) >= 1
        conn.execute(
            "UPDATE appointments SET reminded_at=? WHERE appointment_id=?",
            ("2026-01-01T00:00:00", appointment_id),
        )
        remaining = [
            row for row in due_within(conn, 7)
            if row["appointment_id"] == appointment_id
        ]
    assert remaining == []


def test_clinic_schedule_joins_patient(patient):
    with db() as conn:
        create_appointment(
            conn, patient["patient_id"], {"appointment_date": _tomorrow()}, "pytest"
        )
        schedule = clinic_schedule(conn, days=7)
    assert any(row["full_name"] == patient["full_name"] for row in schedule)


# --- lab results ------------------------------------------------------------
def test_add_lab_result_flags_in_range(patient):
    with db() as conn:
        result = add_lab_result(
            conn, patient, {"value": "2.5", "test_date": today()}, "pytest"
        )
    assert result["assessment"].band == "in_range"
    with read_db() as conn:
        assert latest_lab(conn, patient["patient_id"])["in_range"] == 1


def test_add_lab_result_flags_out_of_range(patient):
    with db() as conn:
        result = add_lab_result(
            conn, patient, {"value": "5.5", "test_date": today()}, "pytest"
        )
    assert result["assessment"].urgency == 2
    with read_db() as conn:
        assert latest_lab(conn, patient["patient_id"])["in_range"] == 0


@pytest.mark.parametrize("value", ["", "abc", "0.1", "40"])
def test_add_lab_result_rejects_bad_values(patient, value):
    with pytest.raises(AppointmentError), db() as conn:
        add_lab_result(conn, patient, {"value": value}, "pytest")


def test_add_lab_result_updates_next_inr_date(patient):
    with db() as conn:
        add_lab_result(
            conn, patient,
            {"value": "2.5", "test_date": today(), "next_inr_date": _tomorrow()},
            "pytest",
        )
        row = conn.execute(
            "SELECT next_inr_date FROM patients WHERE patient_id=?",
            (patient["patient_id"],),
        ).fetchone()
    assert row[0] == _tomorrow()


def test_lab_form_over_http(admin_client, patient):
    response = admin_client.post(
        f"/patients/{patient['patient_id']}/labs",
        data={"value": "2.4", "test_date": today()},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with read_db() as conn:
        assert latest_lab(conn, patient["patient_id"]) is not None


def test_dose_adjustment_history(patient):
    with db() as conn:
        record_dose_adjustment(
            conn, patient["patient_id"], 21.0, 24.5, 1.7, "INR ต่ำ", "pytest"
        )
        rows = conn.execute(
            "SELECT * FROM dose_adjustments WHERE patient_id=?",
            (patient["patient_id"],),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["new_weekly_mg"] == 24.5
