"""
Tests for Story A (Mac local runner) — _preflight_checks() in run_all.py.

Critical sections:
  1. No short-circuit  — all checks run even when one fails
  2. Backend branching — correct checks skipped/run based on MODEL_BACKEND
  3. Per-account token — each active Gmail account's token file checked individually
  4. Ollama model name — LOCAL_MODEL env var drives the model check
"""

import sys
import json
from pathlib import Path
from unittest import mock
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_preflight(monkeypatch, *, backend="local", env_file=True,
                   credentials=True, token_files=None, accounts=None,
                   gemini_key=True, pip_ok=True,
                   ollama_installed=True, ollama_running=True,
                   ollama_models=None, local_model="gemma4:27b",
                   pull_confirmed=False, pull_exit_code=0):
    """
    Run _preflight_checks() with fine-grained control over each check.
    Returns (passed: bool | None, stdout: str, sys_exit_code: int | None).

    pull_confirmed : simulate user typing "y" (True) or "n" (False) at the
                     download prompt (sprint 21 — auto-pull on startup).
    pull_exit_code : returncode for the `ollama pull` subprocess (0 = success).
    sys_exit_code  : set in result when sys.exit() is called (user declined).
    """
    import run_all

    if accounts is None:
        accounts = [{"id": "gmail-mom", "provider": "gmail", "active": True,
                     "token_file": "token_mom.json"}]
    if token_files is None:
        token_files = {"token_mom.json"}
    if ollama_models is None:
        ollama_models = [local_model]

    monkeypatch.setenv("MODEL_BACKEND", backend)
    if backend == "gemini" and gemini_key:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    elif backend == "gemini" and not gemini_key:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    monkeypatch.setenv("LOCAL_MODEL", local_model)
    monkeypatch.setattr(run_all, "load_accounts", lambda: accounts)

    existing = set()
    if env_file:
        existing.add(".env")
    if credentials:
        existing.add("credentials.json")
    existing |= token_files

    monkeypatch.setattr("os.path.exists", lambda p: str(p) in existing)

    # pip check
    pip_result = mock.MagicMock()
    pip_result.returncode = 0 if pip_ok else 1

    # ollama --version
    ollama_ver_result = mock.MagicMock()
    ollama_ver_result.returncode = 0 if ollama_installed else 1
    ollama_ver_result.stdout = "ollama version 0.6.1\n"

    # ollama pull (sprint 21)
    pull_result = mock.MagicMock()
    pull_result.returncode = pull_exit_code

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[0] == "ollama":
            if not ollama_installed:
                raise FileNotFoundError("ollama not found")
            if len(cmd) > 1 and cmd[1] == "pull":
                return pull_result
            return ollama_ver_result
        return pip_result

    monkeypatch.setattr("subprocess.run", fake_subprocess_run)

    # Mock input() for the download confirmation prompt (sprint 21)
    monkeypatch.setattr("builtins.input", lambda _: "y" if pull_confirmed else "n")

    # urllib for Ollama running check
    if ollama_running:
        tags_data = json.dumps({"models": [{"name": m} for m in ollama_models]}).encode()

        class FakeResp:
            def read(self): return tags_data
            def __enter__(self): return self
            def __exit__(self, *a): pass

        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: FakeResp())
    else:
        monkeypatch.setattr("urllib.request.urlopen",
                            mock.Mock(side_effect=OSError("connection refused")))

    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    exited_with = None
    passed = None
    try:
        with redirect_stdout(buf):
            passed = run_all._preflight_checks()
    except SystemExit as e:
        exited_with = e.code
    return passed, buf.getvalue(), exited_with


# ---------------------------------------------------------------------------
# Critical section 1: no short-circuit — all checks run
# ---------------------------------------------------------------------------

class TestPreflightNoShortCircuit:

    def test_both_errors_shown_when_env_and_credentials_missing(self, monkeypatch):
        """Missing .env AND credentials.json → both ✗ lines appear, not just first."""
        passed, out, _ = _run_preflight(monkeypatch, env_file=False, credentials=False)
        assert not passed
        assert ".env" in out
        assert "credentials.json" in out

    def test_returns_false_when_any_check_fails(self, monkeypatch):
        """Any single failure → return False."""
        passed, out, _ = _run_preflight(monkeypatch, env_file=False)
        assert passed is False

    def test_returns_true_when_all_pass(self, monkeypatch):
        """All checks passing → return True."""
        passed, out, _ = _run_preflight(monkeypatch)
        assert passed is True


# ---------------------------------------------------------------------------
# Critical section 2: backend branching
# ---------------------------------------------------------------------------

class TestPreflightBackendBranching:

    def test_gemini_key_not_checked_in_local_mode(self, monkeypatch):
        """MODEL_BACKEND=local → GEMINI_API_KEY line does not appear in output."""
        _, out, __ = _run_preflight(monkeypatch, backend="local")
        assert "GEMINI_API_KEY" not in out

    def test_gemini_key_checked_in_gemini_mode(self, monkeypatch):
        """MODEL_BACKEND=gemini → GEMINI_API_KEY line appears in output."""
        _, out, __ = _run_preflight(monkeypatch, backend="gemini",
                                ollama_installed=False)
        assert "GEMINI_API_KEY" in out

    def test_gemini_key_failure_shown_in_gemini_mode(self, monkeypatch):
        """MODEL_BACKEND=gemini, no API key → ✗ GEMINI_API_KEY in output."""
        passed, out, _ = _run_preflight(monkeypatch, backend="gemini",
                                     gemini_key=False, ollama_installed=False)
        assert not passed
        assert "✗" in out
        assert "GEMINI_API_KEY" in out

    def test_ollama_checks_skipped_in_gemini_mode(self, monkeypatch):
        """MODEL_BACKEND=gemini → no Ollama lines in output."""
        _, out, __ = _run_preflight(monkeypatch, backend="gemini",
                                ollama_installed=False)
        assert "Ollama" not in out

    def test_ollama_checks_run_in_local_mode(self, monkeypatch):
        """MODEL_BACKEND=local → Ollama lines appear in output."""
        _, out, __ = _run_preflight(monkeypatch, backend="local")
        assert "Ollama" in out

    def test_ollama_not_installed_shows_brew_hint(self, monkeypatch):
        """Ollama not found → ✗ line + brew install hint."""
        passed, out, _ = _run_preflight(monkeypatch, ollama_installed=False)
        assert not passed
        assert "✗" in out
        assert "brew install ollama" in out

    def test_ollama_not_running_shows_serve_hint(self, monkeypatch):
        """Ollama installed but not running → ✗ line + ollama serve hint."""
        passed, out, _ = _run_preflight(monkeypatch, ollama_running=False)
        assert not passed
        assert "✗" in out
        assert "ollama serve" in out


# ---------------------------------------------------------------------------
# Critical section 3: per-account token file check
# ---------------------------------------------------------------------------

class TestPreflightPerAccountToken:

    def test_each_account_token_checked_individually(self, monkeypatch):
        """Two accounts → two token check lines, each with account id."""
        accounts = [
            {"id": "gmail-mom",  "provider": "gmail", "active": True, "token_file": "token_mom.json"},
            {"id": "gmail-work", "provider": "gmail", "active": True, "token_file": "token_work.json"},
        ]
        _, out, __ = _run_preflight(monkeypatch, accounts=accounts,
                                token_files={"token_mom.json", "token_work.json"})
        assert "gmail-mom" in out
        assert "gmail-work" in out

    def test_missing_token_shows_account_id_and_fetch_hint(self, monkeypatch):
        """Missing token file → ✗ line includes account id and skipping notice; run is not aborted."""
        # sprint 22 — warn-and-skip: missing token no longer fails the whole preflight
        accounts = [
            {"id": "gmail-mom", "provider": "gmail", "active": True, "token_file": "token_mom.json"},
        ]
        passed, out, _ = _run_preflight(monkeypatch, accounts=accounts, token_files=set())
        assert passed  # run continues for other accounts
        assert "gmail-mom" in out
        assert "skipping" in out

    def test_present_token_shows_checkmark_with_account_id(self, monkeypatch):
        """Present token file → ✓ line includes account id."""
        accounts = [
            {"id": "gmail-mom", "provider": "gmail", "active": True, "token_file": "token_mom.json"},
        ]
        _, out, __ = _run_preflight(monkeypatch, accounts=accounts,
                                token_files={"token_mom.json"})
        assert "✓" in out
        assert "gmail-mom" in out

    def test_one_missing_one_present_both_reported(self, monkeypatch):
        """One token missing, one present → both reported; run is not aborted."""
        # sprint 22 — warn-and-skip: one missing token warns but doesn't block other accounts
        accounts = [
            {"id": "gmail-mom",  "provider": "gmail", "active": True, "token_file": "token_mom.json"},
            {"id": "gmail-work", "provider": "gmail", "active": True, "token_file": "token_work.json"},
        ]
        passed, out, _ = _run_preflight(monkeypatch, accounts=accounts,
                                     token_files={"token_mom.json"})
        assert passed  # run continues for accounts with valid tokens
        assert "gmail-mom" in out
        assert "gmail-work" in out


# ---------------------------------------------------------------------------
# Critical section 4: Ollama model name from LOCAL_MODEL
# ---------------------------------------------------------------------------

class TestPreflightOllamaModel:

    def test_uses_local_model_env_var_in_check(self, monkeypatch):
        """LOCAL_MODEL=gemma4:27b → 'gemma4:27b' appears in output."""
        _, out, __ = _run_preflight(monkeypatch, local_model="gemma4:27b",
                                ollama_models=["gemma4:27b"])
        assert "gemma4:27b" in out

    # sprint 21 — auto-pull model on startup
    def test_model_not_found_user_declines_exits_cleanly(self, monkeypatch):
        """Model missing + user types n → sys.exit(0), decline message shown."""
        passed, out, exit_code = _run_preflight(
            monkeypatch, local_model="gemma4:27b",
            ollama_models=["llama3.1:latest"],
            pull_confirmed=False,
        )
        assert passed is None          # sys.exit was called, not a return
        assert exit_code == 0          # clean exit, not an error
        assert "gemma4:27b" in out
        assert "required" in out.lower()

    def test_model_not_found_user_confirms_pull_succeeds(self, monkeypatch):
        """Model missing + user confirms + pull succeeds → preflight passes."""
        passed, out, exit_code = _run_preflight(
            monkeypatch, local_model="gemma4:27b",
            ollama_models=["llama3.1:latest"],
            pull_confirmed=True, pull_exit_code=0,
        )
        assert exit_code is None       # no sys.exit
        assert passed is True
        assert "gemma4:27b" in out
        assert "pulled and ready" in out

    def test_model_not_found_user_confirms_pull_fails(self, monkeypatch):
        """Model missing + user confirms + pull fails → preflight fails."""
        passed, out, exit_code = _run_preflight(
            monkeypatch, local_model="gemma4:27b",
            ollama_models=["llama3.1:latest"],
            pull_confirmed=True, pull_exit_code=1,
        )
        assert exit_code is None       # no sys.exit
        assert passed is False
        assert "Failed to pull" in out

    def test_model_not_found_warns_about_data_usage(self, monkeypatch):
        """Model missing → warning about data/battery shown before prompt."""
        _, out, _ = _run_preflight(
            monkeypatch, local_model="gemma4:27b",
            ollama_models=["llama3.1:latest"],
            pull_confirmed=False,
        )
        assert "Wi-Fi" in out
        assert "plugged in" in out

    def test_model_found_shows_checkmark(self, monkeypatch):
        """Model present in Ollama → ✓ Model <name> available."""
        _, out, __ = _run_preflight(monkeypatch, local_model="gemma4:27b",
                                ollama_models=["gemma4:27b"])
        assert "✓" in out
        assert "gemma4:27b" in out
        assert "available" in out
