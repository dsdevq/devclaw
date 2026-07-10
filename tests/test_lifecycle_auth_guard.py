"""Unit tests for ``lifecycle.auth_startup_error`` — the fail-closed startup
guard that refuses to serve the HTTP transport unauthenticated on a
non-loopback bind (T0.9). Before this guard the shipped default posture
(``DEVCLAW_TRANSPORT=http`` + default host ``0.0.0.0`` + no ``DEVCLAW_TOKEN``)
exposed every route — including mutating ones like ``/prs/merge`` and
``/control/pause`` — on all interfaces with no auth.

Pure-function tests only; no server spin-up. Importing
``devclaw.server.lifecycle`` pulls in ``devclaw.server._state`` (module-level
services against the default ``DEVCLAW_DB``) — the same import posture the
suite already takes via ``devclaw.server.http`` in
``tests/test_console_prs_endpoint.py``.
"""

from __future__ import annotations

import pytest

from devclaw.server.lifecycle import auth_startup_error

# ── stdio: never errors, regardless of host/token ──────────────────────────


@pytest.mark.parametrize("host", ["0.0.0.0", "127.0.0.1", "100.64.0.5", "::", ""])
@pytest.mark.parametrize("token", [None, ""])
def test_stdio_never_errors(host, token):
    assert auth_startup_error("stdio", host, token) is None


# ── http + loopback + no token: local dev keeps working ────────────────────


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "::1", "[::1]", "LOCALHOST", "127.0.0.2", " 127.0.0.1 "],
)
def test_http_loopback_without_token_is_allowed(host):
    assert auth_startup_error("http", host, None) is None
    assert auth_startup_error("http", host, "") is None


# ── http + non-loopback + no token: refuse, with an actionable message ─────


@pytest.mark.parametrize("host", ["0.0.0.0", "100.64.0.5", "192.168.1.10", "::"])
@pytest.mark.parametrize("token", [None, ""])
def test_http_nonloopback_without_token_refuses(host, token):
    msg = auth_startup_error("http", host, token)
    assert msg is not None
    # The message must tell the operator exactly what to do.
    assert "DEVCLAW_TOKEN" in msg
    assert "DEVCLAW_HOST=127.0.0.1" in msg
    assert host in msg


# ── http + token set: any bind is fine, token-set deployments untouched ────


@pytest.mark.parametrize("host", ["0.0.0.0", "100.64.0.5", "127.0.0.1"])
def test_http_with_token_is_allowed_on_any_host(host):
    assert auth_startup_error("http", host, "s3cret") is None
