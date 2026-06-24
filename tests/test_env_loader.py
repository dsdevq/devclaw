"""Tests for the in-house .env loader (no python-dotenv dep)."""

from __future__ import annotations

import os

from devclaw._env_loader import _unquote, load_dotenv


def test_real_env_wins_over_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DEVCLAW_TEST_K=from_dotenv\n")
    monkeypatch.setenv("DEVCLAW_TEST_K", "from_shell")
    monkeypatch.chdir(tmp_path)
    load_dotenv()
    assert os.environ["DEVCLAW_TEST_K"] == "from_shell"


def test_dotenv_fills_missing_keys(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DEVCLAW_TEST_FILL=ok\n")
    monkeypatch.delenv("DEVCLAW_TEST_FILL", raising=False)
    monkeypatch.chdir(tmp_path)
    loaded = load_dotenv()
    assert loaded == tmp_path / ".env"
    assert os.environ["DEVCLAW_TEST_FILL"] == "ok"


def test_explicit_path_via_env_overrides_cwd(tmp_path, monkeypatch):
    explicit = tmp_path / "alt.env"
    explicit.write_text("DEVCLAW_TEST_EXPL=from_alt\n")
    (tmp_path / ".env").write_text("DEVCLAW_TEST_EXPL=from_cwd\n")
    monkeypatch.delenv("DEVCLAW_TEST_EXPL", raising=False)
    monkeypatch.setenv("DEVCLAW_DOTENV", str(explicit))
    monkeypatch.chdir(tmp_path)
    load_dotenv()
    assert os.environ["DEVCLAW_TEST_EXPL"] == "from_alt"


def test_missing_file_is_quiet_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEVCLAW_DOTENV", str(tmp_path / "does-not-exist.env"))
    assert load_dotenv() is None


def test_comments_and_blank_lines_skipped(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "# comment\n"
        "\n"
        "DEVCLAW_TEST_A=alpha\n"
        "  # indented comment\n"
        "DEVCLAW_TEST_B=beta\n"
    )
    monkeypatch.delenv("DEVCLAW_TEST_A", raising=False)
    monkeypatch.delenv("DEVCLAW_TEST_B", raising=False)
    monkeypatch.chdir(tmp_path)
    load_dotenv()
    assert os.environ["DEVCLAW_TEST_A"] == "alpha"
    assert os.environ["DEVCLAW_TEST_B"] == "beta"


def test_unquote_strips_paired_quotes_only():
    assert _unquote('"hello"') == "hello"
    assert _unquote("'world'") == "world"
    assert _unquote('"mismatched\'') == '"mismatched\''
    assert _unquote("plain") == "plain"


def test_unquote_strips_inline_comment_when_unquoted():
    assert _unquote("value # trailing") == "value"
    # but not inside quotes
    assert _unquote('"value # kept"') == "value # kept"


def test_malformed_line_logs_to_stderr_not_raises(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("BAD LINE WITH NO EQUALS\nDEVCLAW_TEST_OK=fine\n")
    monkeypatch.delenv("DEVCLAW_TEST_OK", raising=False)
    monkeypatch.chdir(tmp_path)
    load_dotenv()
    err = capsys.readouterr().err
    assert "ignored" in err
    assert os.environ["DEVCLAW_TEST_OK"] == "fine"
