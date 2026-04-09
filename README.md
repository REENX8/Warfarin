# ระบบติดตามการกินยาวาร์ฟาริน
### Warfarin Medication Tracking System
**โรงพยาบาลสุไหงปาดี จังหวัดนราธิวาส**

ระบบติดตามความต่อเนื่องในการกินยาวาร์ฟาริน พัฒนาเพื่องานวิจัย ผสานการแจ้งเตือนผ่าน LINE OA และการยืนยันการกินยาผ่าน QR Code

---

## คุณสมบัติหลัก

- **Dashboard** — ภาพรวมรายวัน: กินแล้ว / ยังไม่กิน / พลาด / ผู้ป่วยเสี่ยง
- **จัดการผู้ป่วย** — เพิ่ม / แก้ไข / ค้นหา พร้อมข้อมูลผู้ดูแล
- **แผนการกินยา** — สร้างแบบ bulk รองรับขนาดยาต่างกันตามวัน (จ-อา)
- **QR Code ยืนยันยา** — ผู้ป่วยสแกนหรือกดลิงก์บน LINE เพื่อยืนยัน
- **LINE OA Integration** — แจ้งเตือน 18:00, เตือนซ้ำ 19:30, mark missed 21:00
- **INR Tracking** — บันทึกผล Lab พร้อมกราฟ Chart.js และ target range zone
- **Adherence & Streak** — คำนวณ % การกินยา 7 / 30 / 365 วัน และวันติดต่อกัน
- **Gamification Score** — คะแนนรวม (adherence + streak bonus)
- **Pre/Post Test Score** — บันทึกผลแบบทดสอบความรู้ผู้ป่วย
- **แบบสอบถามความพึงพอใจ** — 3 มิติ พร้อมสรุปค่าเฉลี่ยในรายงาน
- **รายงาน + Export CSV** — สรุปทุกผู้ป่วย ดาวน์โหลดเป็น CSV

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11+, FastAPI |
| Frontend | Jinja2, Tailwind CSS (CDN), Chart.js |
| Database | SQLite (WAL mode) |
| Messaging | LINE Messaging API v3 (`line-bot-sdk`) |
| Scheduler | APScheduler (BackgroundScheduler) |
| Deploy | Uvicorn, Procfile (Render / Railway) |

---

## การติดตั้ง

### 1. Clone & สร้าง Virtual Environment

```bash
git clone https://github.com/REENX8/Warfarin.git
cd Warfarin
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate
```

### 2. ติดตั้ง Dependencies

```bash
pip install -r requirements.txt
```

### 3. ตั้งค่า Environment Variables

```bash
cp .env.example .env
```

แก้ไขไฟล์ `.env`:

```env
SECRET_KEY=your-random-secret-key
LINE_CHANNEL_SECRET=6142c0a719615fb438bfbf116869f2d3
LINE_CHANNEL_ACCESS_TOKEN=your-token-from-line-developers
BASE_URL=https://your-domain.com
DB_PATH=./medtrack.db
```

### 4. รันระบบ

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

เปิดเบราว์เซอร์ไปที่ `http://localhost:8000`  
Login เริ่มต้น: **admin / admin123**

---

## Deploy บน Render / Railway

1. Push โค้ดขึ้น GitHub
2. สร้าง Web Service ชี้ที่ repo นี้
3. ตั้ง Environment Variables ตามข้อ 3 ด้านบน
4. Start Command จะใช้ `Procfile` อัตโนมัติ:
   ```
   web: uvicorn app:app --host 0.0.0.0 --port $PORT
   ```

> **หมายเหตุ:** SQLite ไม่เหมาะกับ ephemeral filesystem — ใช้ persistent disk (Render Disk) หรือ mount volume

---

## LINE OA Setup

1. สร้าง LINE Messaging API Channel ที่ [LINE Developers Console](https://developers.line.biz/)
2. คัดลอก **Channel Secret** และ **Channel Access Token** ใส่ `.env`
3. ตั้ง Webhook URL: `https://your-domain.com/webhook`
4. เปิด **Use webhook** และปิด **Auto-reply messages**
5. ลงทะเบียน LINE User ID ของผู้ป่วยในระบบ (ช่อง "LINE User ID" ในฟอร์มผู้ป่วย)

### คำสั่ง LINE ที่ผู้ป่วยใช้ได้

| พิมพ์ | ผลลัพธ์ |
|-------|---------|
| `สถานะ` | สถานะยาวันนี้ + adherence 7 วัน + streak |
| `ยา` | รายละเอียดยาวันนี้ + ลิงก์ยืนยัน |

---

## โครงสร้างไฟล์

```
Warfarin/
├── app.py                    # FastAPI backend (routes, DB, scheduler)
├── requirements.txt
├── Procfile
├── .env.example
└── templates/
    ├── base.html             # Layout หลัก (sidebar, navbar)
    ├── login.html
    ├── dashboard.html
    ├── patients.html         # รายชื่อผู้ป่วย
    ├── patient_form.html     # เพิ่ม/แก้ไขผู้ป่วย
    ├── patient_detail.html   # ข้อมูลผู้ป่วย, doses, INR chart
    ├── dose_confirm.html     # หน้ายืนยันยา (mobile-first, ไม่ต้อง login)
    ├── dose_result.html      # ผลการยืนยัน
    ├── reports.html          # รายงาน + survey summary
    └── survey_form.html      # แบบสอบถามความพึงพอใจ
```

---

## ตารางฐานข้อมูล

| ตาราง | คำอธิบาย |
|-------|---------|
| `staff` | ผู้ใช้งานระบบ (เภสัชกร, พยาบาล) |
| `patients` | ข้อมูลผู้ป่วย |
| `caregivers` | ข้อมูลผู้ดูแล + LINE ID |
| `medication_plan` | แผนและสถานะการกินยารายวัน |
| `dose_tokens` | QR token + reminder_count |
| `lab_results` | ผล INR Lab |
| `test_scores` | คะแนน pre/post test |
| `satisfaction_surveys` | แบบสอบถามความพึงพอใจ |
| `notification_log` | Log การส่ง LINE |
| `audit_log` | บันทึกการกระทำในระบบ |

---

## Scheduler Jobs (Asia/Bangkok)

| เวลา | งาน |
|------|-----|
| 18:00 | ส่ง LINE เตือนกินยาครั้งที่ 1 |
| 19:30 | ส่ง LINE เตือนซ้ำครั้งที่ 2 (เฉพาะที่ยังไม่ยืนยัน) |
| 21:00 | Mark missed + แจ้งผู้ป่วยและผู้ดูแล |
| 03:00 | ลบ session เก่า > 24 ชม. |

---

## API Endpoints

| Method | Path | คำอธิบาย |
|--------|------|---------|
| GET | `/dashboard` | แดชบอร์ด |
| GET/POST | `/patients` | รายชื่อผู้ป่วย |
| GET/POST | `/patients/new` | เพิ่มผู้ป่วย |
| GET | `/patients/{pid}` | ข้อมูลผู้ป่วย |
| GET/POST | `/patients/{pid}/edit` | แก้ไขผู้ป่วย |
| POST | `/patients/{pid}/doses` | สร้างแผนยา |
| POST | `/patients/{pid}/lab` | บันทึก INR |
| POST | `/patients/{pid}/test-score` | บันทึกคะแนน |
| GET/POST | `/patients/{pid}/survey` | แบบสอบถาม |
| GET | `/dose/{token_id}` | หน้ายืนยันยา (ผู้ป่วย) |
| POST | `/dose/{token_id}/confirm` | ยืนยันการกินยา |
| GET | `/reports` | รายงาน |
| GET | `/reports/export` | Export CSV |
| POST | `/webhook` | LINE Webhook |
| GET | `/api/patients/{pid}/inr-data` | JSON INR สำหรับกราฟ |
| GET | `/api/patients/{pid}/adherence-data` | JSON adherence 30 วัน |

---

## งานวิจัย

ระบบนี้พัฒนาเพื่อรองรับงานวิจัยการเพิ่มความต่อเนื่องในการกินยาวาร์ฟารินในผู้ป่วยโรคหัวใจและหลอดเลือด โรงพยาบาลสุไหงปาดี จังหวัดนราธิวาส โดยใช้เทคโนโลยี LINE OA เป็นช่องทางการสื่อสารและติดตาม

**ตัวชี้วัดหลัก:**
- Medication Adherence Rate (%)
- Time in Therapeutic Range — TTR (%)
- Streak (วันกินยาติดต่อกัน)
- ความพึงพอใจของผู้ป่วยต่อระบบ (1-5 ดาว)
