"""Tests for the Telegram narration helper.

Mocks the HTTP call — never touches the network.
"""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

from orchestrator import notify


def test_notify_telegram_skips_without_token(monkeypatch):
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    with patch.object(notify._urlrequest, "urlopen") as mock_open:
        notify.notify_telegram("chat-123", "hello")
    mock_open.assert_not_called()


def test_notify_telegram_posts_with_token(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "secret-token")
    with patch.object(notify._urlrequest, "urlopen") as mock_open:
        mock_open.return_value.__enter__ = lambda self: self
        mock_open.return_value.__exit__ = lambda self, *a: None
        notify.notify_telegram("chat-123", "🚀 dispatched abc (kind=code)")

    assert mock_open.call_count == 1
    req = mock_open.call_args.args[0]
    assert req.full_url == notify.GATEWAY_URL
    assert req.get_header("Authorization") == "Bearer secret-token"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"chat_id": "chat-123", "text": "🚀 dispatched abc (kind=code)"}


def test_notify_telegram_explicit_token_overrides_env(monkeypatch):
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    with patch.object(notify._urlrequest, "urlopen") as mock_open:
        mock_open.return_value.__enter__ = lambda self: self
        mock_open.return_value.__exit__ = lambda self, *a: None
        notify.notify_telegram("chat-9", "hi", token="explicit")

    assert mock_open.call_count == 1
    req = mock_open.call_args.args[0]
    assert req.get_header("Authorization") == "Bearer explicit"


def test_notify_telegram_swallows_http_errors(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "secret")
    from urllib.error import URLError

    with patch.object(notify._urlrequest, "urlopen", side_effect=URLError("boom")):
        notify.notify_telegram("chat-x", "msg")
