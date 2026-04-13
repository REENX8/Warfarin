"""ระบบติดตามการกินยาวาร์ฟาริน — Sukhirin Padee Hospital, Narathiwat"""

import os, sqlite3, uuid, hashlib, hmac, base64, json, csv, io
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager, asynccontextmanager
from zoneinfo import ZoneInfo
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

# QR Code (optional — fail gracefully if unavailable)
try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False

# LINE Bot SDK v3
try:
    from linebot.v3.messaging import (
        Configuration, ApiClient, MessagingApi,
        ReplyMessageRequest, PushMessageRequest, BroadcastRequest,
        TextMessage,
    )
    from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent, UnfollowEvent
    from linebot.v3.webhook import WebhookHandler
    from linebot.v3.exceptions import InvalidSignatureError
    LINE_SDK_AVAILABLE = True
except ImportError:
    LINE_SDK_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "warfarin-tracker-secret-2024")
# ⚠️ ไม่มีการฝังค่าเริ่มต้นของ LINE channel secret ใน code — ต้องกำหนดผ่าน env
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
DB_PATH = os.getenv("DB_PATH", "./medtrack.db")

TZ = ZoneInfo("Asia/Bangkok")

# ---------------------------------------------------------------------------
# Scheduler (ประกาศก่อน — add_job ใน lifespan)
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler(timezone="Asia/Bangkok")

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if not scheduler.running:
        scheduler.add_job(job_send_reminders,       "cron", hour=18, minute=0,  id="remind_first",  replace_existing=True)
        scheduler.add_job(job_send_second_reminders,"cron", hour=19, minute=30, id="remind_second", replace_existing=True)
        scheduler.add_job(job_mark_missed,           "cron", hour=21, minute=0,  id="mark_missed",   replace_existing=True)
        scheduler.add_job(job_cleanup_sessions,      "cron", hour=3,  minute=0,  id="clean_sessions",replace_existing=True)
        scheduler.start()
    yield
    if scheduler.running:
        scheduler.shutdown(wait=False)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Warfarin Medication Tracker", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

os.makedirs("templates", exist_ok=True)
os.makedirs("static", exist_ok=True)
templates = Jinja2Templates(directory="templates")
templates.env.cache = None
templates.env.cache_size = 0
app.mount("/static", StaticFiles(directory="static"), name="static")

SESSIONS: dict[str, dict] = {}

# LINE setup
line_handler: Optional[object] = None
line_api: Optional[object] = None
if LINE_SDK_AVAILABLE and LINE_CHANNEL_SECRET:
    line_handler = WebhookHandler(LINE_CHANNEL_SECRET)
if LINE_SDK_AVAILABLE and LINE_CHANNEL_ACCESS_TOKEN:
    config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    api_client = ApiClient(config)
    line_api = MessagingApi(api_client)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

@contextmanager
def db():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def _now() -> str:
    """เวลาปัจจุบัน Asia/Bangkok ในรูปแบบ ISO string (ไม่มี tzinfo)"""
    return datetime.now(TZ).replace(tzinfo=None).isoformat()

def _today() -> str:
    """วันที่ปัจจุบัน Asia/Bangkok (YYYY-MM-DD)"""
    return datetime.now(TZ).strftime("%Y-%m-%d")

def _now_dt() -> datetime:
    """datetime ปัจจุบัน Asia/Bangkok (naive)"""
    return datetime.now(TZ).replace(tzinfo=None)

def _hash_pw(pw: str) -> str:
    return hashlib.sha256((pw + SECRET_KEY).encode()).hexdigest()

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS staff (
            staff_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
            full_name TEXT, role TEXT DEFAULT 'staff', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS patients (
            patient_id INTEGER PRIMARY KEY AUTOINCREMENT,
            hn TEXT UNIQUE, full_name TEXT NOT NULL, birth_date TEXT, age_years INTEGER,
            weight_kg REAL, phone TEXT, line_user_id TEXT,
            chronic_conditions TEXT, diagnosis TEXT,
            target_inr_min REAL DEFAULT 2.0, target_inr_max REAL DEFAULT 3.0,
            active INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS caregivers (
            caregiver_id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER REFERENCES patients(patient_id),
            name TEXT, phone TEXT, line_user_id TEXT,
            relationship TEXT, notify_enabled INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS medication_plan (
            dose_id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER REFERENCES patients(patient_id),
            scheduled_date TEXT, scheduled_time TEXT DEFAULT '18:00',
            warfarin_mg REAL, pill_description TEXT,
            status TEXT DEFAULT 'planned', confirmed_at TEXT, confirmed_by TEXT,
            late_minutes INTEGER DEFAULT 0, notes TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS dose_tokens (
            token_id TEXT PRIMARY KEY, dose_id INTEGER UNIQUE REFERENCES medication_plan(dose_id),
            created_at TEXT, expires_at TEXT, is_used INTEGER DEFAULT 0, used_at TEXT,
            reminder_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS lab_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER REFERENCES patients(patient_id),
            lab_name TEXT DEFAULT 'INR', value REAL, test_date TEXT,
            in_range INTEGER DEFAULT 0, notes TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS test_scores (
            score_id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER REFERENCES patients(patient_id),
            test_type TEXT DEFAULT 'pre', score REAL, max_score REAL DEFAULT 100, taken_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notification_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER REFERENCES patients(patient_id),
            dose_id INTEGER, channel TEXT DEFAULT 'line',
            message_type TEXT, message_text TEXT, sent_at TEXT, delivered INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT, entity_type TEXT, entity_id TEXT,
            performed_by TEXT, details TEXT, created_at TEXT
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
        """)
        # Migration — columns added in later versions
        migrations = [
            "ALTER TABLE dose_tokens ADD COLUMN reminder_count INTEGER DEFAULT 0",
            "ALTER TABLE medication_plan ADD COLUMN confirm_source TEXT DEFAULT 'patient'",
            "ALTER TABLE patients ADD COLUMN pill_inventory INTEGER DEFAULT 0",
            "ALTER TABLE patients ADD COLUMN registration_code TEXT",
        ]
        for m in migrations:
            try:
                conn.execute(m)
            except Exception:
                pass
        # สร้าง admin เริ่มต้นถ้ายังไม่มี staff
        row = conn.execute("SELECT COUNT(*) c FROM staff").fetchone()
        if row["c"] == 0:
            conn.execute(
                "INSERT INTO staff (username, password_hash, full_name, role, created_at) VALUES (?,?,?,?,?)",
                ("admin", _hash_pw("admin123"), "ผู้ดูแลระบบ", "admin", _now()),
            )

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
# login attempt counter: {ip: {"count": int, "window_start": datetime}}
_login_attempts: dict[str, dict] = {}

def get_current_user(request: Request) -> Optional[dict]:
    sid = request.cookies.get("session_id")
    if sid and sid in SESSIONS:
        return SESSIONS[sid]
    return None

def require_login(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user

def log_audit(conn, action, entity_type, entity_id, performed_by, details=""):
    conn.execute(
        "INSERT INTO audit_log (action,entity_type,entity_id,performed_by,details,created_at) VALUES(?,?,?,?,?,?)",
        (action, entity_type, str(entity_id), str(performed_by), details, _now()),
    )

# ---------------------------------------------------------------------------
# Computation helpers
# ---------------------------------------------------------------------------
def compute_adherence(conn, patient_id, days=7) -> dict:
    """นับเฉพาะโดสที่ผ่านไปแล้วหรือวันนี้ — ไม่รวมอนาคต
    ช่วง = (days-1) ย้อนหลัง ถึง วันนี้ (รวม = days วัน)"""
    today_str = _today()
    since = (_now_dt() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT status FROM medication_plan WHERE patient_id=? AND scheduled_date>=? AND scheduled_date<=?",
        (patient_id, since, today_str),
    ).fetchall()
    total = len(rows)
    taken = sum(1 for r in rows if r["status"] == "taken")
    missed = sum(1 for r in rows if r["status"] == "missed")
    late = sum(1 for r in rows if r["status"] == "late")
    pending = sum(1 for r in rows if r["status"] == "planned")
    pct = round(((taken + late) / total) * 100, 1) if total else 0
    return {"total": total, "taken": taken, "missed": missed, "late": late, "pending": pending, "percent": pct}

def compute_streak(conn, patient_id) -> int:
    """นับจำนวนวันติดต่อกันที่กินยา — group ตามวัน ข้ามวันนี้ถ้ายังมีสถานะ 'planned' เท่านั้น"""
    today = _today()
    rows = conn.execute(
        "SELECT scheduled_date, status FROM medication_plan WHERE patient_id=? "
        "ORDER BY scheduled_date DESC",
        (patient_id,),
    ).fetchall()
    # Group by date — วันหนึ่งจะถือว่า 'taken' ก็ต่อเมื่อทุกโดสในวันนั้นถูกยืนยัน (taken/late)
    by_day: dict[str, list[str]] = {}
    for r in rows:
        by_day.setdefault(r["scheduled_date"], []).append(r["status"])
    streak = 0
    for date in sorted(by_day.keys(), reverse=True):
        statuses = by_day[date]
        all_done = all(s in ("taken", "late") for s in statuses)
        all_pending_today = date == today and all(s == "planned" for s in statuses)
        if all_pending_today:
            continue
        if all_done:
            streak += 1
        else:
            break
    return streak

def compute_gamification_score(adherence_pct, streak):
    return round(adherence_pct + min(streak, 30) * 0.5, 1)

def compute_ttr(conn, patient_id) -> Optional[float]:
    """Time in Therapeutic Range (linear interpolation, Rosendaal method simplified).
    ถ้ามีผล INR < 2 ไม่สามารถคำนวณได้ จะคืน None"""
    rows = conn.execute(
        "SELECT test_date, value, in_range FROM lab_results "
        "WHERE patient_id=? AND value IS NOT NULL ORDER BY test_date",
        (patient_id,),
    ).fetchall()
    if len(rows) < 2:
        return None
    pt = conn.execute("SELECT target_inr_min, target_inr_max FROM patients WHERE patient_id=?", (patient_id,)).fetchone()
    if not pt:
        return None
    lo, hi = pt["target_inr_min"], pt["target_inr_max"]
    in_range_days = 0
    total_days = 0
    for i in range(len(rows) - 1):
        try:
            d1 = datetime.strptime(rows[i]["test_date"], "%Y-%m-%d")
            d2 = datetime.strptime(rows[i + 1]["test_date"], "%Y-%m-%d")
        except Exception:
            continue
        gap = (d2 - d1).days
        if gap <= 0:
            continue
        v1, v2 = rows[i]["value"], rows[i + 1]["value"]
        # Linear interpolation — day-by-day
        for step in range(gap):
            frac = step / gap
            v = v1 + (v2 - v1) * frac
            if lo <= v <= hi:
                in_range_days += 1
            total_days += 1
    if not total_days:
        return None
    return round(in_range_days / total_days * 100, 1)

def update_missed_doses(conn):
    """ทำเครื่องหมายโดสที่เลยเวลาแล้วเป็น missed"""
    conn.execute(
        "UPDATE medication_plan SET status='missed' WHERE status='planned' AND scheduled_date < ?",
        (_today(),),
    )

# ---------------------------------------------------------------------------
# LINE Push helpers (ไม่ error ถ้า LINE ใช้ไม่ได้)
# ---------------------------------------------------------------------------
def _push_line(user_id: str, text: str) -> bool:
    """Push ข้อความ LINE — คืน True ถ้าสำเร็จ"""
    if not line_api or not user_id:
        return False
    try:
        line_api.push_message(PushMessageRequest(
            to=user_id, messages=[TextMessage(text=text[:4900])]
        ))
        return True
    except Exception as e:
        print(f"[LINE push error] {user_id[:8]}... → {e}")
        return False

def _push_line_multi(user_id: str, texts: list[str]) -> bool:
    if not line_api or not user_id:
        return False
    try:
        msgs = [TextMessage(text=t[:4900]) for t in texts[:5]]
        line_api.push_message(PushMessageRequest(to=user_id, messages=msgs))
        return True
    except Exception as e:
        print(f"[LINE push-multi error] {e}")
        return False

def _log_notification(conn, patient_id, dose_id, msg_type, text, delivered):
    conn.execute(
        "INSERT INTO notification_log (patient_id,dose_id,channel,message_type,message_text,sent_at,delivered) "
        "VALUES(?,?,?,?,?,?,?)",
        (patient_id, dose_id, "line", msg_type, text[:500], _now(), 1 if delivered else 0),
    )

def send_line_reminder(patient: dict, reminder_num: int = 1) -> bool:
    with db() as conn:
        row = conn.execute(
            "SELECT dt.token_id, mp.warfarin_mg, mp.pill_description, mp.dose_id "
            "FROM medication_plan mp JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.patient_id=? AND mp.scheduled_date=? AND mp.status='planned' "
            "ORDER BY mp.scheduled_time LIMIT 1",
            (patient["patient_id"], _today()),
        ).fetchone()
    if row:
        url = f"{BASE_URL}/dose/{row['token_id']}"
        pill = row["pill_description"] or "ยาวาร์ฟาริน"
        if reminder_num == 1:
            msg = (
                f"💊 ถึงเวลากินยาแล้วค่ะ\n"
                f"คุณ{patient.get('full_name','')}\n"
                f"• ขนาด: {row['warfarin_mg']} mg\n"
                f"• ลักษณะ: {pill}\n\n"
                f"✅ กดลิงก์ยืนยันหลังกินยา:\n{url}\n\n"
                f"หากมีอาการผิดปกติ พิมพ์ 'อาการ' เพื่อรายงาน"
            )
        else:
            msg = (
                f"⏰ แจ้งเตือนซ้ำ (ครั้งที่ {reminder_num})\n"
                f"ยังไม่พบการยืนยันกินยา {row['warfarin_mg']} mg วันนี้\n"
                f"กรุณาอย่าลืมกินยาและกดยืนยัน:\n{url}"
            )
        delivered = _push_line(patient.get("line_user_id", ""), msg)
    else:
        msg = "ถึงเวลากินยาวาร์ฟารินแล้วค่ะ กรุณายืนยันการกินยา"
        delivered = _push_line(patient.get("line_user_id", ""), msg)
    return delivered

def send_line_confirmation(patient: dict, dose: dict, streak: int):
    """ส่งข้อความยืนยันการกินยา — รับ streak มาจากภายนอกเพื่อไม่เปิด conn ซ้อน"""
    if streak >= 30:
        trophy = "🏆"
    elif streak >= 14:
        trophy = "🥇"
    elif streak >= 7:
        trophy = "🥈"
    else:
        trophy = "🎯"
    msg = (
        f"บันทึกการกินยาเรียบร้อย! ✅\n"
        f"ยาวาร์ฟาริน {dose.get('warfarin_mg','')} mg\n"
        f"{trophy} ต่อเนื่อง {streak} วัน\n\n"
        f"รักษาความสม่ำเสมอไว้นะคะ 💪"
    )
    _push_line(patient.get("line_user_id", ""), msg)

def send_line_missed_alert(patient: dict):
    msg = (
        f"⚠️ คุณ{patient['full_name']}\n"
        f"ยังไม่พบการยืนยันกินยาวาร์ฟารินวันนี้\n"
        f"หากลืม กรุณาติดต่อเภสัชกรก่อนกินเพิ่ม\n\n"
        f"📞 รพ.สุไหงปาดี"
    )
    _push_line(patient.get("line_user_id", ""), msg)
    with db() as conn:
        cgs = conn.execute(
            "SELECT name, line_user_id FROM caregivers "
            "WHERE patient_id=? AND notify_enabled=1 AND line_user_id IS NOT NULL AND line_user_id!=''",
            (patient["patient_id"],),
        ).fetchall()
    for cg in cgs:
        _push_line(
            cg["line_user_id"],
            f"⚠️ แจ้งผู้ดูแล\nผู้ป่วย {patient['full_name']} ยังไม่ได้กินยาวาร์ฟารินวันนี้\nกรุณาช่วยเตือนด้วยค่ะ",
        )

# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------
def job_send_reminders():
    """18:00 — เตือนยาวันนี้ครั้งแรก"""
    with db() as conn:
        patients = conn.execute(
            "SELECT DISTINCT p.* FROM patients p JOIN medication_plan mp ON p.patient_id=mp.patient_id "
            "WHERE mp.scheduled_date=? AND mp.status='planned' AND p.active=1 "
            "AND p.line_user_id IS NOT NULL AND p.line_user_id!=''",
            (_today(),)
        ).fetchall()
    for p in patients:
        delivered = send_line_reminder(dict(p), reminder_num=1)
        with db() as conn:
            conn.execute(
                "UPDATE dose_tokens SET reminder_count=reminder_count+1 "
                "WHERE dose_id IN (SELECT dose_id FROM medication_plan "
                "WHERE patient_id=? AND scheduled_date=? AND status='planned')",
                (p["patient_id"], _today()),
            )
            _log_notification(conn, p["patient_id"], None, "reminder_1",
                              "ส่งเตือนกินยาครั้งที่ 1", delivered)

def job_send_second_reminders():
    """19:30 — เตือนซ้ำสำหรับผู้ที่ยังไม่ยืนยัน"""
    with db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT p.* FROM patients p "
            "JOIN medication_plan mp ON p.patient_id=mp.patient_id "
            "JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.scheduled_date=? AND mp.status='planned' AND p.active=1 "
            "AND p.line_user_id IS NOT NULL AND p.line_user_id!='' "
            "AND dt.reminder_count < 2",
            (_today(),)
        ).fetchall()
    for p in rows:
        delivered = send_line_reminder(dict(p), reminder_num=2)
        with db() as conn:
            conn.execute(
                "UPDATE dose_tokens SET reminder_count=reminder_count+1 "
                "WHERE dose_id IN (SELECT dose_id FROM medication_plan "
                "WHERE patient_id=? AND scheduled_date=? AND status='planned')",
                (p["patient_id"], _today()),
            )
            _log_notification(conn, p["patient_id"], None, "reminder_2",
                              "ส่งเตือนกินยาครั้งที่ 2", delivered)

def job_mark_missed():
    """21:00 — mark missed ก่อน แล้วค่อยแจ้ง LINE"""
    with db() as conn:
        pending_rows = conn.execute(
            "SELECT mp.dose_id, p.* FROM medication_plan mp "
            "JOIN patients p ON mp.patient_id=p.patient_id "
            "WHERE mp.scheduled_date=? AND mp.status='planned' AND p.active=1",
            (_today(),)
        ).fetchall()
        update_missed_doses(conn)
    for row in pending_rows:
        send_line_missed_alert(dict(row))
        with db() as conn:
            _log_notification(conn, row["patient_id"], row["dose_id"], "missed",
                              "แจ้งเตือนลืมกินยา", True)

def job_cleanup_sessions():
    """03:00 — ลบ session ที่เก่ากว่า 24 ชม. + ล้าง login attempts"""
    global SESSIONS, _login_attempts
    cutoff = _now_dt() - timedelta(hours=24)
    expired = [sid for sid, s in SESSIONS.items() if s.get("created_at", _now_dt()) < cutoff]
    for sid in expired:
        SESSIONS.pop(sid, None)
    # clean login attempts เก่ากว่า 1 ชม.
    attempt_cutoff = _now_dt() - timedelta(hours=1)
    stale = [ip for ip, a in _login_attempts.items() if a.get("window_start", _now_dt()) < attempt_cutoff]
    for ip in stale:
        _login_attempts.pop(ip, None)

# ---------------------------------------------------------------------------
# Routes: Auth
# ---------------------------------------------------------------------------
@app.get("/")
def root(request: Request):
    user = get_current_user(request)
    return RedirectResponse("/dashboard" if user else "/login", status_code=303)

@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {
        "error": "", "current_year": datetime.now(TZ).year,
    })

@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = request.client.host if request.client else "unknown"
    now = _now_dt()
    # Rate limiting: 5 ครั้ง / 15 นาที
    attempt = _login_attempts.get(ip, {"count": 0, "window_start": now})
    if (now - attempt["window_start"]).total_seconds() > 900:
        attempt = {"count": 0, "window_start": now}
    if attempt["count"] >= 5:
        return templates.TemplateResponse(request, "login.html", {
            "error": "พยายาม login มากเกินไป กรุณารอ 15 นาที",
            "current_year": now.year,
        })

    with db() as conn:
        staff = conn.execute("SELECT * FROM staff WHERE username=?", (username,)).fetchone()
    if not staff or staff["password_hash"] != _hash_pw(password):
        attempt["count"] += 1
        _login_attempts[ip] = attempt
        return templates.TemplateResponse(request, "login.html", {
            "error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง",
            "current_year": now.year,
        })

    _login_attempts.pop(ip, None)
    sid = str(uuid.uuid4())
    SESSIONS[sid] = {
        "staff_id": staff["staff_id"], "username": staff["username"],
        "full_name": staff["full_name"], "role": staff["role"],
        "created_at": _now_dt(),
    }
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie("session_id", sid, httponly=True, samesite="lax")
    return resp

@app.get("/logout")
def logout(request: Request):
    sid = request.cookies.get("session_id")
    if sid and sid in SESSIONS:
        del SESSIONS[sid]
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session_id")
    return resp

# ---------------------------------------------------------------------------
# Routes: Dashboard
# ---------------------------------------------------------------------------
@app.get("/dashboard")
def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        total_patients = conn.execute("SELECT COUNT(*) c FROM patients").fetchone()["c"]
        active_patients = conn.execute("SELECT COUNT(*) c FROM patients WHERE active=1").fetchone()["c"]
        today = _today()
        today_taken = conn.execute("SELECT COUNT(*) c FROM medication_plan WHERE scheduled_date=? AND status IN ('taken','late')", (today,)).fetchone()["c"]
        today_missed = conn.execute("SELECT COUNT(*) c FROM medication_plan WHERE scheduled_date=? AND status='missed'", (today,)).fetchone()["c"]
        today_pending = conn.execute("SELECT COUNT(*) c FROM medication_plan WHERE scheduled_date=? AND status='planned'", (today,)).fetchone()["c"]
        pats = conn.execute("SELECT patient_id FROM patients WHERE active=1").fetchall()
        adh_list = [compute_adherence(conn, p["patient_id"], 7)["percent"] for p in pats]
        adherence_avg = round(sum(adh_list) / len(adh_list), 1) if adh_list else 0
        # ผู้ป่วยเสี่ยง (LIMIT 20)
        at_risk = []
        for p in pats:
            a = compute_adherence(conn, p["patient_id"], 7)
            if a["percent"] < 70 or a["missed"] >= 3:
                pt = conn.execute("SELECT * FROM patients WHERE patient_id=?", (p["patient_id"],)).fetchone()
                at_risk.append({"patient": dict(pt), "adherence": a})
                if len(at_risk) >= 20:
                    break
        recent = conn.execute(
            "SELECT mp.*, p.full_name, p.hn FROM medication_plan mp "
            "JOIN patients p ON mp.patient_id=p.patient_id "
            "WHERE mp.status IN ('taken','late') AND mp.confirmed_at IS NOT NULL "
            "ORDER BY mp.confirmed_at DESC LIMIT 10"
        ).fetchall()
        # Pending symptom reports (ระดับ ≥3)
        urgent_symptoms = conn.execute(
            "SELECT sr.*, p.full_name FROM symptom_reports sr "
            "JOIN patients p ON sr.patient_id=p.patient_id "
            "WHERE sr.severity >= 3 AND sr.created_at >= ? "
            "ORDER BY sr.created_at DESC LIMIT 5",
            ((_now_dt() - timedelta(days=7)).isoformat(),),
        ).fetchall()
        # จำนวนผู้ป่วยที่เชื่อม LINE แล้ว
        line_linked = conn.execute(
            "SELECT COUNT(*) c FROM patients WHERE active=1 AND line_user_id IS NOT NULL AND line_user_id!=''"
        ).fetchone()["c"]
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "total_patients": total_patients,
        "active_patients": active_patients, "today_taken": today_taken,
        "today_missed": today_missed, "today_pending": today_pending,
        "adherence_avg": adherence_avg, "at_risk_patients": at_risk,
        "recent_activity": [dict(r) for r in recent],
        "urgent_symptoms": [dict(s) for s in urgent_symptoms],
        "line_linked": line_linked,
        "line_configured": bool(line_api),
    })

# ---------------------------------------------------------------------------
# Routes: Patient management
# ---------------------------------------------------------------------------
@app.get("/patients")
def patients_list(request: Request, q: str = ""):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        if q:
            rows = conn.execute(
                "SELECT * FROM patients WHERE full_name LIKE ? OR hn LIKE ? ORDER BY full_name",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM patients ORDER BY full_name").fetchall()
    # แปลง Row → dict ก่อนส่งไป template
    patients = [dict(p) for p in rows]
    return templates.TemplateResponse(request, "patients.html", {
        "user": user, "patients": patients, "q": q
    })

@app.get("/patients/new")
def patient_new_form(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "patient_form.html", {
        "user": user, "patient": None, "caregivers": []
    })

@app.post("/patients/new")
async def patient_create(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    with db() as conn:
        conn.execute(
            "INSERT INTO patients (hn,full_name,birth_date,age_years,weight_kg,phone,line_user_id,"
            "chronic_conditions,diagnosis,target_inr_min,target_inr_max,active,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (form.get("hn"), form["full_name"], form.get("birth_date"), form.get("age_years"),
             form.get("weight_kg"), form.get("phone"), form.get("line_user_id"),
             form.get("chronic_conditions"), form.get("diagnosis"),
             float(form.get("target_inr_min") or 2.0), float(form.get("target_inr_max") or 3.0),
             _now(), _now()),
        )
        pid = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
        cg_name = form.get("caregiver_name")
        if cg_name:
            conn.execute(
                "INSERT INTO caregivers (patient_id,name,phone,line_user_id,relationship) VALUES(?,?,?,?,?)",
                (pid, cg_name, form.get("caregiver_phone"), form.get("caregiver_line"), form.get("caregiver_relationship")),
            )
        log_audit(conn, "create", "patient", pid, user["username"], f"สร้างผู้ป่วย {form['full_name']}")
    return RedirectResponse(f"/patients/{pid}", status_code=303)

@app.get("/patients/{pid}")
def patient_detail(request: Request, pid: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        patient = conn.execute("SELECT * FROM patients WHERE patient_id=?", (pid,)).fetchone()
        if not patient:
            raise HTTPException(404, "ไม่พบผู้ป่วย")
        caregivers = conn.execute("SELECT * FROM caregivers WHERE patient_id=?", (pid,)).fetchall()
        doses = conn.execute(
            "SELECT mp.*, dt.token_id FROM medication_plan mp "
            "LEFT JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.patient_id=? ORDER BY mp.scheduled_date DESC, mp.scheduled_time DESC LIMIT 60",
            (pid,),
        ).fetchall()
        labs = conn.execute("SELECT * FROM lab_results WHERE patient_id=? ORDER BY test_date DESC", (pid,)).fetchall()
        scores = conn.execute("SELECT * FROM test_scores WHERE patient_id=? ORDER BY taken_at DESC", (pid,)).fetchall()
        adh7 = compute_adherence(conn, pid, 7)
        adh30 = compute_adherence(conn, pid, 30)
        adh_all = compute_adherence(conn, pid, 365)
        streak = compute_streak(conn, pid)
        gami = compute_gamification_score(adh7["percent"], streak)
        ttr = compute_ttr(conn, pid)
        surveys = conn.execute(
            "SELECT * FROM satisfaction_surveys WHERE patient_id=? ORDER BY survey_date DESC LIMIT 5", (pid,)
        ).fetchall()
        symptoms = conn.execute(
            "SELECT * FROM symptom_reports WHERE patient_id=? ORDER BY created_at DESC LIMIT 10", (pid,)
        ).fetchall()
        # โดสวันนี้ที่รอกิน (สำหรับ QR code ด้านบน)
        today_dose = conn.execute(
            "SELECT mp.*, dt.token_id FROM medication_plan mp "
            "LEFT JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.patient_id=? AND mp.scheduled_date=? "
            "ORDER BY mp.scheduled_time LIMIT 1",
            (pid, _today()),
        ).fetchone()
    return templates.TemplateResponse(request, "patient_detail.html", {
        "user": user, "patient": dict(patient),
        "caregivers": [dict(c) for c in caregivers], "doses": [dict(d) for d in doses],
        "labs": [dict(l) for l in labs], "scores": [dict(s) for s in scores],
        "adh7": adh7, "adh30": adh30, "adh_all": adh_all,
        "streak": streak, "gamification_score": gami, "ttr": ttr,
        "surveys": [dict(s) for s in surveys],
        "symptoms": [dict(s) for s in symptoms],
        "today_dose": dict(today_dose) if today_dose else None,
        "base_url": BASE_URL,
    })

@app.get("/patients/{pid}/edit")
def patient_edit_form(request: Request, pid: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        patient = conn.execute("SELECT * FROM patients WHERE patient_id=?", (pid,)).fetchone()
        caregivers = conn.execute("SELECT * FROM caregivers WHERE patient_id=?", (pid,)).fetchall()
    if not patient:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "patient_form.html", {
        "user": user, "patient": dict(patient), "caregivers": [dict(c) for c in caregivers],
    })

@app.post("/patients/{pid}/delete")
def patient_delete(request: Request, pid: int):
    """Soft delete — set active=0"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        conn.execute("UPDATE patients SET active=0, updated_at=? WHERE patient_id=?", (_now(), pid))
        log_audit(conn, "deactivate", "patient", pid, user["username"], "soft delete")
    return RedirectResponse("/patients", status_code=303)

@app.post("/patients/{pid}/reactivate")
def patient_reactivate(request: Request, pid: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        conn.execute("UPDATE patients SET active=1, updated_at=? WHERE patient_id=?", (_now(), pid))
        log_audit(conn, "reactivate", "patient", pid, user["username"], "")
    return RedirectResponse(f"/patients/{pid}", status_code=303)

@app.post("/doses/{dose_id}/override")
async def dose_override(request: Request, dose_id: int):
    """Staff manual override — เปลี่ยนสถานะโดสด้วยมือ"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    status = form.get("status", "taken")
    if status not in ("taken", "late", "missed", "planned"):
        raise HTTPException(400, "invalid status")
    with db() as conn:
        row = conn.execute("SELECT patient_id FROM medication_plan WHERE dose_id=?", (dose_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        if status in ("taken", "late"):
            conn.execute(
                "UPDATE medication_plan SET status=?,confirmed_at=?,confirmed_by=?,confirm_source='staff' "
                "WHERE dose_id=?",
                (status, _now(), user["username"], dose_id),
            )
            conn.execute("UPDATE dose_tokens SET is_used=1, used_at=? WHERE dose_id=?", (_now(), dose_id))
        else:
            conn.execute(
                "UPDATE medication_plan SET status=?, confirmed_at=NULL, confirmed_by=NULL WHERE dose_id=?",
                (status, dose_id),
            )
            conn.execute("UPDATE dose_tokens SET is_used=0, used_at=NULL WHERE dose_id=?", (dose_id,))
        log_audit(conn, "override_dose", "medication_plan", dose_id, user["username"], f"→ {status}")
        pid = row["patient_id"]
    return RedirectResponse(f"/patients/{pid}", status_code=303)

@app.post("/patients/{pid}/edit")
async def patient_update(request: Request, pid: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    with db() as conn:
        conn.execute(
            "UPDATE patients SET hn=?,full_name=?,birth_date=?,age_years=?,weight_kg=?,phone=?,line_user_id=?,"
            "chronic_conditions=?,diagnosis=?,target_inr_min=?,target_inr_max=?,active=?,updated_at=? WHERE patient_id=?",
            (form.get("hn"), form["full_name"], form.get("birth_date"), form.get("age_years"),
             form.get("weight_kg"), form.get("phone"), form.get("line_user_id"),
             form.get("chronic_conditions"), form.get("diagnosis"),
             float(form.get("target_inr_min") or 2.0), float(form.get("target_inr_max") or 3.0),
             int(form.get("active", 1)), _now(), pid),
        )
        # อัปเดต caregiver ด้วย
        cg_name = form.get("caregiver_name")
        if cg_name:
            existing_cg = conn.execute("SELECT caregiver_id FROM caregivers WHERE patient_id=? LIMIT 1", (pid,)).fetchone()
            if existing_cg:
                conn.execute(
                    "UPDATE caregivers SET name=?,phone=?,line_user_id=?,relationship=? WHERE patient_id=?",
                    (cg_name, form.get("caregiver_phone"), form.get("caregiver_line"),
                     form.get("caregiver_relationship"), pid),
                )
            else:
                conn.execute(
                    "INSERT INTO caregivers (patient_id,name,phone,line_user_id,relationship) VALUES(?,?,?,?,?)",
                    (pid, cg_name, form.get("caregiver_phone"), form.get("caregiver_line"),
                     form.get("caregiver_relationship")),
                )
        log_audit(conn, "update", "patient", pid, user["username"], "แก้ไขข้อมูลผู้ป่วย")
    return RedirectResponse(f"/patients/{pid}", status_code=303)

@app.post("/patients/{pid}/doses")
async def create_doses(request: Request, pid: int):
    """สร้างแผนยาแบบ bulk — รองรับขนาดยาต่างกันตามวัน"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    start = form["start_date"]
    end = form["end_date"]
    time_str = form.get("scheduled_time", "18:00")
    default_mg = float(form.get("warfarin_mg", 2))
    pill_desc = form.get("pill_description", "")
    day_doses = {}
    for i in range(7):
        val = form.get(f"dose_day_{i}")
        day_doses[i] = float(val) if val else default_mg
    start_dt = datetime.strptime(start, "%Y-%m-%d")
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    count = 0
    with db() as conn:
        d = start_dt
        while d <= end_dt:
            ds = d.strftime("%Y-%m-%d")
            mg = day_doses.get(d.weekday(), default_mg)
            conn.execute(
                "INSERT INTO medication_plan (patient_id,scheduled_date,scheduled_time,warfarin_mg,pill_description,status,created_at) "
                "VALUES(?,?,?,?,?,'planned',?)", (pid, ds, time_str, mg, pill_desc, _now()),
            )
            did = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
            token = str(uuid.uuid4())
            # token หมดอายุตอนสิ้นวันนั้น (23:59:59) + 1 วัน buffer
            expires = (d + timedelta(days=1)).replace(hour=23, minute=59, second=59).isoformat()
            conn.execute(
                "INSERT INTO dose_tokens (token_id,dose_id,created_at,expires_at,reminder_count) VALUES(?,?,?,?,0)",
                (token, did, _now(), expires),
            )
            count += 1
            d += timedelta(days=1)
        log_audit(conn, "create_doses", "medication_plan", pid, user["username"], f"สร้างแผนยา {count} โดส")
    return RedirectResponse(f"/patients/{pid}", status_code=303)

@app.post("/patients/{pid}/lab")
async def add_lab(request: Request, pid: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    value = float(form["value"])
    with db() as conn:
        pt = conn.execute("SELECT target_inr_min, target_inr_max FROM patients WHERE patient_id=?", (pid,)).fetchone()
        in_range = 1 if pt and pt["target_inr_min"] <= value <= pt["target_inr_max"] else 0
        conn.execute(
            "INSERT INTO lab_results (patient_id,lab_name,value,test_date,in_range,notes,created_at) VALUES(?,?,?,?,?,?,?)",
            (pid, form.get("lab_name", "INR"), value, form.get("test_date", _today()), in_range, form.get("notes", ""), _now()),
        )
        log_audit(conn, "add_lab", "lab_results", pid, user["username"], f"INR={value} in_range={in_range}")
    return RedirectResponse(f"/patients/{pid}", status_code=303)

@app.post("/patients/{pid}/test-score")
async def add_test_score(request: Request, pid: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    with db() as conn:
        conn.execute(
            "INSERT INTO test_scores (patient_id,test_type,score,max_score,taken_at) VALUES(?,?,?,?,?)",
            (pid, form.get("test_type", "pre"), float(form["score"]), float(form.get("max_score", 100)), _now()),
        )
        log_audit(conn, "add_score", "test_scores", pid, user["username"], f"test={form.get('test_type')} score={form['score']}")
    return RedirectResponse(f"/patients/{pid}", status_code=303)

# ---------------------------------------------------------------------------
# Routes: Satisfaction Survey
# ---------------------------------------------------------------------------
@app.get("/patients/{pid}/survey")
def survey_form(request: Request, pid: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        patient = conn.execute("SELECT * FROM patients WHERE patient_id=?", (pid,)).fetchone()
        if not patient:
            raise HTTPException(404, "ไม่พบผู้ป่วย")
    return templates.TemplateResponse("survey_form.html", {
        "request": request, "user": user, "patient": dict(patient),
        "today": _today(),
    })

@app.post("/patients/{pid}/survey")
async def survey_submit(request: Request, pid: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    with db() as conn:
        conn.execute(
            "INSERT INTO satisfaction_surveys (patient_id,survey_date,ease_of_use,line_satisfaction,reminder_helpful,comments,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (pid, form.get("survey_date", _today()),
             int(form.get("ease_of_use", 3)),
             int(form.get("line_satisfaction", 3)),
             int(form.get("reminder_helpful", 3)),
             form.get("comments", ""), _now()),
        )
        log_audit(conn, "add_survey", "satisfaction_surveys", pid, user["username"], "บันทึกแบบสอบถาม")
    return RedirectResponse(f"/patients/{pid}", status_code=303)

# ---------------------------------------------------------------------------
# Routes: Dose confirmation (ผู้ป่วยใช้ — ไม่ต้อง login)
# ---------------------------------------------------------------------------
def _lookup_token(conn, token_id: str):
    return conn.execute(
        "SELECT dt.token_id, dt.dose_id, dt.is_used, dt.expires_at, dt.reminder_count, "
        "mp.scheduled_date, mp.scheduled_time, mp.warfarin_mg, mp.pill_description, mp.status, "
        "p.patient_id AS pid, p.full_name, p.hn, p.line_user_id "
        "FROM dose_tokens dt "
        "JOIN medication_plan mp ON dt.dose_id=mp.dose_id "
        "JOIN patients p ON mp.patient_id=p.patient_id WHERE dt.token_id=?",
        (token_id,),
    ).fetchone()

@app.get("/dose/{token_id}")
def dose_confirm_page(request: Request, token_id: str):
    with db() as conn:
        tok = _lookup_token(conn, token_id)
        if not tok:
            raise HTTPException(404, "ไม่พบข้อมูลยา หรือลิงก์ไม่ถูกต้อง")
        adh = compute_adherence(conn, tok["pid"], 7)
        streak = compute_streak(conn, tok["pid"])
    already = tok["is_used"] == 1
    # ตรวจสอบการหมดอายุ
    expired = False
    if tok["expires_at"]:
        try:
            exp = datetime.fromisoformat(tok["expires_at"])
            expired = _now_dt() > exp
        except Exception:
            pass
    if adh["percent"] >= 90:
        adh_msg = "ยอดเยี่ยมมาก! คุณกินยาได้สม่ำเสมอมากค่ะ 🌟"
    elif adh["percent"] >= 70:
        adh_msg = "ดีมากค่ะ พยายามกินยาให้สม่ำเสมอต่อไปนะคะ 💪"
    else:
        adh_msg = "อย่าลืมกินยาทุกวันนะคะ สุขภาพสำคัญค่ะ ❤️"
    return templates.TemplateResponse(request, "dose_confirm.html", {
        "token": dict(tok), "already": already, "expired": expired,
        "adherence": adh, "streak": streak, "adh_msg": adh_msg,
        "base_url": BASE_URL,
    })

@app.post("/dose/{token_id}/confirm")
async def dose_confirm(request: Request, token_id: str):
    form = await request.form()
    confirm_source = form.get("confirm_source", "patient")
    if confirm_source not in ("patient", "caregiver", "staff"):
        confirm_source = "patient"
    with db() as conn:
        tok = _lookup_token(conn, token_id)
        if not tok:
            raise HTTPException(404, "ไม่พบข้อมูล")
        if tok["is_used"] == 1:
            return templates.TemplateResponse(request, "dose_result.html", {
                "success": False,
                "message": "โดสนี้ถูกยืนยันไปแล้วก่อนหน้า — ไม่สามารถยืนยันซ้ำได้",
                "dose": dict(tok),
            })
        # ตรวจการหมดอายุ
        if tok["expires_at"]:
            try:
                exp = datetime.fromisoformat(tok["expires_at"])
                if _now_dt() > exp:
                    return templates.TemplateResponse(request, "dose_result.html", {
                        "success": False,
                        "message": "ลิงก์ยืนยันนี้หมดอายุแล้ว กรุณาติดต่อเจ้าหน้าที่",
                        "dose": dict(tok),
                    })
            except Exception:
                pass
        now = _now_dt()
        try:
            sched = datetime.strptime(f"{tok['scheduled_date']} {tok['scheduled_time']}", "%Y-%m-%d %H:%M")
        except Exception:
            sched = now
        diff = (now - sched).total_seconds() / 60
        late = max(0, int(diff))
        status = "late" if late > 120 else "taken"
        conn.execute(
            "UPDATE medication_plan SET status=?,confirmed_at=?,confirmed_by=?,confirm_source=?,late_minutes=? WHERE dose_id=?",
            (status, now.isoformat(), confirm_source, confirm_source, late, tok["dose_id"]),
        )
        conn.execute(
            "UPDATE dose_tokens SET is_used=1,used_at=? WHERE token_id=?",
            (now.isoformat(), token_id),
        )
        # ลดจำนวนยาคงเหลือ
        conn.execute(
            "UPDATE patients SET pill_inventory=MAX(pill_inventory-1, 0) WHERE patient_id=? AND pill_inventory>0",
            (tok["pid"],),
        )
        streak = compute_streak(conn, tok["pid"])
        log_audit(conn, "confirm_dose", "medication_plan", tok["dose_id"], confirm_source,
                  f"status={status} late={late}m source={confirm_source}")
    # ส่ง LINE หลังจากปิด connection
    send_line_confirmation(
        {"patient_id": tok["pid"], "full_name": tok["full_name"], "line_user_id": tok["line_user_id"]},
        {"warfarin_mg": tok["warfarin_mg"]},
        streak,
    )
    return templates.TemplateResponse(request, "dose_result.html", {
        "success": True, "dose": dict(tok), "streak": streak, "status": status,
        "message": f"บันทึกเรียบร้อย! ต่อเนื่อง {streak} วัน"
                   + (" (กินยาช้ากว่ากำหนด)" if status == "late" else ""),
    })

# ---------------------------------------------------------------------------
# Routes: Reports
# ---------------------------------------------------------------------------
@app.get("/reports")
def reports_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        patients = conn.execute("SELECT * FROM patients WHERE active=1 ORDER BY full_name").fetchall()
        data = []
        for p in patients:
            a7 = compute_adherence(conn, p["patient_id"], 7)
            a30 = compute_adherence(conn, p["patient_id"], 30)
            streak = compute_streak(conn, p["patient_id"])
            ttr = compute_ttr(conn, p["patient_id"])
            last_inr = conn.execute(
                "SELECT value, test_date FROM lab_results WHERE patient_id=? ORDER BY test_date DESC LIMIT 1",
                (p["patient_id"],),
            ).fetchone()
            data.append({
                "patient": dict(p), "adh7": a7, "adh30": a30, "streak": streak,
                "ttr": ttr, "last_inr": dict(last_inr) if last_inr else None,
            })
        survey_summary = conn.execute(
            "SELECT AVG(ease_of_use) ease, AVG(line_satisfaction) line_sat, "
            "AVG(reminder_helpful) remind, COUNT(*) total FROM satisfaction_surveys"
        ).fetchone()
    return templates.TemplateResponse(request, "reports.html", {
        "user": user, "report_data": data,
        "survey_summary": dict(survey_summary) if survey_summary else None,
    })

@app.get("/reports/export")
def reports_export(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "patient_id", "hn", "full_name", "period", "total_doses", "taken", "missed",
        "late", "adherence_%", "streak_days", "avg_inr", "inr_in_range_%", "ttr_%",
    ])
    with db() as conn:
        patients = conn.execute("SELECT * FROM patients WHERE active=1").fetchall()
        for p in patients:
            a = compute_adherence(conn, p["patient_id"], 30)
            streak = compute_streak(conn, p["patient_id"])
            ttr = compute_ttr(conn, p["patient_id"])
            inrs = conn.execute("SELECT value, in_range FROM lab_results WHERE patient_id=?", (p["patient_id"],)).fetchall()
            avg_inr = round(sum(r["value"] for r in inrs) / len(inrs), 2) if inrs else 0
            inr_pct = round(sum(r["in_range"] for r in inrs) / len(inrs) * 100, 1) if inrs else 0
            writer.writerow([
                p["patient_id"], p["hn"], p["full_name"], "30d",
                a["total"], a["taken"], a["missed"], a["late"], a["percent"],
                streak, avg_inr, inr_pct, ttr if ttr is not None else "",
            ])
    output.seek(0)
    filename = f"warfarin_report_{_today()}.csv"
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

# ---------------------------------------------------------------------------
# LINE Webhook
# ---------------------------------------------------------------------------
def _verify_line_signature(body: bytes, signature: str) -> bool:
    """Verify LINE signature manually — safer than relying on SDK only"""
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    mac = hmac.new(LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)

@app.post("/webhook")
async def line_webhook(request: Request):
    """LINE webhook — ตอบ 200 ทุกกรณีเพื่อไม่ให้ LINE retry เยอะเกินไป
    ยกเว้นกรณี signature ไม่ถูกต้องจริง ๆ จึงตอบ 401"""
    body = await request.body()
    # empty body = verify request จาก LINE Developers Console
    if not body:
        return JSONResponse({"status": "ok"})
    sig = request.headers.get("X-Line-Signature", "")
    if not line_handler or not LINE_CHANNEL_SECRET:
        return JSONResponse({"status": "LINE not configured"})
    # Verify first
    if not _verify_line_signature(body, sig):
        return JSONResponse({"status": "invalid signature"}, status_code=401)
    try:
        line_handler.handle(body.decode("utf-8"), sig)
    except Exception as e:
        print(f"[LINE webhook error] {e}")
        # ยังตอบ 200 เพื่อไม่ให้ retry
    return JSONResponse({"status": "ok"})

if LINE_SDK_AVAILABLE and line_handler:
    @line_handler.add(FollowEvent)
    def handle_follow(event):
        uid = event.source.user_id
        # ถ้ามี registration_code ที่ match ให้ auto-link
        with db() as conn:
            log_audit(conn, "line_follow", "line", uid, "system", "")
        msg = (
            "สวัสดีค่ะ! ยินดีต้อนรับสู่ระบบติดตามยาวาร์ฟาริน 💊\n"
            "รพ.สุไหงปาดี\n\n"
            "📝 ขั้นตอนลงทะเบียน:\n"
            "1. แจ้ง LINE User ID กับเภสัชกร หรือ\n"
            "2. พิมพ์ 'ลงทะเบียน <HN>' เช่น ลงทะเบียน 12345\n\n"
            "คำสั่งที่ใช้ได้หลังลงทะเบียน:\n"
            "• 'สถานะ' — ดูสถานะยาวันนี้\n"
            "• 'ยา' — ดูรายละเอียดยา + ลิงก์ยืนยัน\n"
            "• 'adherence' — ดูความสม่ำเสมอ\n"
            "• 'inr' — ดูผล INR ล่าสุด\n"
            "• 'streak' — ดูจำนวนวันต่อเนื่อง\n"
            "• 'อาการ' — รายงานอาการไม่พึงประสงค์\n"
            "• 'help' — ดูเมนูช่วยเหลือ"
        )
        if line_api:
            try:
                line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token, messages=[TextMessage(text=msg)]
                ))
            except Exception:
                pass

    @line_handler.add(MessageEvent, message=TextMessageContent)
    def handle_message(event):
        uid = event.source.user_id
        raw = event.message.text.strip()
        text = raw.lower()
        reply = _route_line_command(uid, raw, text)
        if line_api:
            try:
                line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token, messages=[TextMessage(text=reply[:4900])]
                ))
            except Exception:
                pass

    @line_handler.add(UnfollowEvent)
    def handle_unfollow(event):
        with db() as conn:
            log_audit(conn, "line_unfollow", "line", event.source.user_id, "system", "")

def _route_line_command(uid: str, raw: str, text: str) -> str:
    """Route LINE commands. `raw` คือข้อความต้นฉบับ, `text` คือ lowercased"""
    # Registration (allow without existing link)
    if raw.startswith("ลงทะเบียน") or text.startswith("register"):
        parts = raw.replace("ลงทะเบียน", "").replace("register", "").strip().split()
        if not parts:
            return "กรุณาพิมพ์: ลงทะเบียน <HN>\nตัวอย่าง: ลงทะเบียน 12345"
        hn = parts[0]
        with db() as conn:
            pt = conn.execute("SELECT * FROM patients WHERE hn=? AND active=1", (hn,)).fetchone()
            if not pt:
                return f"ไม่พบผู้ป่วย HN: {hn}\nกรุณาติดต่อเภสัชกร"
            if pt["line_user_id"] and pt["line_user_id"] != uid:
                return "HN นี้ถูกลงทะเบียนไปแล้ว หากเป็นของคุณกรุณาติดต่อเภสัชกร"
            conn.execute("UPDATE patients SET line_user_id=?, updated_at=? WHERE patient_id=?",
                         (uid, _now(), pt["patient_id"]))
            log_audit(conn, "line_register", "patient", pt["patient_id"], uid, f"linked HN={hn}")
        return (
            f"✅ ลงทะเบียนสำเร็จ\n"
            f"คุณ{pt['full_name']} (HN: {hn})\n\n"
            f"พิมพ์ 'help' เพื่อดูเมนูคำสั่ง"
        )
    # Help
    if text in ("help", "ช่วยเหลือ", "เมนู", "menu"):
        return (
            "📋 เมนูคำสั่ง:\n"
            "• สถานะ — สถานะยาวันนี้\n"
            "• ยา — รายละเอียดยา + ลิงก์ยืนยัน\n"
            "• adherence — ความสม่ำเสมอ 7/30 วัน\n"
            "• inr — ผล INR ล่าสุด\n"
            "• streak — จำนวนวันต่อเนื่อง\n"
            "• อาการ — รายงานอาการไม่พึงประสงค์\n"
            "• ลงทะเบียน <HN> — เชื่อมบัญชี LINE\n"
            "• help — เมนูนี้"
        )
    # Commands ที่ต้องการผู้ป่วยลงทะเบียนแล้ว
    with db() as conn:
        pt = conn.execute("SELECT * FROM patients WHERE line_user_id=? AND active=1", (uid,)).fetchone()
    if not pt:
        return (
            "⚠️ ไม่พบข้อมูลผู้ป่วย\n"
            "พิมพ์: ลงทะเบียน <HN>\n"
            "หรือติดต่อเภสัชกร"
        )
    if text in ("สถานะ", "status"):
        return _get_status_reply(pt)
    if text in ("ยา", "dose", "doses"):
        return _get_dose_reply(pt)
    if text in ("adherence", "ความสม่ำเสมอ"):
        return _get_adherence_reply(pt)
    if text in ("inr", "lab", "ผลเลือด"):
        return _get_inr_reply(pt)
    if text in ("streak", "ต่อเนื่อง"):
        return _get_streak_reply(pt)
    if raw in ("อาการ", "symptom", "symptoms") or text == "symptom":
        return (
            f"📝 รายงานอาการไม่พึงประสงค์\n"
            f"กรุณากดลิงก์:\n{BASE_URL}/report/symptom/{pt['patient_id']}\n\n"
            f"หากมีอาการรุนแรง เช่น เลือดออกมาก ปัสสาวะสีชา อาเจียนเป็นเลือด "
            f"กรุณาพบแพทย์ทันที"
        )
    # Fallback
    return (
        "ไม่เข้าใจคำสั่ง 🤔\n"
        "พิมพ์ 'help' เพื่อดูเมนู\n"
        "หรือพิมพ์ 'สถานะ' เพื่อดูสถานะยาวันนี้"
    )

def _get_status_reply(pt: sqlite3.Row) -> str:
    with db() as conn:
        doses = conn.execute(
            "SELECT * FROM medication_plan WHERE patient_id=? AND scheduled_date=? ORDER BY scheduled_time",
            (pt["patient_id"], _today()),
        ).fetchall()
        adh = compute_adherence(conn, pt["patient_id"], 7)
        streak = compute_streak(conn, pt["patient_id"])
    if not doses:
        return f"สวัสดีคุณ{pt['full_name']}\nวันนี้ไม่มีแผนกินยาค่ะ\nStreak: {streak} วัน"
    status_map = {"taken": "กินแล้ว ✅", "late": "กินแล้ว (ช้า) ⏰",
                  "missed": "พลาด ❌", "planned": "รอกิน 🕐"}
    lines = [f"สวัสดีคุณ{pt['full_name']}", f"📅 สถานะยา {_today()}"]
    for d in doses:
        lines.append(f"• {d['scheduled_time']} — {d['warfarin_mg']}mg — {status_map.get(d['status'], d['status'])}")
    lines.append(f"\n📊 Adherence 7 วัน: {adh['percent']}%")
    lines.append(f"🔥 Streak: {streak} วัน")
    return "\n".join(lines)

def _get_dose_reply(pt: sqlite3.Row) -> str:
    with db() as conn:
        dose = conn.execute(
            "SELECT mp.*, dt.token_id FROM medication_plan mp "
            "LEFT JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.patient_id=? AND mp.scheduled_date=? "
            "ORDER BY mp.scheduled_time LIMIT 1",
            (pt["patient_id"], _today()),
        ).fetchone()
    if not dose:
        return "วันนี้ไม่มีแผนกินยาค่ะ"
    status_map = {"taken": "กินแล้ว ✅", "late": "กินแล้ว (ช้า) ⏰",
                  "missed": "พลาด ❌", "planned": "ยังไม่ยืนยัน 🕐"}
    msg = (
        f"💊 ยาวาร์ฟาริน {dose['warfarin_mg']} mg\n"
        f"📋 {dose['pill_description'] or 'ยาวาร์ฟาริน'}\n"
        f"⏰ เวลา {dose['scheduled_time']} น.\n"
        f"📌 สถานะ: {status_map.get(dose['status'], dose['status'])}"
    )
    if dose["status"] == "planned" and dose["token_id"]:
        msg += f"\n\n✅ กดลิงก์ยืนยัน:\n{BASE_URL}/dose/{dose['token_id']}"
    return msg

def _get_adherence_reply(pt: sqlite3.Row) -> str:
    with db() as conn:
        a7 = compute_adherence(conn, pt["patient_id"], 7)
        a30 = compute_adherence(conn, pt["patient_id"], 30)
        streak = compute_streak(conn, pt["patient_id"])
    gam = compute_gamification_score(a7["percent"], streak)
    return (
        f"📊 ความสม่ำเสมอในการกินยา\n"
        f"คุณ{pt['full_name']}\n\n"
        f"🗓️ 7 วัน: {a7['percent']}% ({a7['taken']}/{a7['total']})\n"
        f"🗓️ 30 วัน: {a30['percent']}% ({a30['taken']}/{a30['total']})\n"
        f"🔥 Streak: {streak} วัน\n"
        f"🏆 คะแนนรวม: {gam}"
    )

def _get_inr_reply(pt: sqlite3.Row) -> str:
    with db() as conn:
        labs = conn.execute(
            "SELECT value, test_date, in_range FROM lab_results "
            "WHERE patient_id=? ORDER BY test_date DESC LIMIT 3",
            (pt["patient_id"],),
        ).fetchall()
    if not labs:
        return "ยังไม่มีผลการตรวจ INR ค่ะ"
    lines = [
        f"🧪 ผล INR ล่าสุด",
        f"คุณ{pt['full_name']}",
        f"เป้าหมาย {pt['target_inr_min']}-{pt['target_inr_max']}",
        "",
    ]
    for l in labs:
        mark = "✅" if l["in_range"] else "⚠️"
        lines.append(f"{mark} {l['test_date']}: {l['value']}")
    return "\n".join(lines)

def _get_streak_reply(pt: sqlite3.Row) -> str:
    with db() as conn:
        streak = compute_streak(conn, pt["patient_id"])
    if streak >= 30:
        emoji, praise = "🏆", "สุดยอด! คุณคือแชมป์ความสม่ำเสมอ"
    elif streak >= 14:
        emoji, praise = "🥇", "ยอดเยี่ยมมาก รักษาไว้นะคะ"
    elif streak >= 7:
        emoji, praise = "🥈", "ดีมาก! อีกนิดเดียวจะถึง 2 สัปดาห์"
    elif streak >= 1:
        emoji, praise = "🎯", "เริ่มต้นดีมาก พยายามต่อไปนะคะ"
    else:
        emoji, praise = "💪", "มาเริ่มสร้างสถิติกันใหม่นะคะ"
    return f"{emoji} ต่อเนื่อง {streak} วัน\n{praise}"

# ---------------------------------------------------------------------------
# API endpoints (JSON for AJAX / Chart.js)
# ---------------------------------------------------------------------------
@app.get("/api/patients/{pid}/inr-data")
def api_inr_data(pid: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT test_date as date, value, in_range FROM lab_results WHERE patient_id=? ORDER BY test_date",
            (pid,),
        ).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/patients/{pid}/adherence-data")
def api_adherence_data(pid: int):
    with db() as conn:
        since = (_now_dt() - timedelta(days=30)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT scheduled_date as date, status FROM medication_plan WHERE patient_id=? AND scheduled_date>=? ORDER BY scheduled_date",
            (pid, since),
        ).fetchall()
    daily = {}
    for r in rows:
        d = r["date"]
        if d not in daily:
            daily[d] = {"date": d, "total": 0, "taken": 0}
        daily[d]["total"] += 1
        if r["status"] in ("taken", "late"):
            daily[d]["taken"] += 1
    result = []
    for d in sorted(daily):
        v = daily[d]
        v["percent"] = round(v["taken"] / v["total"] * 100) if v["total"] else 0
        result.append(v)
    return result

# ---------------------------------------------------------------------------
# QR Code generation
# ---------------------------------------------------------------------------
def _make_qr_png(data: str, box_size: int = 10) -> bytes:
    if not QR_AVAILABLE:
        raise HTTPException(500, "QR library ไม่พร้อมใช้งาน — กรุณาติดตั้ง qrcode[pil]")
    qr = qrcode.QRCode(
        version=None, error_correction=ERROR_CORRECT_M,
        box_size=box_size, border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1e293b", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()

@app.get("/qr/{token_id}.png")
def qr_for_token(token_id: str):
    """QR code สำหรับ token — public endpoint เพราะต้องใช้แบบ embed ใน LINE"""
    with db() as conn:
        tok = conn.execute("SELECT token_id FROM dose_tokens WHERE token_id=?", (token_id,)).fetchone()
    if not tok:
        raise HTTPException(404)
    url = f"{BASE_URL}/dose/{token_id}"
    png = _make_qr_png(url, box_size=10)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})

@app.get("/patients/{pid}/qr-sheet")
def patient_qr_sheet(request: Request, pid: int):
    """หน้าแสดง QR code ทั้งหมดของโดสที่รอกิน — print-friendly"""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        patient = conn.execute("SELECT * FROM patients WHERE patient_id=?", (pid,)).fetchone()
        if not patient:
            raise HTTPException(404)
        doses = conn.execute(
            "SELECT mp.*, dt.token_id FROM medication_plan mp "
            "JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.patient_id=? AND mp.scheduled_date>=? "
            "ORDER BY mp.scheduled_date, mp.scheduled_time LIMIT 31",
            (pid, _today()),
        ).fetchall()
    return templates.TemplateResponse(request, "qr_sheet.html", {
        "user": user, "patient": dict(patient),
        "doses": [dict(d) for d in doses], "base_url": BASE_URL,
    })

# ---------------------------------------------------------------------------
# Symptom reporting (patient, no auth)
# ---------------------------------------------------------------------------
@app.get("/report/symptom/{pid}")
def symptom_form(request: Request, pid: int):
    with db() as conn:
        patient = conn.execute("SELECT patient_id, full_name FROM patients WHERE patient_id=? AND active=1", (pid,)).fetchone()
    if not patient:
        raise HTTPException(404, "ไม่พบผู้ป่วย")
    return templates.TemplateResponse(request, "symptom_form.html", {
        "patient": dict(patient), "today": _today(),
    })

@app.post("/report/symptom/{pid}")
async def symptom_submit(request: Request, pid: int):
    form = await request.form()
    with db() as conn:
        pt = conn.execute("SELECT full_name, line_user_id FROM patients WHERE patient_id=? AND active=1", (pid,)).fetchone()
        if not pt:
            raise HTTPException(404)
        conn.execute(
            "INSERT INTO symptom_reports "
            "(patient_id,report_date,bleeding,bruising,headache,dizziness,nausea,other,severity,source,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (pid, form.get("report_date", _today()),
             1 if form.get("bleeding") else 0,
             1 if form.get("bruising") else 0,
             1 if form.get("headache") else 0,
             1 if form.get("dizziness") else 0,
             1 if form.get("nausea") else 0,
             form.get("other", ""),
             int(form.get("severity", 1)),
             "patient", _now()),
        )
        log_audit(conn, "symptom_report", "symptom_reports", pid, "patient",
                  f"severity={form.get('severity')}")
    severity = int(form.get("severity", 1) or 1)
    # แจ้ง LINE ให้ผู้ป่วยและ log
    if severity >= 4 and pt["line_user_id"]:
        _push_line(pt["line_user_id"],
                   "⚠️ ได้รับรายงานอาการของคุณแล้ว\nเจ้าหน้าที่จะติดต่อกลับโดยเร็ว\nหากอาการรุนแรง กรุณาพบแพทย์ทันที")
    return templates.TemplateResponse(request, "symptom_result.html", {
        "patient": dict(pt), "severity": severity,
    })

@app.get("/symptoms")
def symptoms_list(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        rows = conn.execute(
            "SELECT sr.*, p.full_name, p.hn FROM symptom_reports sr "
            "JOIN patients p ON sr.patient_id=p.patient_id "
            "ORDER BY sr.created_at DESC LIMIT 200"
        ).fetchall()
    return templates.TemplateResponse(request, "symptoms.html", {
        "user": user, "reports": [dict(r) for r in rows],
    })

# ---------------------------------------------------------------------------
# Notification log
# ---------------------------------------------------------------------------
@app.get("/notifications")
def notifications_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with db() as conn:
        logs = conn.execute(
            "SELECT nl.*, p.full_name, p.hn FROM notification_log nl "
            "LEFT JOIN patients p ON nl.patient_id=p.patient_id "
            "ORDER BY nl.sent_at DESC LIMIT 200"
        ).fetchall()
    return templates.TemplateResponse(request, "notifications.html", {
        "user": user, "logs": [dict(l) for l in logs],
    })

# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
@app.get("/audit")
def audit_page(request: Request):
    user = get_current_user(request)
    if not user or user.get("role") != "admin":
        return RedirectResponse("/dashboard", status_code=303)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 300"
        ).fetchall()
    return templates.TemplateResponse(request, "audit.html", {
        "user": user, "logs": [dict(r) for r in rows],
    })

# ---------------------------------------------------------------------------
# LINE broadcast (staff → patients)
# ---------------------------------------------------------------------------
@app.post("/line/broadcast")
async def line_broadcast(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    form = await request.form()
    message = form.get("message", "").strip()
    if not message:
        return JSONResponse({"ok": False, "error": "empty message"})
    if len(message) > 2000:
        message = message[:2000]
    sent = 0
    failed = 0
    with db() as conn:
        patients = conn.execute(
            "SELECT patient_id, line_user_id FROM patients "
            "WHERE active=1 AND line_user_id IS NOT NULL AND line_user_id!=''"
        ).fetchall()
    for p in patients:
        if _push_line(p["line_user_id"], message):
            sent += 1
        else:
            failed += 1
        with db() as conn:
            _log_notification(conn, p["patient_id"], None, "broadcast", message, True)
    with db() as conn:
        log_audit(conn, "broadcast", "line", "all", user["username"], f"sent={sent} failed={failed}")
    return JSONResponse({"ok": True, "sent": sent, "failed": failed})

# ---------------------------------------------------------------------------
# Pill inventory
# ---------------------------------------------------------------------------
@app.post("/patients/{pid}/inventory")
async def update_inventory(request: Request, pid: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    try:
        count = int(form.get("pill_inventory", 0))
    except Exception:
        count = 0
    with db() as conn:
        conn.execute("UPDATE patients SET pill_inventory=?, updated_at=? WHERE patient_id=?",
                     (max(count, 0), _now(), pid))
        log_audit(conn, "update_inventory", "patient", pid, user["username"], f"count={count}")
    return RedirectResponse(f"/patients/{pid}", status_code=303)

@app.get("/api/dashboard-stats")
def api_dashboard_stats(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(401)
    with db() as conn:
        today = _today()
        taken = conn.execute("SELECT COUNT(*) c FROM medication_plan WHERE scheduled_date=? AND status IN ('taken','late')", (today,)).fetchone()["c"]
        missed = conn.execute("SELECT COUNT(*) c FROM medication_plan WHERE scheduled_date=? AND status='missed'", (today,)).fetchone()["c"]
        pending = conn.execute("SELECT COUNT(*) c FROM medication_plan WHERE scheduled_date=? AND status='planned'", (today,)).fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) c FROM patients WHERE active=1").fetchone()["c"]
    return {"today_taken": taken, "today_missed": missed, "today_pending": pending, "active_patients": active}

# ---------------------------------------------------------------------------
# Run with: uvicorn app:app --host 0.0.0.0 --port 8000
# ---------------------------------------------------------------------------
