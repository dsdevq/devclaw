"""Telegram narration via OpenClaw's gateway HTTP endpoint.

The gateway runs in the same docker compose project as the orchestrator and exposes a `send-message` endpoint. Authorization is a bearer token from `OPENCLAW_GATEWAY_TOKEN`. If the token is unset, calls silently no-op so local/dev runs never crash.
"""

from __future__ import annotations

import json
import logging
import os
from urllib import request as _urlrequest
from urllib.error import URLError

logger = logging.getLogger(__name__)

GATEWAY_URL = "http://compose-openclaw-gateway-1:18789/send-message"
TOKEN_ENV = "OPENCLAW_GATEWAY_TOKEN"


def notify_telegram(chat_id: str, text: str, token: str | None = None) -> None:
    """POST a one-line message to the OpenClaw gateway's send-message endpoint.

    If no token is supplied and `OPENCLAW_GATEWAY_TOKEN` is unset, silently skip — narration is best-effort and must never crash the caller. Network/HTTP errors are logged at warning level and swallowed.
    """
    token = token or os.environ.get(TOKEN_ENV)
    if not token:
        return

    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = _urlrequest.Request(
        GATEWAY_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with _urlrequest.urlopen(req, timeout=5):
            pass
    except (URLError, OSError) as exc:
        logger.warning("notify_telegram failed: %s", exc)
