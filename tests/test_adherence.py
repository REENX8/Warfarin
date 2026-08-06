"""Adherence, streak and TTR maths."""
import pytest

from warfarin.adherence import (
    _summarize,
    _ttr_from_labs,
    compute_adherence,
    compute_adherence_bulk,
    compute_at_risk,
    compute_gamification_score,
    compute_streak,
    compute_streak_bulk,
    compute_ttr,
    heatmap_series,
    monthly_trend,
)
from warfarin.db import db, read_db
from warfarin.time_utils import days_ago, now, today


def _seed_doses(patient_id, entries):
    """entries = [(date_string, status), ...]"""
    with db() as conn:
        for date_string, status in entries:
            conn.execute(
                "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, "
                "warfarin_mg, status, created_at) VALUES (?,?,?,?,?,?)",
                (patient_id, date_string, "18:00", 3.0, status, now()),
            )


# --- summarize --------------------------------------------------------------
def test_todays_pending_dose_is_excluded_from_denominator():
    """A dose still 'planned' today is not late yet, so it must not count."""
    summary = _summarize(
        [(days_ago(1), "taken"), (today(), "planned")], today()
    )
    assert summary["total"] == 1
    assert summary["pending"] == 1
    assert summary["percent"] == 100.0


def test_past_planned_dose_counts_as_missed():
    summary = _summarize([(days_ago(2), "planned"), (days_ago(1), "taken")], today())
    assert summary["total"] == 2
    assert summary["missed"] == 1
    assert summary["percent"] == 50.0


def test_late_counts_as_adherent():
    summary = _summarize([(days_ago(1), "late"), (days_ago(2), "taken")], today())
    assert summary["percent"] == 100.0
    assert summary["late"] == 1


def test_no_doses_gives_zero_not_error():
    assert _summarize([], today())["percent"] == 0


# --- per patient ------------------------------------------------------------
def test_compute_adherence_matches_bulk(patient):
    pid = patient["patient_id"]
    _seed_doses(pid, [(days_ago(index), "taken") for index in range(1, 6)])
    with read_db() as conn:
        single = compute_adherence(conn, pid, 7)
        bulk = compute_adherence_bulk(conn, [pid], 7)[pid]
    assert single == bulk
    assert single["percent"] == 100.0


def test_bulk_returns_entry_for_patient_without_doses(patient):
    with read_db() as conn:
        result = compute_adherence_bulk(conn, [patient["patient_id"]], 7)
    assert result[patient["patient_id"]]["total"] == 0


def test_streak_counts_consecutive_confirmed_days(patient):
    pid = patient["patient_id"]
    _seed_doses(pid, [(days_ago(index), "taken") for index in range(1, 4)])
    with read_db() as conn:
        assert compute_streak(conn, pid) == 3


def test_streak_breaks_on_missed_day(patient):
    pid = patient["patient_id"]
    _seed_doses(
        pid,
        [(days_ago(1), "taken"), (days_ago(2), "missed"), (days_ago(3), "taken")],
    )
    with read_db() as conn:
        assert compute_streak(conn, pid) == 1


def test_streak_ignores_todays_pending_dose(patient):
    pid = patient["patient_id"]
    _seed_doses(pid, [(today(), "planned"), (days_ago(1), "taken"), (days_ago(2), "taken")])
    with read_db() as conn:
        assert compute_streak(conn, pid) == 2


def test_streak_bulk_matches_single(patient):
    pid = patient["patient_id"]
    _seed_doses(pid, [(days_ago(index), "taken") for index in range(1, 3)])
    with read_db() as conn:
        assert compute_streak_bulk(conn, [pid])[pid] == compute_streak(conn, pid)


def test_gamification_bonus_is_capped():
    assert compute_gamification_score(100, 200) == compute_gamification_score(100, 30)


# --- TTR --------------------------------------------------------------------
def test_ttr_all_in_range():
    labs = [("2026-01-01", 2.5), ("2026-01-31", 2.5)]
    assert _ttr_from_labs(labs, 2.0, 3.0) == 100.0


def test_ttr_all_out_of_range():
    labs = [("2026-01-01", 1.2), ("2026-01-31", 1.2)]
    assert _ttr_from_labs(labs, 2.0, 3.0) == 0.0


def test_ttr_interpolates_between_measurements():
    """1.0 → 3.0 over 10 days: days at 2.0–3.0 count as in range."""
    labs = [("2026-01-01", 1.0), ("2026-01-11", 3.0)]
    result = _ttr_from_labs(labs, 2.0, 3.0)
    assert 40 <= result <= 60


def test_ttr_needs_two_measurements():
    assert _ttr_from_labs([("2026-01-01", 2.5)], 2.0, 3.0) is None
    assert _ttr_from_labs([], 2.0, 3.0) is None


def test_ttr_skips_same_day_repeats():
    labs = [("2026-01-01", 2.5), ("2026-01-01", 2.6), ("2026-01-11", 2.5)]
    assert _ttr_from_labs(labs, 2.0, 3.0) == 100.0


def test_compute_ttr_uses_patient_target(patient):
    pid = patient["patient_id"]
    with db() as conn:
        for date_string, value in (("2026-01-01", 2.5), ("2026-01-21", 2.5)):
            conn.execute(
                "INSERT INTO lab_results (patient_id, lab_name, value, test_date, in_range, created_at) "
                "VALUES (?,'INR',?,?,1,?)",
                (pid, value, date_string, now()),
            )
    with read_db() as conn:
        assert compute_ttr(conn, pid) == 100.0


# --- risk and series --------------------------------------------------------
def test_at_risk_flags_low_adherence(patient):
    pid = patient["patient_id"]
    _seed_doses(pid, [(days_ago(index), "missed") for index in range(1, 5)])
    with read_db() as conn:
        at_risk = compute_at_risk(conn, [patient])
    assert len(at_risk) == 1
    assert at_risk[0]["consecutive_missed"] >= 2
    assert at_risk[0]["reasons"]


def test_at_risk_ignores_adherent_patient(patient):
    pid = patient["patient_id"]
    _seed_doses(pid, [(days_ago(index), "taken") for index in range(1, 8)])
    with read_db() as conn:
        assert compute_at_risk(conn, [patient]) == []


def test_heatmap_returns_one_entry_per_day(patient):
    with read_db() as conn:
        series = heatmap_series(conn, patient["patient_id"], 90)
    assert len(series) == 90
    assert series[-1]["date"] == today()


def test_monthly_trend_returns_requested_months():
    with read_db() as conn:
        trend = monthly_trend(conn, months=6)
    assert len(trend) == 6
    assert all("label" in row for row in trend)
