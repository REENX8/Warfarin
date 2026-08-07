"""Research study support: cohort, allocation arms, phase outcomes, analysis.

The clinic runs this system as the data-collection instrument for a study on
warfarin adherence, so the research layer sits on top of the routine clinical
data rather than duplicating it: adherence, TTR and INR-in-range for a phase
are computed from the same dose and lab rows the pharmacist already enters.
Only study metadata (arm, consent, phase windows, instrument scores) is stored
separately.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from warfarin.adherence import _summarize, _ttr_from_labs
from warfarin.audit import log_audit
from warfarin.clinical import INDICATIONS
from warfarin.db import fetch_all, fetch_one, insert_returning_id, read_db, scalar
from warfarin.stats import (
    Descriptive,
    chi_square_test,
    describe,
    independent_t_test,
    mcnemar_test,
    paired_t_test,
    proportion_ci,
    wilcoxon_signed_rank,
)
from warfarin.time_utils import DATE_FMT, now, now_dt, parse_date, today

logger = logging.getLogger(__name__)

DEFAULT_STUDY_CODE = "WARFARIN-ADH"

ARMS = {
    "intervention": "กลุ่มทดลอง (ใช้ระบบ LINE)",
    "control": "กลุ่มควบคุม (ดูแลตามปกติ)",
}

STATUSES = {
    "screening": "คัดกรอง",
    "enrolled": "เข้าร่วมแล้ว",
    "completed": "สิ้นสุดการติดตาม",
    "withdrawn": "ถอนตัว / ออกจากการศึกษา",
}

PHASES = {
    "baseline": "ก่อนเริ่มระบบ (Baseline)",
    "endline": "หลังใช้ระบบ (Endline)",
}

# Instruments recorded per phase. `scored` items participate in the
# before/after comparison; free text is kept for qualitative notes.
INSTRUMENTS = {
    "knowledge": {"label": "คะแนนความรู้เรื่องยาวาร์ฟาริน", "max": 20, "scored": True},
    "adherence_self": {"label": "แบบประเมินความร่วมมือใช้ยาด้วยตนเอง", "max": 8, "scored": True},
    "satisfaction": {"label": "ความพึงพอใจต่อระบบ", "max": 5, "scored": True},
    "quality_of_life": {"label": "คุณภาพชีวิต (0–100)", "max": 100, "scored": True},
}

DEFAULT_PHASE_DAYS = 60

WITHDRAWAL_REASONS = [
    "ผู้ป่วยขอถอนตัว",
    "ย้ายสถานพยาบาล",
    "หยุดยาวาร์ฟารินตามแผนการรักษา",
    "เสียชีวิต",
    "ติดต่อไม่ได้",
    "อื่น ๆ",
]


class ResearchError(Exception):
    """Raised for user-correctable problems in the research workflow."""


# ---------------------------------------------------------------------------
# Enrolment
# ---------------------------------------------------------------------------
def enroll(
    conn: sqlite3.Connection, patient_id: int, form: dict, performed_by: str
) -> int:
    """Enrol a patient into the study, or update their existing record."""
    arm = (form.get("arm") or "intervention").strip()
    if arm not in ARMS:
        raise ResearchError("กลุ่มการศึกษาไม่ถูกต้อง")
    status = (form.get("status") or "enrolled").strip()
    if status not in STATUSES:
        raise ResearchError("สถานะการเข้าร่วมไม่ถูกต้อง")

    consent_date = (form.get("consent_date") or "").strip()
    if consent_date and parse_date(consent_date) is None:
        raise ResearchError("รูปแบบวันที่ให้ความยินยอมไม่ถูกต้อง")
    if status == "enrolled" and not consent_date:
        raise ResearchError("ต้องบันทึกวันที่ผู้ป่วยลงนามยินยอมก่อนจึงจะเข้าร่วมได้")

    baseline_start = (form.get("baseline_start") or "").strip() or today()
    if parse_date(baseline_start) is None:
        raise ResearchError("รูปแบบวันเริ่ม baseline ไม่ถูกต้อง")
    windows = phase_windows(baseline_start, form)

    existing = get_by_patient(conn, patient_id)
    timestamp = now()
    values = (
        (form.get("study_code") or DEFAULT_STUDY_CODE).strip()[:40],
        arm, status, consent_date or None,
        (form.get("consent_version") or "").strip()[:40] or None,
        windows["baseline_start"], windows["baseline_end"],
        windows["endline_start"], windows["endline_end"],
        (form.get("notes") or "").strip()[:1000] or None,
    )

    if existing:
        conn.execute(
            "UPDATE study_participants SET study_code=?, arm=?, status=?, consent_date=?, "
            "consent_version=?, baseline_start=?, baseline_end=?, endline_start=?, "
            "endline_end=?, notes=?, updated_at=? WHERE participant_id=?",
            (*values, timestamp, existing["participant_id"]),
        )
        participant_id = existing["participant_id"]
        action = "update_participant"
    else:
        participant_id = insert_returning_id(
            conn,
            "INSERT INTO study_participants (patient_id, study_code, arm, status, "
            "consent_date, consent_version, baseline_start, baseline_end, endline_start, "
            "endline_end, notes, enrolled_at, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (patient_id, *values, timestamp, performed_by, timestamp, timestamp),
        )
        action = "enroll_participant"

    log_audit(
        conn, action, "study_participants", participant_id, performed_by,
        f"patient={patient_id} arm={arm} status={status}",
    )
    return participant_id


def phase_windows(baseline_start: str, form: dict) -> dict:
    """Derive the four phase dates, defaulting to two consecutive periods."""

    def read(field: str) -> str | None:
        value = (form.get(field) or "").strip()
        if value and parse_date(value) is None:
            raise ResearchError(f"รูปแบบวันที่ของ {field} ไม่ถูกต้อง")
        return value or None

    try:
        days = int(form.get("phase_days") or DEFAULT_PHASE_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_PHASE_DAYS
    days = min(max(days, 7), 365)

    start = parse_date(baseline_start)
    baseline_end = read("baseline_end") or (start + timedelta(days=days - 1)).strftime(DATE_FMT)
    endline_start = read("endline_start") or (
        parse_date(baseline_end) + timedelta(days=1)
    ).strftime(DATE_FMT)
    endline_end = read("endline_end") or (
        parse_date(endline_start) + timedelta(days=days - 1)
    ).strftime(DATE_FMT)

    if parse_date(baseline_end) < start:
        raise ResearchError("วันสิ้นสุด baseline ต้องไม่ก่อนวันเริ่ม")
    if parse_date(endline_end) < parse_date(endline_start):
        raise ResearchError("วันสิ้นสุด endline ต้องไม่ก่อนวันเริ่ม")
    return {
        "baseline_start": baseline_start,
        "baseline_end": baseline_end,
        "endline_start": endline_start,
        "endline_end": endline_end,
    }


def withdraw(
    conn: sqlite3.Connection, participant_id: int, reason: str, performed_by: str
) -> bool:
    """Record a withdrawal — required for the CONSORT flow diagram."""
    reason = (reason or "").strip()
    if not reason:
        raise ResearchError("กรุณาระบุเหตุผลของการถอนตัว")
    cursor = conn.execute(
        "UPDATE study_participants SET status='withdrawn', withdrawn_at=?, "
        "withdrawal_reason=?, updated_at=? WHERE participant_id=?",
        (now(), reason[:200], now(), participant_id),
    )
    if not cursor.rowcount:
        return False
    log_audit(
        conn, "withdraw_participant", "study_participants", participant_id,
        performed_by, reason[:200],
    )
    return True


def set_status(
    conn: sqlite3.Connection, participant_id: int, status: str, performed_by: str
) -> bool:
    if status not in STATUSES:
        raise ResearchError("สถานะไม่ถูกต้อง")
    cursor = conn.execute(
        "UPDATE study_participants SET status=?, updated_at=? WHERE participant_id=?",
        (status, now(), participant_id),
    )
    if not cursor.rowcount:
        return False
    log_audit(
        conn, "update_participant", "study_participants", participant_id,
        performed_by, f"status={status}",
    )
    return True


def get_by_patient(conn: sqlite3.Connection, patient_id: int) -> dict | None:
    return fetch_one(
        conn, "SELECT * FROM study_participants WHERE patient_id=?", (patient_id,)
    )


def get_participant(conn: sqlite3.Connection, participant_id: int) -> dict | None:
    return fetch_one(
        conn,
        "SELECT sp.*, p.full_name, p.hn, p.sex, p.age_years, p.weight_kg, p.indication, "
        "p.target_inr_min, p.target_inr_max, p.line_user_id, p.active "
        "FROM study_participants sp JOIN patients p ON sp.patient_id=p.patient_id "
        "WHERE sp.participant_id=?",
        (participant_id,),
    )


def list_participants(
    conn: sqlite3.Connection, arm: str = "", status: str = "", study_code: str = ""
) -> list[dict]:
    sql = (
        "SELECT sp.*, p.full_name, p.hn, p.sex, p.age_years, p.weight_kg, p.indication, "
        "p.target_inr_min, p.target_inr_max, p.line_user_id, p.active "
        "FROM study_participants sp JOIN patients p ON sp.patient_id=p.patient_id WHERE 1=1"
    )
    params: list = []
    if arm in ARMS:
        sql += " AND sp.arm=?"
        params.append(arm)
    if status in STATUSES:
        sql += " AND sp.status=?"
        params.append(status)
    if study_code:
        sql += " AND sp.study_code=?"
        params.append(study_code)
    sql += " ORDER BY sp.arm, p.full_name"
    return fetch_all(conn, sql, tuple(params))


def analysis_population(conn: sqlite3.Connection, study_code: str = "") -> list[dict]:
    """Participants included in the outcome analysis (enrolled or completed)."""
    return [
        row for row in list_participants(conn, study_code=study_code)
        if row["status"] in ("enrolled", "completed")
    ]


def study_codes(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT study_code FROM study_participants ORDER BY study_code"
    ).fetchall()
    return [row[0] for row in rows if row[0]]


def enrollment_summary(conn: sqlite3.Connection, study_code: str = "") -> dict:
    """Counts for the CONSORT flow: screened, enrolled, completed, withdrawn."""
    participants = list_participants(conn, study_code=study_code)
    by_status = dict.fromkeys(STATUSES, 0)
    by_arm = dict.fromkeys(ARMS, 0)
    for row in participants:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
        if row["status"] in ("enrolled", "completed"):
            by_arm[row["arm"]] = by_arm.get(row["arm"], 0) + 1
    withdrawal_reasons: dict[str, int] = {}
    for row in participants:
        if row["status"] == "withdrawn" and row.get("withdrawal_reason"):
            reason = row["withdrawal_reason"]
            withdrawal_reasons[reason] = withdrawal_reasons.get(reason, 0) + 1
    total_eligible = by_status["enrolled"] + by_status["completed"] + by_status["withdrawn"]
    return {
        "total": len(participants),
        "by_status": by_status,
        "by_arm": by_arm,
        "analysed": by_status["enrolled"] + by_status["completed"],
        "withdrawal_reasons": withdrawal_reasons,
        "retention_percent": (
            round((total_eligible - by_status["withdrawn"]) / total_eligible * 100, 1)
            if total_eligible else None
        ),
    }


# ---------------------------------------------------------------------------
# Instrument measurements
# ---------------------------------------------------------------------------
def record_measurement(
    conn: sqlite3.Connection,
    participant_id: int,
    phase: str,
    instrument: str,
    value,
    performed_by: str,
    measured_on: str = "",
    text_value: str = "",
) -> int:
    if phase not in PHASES:
        raise ResearchError("ช่วงการเก็บข้อมูลไม่ถูกต้อง")
    if instrument not in INSTRUMENTS:
        raise ResearchError("เครื่องมือวัดไม่ถูกต้อง")
    maximum = INSTRUMENTS[instrument]["max"]
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        raise ResearchError("คะแนนต้องเป็นตัวเลข")
    if not 0 <= numeric <= maximum:
        raise ResearchError(f"คะแนนต้องอยู่ระหว่าง 0 ถึง {maximum}")

    measured = (measured_on or "").strip() or today()
    if parse_date(measured) is None:
        raise ResearchError("รูปแบบวันที่วัดไม่ถูกต้อง")

    # One value per (participant, phase, instrument): re-entering overwrites.
    conn.execute(
        "INSERT INTO study_measurements (participant_id, phase, instrument, value, "
        "max_value, text_value, measured_on, recorded_by, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(participant_id, phase, instrument) DO UPDATE SET "
        "value=excluded.value, max_value=excluded.max_value, "
        "text_value=excluded.text_value, measured_on=excluded.measured_on, "
        "recorded_by=excluded.recorded_by, created_at=excluded.created_at",
        (
            participant_id, phase, instrument, numeric, maximum,
            (text_value or "").strip()[:500] or None, measured, performed_by, now(),
        ),
    )
    row = fetch_one(
        conn,
        "SELECT measurement_id FROM study_measurements "
        "WHERE participant_id=? AND phase=? AND instrument=?",
        (participant_id, phase, instrument),
    )
    log_audit(
        conn, "record_measurement", "study_measurements", row["measurement_id"],
        performed_by, f"participant={participant_id} {phase}/{instrument}={numeric}",
    )
    return row["measurement_id"]


def measurements_for(conn: sqlite3.Connection, participant_id: int) -> dict:
    """Return {instrument: {phase: value}} for one participant."""
    rows = fetch_all(
        conn,
        "SELECT phase, instrument, value, measured_on FROM study_measurements "
        "WHERE participant_id=?",
        (participant_id,),
    )
    result: dict[str, dict] = {}
    for row in rows:
        result.setdefault(row["instrument"], {})[row["phase"]] = row
    return result


def measurements_bulk(
    conn: sqlite3.Connection, participant_ids: list[int]
) -> dict[int, dict]:
    if not participant_ids:
        return {}
    placeholders = ",".join("?" * len(participant_ids))
    rows = fetch_all(
        conn,
        f"SELECT participant_id, phase, instrument, value FROM study_measurements "
        f"WHERE participant_id IN ({placeholders})",
        tuple(participant_ids),
    )
    result: dict[int, dict] = {pid: {} for pid in participant_ids}
    for row in rows:
        result.setdefault(row["participant_id"], {}).setdefault(
            row["instrument"], {}
        )[row["phase"]] = row["value"]
    return result


# ---------------------------------------------------------------------------
# Phase outcomes computed from routine clinical data
# ---------------------------------------------------------------------------
@dataclass
class PhaseOutcome:
    adherence_percent: float | None
    doses_total: int
    doses_taken: int
    ttr_percent: float | None
    inr_tests: int
    inr_in_range: int
    inr_in_range_percent: float | None
    mean_inr: float | None


EMPTY_OUTCOME = PhaseOutcome(None, 0, 0, None, 0, 0, None, None)


def phase_outcome(
    conn: sqlite3.Connection, participant: dict, phase: str
) -> PhaseOutcome:
    """Adherence, TTR and INR control for one participant during one phase."""
    start = participant.get(f"{phase}_start")
    end = participant.get(f"{phase}_end")
    if not start or not end:
        return EMPTY_OUTCOME
    patient_id = participant["patient_id"]

    dose_rows = conn.execute(
        "SELECT scheduled_date, status FROM medication_plan "
        "WHERE patient_id=? AND scheduled_date>=? AND scheduled_date<=?",
        (patient_id, start, end),
    ).fetchall()
    # The phase window is historical, so nothing in it is "still pending";
    # pass a sentinel date so no dose is excluded from the denominator.
    summary = _summarize([(r[0], r[1]) for r in dose_rows], "0000-00-00")

    lab_rows = conn.execute(
        "SELECT test_date, value, in_range FROM lab_results "
        "WHERE patient_id=? AND lab_name='INR' AND value IS NOT NULL "
        "AND test_date>=? AND test_date<=? ORDER BY test_date",
        (patient_id, start, end),
    ).fetchall()
    values = [row[1] for row in lab_rows]
    in_range = sum(1 for row in lab_rows if row[2])
    ttr = _ttr_from_labs(
        [(row[0], row[1]) for row in lab_rows],
        participant.get("target_inr_min"), participant.get("target_inr_max"),
    )
    return PhaseOutcome(
        adherence_percent=summary["percent"] if summary["total"] else None,
        doses_total=summary["total"],
        doses_taken=summary["taken"] + summary["late"],
        ttr_percent=ttr,
        inr_tests=len(lab_rows),
        inr_in_range=in_range,
        inr_in_range_percent=round(in_range / len(lab_rows) * 100, 1) if lab_rows else None,
        mean_inr=round(sum(values) / len(values), 2) if values else None,
    )


def participant_dataset(conn: sqlite3.Connection, study_code: str = "") -> list[dict]:
    """One flat row per participant — the analysis dataset."""
    participants = analysis_population(conn, study_code)
    measurements = measurements_bulk(
        conn, [p["participant_id"] for p in participants]
    )
    dataset = []
    for participant in participants:
        baseline = phase_outcome(conn, participant, "baseline")
        endline = phase_outcome(conn, participant, "endline")
        scores = measurements.get(participant["participant_id"], {})
        row = {
            "participant_id": participant["participant_id"],
            "patient_id": participant["patient_id"],
            "study_code": participant["study_code"],
            "hn": participant.get("hn"),
            "full_name": participant.get("full_name"),
            "arm": participant["arm"],
            "status": participant["status"],
            "sex": participant.get("sex"),
            "age_years": participant.get("age_years"),
            "weight_kg": participant.get("weight_kg"),
            "indication": participant.get("indication"),
            "target_inr_min": participant.get("target_inr_min"),
            "target_inr_max": participant.get("target_inr_max"),
            "line_linked": 1 if participant.get("line_user_id") else 0,
            "consent_date": participant.get("consent_date"),
            "baseline": baseline,
            "endline": endline,
        }
        for instrument in INSTRUMENTS:
            phases = scores.get(instrument, {})
            row[f"{instrument}_baseline"] = phases.get("baseline")
            row[f"{instrument}_endline"] = phases.get("endline")
        dataset.append(row)
    return dataset


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
CONTINUOUS_OUTCOMES = {
    "adherence_percent": "ความสม่ำเสมอในการกินยา (%)",
    "ttr_percent": "Time in Therapeutic Range (%)",
    "inr_in_range_percent": "สัดส่วนผล INR ที่อยู่ในเป้าหมาย (%)",
}


def _pair_values(dataset: list[dict], attribute: str) -> tuple[list, list]:
    before, after = [], []
    for row in dataset:
        before.append(getattr(row["baseline"], attribute))
        after.append(getattr(row["endline"], attribute))
    return before, after


def within_group_analysis(dataset: list[dict]) -> list[dict]:
    """Before/after comparison inside one group (the primary analysis)."""
    results = []
    for attribute, label in CONTINUOUS_OUTCOMES.items():
        before, after = _pair_values(dataset, attribute)
        results.append({
            "key": attribute,
            "label": label,
            "baseline": describe(before),
            "endline": describe(after),
            "test": paired_t_test(before, after),
            "nonparametric": wilcoxon_signed_rank(before, after),
        })
    for instrument, info in INSTRUMENTS.items():
        if not info["scored"]:
            continue
        before = [row.get(f"{instrument}_baseline") for row in dataset]
        after = [row.get(f"{instrument}_endline") for row in dataset]
        if not any(v is not None for v in before + after):
            continue
        results.append({
            "key": instrument,
            "label": f"{info['label']} (เต็ม {info['max']})",
            "baseline": describe(before),
            "endline": describe(after),
            "test": paired_t_test(before, after),
            "nonparametric": wilcoxon_signed_rank(before, after),
        })
    return results


ADHERENCE_TARGET = 80.0
TTR_TARGET = 65.0


def binary_outcome_analysis(dataset: list[dict]) -> list[dict]:
    """Proportion meeting a clinical threshold, before vs after (McNemar)."""
    definitions = [
        ("adherence_percent", ADHERENCE_TARGET,
         f"ความสม่ำเสมอ ≥ {ADHERENCE_TARGET:.0f}%"),
        ("ttr_percent", TTR_TARGET, f"TTR ≥ {TTR_TARGET:.0f}%"),
    ]
    results = []
    for attribute, threshold, label in definitions:
        before_values, after_values = _pair_values(dataset, attribute)
        before = [
            None if value is None else value >= threshold for value in before_values
        ]
        after = [
            None if value is None else value >= threshold for value in after_values
        ]
        baseline_n = sum(1 for value in before if value is not None)
        endline_n = sum(1 for value in after if value is not None)
        results.append({
            "label": label,
            "baseline": proportion_ci(sum(1 for v in before if v), baseline_n),
            "endline": proportion_ci(sum(1 for v in after if v), endline_n),
            "baseline_n": baseline_n,
            "endline_n": endline_n,
            "test": mcnemar_test(before, after),
        })
    return results


def between_group_analysis(dataset: list[dict]) -> list[dict]:
    """Intervention vs control at endline, when a control arm exists."""
    intervention = [row for row in dataset if row["arm"] == "intervention"]
    control = [row for row in dataset if row["arm"] == "control"]
    if not intervention or not control:
        return []
    results = []
    for attribute, label in CONTINUOUS_OUTCOMES.items():
        a = [getattr(row["endline"], attribute) for row in intervention]
        b = [getattr(row["endline"], attribute) for row in control]
        results.append({
            "key": attribute,
            "label": label,
            "intervention": describe(a),
            "control": describe(b),
            "test": independent_t_test(a, b),
        })
    for instrument, info in INSTRUMENTS.items():
        if not info["scored"]:
            continue
        a = [row.get(f"{instrument}_endline") for row in intervention]
        b = [row.get(f"{instrument}_endline") for row in control]
        if not any(v is not None for v in a + b):
            continue
        results.append({
            "key": instrument,
            "label": f"{info['label']} (เต็ม {info['max']})",
            "intervention": describe(a),
            "control": describe(b),
            "test": independent_t_test(a, b),
        })
    return results


def baseline_characteristics(dataset: list[dict]) -> dict:
    """Table 1 — demographics by arm, with a comparability test."""
    intervention = [row for row in dataset if row["arm"] == "intervention"]
    control = [row for row in dataset if row["arm"] == "control"]
    has_control = bool(control)

    def continuous(rows: list[dict], key: str) -> Descriptive:
        return describe([row.get(key) for row in rows])

    continuous_rows = []
    for key, label in (("age_years", "อายุ (ปี)"), ("weight_kg", "น้ำหนัก (กก.)")):
        entry = {
            "label": label,
            "all": continuous(dataset, key),
            "intervention": continuous(intervention, key),
            "control": continuous(control, key) if has_control else None,
            "test": (
                independent_t_test(
                    [row.get(key) for row in intervention],
                    [row.get(key) for row in control],
                )
                if has_control else None
            ),
        }
        continuous_rows.append(entry)

    baseline_adherence = {
        "label": "ความสม่ำเสมอที่ baseline (%)",
        "all": describe([row["baseline"].adherence_percent for row in dataset]),
        "intervention": describe(
            [row["baseline"].adherence_percent for row in intervention]
        ),
        "control": (
            describe([row["baseline"].adherence_percent for row in control])
            if has_control else None
        ),
        "test": (
            independent_t_test(
                [row["baseline"].adherence_percent for row in intervention],
                [row["baseline"].adherence_percent for row in control],
            )
            if has_control else None
        ),
    }
    continuous_rows.append(baseline_adherence)

    categorical_rows = []
    for key, label, options in (
        ("sex", "เพศ", {"male": "ชาย", "female": "หญิง"}),
        ("indication", "ข้อบ่งใช้",
         {code: info["label"] for code, info in INDICATIONS.items()}),
    ):
        counts_all: dict[str, int] = {}
        counts_intervention: dict[str, int] = {}
        counts_control: dict[str, int] = {}
        for row in dataset:
            value = row.get(key) or "unknown"
            counts_all[value] = counts_all.get(value, 0) + 1
        for row in intervention:
            value = row.get(key) or "unknown"
            counts_intervention[value] = counts_intervention.get(value, 0) + 1
        for row in control:
            value = row.get(key) or "unknown"
            counts_control[value] = counts_control.get(value, 0) + 1

        levels = sorted(counts_all)
        test = None
        if has_control and len(levels) >= 2:
            table = [
                [counts_intervention.get(level, 0) for level in levels],
                [counts_control.get(level, 0) for level in levels],
            ]
            test = chi_square_test(table)
        categorical_rows.append({
            "label": label,
            "levels": [
                {
                    "label": options.get(level, "ไม่ระบุ" if level == "unknown" else level),
                    "all": counts_all.get(level, 0),
                    "all_percent": round(counts_all.get(level, 0) / len(dataset) * 100, 1)
                    if dataset else 0,
                    "intervention": counts_intervention.get(level, 0),
                    "control": counts_control.get(level, 0),
                }
                for level in levels
            ],
            "test": test,
        })

    return {
        "n_total": len(dataset),
        "n_intervention": len(intervention),
        "n_control": len(control),
        "has_control": has_control,
        "continuous": continuous_rows,
        "categorical": categorical_rows,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
EXPORT_COLUMNS = [
    ("participant_id", "รหัสผู้เข้าร่วม (study ID)", "integer"),
    ("hn", "HN (ระบุตัวตน)", "text"),
    ("full_name", "ชื่อ-สกุล (ระบุตัวตน)", "text"),
    ("arm", "กลุ่ม: intervention / control", "text"),
    ("status", "สถานะ: enrolled / completed / withdrawn", "text"),
    ("sex", "เพศ: male / female", "text"),
    ("age_years", "อายุ (ปี)", "integer"),
    ("weight_kg", "น้ำหนัก (กก.)", "numeric"),
    ("indication", "ข้อบ่งใช้วาร์ฟาริน", "text"),
    ("target_inr_min", "INR เป้าหมายต่ำสุด", "numeric"),
    ("target_inr_max", "INR เป้าหมายสูงสุด", "numeric"),
    ("line_linked", "เชื่อม LINE แล้ว: 1 = ใช่, 0 = ไม่", "integer"),
    ("baseline_adherence", "ความสม่ำเสมอ baseline (%)", "numeric"),
    ("endline_adherence", "ความสม่ำเสมอ endline (%)", "numeric"),
    ("adherence_change", "ผลต่างความสม่ำเสมอ (endline − baseline)", "numeric"),
    ("baseline_doses_total", "จำนวนโดสใน baseline", "integer"),
    ("endline_doses_total", "จำนวนโดสใน endline", "integer"),
    ("baseline_ttr", "TTR baseline (%)", "numeric"),
    ("endline_ttr", "TTR endline (%)", "numeric"),
    ("ttr_change", "ผลต่าง TTR", "numeric"),
    ("baseline_inr_tests", "จำนวนครั้งที่ตรวจ INR ใน baseline", "integer"),
    ("endline_inr_tests", "จำนวนครั้งที่ตรวจ INR ใน endline", "integer"),
    ("baseline_inr_in_range_pct", "สัดส่วน INR ในเป้าหมาย baseline (%)", "numeric"),
    ("endline_inr_in_range_pct", "สัดส่วน INR ในเป้าหมาย endline (%)", "numeric"),
    ("baseline_mean_inr", "ค่าเฉลี่ย INR baseline", "numeric"),
    ("endline_mean_inr", "ค่าเฉลี่ย INR endline", "numeric"),
]

IDENTIFYING_COLUMNS = {"hn", "full_name"}


def export_rows(dataset: list[dict], deidentified: bool = True) -> tuple[list[str], list[list]]:
    """Flatten the dataset into header + rows, ready for CSV/Excel.

    De-identified is the default: an analysis file should not carry the HN or
    the patient's name out of the hospital unless someone deliberately asks.
    """
    columns = [
        column for column in EXPORT_COLUMNS
        if not (deidentified and column[0] in IDENTIFYING_COLUMNS)
    ]
    instrument_columns = []
    for instrument, info in INSTRUMENTS.items():
        for phase in PHASES:
            instrument_columns.append((
                f"{instrument}_{phase}",
                f"{info['label']} — {PHASES[phase]}",
                "numeric",
            ))
        instrument_columns.append((
            f"{instrument}_change",
            f"{info['label']} — ผลต่าง",
            "numeric",
        ))
    columns = columns + instrument_columns

    header = [name for name, _, _ in columns]
    rows = []
    for record in dataset:
        flat = _flatten(record)
        rows.append([flat.get(name) for name in header])
    return header, rows


def _difference(after, before):
    if after is None or before is None:
        return None
    return round(after - before, 2)


def _flatten(record: dict) -> dict:
    baseline, endline = record["baseline"], record["endline"]
    flat = {
        "participant_id": record["participant_id"],
        "hn": record.get("hn"),
        "full_name": record.get("full_name"),
        "arm": record["arm"],
        "status": record["status"],
        "sex": record.get("sex"),
        "age_years": record.get("age_years"),
        "weight_kg": record.get("weight_kg"),
        "indication": record.get("indication"),
        "target_inr_min": record.get("target_inr_min"),
        "target_inr_max": record.get("target_inr_max"),
        "line_linked": record.get("line_linked"),
        "baseline_adherence": baseline.adherence_percent,
        "endline_adherence": endline.adherence_percent,
        "adherence_change": _difference(endline.adherence_percent, baseline.adherence_percent),
        "baseline_doses_total": baseline.doses_total,
        "endline_doses_total": endline.doses_total,
        "baseline_ttr": baseline.ttr_percent,
        "endline_ttr": endline.ttr_percent,
        "ttr_change": _difference(endline.ttr_percent, baseline.ttr_percent),
        "baseline_inr_tests": baseline.inr_tests,
        "endline_inr_tests": endline.inr_tests,
        "baseline_inr_in_range_pct": baseline.inr_in_range_percent,
        "endline_inr_in_range_pct": endline.inr_in_range_percent,
        "baseline_mean_inr": baseline.mean_inr,
        "endline_mean_inr": endline.mean_inr,
    }
    for instrument in INSTRUMENTS:
        before = record.get(f"{instrument}_baseline")
        after = record.get(f"{instrument}_endline")
        flat[f"{instrument}_baseline"] = before
        flat[f"{instrument}_endline"] = after
        flat[f"{instrument}_change"] = _difference(after, before)
    return flat


def codebook(deidentified: bool = True) -> list[dict]:
    """Variable dictionary shipped alongside the data for SPSS/R users."""
    entries = [
        {"name": name, "label": label, "type": kind}
        for name, label, kind in EXPORT_COLUMNS
        if not (deidentified and name in IDENTIFYING_COLUMNS)
    ]
    for instrument, info in INSTRUMENTS.items():
        for phase, phase_label in PHASES.items():
            entries.append({
                "name": f"{instrument}_{phase}",
                "label": f"{info['label']} — {phase_label} (0–{info['max']})",
                "type": "numeric",
            })
        entries.append({
            "name": f"{instrument}_change",
            "label": f"{info['label']} — ผลต่าง endline − baseline",
            "type": "numeric",
        })
    return entries


def open_measurement_gaps(conn: sqlite3.Connection, study_code: str = "") -> list[dict]:
    """Participants missing a scored instrument — the data-collection worklist."""
    participants = analysis_population(conn, study_code)
    measurements = measurements_bulk(conn, [p["participant_id"] for p in participants])
    gaps = []
    for participant in participants:
        recorded = measurements.get(participant["participant_id"], {})
        missing = [
            f"{info['label']} ({PHASES[phase]})"
            for instrument, info in INSTRUMENTS.items() if info["scored"]
            for phase in PHASES
            if recorded.get(instrument, {}).get(phase) is None
        ]
        if missing:
            gaps.append({"participant": participant, "missing": missing})
    return gaps


def study_progress(conn: sqlite3.Connection, study_code: str = "") -> dict:
    """How far through their follow-up window each participant is."""
    participants = analysis_population(conn, study_code)
    current = now_dt().date()
    finished = ongoing = not_started = 0
    for participant in participants:
        endline_end = parse_date(participant.get("endline_end"))
        baseline_start = parse_date(participant.get("baseline_start"))
        if endline_end and current > endline_end:
            finished += 1
        elif baseline_start and current < baseline_start:
            not_started += 1
        else:
            ongoing += 1
    return {
        "total": len(participants),
        "finished": finished,
        "ongoing": ongoing,
        "not_started": not_started,
    }


def enrolled_count() -> int:
    """Badge count for the navigation."""
    try:
        with read_db() as conn:
            return int(
                scalar(
                    conn,
                    "SELECT COUNT(*) FROM study_participants "
                    "WHERE status IN ('enrolled','completed')",
                )
            )
    except Exception:  # pragma: no cover - table may predate the migration
        return 0
