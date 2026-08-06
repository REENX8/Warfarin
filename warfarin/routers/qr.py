"""QR code images and the staff QR scanner."""
from __future__ import annotations

import io
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from warfarin.config import get_settings
from warfarin.db import fetch_one, read_db
from warfarin.deps import csrf_protect, enforce_rate_limit, require_user
from warfarin.templating import render

logger = logging.getLogger(__name__)

router = APIRouter(tags=["qr"], dependencies=[Depends(csrf_protect)])

try:  # pragma: no cover - depends on the deployed environment
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M

    QR_AVAILABLE = True
except ImportError:  # pragma: no cover
    QR_AVAILABLE = False


def make_qr_png(data: str, box_size: int = 10) -> bytes:
    if not QR_AVAILABLE:
        raise HTTPException(
            503, "ไม่สามารถสร้าง QR ได้ — ต้องติดตั้งไลบรารี qrcode[pil]"
        )
    code = qrcode.QRCode(
        version=None, error_correction=ERROR_CORRECT_M, box_size=box_size, border=2
    )
    code.add_data(data)
    code.make(fit=True)
    image = code.make_image(fill_color="#1e293b", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@router.get("/qr/{token_id}.png")
def dose_qr(request: Request, token_id: str):
    """Public: printed QR sheets and LINE messages embed this image."""
    enforce_rate_limit(request, "qr", 120, 60)
    with read_db() as conn:
        token = fetch_one(
            conn, "SELECT token_id FROM dose_tokens WHERE token_id=?", (token_id,)
        )
    if token is None:
        raise HTTPException(404, "ไม่พบรหัสยืนยัน")
    png = make_qr_png(f"{get_settings().base_url}/dose/{token_id}")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/qr/portal/{token}.png")
def portal_qr(request: Request, token: str, user: dict = Depends(require_user)):
    """QR of a patient's personal portal link, for printing on a handout."""
    with read_db() as conn:
        patient = fetch_one(
            conn, "SELECT patient_id FROM patients WHERE access_token=?", (token,)
        )
    if patient is None:
        raise HTTPException(404, "ไม่พบผู้ป่วย")
    png = make_qr_png(f"{get_settings().base_url}/p/{token}", box_size=8)
    return Response(content=png, media_type="image/png")


@router.get("/scan")
def scan_page(request: Request, user: dict = Depends(require_user)):
    """Camera-based scanner so staff can confirm a dose from a printed sheet."""
    return render(request, "scan.html", {"user": user})
