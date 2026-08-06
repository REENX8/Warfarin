"""LINE Rich Menu generation.

The menu image is drawn with Pillow so no design asset has to ship with the
repository. Creation is best-effort: a LINE or font failure logs and returns,
it never blocks application startup.
"""
from __future__ import annotations

import io
import logging
import os
import urllib.request

from warfarin import line_service as ls
from warfarin.config import get_settings

logger = logging.getLogger(__name__)

_rich_menu_id: str | None = None

WIDTH, HEIGHT = 1200, 810
CELL_WIDTH, CELL_HEIGHT = 400, 405

CELLS = [
    (0, 0, "สถานะ", "สถานะ", "#4f46e5"),
    (400, 0, "ยาวันนี้", "ยา", "#059669"),
    (800, 0, "ความสม่ำเสมอ", "ความสม่ำเสมอ", "#0891b2"),
    (0, 405, "ผลเลือด", "ผลเลือด", "#9333ea"),
    (400, 405, "นัดหมาย", "นัด", "#0284c7"),
    (800, 405, "แจ้งอาการ", "อาการ", "#e11d48"),
]

FONT_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/opentype/tlwg/Loma-Bold.otf",
    "/usr/share/fonts/opentype/tlwg/Loma.otf",
    "/usr/share/fonts/truetype/tlwg/Loma-Bold.ttf",
    "/usr/share/fonts/truetype/tlwg/Loma.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
FONT_URL = (
    "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/"
    "NotoSansThai/NotoSansThai-Regular.ttf"
)
FONT_CACHE = os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "warfarin_NotoSansThai.ttf"
)


def load_thai_font(size: int):
    """Find a Thai-capable font, downloading Noto Sans Thai only as a fallback."""
    from PIL import ImageFont

    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    if not os.path.exists(FONT_CACHE):
        try:
            urllib.request.urlretrieve(FONT_URL, FONT_CACHE)
        except Exception:
            logger.info("Could not download Thai font for the rich menu", exc_info=True)
    try:
        return ImageFont.truetype(FONT_CACHE, size)
    except OSError:
        logger.warning("Falling back to the default bitmap font (Thai may not render)")
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 9.2
        return ImageFont.load_default()


def _draw_icon(draw, x: int, y: int, key: str) -> None:
    """Simple geometric white glyph centred in a menu cell."""
    cx, cy = x + CELL_WIDTH // 2, y + 150
    stroke = 3
    if key == "สถานะ":
        draw.rounded_rectangle([cx - 30, cy - 35, cx + 30, cy + 35], radius=6,
                               outline="white", width=stroke)
        draw.rectangle([cx - 30, cy - 35, cx + 30, cy - 22], fill="white")
        for offset in (-12, 2, 16):
            draw.line([cx - 18, cy + offset, cx + 18, cy + offset], fill="white", width=2)
    elif key == "ยาวันนี้":
        draw.rounded_rectangle([cx - 32, cy - 14, cx + 32, cy + 14], radius=14,
                               outline="white", width=stroke)
        draw.rounded_rectangle([cx, cy - 14, cx + 32, cy + 14], radius=14, fill="white")
    elif key == "ความสม่ำเสมอ":
        bar, gap = 14, 6
        left = cx - int(1.5 * bar + gap)
        for index, height in enumerate([40, 55, 30]):
            bx = left + index * (bar + gap)
            draw.rectangle([bx, cy + 28 - height, bx + bar, cy + 28], fill="white")
        draw.line([left - 4, cy + 30, left + 3 * (bar + gap), cy + 30],
                  fill="white", width=2)
    elif key == "ผลเลือด":
        draw.ellipse([cx - 28, cy - 28, cx + 28, cy + 28], outline="white", width=stroke)
        draw.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], fill="white")
        draw.line([cx + 20, cy + 20, cx + 36, cy + 36], fill="white", width=stroke)
    elif key == "นัดหมาย":
        draw.rounded_rectangle([cx - 30, cy - 26, cx + 30, cy + 30], radius=6,
                               outline="white", width=stroke)
        draw.rectangle([cx - 30, cy - 26, cx + 30, cy - 12], fill="white")
        draw.line([cx - 18, cy - 36, cx - 18, cy - 18], fill="white", width=stroke)
        draw.line([cx + 18, cy - 36, cx + 18, cy - 18], fill="white", width=stroke)
        for row in (2, 16):
            for col in (-18, 0, 18):
                draw.rectangle([cx + col - 4, cy + row - 4, cx + col + 4, cy + row + 4],
                               fill="white")
    elif key == "แจ้งอาการ":
        draw.rounded_rectangle([cx - 22, cy - 30, cx + 22, cy + 34], radius=4,
                               outline="white", width=stroke)
        draw.line([cx - 22, cy - 16, cx + 22, cy - 16], fill="white", width=stroke)
        for offset in (-2, 10, 22):
            draw.line([cx - 14, cy + offset, cx + 14, cy + offset], fill="white", width=2)


def build_image() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (WIDTH, HEIGHT), "#f1f5f9")
    draw = ImageDraw.Draw(image)
    font = load_thai_font(38)
    for x, y, label, _command, color in CELLS:
        draw.rectangle([x + 6, y + 6, x + CELL_WIDTH - 6, y + CELL_HEIGHT - 6], fill=color)
        _draw_icon(draw, x, y, label)
        draw.text(
            (x + CELL_WIDTH // 2, y + 285), label, anchor="mm", font=font, fill="white"
        )
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def create_rich_menu(force: bool = False) -> tuple[bool, str]:
    """Create the rich menu and set it as default. Returns (ok, detail)."""
    global _rich_menu_id
    settings = get_settings()
    if not ls.RICHMENU_AVAILABLE:
        return False, "LINE SDK ไม่รองรับ Rich Menu ในสภาพแวดล้อมนี้"
    if not settings.line_push_enabled:
        return False, "ยังไม่ได้ตั้งค่า LINE Channel Access Token"
    if _rich_menu_id and not force:
        return True, f"ใช้เมนูเดิม ({_rich_menu_id})"
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return False, "ต้องติดตั้ง Pillow เพื่อสร้างภาพเมนู"

    try:
        request_body = ls.RichMenuRequest(
            size=ls.RichMenuSize(width=WIDTH, height=HEIGHT),
            selected=True,
            name="Warfarin Menu",
            chat_bar_text="เมนู",
            areas=[
                ls.RichMenuArea(
                    bounds=ls.RichMenuBounds(
                        x=x, y=y, width=CELL_WIDTH, height=CELL_HEIGHT
                    ),
                    action=ls.MessageAction(label=label[:20], text=command),
                )
                for x, y, label, command, _color in CELLS
            ],
        )
        client = ls.MessagingApi(
            ls.ApiClient(ls.Configuration(access_token=settings.line_channel_access_token))
        )
        response = client.create_rich_menu(rich_menu_request=request_body)
        menu_id = response.rich_menu_id

        blob = ls.MessagingApiBlob(
            ls.ApiClient(ls.Configuration(access_token=settings.line_channel_access_token))
        )
        blob.set_rich_menu_image(
            rich_menu_id=menu_id,
            body=build_image(),
            _headers={"Content-Type": "image/png"},
        )
        client.set_default_rich_menu(rich_menu_id=menu_id)
        _rich_menu_id = menu_id
        logger.info("LINE rich menu created: %s", menu_id)
        return True, menu_id
    except Exception as exc:
        logger.warning("Rich menu creation failed", exc_info=True)
        return False, str(exc)


def current_menu_id() -> str | None:
    return _rich_menu_id
