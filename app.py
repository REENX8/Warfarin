"""ระบบติดตามการกินยาวาร์ฟาริน — Sukhirin Padee Hospital, Narathiwat"""

import os, sqlite3, uuid, hashlib, json, csv, io
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

# LINE Bot SDK v3
try:
    from linebot.v3.messaging import (
        Configuration, ApiClient, MessagingApi,
        ReplyMessageRequest, PushMessageRequest, TextMessage,
    )
    from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent, UnfollowEvent
    from linebot.v3.webhook import WebhookHandler
    LINE_SDK_AVAILABLE = True
except ImportError:
    LINE_SDK_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "warfarin-tracker-secret-2024")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "6142c0a719615fb438bfbf116869f2d3")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
DB_PATH = os.getenv("DB_PATH", "./medtrack.db")

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="Warfarin Medication Tracker")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=True)

os.makedirs("templates", exist_ok=True)
os.makedirs("static", exist_ok=True)
templates = Jinja2Templates(directory="templates")
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
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

def _today() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")

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
            created_at TEXT, expires_at TEXT, is_used INTEGER DEFAULT 0, used_at TEXT
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
        """)
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
    since = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT status FROM medication_plan WHERE patient_id=? AND scheduled_date>=? AND scheduled_date<=?",
        (patient_id, since, _today()),
    ).fetchall()
    total = len(rows)
    taken = sum(1 for r in rows if r["status"] == "taken")
    missed = sum(1 for r in rows if r["status"] == "missed")
    late = sum(1 for r in rows if r["status"] == "late")
    pending = sum(1 for r in rows if r["status"] == "planned")
    pct = round(((taken + late) / total) * 100, 1) if total else 0
    return {"total": total, "taken": taken, "missed": missed, "late": late, "pending": pending, "percent": pct}

def compute_streak(conn, patient_id) -> int:
    rows = conn.execute(
        "SELECT scheduled_date, status FROM medication_plan WHERE patient_id=? AND scheduled_date<=? ORDER BY scheduled_date DESC",
        (patient_id, _today()),
    ).fetchall()
    streak = 0
    for r in rows:
        if r["status"] in ("taken", "late"):
            streak += 1
        else:
            break
    return streak

def compute_gamification_score(adherence_pct, streak):
    return round(adherence_pct + min(streak, 30) * 0.5, 1)

def update_missed_doses(conn):
    """ทำเครื่องหมายโดสที่เลยเวลาแล้วเป็น missed"""
    conn.execute(
        "UPDATE medication_plan SET status='missed' WHERE status='planned' AND scheduled_date < ?",
        (_today(),),
    )

# ---------------------------------------------------------------------------
# LINE Push helpers (ไม่ error ถ้า LINE ใช้ไม่ได้)
# ---------------------------------------------------------------------------
def _push_line(user_id: str, text: str):
    if not line_api or not user_id:
        return
    try:
        line_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=text)]))
    except Exception:
        pass

def send_line_reminder(patient: dict):
    url = ""
    with db() as conn:
        row = conn.execute(
            "SELECT dt.token_id, mp.warfarin_mg FROM medication_plan mp JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.patient_id=? AND mp.scheduled_date=? AND mp.status='planned' LIMIT 1",
            (patient["patient_id"], _today()),
        ).fetchone()
        if row:
            url = f"{BASE_URL}/dose/{row['token_id']}"
            msg = f"ถึงเวลากินยาวาร์ฟาริน {row['warfarin_mg']}mg แล้วค่ะ\nกรุณาสแกน QR หรือกดลิงก์ยืนยัน:\n{url}"
        else:
            msg = "ถึงเวลากินยาวาร์ฟารินแล้วค่ะ กรุณายืนยันการกินยา"
    _push_line(patient.get("line_user_id", ""), msg)

def send_line_confirmation(patient: dict, dose: dict):
    with db() as conn:
        streak = compute_streak(conn, patient["patient_id"])
    msg = f"บันทึกการกินยาเรียบร้อย! ✅\nยาวาร์ฟาริน {dose.get('warfarin_mg','')}mg\nStreak {streak} วันติดต่อกัน 🎯"
    _push_line(patient.get("line_user_id", ""), msg)

def send_line_missed_alert(patient: dict):
    msg = f"⚠️ คุณ{patient['full_name']} ยังไม่ได้กินยาวาร์ฟารินวันนี้ กรุณากินยาโดยเร็ว"
    _push_line(patient.get("line_user_id", ""), msg)
    # แจ้งผู้ดูแลด้วย
    with db() as conn:
        cgs = conn.execute(
            "SELECT line_user_id FROM caregivers WHERE patient_id=? AND notify_enabled=1 AND line_user_id IS NOT NULL",
            (patient["patient_id"],),
        ).fetchall()
        for cg in cgs:
            _push_line(cg["line_user_id"], f"⚠️ ผู้ป่วย {patient['full_name']} ยังไม่ได้กินยาวาร์ฟารินวันนี้")

# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------
scheduler = BackgroundScheduler()

def job_send_reminders():
    """18:00 — ส่งเตือนยาวันนี้"""
    with db() as conn:
        patients = conn.execute(
            "SELECT DISTINCT p.* FROM patients p JOIN medication_plan mp ON p.patient_id=mp.patient_id "
            "WHERE mp.scheduled_date=? AND mp.status='planned' AND p.active=1", (_today(),)
        ).fetchall()
        for p in patients:
            send_line_reminder(dict(p))
            conn.execute(
                "INSERT INTO notification_log (patient_id,channel,message_type,message_text,sent_at) VALUES(?,?,?,?,?)",
                (p["patient_id"], "line", "reminder", "ส่งเตือนกินยา", _now()),
            )

def job_mark_missed():
    """21:00 — ทำเครื่องหมาย missed + แจ้งเตือน"""
    with db() as conn:
        missed = conn.execute(
            "SELECT mp.dose_id, p.* FROM medication_plan mp JOIN patients p ON mp.patient_id=p.patient_id "
            "WHERE mp.scheduled_date=? AND mp.status='planned' AND p.active=1", (_today(),)
        ).fetchall()
        update_missed_doses(conn)
        for row in missed:
            send_line_missed_alert(dict(row))
            conn.execute(
                "INSERT INTO notification_log (patient_id,dose_id,channel,message_type,message_text,sent_at) VALUES(?,?,?,?,?,?)",
                (row["patient_id"], row["dose_id"], "line", "missed", "แจ้งเตือนลืมกินยา", _now()),
            )

scheduler.add_job(job_send_reminders, "cron", hour=18, minute=0)
scheduler.add_job(job_mark_missed, "cron", hour=21, minute=0)

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    init_db()
    if not scheduler.running:
        scheduler.start()

@app.on_event("shutdown")
def on_shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)

# ---------------------------------------------------------------------------
# Routes: Auth
# ---------------------------------------------------------------------------
@app.get("/")
def root(request: Request):
    user = get_current_user(request)
    return RedirectResponse("/dashboard" if user else "/login", status_code=303)

@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})

@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    with db() as conn:
        staff = conn.execute("SELECT * FROM staff WHERE username=?", (username,)).fetchone()
    if not staff or staff["password_hash"] != _hash_pw(password):
        return templates.TemplateResponse("login.html", {"request": request, "error": "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"})
    sid = str(uuid.uuid4())
    SESSIONS[sid] = {"staff_id": staff["staff_id"], "username": staff["username"], "full_name": staff["full_name"], "role": staff["role"]}
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie("session_id", sid, httponly=True)
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
        # ค่าเฉลี่ย adherence 7 วัน
        pats = conn.execute("SELECT patient_id FROM patients WHERE active=1").fetchall()
        adh_list = [compute_adherence(conn, p["patient_id"], 7)["percent"] for p in pats]
        adherence_avg = round(sum(adh_list) / len(adh_list), 1) if adh_list else 0
        # ผู้ป่วยเสี่ยง
        at_risk = []
        for p in pats:
            a = compute_adherence(conn, p["patient_id"], 7)
            if a["percent"] < 70 or a["missed"] >= 3:
                pt = conn.execute("SELECT * FROM patients WHERE patient_id=?", (p["patient_id"],)).fetchone()
                at_risk.append({"patient": dict(pt), "adherence": a})
        # กิจกรรมล่าสุด
        recent = conn.execute(
            "SELECT mp.*, p.full_name, p.hn FROM medication_plan mp JOIN patients p ON mp.patient_id=p.patient_id "
            "WHERE mp.status IN ('taken','late') ORDER BY mp.confirmed_at DESC LIMIT 10"
        ).fetchall()
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "user": user, "total_patients": total_patients,
        "active_patients": active_patients, "today_taken": today_taken,
        "today_missed": today_missed, "today_pending": today_pending,
        "adherence_avg": adherence_avg, "at_risk_patients": at_risk,
        "recent_activity": [dict(r) for r in recent],
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
            patients = conn.execute(
                "SELECT * FROM patients WHERE full_name LIKE ? OR hn LIKE ? ORDER BY full_name",
                (f"%{q}%", f"%{q}%"),
            ).fetchall()
        else:
            patients = conn.execute("SELECT * FROM patients ORDER BY full_name").fetchall()
    return templates.TemplateResponse("patients.html", {"request": request, "user": user, "patients": patients, "q": q})

@app.get("/patients/new")
def patient_new_form(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("patient_form.html", {"request": request, "user": user, "patient": None, "caregivers": []})

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
        # เพิ่มผู้ดูแล
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
        doses = conn.execute("SELECT * FROM medication_plan WHERE patient_id=? ORDER BY scheduled_date DESC, scheduled_time DESC LIMIT 60", (pid,)).fetchall()
        labs = conn.execute("SELECT * FROM lab_results WHERE patient_id=? ORDER BY test_date DESC", (pid,)).fetchall()
        scores = conn.execute("SELECT * FROM test_scores WHERE patient_id=? ORDER BY taken_at DESC", (pid,)).fetchall()
        adh7 = compute_adherence(conn, pid, 7)
        adh30 = compute_adherence(conn, pid, 30)
        adh_all = compute_adherence(conn, pid, 365)
        streak = compute_streak(conn, pid)
        gami = compute_gamification_score(adh7["percent"], streak)
    return templates.TemplateResponse("patient_detail.html", {
        "request": request, "user": user, "patient": dict(patient),
        "caregivers": [dict(c) for c in caregivers], "doses": [dict(d) for d in doses],
        "labs": [dict(l) for l in labs], "scores": [dict(s) for s in scores],
        "adh7": adh7, "adh30": adh30, "adh_all": adh_all,
        "streak": streak, "gamification_score": gami,
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
    return templates.TemplateResponse("patient_form.html", {
        "request": request, "user": user, "patient": dict(patient), "caregivers": [dict(c) for c in caregivers],
    })

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
        log_audit(conn, "update", "patient", pid, user["username"], f"แก้ไขข้อมูลผู้ป่วย")
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
    # ขนาดยาตามวัน (จันทร์-อาทิตย์) — ถ้าไม่กำหนดใช้ค่า default
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
            expires = (d + timedelta(days=2)).isoformat()
            conn.execute(
                "INSERT INTO dose_tokens (token_id,dose_id,created_at,expires_at) VALUES(?,?,?,?)",
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
# Routes: Dose confirmation (ผู้ป่วยใช้ — ไม่ต้อง login)
# ---------------------------------------------------------------------------
@app.get("/dose/{token_id}")
def dose_confirm_page(request: Request, token_id: str):
    with db() as conn:
        tok = conn.execute(
            "SELECT dt.*, mp.*, p.full_name, p.patient_id AS pid FROM dose_tokens dt "
            "JOIN medication_plan mp ON dt.dose_id=mp.dose_id "
            "JOIN patients p ON mp.patient_id=p.patient_id WHERE dt.token_id=?", (token_id,)
        ).fetchone()
    if not tok:
        raise HTTPException(404, "ไม่พบข้อมูลยา หรือลิงก์ไม่ถูกต้อง")
    already = tok["is_used"] == 1
    with db() as conn:
        adh = compute_adherence(conn, tok["pid"], 7)
        streak = compute_streak(conn, tok["pid"])
    # ข้อความให้กำลังใจตาม adherence
    if adh["percent"] >= 90:
        adh_msg = "ยอดเยี่ยมมาก! คุณกินยาได้สม่ำเสมอมากค่ะ 🌟"
    elif adh["percent"] >= 70:
        adh_msg = "ดีมากค่ะ พยายามกินยาให้สม่ำเสมอต่อไปนะคะ 💪"
    else:
        adh_msg = "อย่าลืมกินยาทุกวันนะคะ สุขภาพสำคัญค่ะ ❤️"
    return templates.TemplateResponse("dose_confirm.html", {
        "request": request, "token": dict(tok), "already": already,
        "adherence": adh, "streak": streak, "adh_msg": adh_msg,
    })

@app.post("/dose/{token_id}/confirm")
def dose_confirm(request: Request, token_id: str):
    with db() as conn:
        tok = conn.execute(
            "SELECT dt.*, mp.*, p.full_name, p.patient_id AS pid, p.line_user_id FROM dose_tokens dt "
            "JOIN medication_plan mp ON dt.dose_id=mp.dose_id "
            "JOIN patients p ON mp.patient_id=p.patient_id WHERE dt.token_id=?", (token_id,)
        ).fetchone()
        if not tok:
            raise HTTPException(404, "ไม่พบข้อมูล")
        if tok["is_used"] == 1:
            return templates.TemplateResponse("dose_result.html", {
                "request": request, "success": False, "message": "ยืนยันไปแล้ว", "dose": dict(tok),
            })
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        sched = datetime.strptime(f"{tok['scheduled_date']} {tok['scheduled_time']}", "%Y-%m-%d %H:%M")
        diff = (now - sched).total_seconds() / 60
        late = max(0, int(diff))
        status = "late" if late > 120 else "taken"  # >2 ชม.ถือว่า late
        conn.execute(
            "UPDATE medication_plan SET status=?,confirmed_at=?,confirmed_by='patient',late_minutes=? WHERE dose_id=?",
            (status, now.isoformat(), late, tok["dose_id"]),
        )
        conn.execute("UPDATE dose_tokens SET is_used=1,used_at=? WHERE token_id=?", (now.isoformat(), token_id))
        streak = compute_streak(conn, tok["pid"])
        log_audit(conn, "confirm_dose", "medication_plan", tok["dose_id"], "patient", f"status={status} late={late}m")
    # ส่ง LINE ยืนยัน
    send_line_confirmation(
        {"patient_id": tok["pid"], "full_name": tok["full_name"], "line_user_id": tok["line_user_id"]},
        {"warfarin_mg": tok["warfarin_mg"]},
    )
    return templates.TemplateResponse("dose_result.html", {
        "request": request, "success": True, "dose": dict(tok),
        "message": f"บันทึกเรียบร้อย! Streak {streak} วัน" + (" (กินยาช้า)" if status == "late" else ""),
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
            last_inr = conn.execute(
                "SELECT value, test_date FROM lab_results WHERE patient_id=? ORDER BY test_date DESC LIMIT 1",
                (p["patient_id"],),
            ).fetchone()
            data.append({"patient": dict(p), "adh7": a7, "adh30": a30, "streak": streak, "last_inr": dict(last_inr) if last_inr else None})
    return templates.TemplateResponse("reports.html", {"request": request, "user": user, "report_data": data})

@app.get("/reports/export")
def reports_export(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["patient_id", "hn", "full_name", "period", "total_doses", "taken", "missed", "late", "adherence_%", "avg_inr", "inr_in_range_%"])
    with db() as conn:
        patients = conn.execute("SELECT * FROM patients WHERE active=1").fetchall()
        for p in patients:
            a = compute_adherence(conn, p["patient_id"], 30)
            inrs = conn.execute("SELECT value, in_range FROM lab_results WHERE patient_id=?", (p["patient_id"],)).fetchall()
            avg_inr = round(sum(r["value"] for r in inrs) / len(inrs), 2) if inrs else 0
            inr_pct = round(sum(r["in_range"] for r in inrs) / len(inrs) * 100, 1) if inrs else 0
            writer.writerow([p["patient_id"], p["hn"], p["full_name"], "30d", a["total"], a["taken"], a["missed"], a["late"], a["percent"], avg_inr, inr_pct])
    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=warfarin_report.csv"},
    )

# ---------------------------------------------------------------------------
# LINE Webhook
# ---------------------------------------------------------------------------
@app.post("/webhook")
async def line_webhook(request: Request):
    if not line_handler:
        return JSONResponse({"status": "LINE not configured"})
    body = await request.body()
    sig = request.headers.get("X-Line-Signature", "")
    try:
        line_handler.handle(body.decode(), sig)
    except Exception:
        raise HTTPException(400, "Invalid signature")
    return JSONResponse({"status": "ok"})

if LINE_SDK_AVAILABLE and line_handler:
    @line_handler.add(FollowEvent)
    def handle_follow(event):
        uid = event.source.user_id
        msg = "สวัสดีค่ะ! ยินดีต้อนรับสู่ระบบติดตามยาวาร์ฟาริน 💊\nกรุณาแจ้งเภสัชกรเพื่อลงทะเบียน LINE ของคุณ\n\nพิมพ์ 'สถานะ' เพื่อดูสถานะยาวันนี้\nพิมพ์ 'ยา' เพื่อดูรายละเอียดยา"
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
        text = event.message.text.strip().lower()
        if text in ("สถานะ", "status"):
            reply = _get_status_reply(uid)
        elif text in ("ยา", "dose"):
            reply = _get_dose_reply(uid)
        else:
            reply = "พิมพ์ 'สถานะ' เพื่อดูสถานะยา\nพิมพ์ 'ยา' เพื่อดูรายละเอียดยาวันนี้"
        if line_api:
            try:
                line_api.reply_message(ReplyMessageRequest(
                    reply_token=event.reply_token, messages=[TextMessage(text=reply)]
                ))
            except Exception:
                pass

    @line_handler.add(UnfollowEvent)
    def handle_unfollow(event):
        with db() as conn:
            log_audit(conn, "line_unfollow", "line", event.source.user_id, "system", "")

def _get_status_reply(uid: str) -> str:
    with db() as conn:
        pt = conn.execute("SELECT * FROM patients WHERE line_user_id=? AND active=1", (uid,)).fetchone()
        if not pt:
            return "ไม่พบข้อมูลผู้ป่วย กรุณาแจ้งเภสัชกรเพื่อลงทะเบียน"
        doses = conn.execute(
            "SELECT * FROM medication_plan WHERE patient_id=? AND scheduled_date=?",
            (pt["patient_id"], _today()),
        ).fetchall()
        adh = compute_adherence(conn, pt["patient_id"], 7)
        streak = compute_streak(conn, pt["patient_id"])
    if not doses:
        return f"สวัสดีคุณ{pt['full_name']}\nวันนี้ไม่มีแผนกินยาค่ะ\nStreak: {streak} วัน"
    status_map = {"taken": "กินแล้ว ✅", "late": "กินแล้ว (ช้า) ⏰", "missed": "พลาด ❌", "planned": "รอกิน 🕐"}
    lines = [f"สวัสดีคุณ{pt['full_name']} สถานะยาวันนี้:"]
    for d in doses:
        lines.append(f"  {d['warfarin_mg']}mg — {status_map.get(d['status'], d['status'])}")
    lines.append(f"\nAdherence 7 วัน: {adh['percent']}%\nStreak: {streak} วัน")
    return "\n".join(lines)

def _get_dose_reply(uid: str) -> str:
    with db() as conn:
        pt = conn.execute("SELECT * FROM patients WHERE line_user_id=? AND active=1", (uid,)).fetchone()
        if not pt:
            return "ไม่พบข้อมูลผู้ป่วย"
        dose = conn.execute(
            "SELECT mp.*, dt.token_id FROM medication_plan mp LEFT JOIN dose_tokens dt ON mp.dose_id=dt.dose_id "
            "WHERE mp.patient_id=? AND mp.scheduled_date=? LIMIT 1",
            (pt["patient_id"], _today()),
        ).fetchone()
    if not dose:
        return "วันนี้ไม่มีแผนกินยาค่ะ"
    url = f"{BASE_URL}/dose/{dose['token_id']}" if dose["token_id"] else ""
    msg = f"💊 ยาวาร์ฟาริน {dose['warfarin_mg']}mg\n📋 {dose['pill_description'] or '-'}\n⏰ เวลา {dose['scheduled_time']} น."
    if url:
        msg += f"\n\nกดลิงก์ยืนยัน:\n{url}"
    return msg

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
        since = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)).strftime("%Y-%m-%d")
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
