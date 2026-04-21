"""Smoke tests for Warfarin medication tracker.

Covers: auth, dashboard render, QR PNG, heatmap API, LINE webhook signature,
LINE command router (help, education), adherence math, TTR edge case, education page,
Quick Reply, targeted broadcast preview, Rich Menu image generation.
"""
import sqlite3
import uuid

import app


# ---------------------------------------------------------------------------
# HTTP smoke tests
# ---------------------------------------------------------------------------
def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "เข้าสู่ระบบ" in r.text or "login" in r.text.lower()


def test_login_flow_redirect(client):
    r = client.post(
        "/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    if r.status_code == 303:
        assert "/dashboard" in r.headers.get("location", "")


def test_dashboard_renders_after_login(admin_login):
    r = admin_login.get("/dashboard")
    assert r.status_code == 200


def test_education_page_renders(client):
    r = client.get("/education")
    assert r.status_code == 200
    assert "วิตามินเค" in r.text
    assert "สัญญาณอันตราย" in r.text


def test_qr_png_returns_png(client, db_conn, seed_patient):
    """seed dose + token แล้ว GET /qr/<id>.png"""
    pid = seed_patient
    cur = db_conn.execute(
        "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, warfarin_mg, status, created_at) "
        "VALUES (?, ?, ?, ?, 'planned', ?)",
        (pid, app._today(), "18:00", 3.0, app._now()),
    )
    dose_id = cur.lastrowid
    token = uuid.uuid4().hex[:24]
    db_conn.execute(
        "INSERT INTO dose_tokens (token_id, dose_id, created_at, expires_at, is_used) "
        "VALUES (?, ?, ?, ?, 0)",
        (token, dose_id, app._now(), "2099-12-31T23:59:59"),
    )
    db_conn.commit()
    r = client.get(f"/qr/{token}.png")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("image/png")
    assert r.content[:4] == b"\x89PNG"


def test_heatmap_endpoint_shape(client, db_conn, seed_patient):
    pid = seed_patient
    today = app._today()
    for i, status in enumerate(("taken", "taken", "missed", "late", "planned")):
        db_conn.execute(
            "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, warfarin_mg, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pid, today, f"{8+i:02d}:00", 3.0, status, app._now()),
        )
    db_conn.commit()
    r = client.get(f"/api/patients/{pid}/heatmap-data")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 90
    for row in data:
        assert "date" in row and "status" in row
    today_rows = [d for d in data if d["date"] == today]
    assert today_rows
    # มี status missed ปนอยู่ → ต้องเป็น missed (priority สูงสุด)
    assert today_rows[0]["status"] == "missed"


def test_line_webhook_bad_signature(client):
    """LINE webhook ต้อง 200 เสมอเมื่อไม่มี config (test environment)"""
    r = client.post(
        "/webhook",
        content=b'{"events":[]}',
        headers={"X-Line-Signature": "garbage"},
    )
    assert r.status_code == 200


def test_line_webhook_empty_body(client):
    r = client.post("/webhook", content=b"")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# LINE command router (offline — direct function calls)
# ---------------------------------------------------------------------------
def _msg_text(m) -> str:
    """ดึงข้อความออกจาก Message object หรือ str"""
    if isinstance(m, str):
        return m
    for attr in ("text", "alt_text"):
        v = getattr(m, attr, None)
        if v:
            return v
    return ""


def test_route_line_command_help_lists_commands():
    r = app._route_line_command("Utest", "help", "help")
    assert isinstance(r, list) and r
    text = _msg_text(r[0])
    assert "สถานะ" in text
    assert "ความรู้" in text


def test_route_line_command_education():
    r = app._route_line_command("Utest", "ความรู้", "ความรู้")
    assert isinstance(r, list) and r
    text = _msg_text(r[0])
    assert "วาร์ฟาริน" in text or "คู่มือ" in text


def test_route_line_command_unknown_user():
    """ผู้ใช้ที่ไม่ลงทะเบียน → ต้องบอกให้ลงทะเบียน ไม่ crash"""
    r = app._route_line_command("Uneverseen_xyz", "สถานะ", "สถานะ")
    assert isinstance(r, list) and r
    text = _msg_text(r[0])
    assert "ลงทะเบียน" in text or "ผู้ป่วย" in text


# ---------------------------------------------------------------------------
# Pure-function math tests (no HTTP)
# ---------------------------------------------------------------------------
def test_adherence_math_on_fixture(db_conn, seed_patient):
    pid = seed_patient
    today = app._today()
    # 7 taken + 3 missed within last 30 days
    for i in range(7):
        db_conn.execute(
            "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, warfarin_mg, status, created_at) "
            "VALUES (?, ?, ?, ?, 'taken', ?)",
            (pid, today, f"{i:02d}:00", 3.0, app._now()),
        )
    for i in range(3):
        db_conn.execute(
            "INSERT INTO medication_plan (patient_id, scheduled_date, scheduled_time, warfarin_mg, status, created_at) "
            "VALUES (?, ?, ?, ?, 'missed', ?)",
            (pid, today, f"{10+i:02d}:00", 3.0, app._now()),
        )
    db_conn.commit()
    with app.db() as conn:
        a = app.compute_adherence(conn, pid, 30)
    assert a["taken"] == 7
    assert a["total"] == 10
    assert a["percent"] == 70


def test_ttr_single_value_edge_case(db_conn, seed_patient):
    """single in-range lab → ต้องไม่ raise (อาจคืน None หรือ 100.0)"""
    pid = seed_patient
    db_conn.execute(
        "INSERT INTO lab_results (patient_id, lab_name, value, test_date, in_range, created_at) "
        "VALUES (?, 'INR', 2.5, ?, 1, ?)",
        (pid, app._today(), app._now()),
    )
    db_conn.commit()
    with app.db() as conn:
        v = app.compute_ttr(conn, pid)
    assert v is None or isinstance(v, (int, float))


# ---------------------------------------------------------------------------
# Quick Reply tests
# ---------------------------------------------------------------------------
def test_make_quick_reply_uri():
    """URIAction สำหรับ URL, LineMessageAction สำหรับ text"""
    qr = app._make_quick_reply([("กดยืนยัน", "http://localhost/dose/abc"), ("สถานะ", "สถานะ")])
    if qr is None:
        return  # SDK ไม่พร้อม — ข้าม
    assert len(qr.items) == 2


def test_make_quick_reply_max_13():
    """ตัด items เกิน 13 ออก"""
    items = [(str(i), str(i)) for i in range(20)]
    qr = app._make_quick_reply(items)
    if qr is None:
        return
    assert len(qr.items) <= 13


def test_flex_status_bubble_has_quick_reply(db_conn, seed_patient):
    """_flex_status_bubble ต้องแนบ quick_reply ถ้า FLEX_AVAILABLE"""
    if not app.FLEX_AVAILABLE:
        return
    pid = seed_patient
    pt_row = None
    with app.db() as conn:
        pt_row = conn.execute("SELECT * FROM patients WHERE patient_id=?", (pid,)).fetchone()
    msg = app._flex_status_bubble(pt_row)
    assert msg is not None
    assert msg.quick_reply is not None
    assert len(msg.quick_reply.items) >= 2


# ---------------------------------------------------------------------------
# Targeted broadcast preview
# ---------------------------------------------------------------------------
def test_broadcast_preview_all(admin_login, db_conn, seed_patient):
    r = admin_login.get("/api/broadcast-preview?target=all")
    assert r.status_code == 200
    j = r.json()
    assert "count" in j and isinstance(j["count"], int)


def test_broadcast_preview_low_adherence(admin_login, db_conn, seed_patient):
    r = admin_login.get("/api/broadcast-preview?target=low_adherence")
    assert r.status_code == 200
    j = r.json()
    assert j["target"] == "low_adherence"
    assert j["count"] == 0  # patient ไม่มี line_user_id → ไม่ถูกนับ


def test_broadcast_with_target_field(admin_login):
    """POST /line/broadcast รับ target field และคืน target ใน response"""
    r = admin_login.post(
        "/line/broadcast",
        data={"message": "test", "target": "low_adherence"},
    )
    assert r.status_code == 200
    j = r.json()
    assert j.get("target") == "low_adherence"


# ---------------------------------------------------------------------------
# Rich Menu image
# ---------------------------------------------------------------------------
def test_build_rich_menu_image_returns_png():
    """ภาพ Rich Menu ต้องเป็น PNG bytes"""
    try:
        from PIL import Image
    except ImportError:
        return  # Pillow ไม่มี — ข้าม
    data = app._build_rich_menu_image()
    assert data[:4] == b"\x89PNG", "output ต้องเป็น PNG"
    assert len(data) > 1000  # มีเนื้อหา
