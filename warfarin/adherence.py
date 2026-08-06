"""Adherence, streak and INR quality metrics.

Every metric has a bulk variant: dashboards and reports iterate over the whole
active caseload, and a per-patient query there is an N+1 that gets slower with
every patient enrolled.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from warfarin.time_utils import DATE_FMT, now_dt, today

DONE_STATUSES = ("taken", "late")
EMPTY_ADHERENCE = {
    "total": 0, "taken": 0, "missed": 0, "late": 0,
    "pending": 0, "percent": 0.0,
}


def _summarize(rows: list[tuple[str, str]], today_str: str) -> dict:
    """Turn (scheduled_date, status) rows into an adherence summary.

    Doses still 'planned' for today are excluded from the denominator — they
    are not late yet, and counting them would make every patient look
    non-adherent until the evening reminder cycle finishes.
    """
    taken = late = missed = pending = 0
    for date_str, status in rows:
        if status == "taken":
            taken += 1
        elif status == "late":
            late += 1
        elif status == "missed":
            missed += 1
        elif date_str == today_str:
            pending += 1
        else:
            # A past dose still marked 'planned' has effectively been missed;
            # the 21:00 job will confirm it, but the metric should not wait.
            missed += 1
    total = taken + late + missed
    percent = round((taken + late) / total * 100, 1) if total else 0.0
    return {
        "total": total, "taken": taken, "missed": missed,
        "late": late, "pending": pending, "percent": percent,
    }


def compute_adherence(conn: sqlite3.Connection, patient_id: int, days: int = 7) -> dict:
    """Adherence over the last `days` days (inclusive of today)."""
    today_str = today()
    since = (now_dt() - timedelta(days=max(days, 1) - 1)).strftime(DATE_FMT)
    rows = conn.execute(
        "SELECT scheduled_date, status FROM medication_plan "
        "WHERE patient_id=? AND scheduled_date>=? AND scheduled_date<=?",
        (patient_id, since, today_str),
    ).fetchall()
    return _summarize([(r[0], r[1]) for r in rows], today_str)


def compute_adherence_bulk(
    conn: sqlite3.Connection, patient_ids: list[int], days: int = 7
) -> dict[int, dict]:
    """Adherence for many patients in one query."""
    if not patient_ids:
        return {}
    today_str = today()
    since = (now_dt() - timedelta(days=max(days, 1) - 1)).strftime(DATE_FMT)
    placeholders = ",".join("?" * len(patient_ids))
    rows = conn.execute(
        f"SELECT patient_id, scheduled_date, status FROM medication_plan "
        f"WHERE patient_id IN ({placeholders}) AND scheduled_date>=? AND scheduled_date<=?",
        (*patient_ids, since, today_str),
    ).fetchall()
    grouped: dict[int, list[tuple[str, str]]] = {pid: [] for pid in patient_ids}
    for r in rows:
        grouped.setdefault(r[0], []).append((r[1], r[2]))
    return {pid: _summarize(items, today_str) for pid, items in grouped.items()}


def _streak_from_days(by_day: dict[str, list[str]], today_str: str) -> int:
    """Consecutive days where every scheduled dose was confirmed."""
    streak = 0
    for date_str in sorted(by_day.keys(), reverse=True):
        statuses = by_day[date_str]
        if date_str > today_str:
            continue  # future plan rows never break or extend a streak
        if date_str == today_str and all(s == "planned" for s in statuses):
            continue  # today is still open
        if all(s in DONE_STATUSES for s in statuses):
            streak += 1
        else:
            break
    return streak


def compute_streak(conn: sqlite3.Connection, patient_id: int) -> int:
    rows = conn.execute(
        "SELECT scheduled_date, status FROM medication_plan "
        "WHERE patient_id=? ORDER BY scheduled_date DESC LIMIT 1000",
        (patient_id,),
    ).fetchall()
    by_day: dict[str, list[str]] = {}
    for r in rows:
        by_day.setdefault(r[0], []).append(r[1])
    return _streak_from_days(by_day, today())


def compute_streak_bulk(
    conn: sqlite3.Connection, patient_ids: list[int], lookback_days: int = 400
) -> dict[int, int]:
    if not patient_ids:
        return {}
    since = (now_dt() - timedelta(days=lookback_days)).strftime(DATE_FMT)
    placeholders = ",".join("?" * len(patient_ids))
    rows = conn.execute(
        f"SELECT patient_id, scheduled_date, status FROM medication_plan "
        f"WHERE patient_id IN ({placeholders}) AND scheduled_date>=? "
        f"ORDER BY scheduled_date DESC",
        (*patient_ids, since),
    ).fetchall()
    per_patient: dict[int, dict[str, list[str]]] = {pid: {} for pid in patient_ids}
    for r in rows:
        per_patient.setdefault(r[0], {}).setdefault(r[1], []).append(r[2])
    today_str = today()
    return {pid: _streak_from_days(days, today_str) for pid, days in per_patient.items()}


def compute_gamification_score(adherence_pct: float, streak: int) -> float:
    """Adherence plus a capped streak bonus — used for patient encouragement."""
    return round(adherence_pct + min(streak, 30) * 0.5, 1)


def _ttr_from_labs(labs: list[tuple[str, float]], lo: float, hi: float) -> float | None:
    """Rosendaal linear interpolation between consecutive INR measurements."""
    if len(labs) < 2 or lo is None or hi is None:
        return None
    in_range_days = 0
    total_days = 0
    for i in range(len(labs) - 1):
        try:
            d1 = datetime.strptime(labs[i][0][:10], DATE_FMT)
            d2 = datetime.strptime(labs[i + 1][0][:10], DATE_FMT)
        except (ValueError, TypeError):
            continue
        gap = (d2 - d1).days
        # Skip same-day repeats and absurd gaps (>1 year is not interpolatable).
        if gap <= 0 or gap > 365:
            continue
        v1, v2 = labs[i][1], labs[i + 1][1]
        if v1 is None or v2 is None:
            continue
        for step in range(gap):
            value = v1 + (v2 - v1) * (step / gap)
            if lo <= value <= hi:
                in_range_days += 1
            total_days += 1
    if not total_days:
        return None
    return round(in_range_days / total_days * 100, 1)


def compute_ttr(conn: sqlite3.Connection, patient_id: int) -> float | None:
    """Time in Therapeutic Range (%) — the primary quality metric for warfarin."""
    target = conn.execute(
        "SELECT target_inr_min, target_inr_max FROM patients WHERE patient_id=?",
        (patient_id,),
    ).fetchone()
    if target is None:
        return None
    rows = conn.execute(
        "SELECT test_date, value FROM lab_results "
        "WHERE patient_id=? AND value IS NOT NULL AND lab_name='INR' "
        "ORDER BY test_date",
        (patient_id,),
    ).fetchall()
    return _ttr_from_labs([(r[0], r[1]) for r in rows], target[0], target[1])


def compute_ttr_bulk(
    conn: sqlite3.Connection, patient_ids: list[int]
) -> dict[int, float | None]:
    if not patient_ids:
        return {}
    placeholders = ",".join("?" * len(patient_ids))
    targets = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            f"SELECT patient_id, target_inr_min, target_inr_max FROM patients "
            f"WHERE patient_id IN ({placeholders})",
            tuple(patient_ids),
        ).fetchall()
    }
    rows = conn.execute(
        f"SELECT patient_id, test_date, value FROM lab_results "
        f"WHERE patient_id IN ({placeholders}) AND value IS NOT NULL AND lab_name='INR' "
        f"ORDER BY patient_id, test_date",
        tuple(patient_ids),
    ).fetchall()
    grouped: dict[int, list[tuple[str, float]]] = {pid: [] for pid in patient_ids}
    for r in rows:
        grouped.setdefault(r[0], []).append((r[1], r[2]))
    result: dict[int, float | None] = {}
    for pid in patient_ids:
        lo, hi = targets.get(pid, (None, None))
        result[pid] = _ttr_from_labs(grouped.get(pid, []), lo, hi)
    return result


def compute_at_risk(
    conn: sqlite3.Connection, patients: list[dict], limit: int | None = None
) -> list[dict]:
    """Patients needing follow-up, worst first.

    Risk triggers: adherence below 70% in the last 7 days, three or more
    missed doses, two or more consecutive missed days, or a last INR outside
    the patient's own target range.
    """
    if not patients:
        return []
    ids = [p["patient_id"] for p in patients]
    adherence = compute_adherence_bulk(conn, ids, 7)
    adherence30 = compute_adherence_bulk(conn, ids, 30)
    streaks = compute_streak_bulk(conn, ids)
    consecutive = _consecutive_missed_bulk(conn, ids)
    last_inr = last_inr_bulk(conn, ids)

    result = []
    for patient in patients:
        pid = patient["patient_id"]
        adh = adherence.get(pid, dict(EMPTY_ADHERENCE))
        inr = last_inr.get(pid)
        out_of_range = bool(inr and not inr["in_range"])
        missed_run = consecutive.get(pid, 0)
        reasons = []
        if adh["total"] and adh["percent"] < 70:
            reasons.append(f"กินยาสม่ำเสมอ {adh['percent']}%")
        if adh["missed"] >= 3:
            reasons.append(f"ลืมกินยา {adh['missed']} ครั้งใน 7 วัน")
        if missed_run >= 2:
            reasons.append(f"ขาดยาติดต่อกัน {missed_run} วัน")
        if out_of_range:
            reasons.append(f"INR ล่าสุด {inr['value']} นอกเป้าหมาย")
        if not reasons:
            continue
        result.append({
            "patient": patient,
            "adherence": adh,
            "adherence30": adherence30.get(pid, dict(EMPTY_ADHERENCE)),
            "streak": streaks.get(pid, 0),
            "consecutive_missed": missed_run,
            "last_inr": inr,
            "reasons": reasons,
        })
    result.sort(
        key=lambda x: (-x["consecutive_missed"], x["adherence"]["percent"])
    )
    return result[:limit] if limit else result


def _consecutive_missed_bulk(
    conn: sqlite3.Connection, patient_ids: list[int]
) -> dict[int, int]:
    """Count of consecutive missed days ending at the most recent past dose."""
    if not patient_ids:
        return {}
    today_str = today()
    since = (now_dt() - timedelta(days=60)).strftime(DATE_FMT)
    placeholders = ",".join("?" * len(patient_ids))
    rows = conn.execute(
        f"SELECT patient_id, scheduled_date, status FROM medication_plan "
        f"WHERE patient_id IN ({placeholders}) AND scheduled_date>=? AND scheduled_date<? "
        f"ORDER BY scheduled_date DESC",
        (*patient_ids, since, today_str),
    ).fetchall()
    by_patient: dict[int, dict[str, list[str]]] = {pid: {} for pid in patient_ids}
    for r in rows:
        by_patient.setdefault(r[0], {}).setdefault(r[1], []).append(r[2])
    result = {}
    for pid, days in by_patient.items():
        run = 0
        for date_str in sorted(days.keys(), reverse=True):
            if any(s not in DONE_STATUSES for s in days[date_str]):
                run += 1
            else:
                break
        result[pid] = run
    return result


def last_inr_bulk(conn: sqlite3.Connection, patient_ids: list[int]) -> dict[int, dict]:
    """Most recent INR per patient, in one query."""
    if not patient_ids:
        return {}
    placeholders = ",".join("?" * len(patient_ids))
    rows = conn.execute(
        f"SELECT l.patient_id, l.value, l.test_date, l.in_range FROM lab_results l "
        f"JOIN (SELECT patient_id, MAX(test_date) AS md FROM lab_results "
        f"      WHERE patient_id IN ({placeholders}) AND lab_name='INR' "
        f"      GROUP BY patient_id) m "
        f"  ON l.patient_id=m.patient_id AND l.test_date=m.md "
        f"WHERE l.lab_name='INR' "
        f"GROUP BY l.patient_id",
        tuple(patient_ids),
    ).fetchall()
    return {r[0]: {"value": r[1], "test_date": r[2], "in_range": r[3]} for r in rows}


def daily_adherence_series(
    conn: sqlite3.Connection, patient_id: int, days: int = 30
) -> list[dict]:
    """Per-day taken/total series used by the patient chart."""
    since = (now_dt() - timedelta(days=days)).strftime(DATE_FMT)
    rows = conn.execute(
        "SELECT scheduled_date, status FROM medication_plan "
        "WHERE patient_id=? AND scheduled_date>=? ORDER BY scheduled_date",
        (patient_id, since),
    ).fetchall()
    daily: dict[str, dict] = {}
    for date_str, status in rows:
        slot = daily.setdefault(date_str, {"date": date_str, "total": 0, "taken": 0})
        slot["total"] += 1
        if status in DONE_STATUSES:
            slot["taken"] += 1
    series = []
    for date_str in sorted(daily):
        slot = daily[date_str]
        slot["percent"] = round(slot["taken"] / slot["total"] * 100) if slot["total"] else 0
        series.append(slot)
    return series


def heatmap_series(conn: sqlite3.Connection, patient_id: int, days: int = 90) -> list[dict]:
    """Calendar heatmap data; worst status of the day wins."""
    end = now_dt().date()
    start = end - timedelta(days=days - 1)
    rows = conn.execute(
        "SELECT scheduled_date, status FROM medication_plan "
        "WHERE patient_id=? AND scheduled_date>=? AND scheduled_date<=? "
        "ORDER BY scheduled_date",
        (patient_id, start.strftime(DATE_FMT), end.strftime(DATE_FMT)),
    ).fetchall()
    priority = {"missed": 4, "planned": 3, "late": 2, "taken": 1}
    daily: dict[str, dict] = {}
    for date_str, status in rows:
        slot = daily.setdefault(
            date_str, {"date": date_str, "total": 0, "taken": 0, "status": "none"}
        )
        slot["total"] += 1
        if status in DONE_STATUSES:
            slot["taken"] += 1
        if priority.get(status, 0) > priority.get(slot["status"], 0):
            slot["status"] = status
    return [
        daily.get(
            (start + timedelta(days=i)).strftime(DATE_FMT),
            {
                "date": (start + timedelta(days=i)).strftime(DATE_FMT),
                "status": "none", "total": 0, "taken": 0,
            },
        )
        for i in range(days)
    ]


def monthly_trend(conn: sqlite3.Connection, months: int = 6) -> list[dict]:
    """Clinic-wide adherence per month, for the report trend chart."""
    from warfarin.time_utils import THAI_MONTHS_SHORT

    end = now_dt().date()
    buckets: list[tuple[int, int]] = []
    for delta in range(months - 1, -1, -1):
        total = end.year * 12 + (end.month - 1) - delta
        buckets.append((total // 12, total % 12 + 1))
    first = f"{buckets[0][0]:04d}-{buckets[0][1]:02d}-01"
    rows = conn.execute(
        "SELECT substr(scheduled_date,1,7) AS ym, status, COUNT(*) AS n "
        "FROM medication_plan WHERE scheduled_date>=? AND scheduled_date<=? "
        "GROUP BY ym, status",
        (first, today()),
    ).fetchall()
    data: dict[str, dict] = {}
    for ym, status, count in rows:
        slot = data.setdefault(ym, {"total": 0, "done": 0})
        if status in ("taken", "late", "missed"):
            slot["total"] += count
        if status in DONE_STATUSES:
            slot["done"] += count
    series = []
    for year, month in buckets:
        slot = data.get(f"{year:04d}-{month:02d}")
        percent = (
            round(slot["done"] / slot["total"] * 100, 1)
            if slot and slot["total"] else None
        )
        series.append({
            "year": year, "month": month,
            "label": f"{THAI_MONTHS_SHORT[month]} {year + 543}",
            "percent": percent,
        })
    return series
