"""Pytest fixtures for Warfarin smoke tests.

DB_PATH ถูก capture ที่ module-import time ของ app.py — ดังนั้น set env ก่อน import,
และ patch BackgroundScheduler ให้เป็น MagicMock ก่อน TestClient เพื่อกัน thread leak
"""
import os
import sys
import tempfile
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# วาง project root ใน path + เตรียม temp DB ก่อน import app
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP_DIR = tempfile.mkdtemp(prefix="warfarin-test-")
_TMP_DB = os.path.join(_TMP_DIR, "test.db")
os.environ["DB_PATH"] = _TMP_DB
os.environ.setdefault("BASE_URL", "http://testserver")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
# ห้ามตั้ง LINE_CHANNEL_SECRET — webhook test ต้องการให้ไม่มี config

# Stub APScheduler ก่อน import app เพื่อกัน BackgroundScheduler thread
import apscheduler.schedulers.background as _aps
_aps.BackgroundScheduler = MagicMock(return_value=MagicMock(running=False))

import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Patch app.scheduler ด้วย เผื่อมี reference ค้างจาก import time
app.scheduler = MagicMock(running=False)


@pytest.fixture(scope="session")
def client():
    """TestClient ที่ใช้ DB_PATH ชั่วคราว"""
    app.init_db()
    with TestClient(app.app) as c:
        yield c


@pytest.fixture
def db_conn():
    """ให้ raw sqlite connection สำหรับ seed ข้อมูลใน test"""
    conn = sqlite3.connect(_TMP_DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def seed_patient(db_conn):
    """สร้างผู้ป่วยตัวอย่างใหม่ทุกครั้งด้วย HN ไม่ซ้ำ แล้วคืน patient_id"""
    import uuid as _uuid
    hn = "T" + _uuid.uuid4().hex[:8].upper()
    cur = db_conn.execute(
        "INSERT INTO patients (hn, full_name, target_inr_min, target_inr_max, active, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?)",
        (hn, "ผู้ทดสอบ ใจดี", 2.0, 3.0, app._now(), app._now()),
    )
    db_conn.commit()
    return cur.lastrowid


@pytest.fixture
def admin_login(client):
    """ล็อกอิน admin/admin123 และคืน TestClient ที่มี session cookie"""
    r = client.post("/login",
                    data={"username": "admin", "password": "admin123"},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    return client
