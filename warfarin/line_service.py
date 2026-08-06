"""LINE Messaging API transport.

The SDK is used when installed (it gives us typed Flex objects and rich-menu
helpers), but every outbound call falls back to plain HTTPS via urllib so the
system keeps working on a host where the SDK failed to install.

No call here ever raises: a LINE outage must not break a patient confirming
a dose or a pharmacist saving a lab result.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request

from warfarin.config import get_settings

logger = logging.getLogger(__name__)

PUSH_URL = "https://api.line.me/v2/bot/message/push"
MULTICAST_URL = "https://api.line.me/v2/bot/message/multicast"
REPLY_URL = "https://api.line.me/v2/bot/message/reply"
PROFILE_URL = "https://api.line.me/v2/bot/profile/"
MAX_TEXT = 4900          # LINE hard limit is 5000 characters
MAX_MESSAGES = 5         # LINE allows at most 5 messages per request
MULTICAST_CHUNK = 150    # LINE allows at most 500 recipients; stay well under

# --- optional SDK ----------------------------------------------------------
try:  # pragma: no cover - import shape depends on the deployed environment
    from linebot.v3.messaging import (
        ApiClient,
        Configuration,
        MessagingApi,
        PushMessageRequest,
        ReplyMessageRequest,
        TextMessage,
    )

    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    SDK_AVAILABLE = False

try:  # pragma: no cover
    from linebot.v3.messaging import (
        FlexBox,
        FlexBubble,
        FlexButton,
        FlexCarousel,
        FlexMessage,
        FlexSeparator,
        FlexText,
        MessageAction,
        QuickReply,
        QuickReplyItem,
        URIAction,
    )

    FLEX_AVAILABLE = SDK_AVAILABLE
except ImportError:  # pragma: no cover
    FLEX_AVAILABLE = False

try:  # pragma: no cover
    from linebot.v3.messaging import (
        MessagingApiBlob,
        RichMenuArea,
        RichMenuBounds,
        RichMenuRequest,
        RichMenuSize,
    )

    RICHMENU_AVAILABLE = SDK_AVAILABLE
except ImportError:  # pragma: no cover
    RICHMENU_AVAILABLE = False


def access_token() -> str:
    return get_settings().line_channel_access_token


def channel_secret() -> str:
    return get_settings().line_channel_secret


def push_enabled() -> bool:
    return bool(access_token())


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------
def verify_signature(body: bytes, signature: str, secret: str | None = None) -> bool:
    """Validate X-Line-Signature (HMAC-SHA256, base64)."""
    key = secret if secret is not None else channel_secret()
    if not key or not signature:
        return False
    mac = hmac.new(key.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("ascii")
    return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------
def _post(url: str, payload: dict, timeout: int = 10) -> bool:
    token = access_token()
    if not token:
        return False
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        logger.warning("LINE API %s failed: HTTP %s %s", url, exc.code, body)
        return False
    except Exception:
        logger.warning("LINE API %s failed", url, exc_info=True)
        return False


def _sdk_client():  # pragma: no cover - requires a real token
    if not SDK_AVAILABLE or not access_token():
        return None
    try:
        return MessagingApi(ApiClient(Configuration(access_token=access_token())))
    except Exception:
        logger.warning("Could not build LINE SDK client", exc_info=True)
        return None


def _as_payload(messages: list) -> list[dict]:
    """Normalise SDK message objects / strings into raw API dicts."""
    payload = []
    for message in messages[:MAX_MESSAGES]:
        if isinstance(message, str):
            payload.append({"type": "text", "text": message[:MAX_TEXT]})
        elif hasattr(message, "to_dict"):
            try:
                payload.append(message.to_dict())
            except Exception:
                logger.warning("Could not serialise LINE message", exc_info=True)
        elif isinstance(message, dict):
            payload.append(message)
    return payload


# ---------------------------------------------------------------------------
# Public send helpers
# ---------------------------------------------------------------------------
def push_text(user_id: str, text: str) -> bool:
    """Push a single text message. Returns True when LINE accepted it."""
    if not user_id or not text:
        return False
    return _post(PUSH_URL, {
        "to": user_id,
        "messages": [{"type": "text", "text": text[:MAX_TEXT]}],
    })


def push_messages(user_id: str, messages: list) -> bool:
    payload = _as_payload(messages)
    if not user_id or not payload:
        return False
    return _post(PUSH_URL, {"to": user_id, "messages": payload})


def multicast_text(user_ids: list[str], text: str) -> tuple[int, int]:
    """Send one text to many users. Returns (delivered, failed) counts."""
    recipients = [uid for uid in dict.fromkeys(user_ids) if uid]
    if not recipients or not text:
        return 0, 0
    delivered = failed = 0
    for start in range(0, len(recipients), MULTICAST_CHUNK):
        chunk = recipients[start:start + MULTICAST_CHUNK]
        ok = _post(MULTICAST_URL, {
            "to": chunk,
            "messages": [{"type": "text", "text": text[:MAX_TEXT]}],
        })
        if ok:
            delivered += len(chunk)
        else:
            failed += len(chunk)
    return delivered, failed


def reply_text(reply_token: str, text: str) -> bool:
    if not reply_token or not text:
        return False
    return _post(REPLY_URL, {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:MAX_TEXT]}],
    })


def reply_messages(reply_token: str, messages: list) -> bool:
    payload = _as_payload(messages)
    if not reply_token or not payload:
        return False
    return _post(REPLY_URL, {"replyToken": reply_token, "messages": payload})


def fetch_display_name(user_id: str) -> str | None:
    token = access_token()
    if not token or not user_id:
        return None
    request = urllib.request.Request(
        PROFILE_URL + user_id, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8")).get("displayName")
    except Exception:
        logger.info("LINE profile fetch failed for %s", user_id[:8], exc_info=True)
        return None
