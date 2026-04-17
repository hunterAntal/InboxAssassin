"""Tests for setup_helper.py — the testable logic backing setup.sh."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import setup_helper


# ============================================================================
# check_python_version
# ============================================================================

class TestCheckPythonVersion:
    def test_current_interpreter_passes(self):
        """The interpreter running pytest is ≥ 3.10 — should pass."""
        ok, version_str = setup_helper.check_python_version()
        assert ok is True
        assert version_str.startswith("3.")

    def test_returns_dotted_version_string(self):
        ok, version_str = setup_helper.check_python_version()
        parts = version_str.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_old_version_fails(self, monkeypatch):
        monkeypatch.setattr(sys, "version_info", (3, 9, 7))
        ok, version_str = setup_helper.check_python_version()
        assert ok is False
        assert version_str == "3.9.7"

    def test_minimum_accepted_version(self, monkeypatch):
        monkeypatch.setattr(sys, "version_info", (3, 10, 0))
        ok, _ = setup_helper.check_python_version()
        assert ok is True


# ============================================================================
# validate_backend
# ============================================================================

class TestValidateBackend:
    def test_choice_1_valid(self):
        assert setup_helper.validate_backend("1") is True

    def test_choice_2_valid(self):
        assert setup_helper.validate_backend("2") is True

    def test_empty_string_invalid(self):
        assert setup_helper.validate_backend("") is False

    def test_other_string_invalid(self):
        assert setup_helper.validate_backend("3") is False
        assert setup_helper.validate_backend("gemini") is False
        assert setup_helper.validate_backend("local") is False


# ============================================================================
# validate_api_key
# ============================================================================

class TestValidateApiKey:
    def test_non_empty_key_valid(self):
        assert setup_helper.validate_api_key("AIzaSyABC123") is True

    def test_empty_string_invalid(self):
        assert setup_helper.validate_api_key("") is False

    def test_whitespace_only_invalid(self):
        assert setup_helper.validate_api_key("   ") is False


# ============================================================================
# build_env_content
# ============================================================================

class TestBuildEnvContent:
    def test_gemini_backend_sets_model_backend(self):
        content = setup_helper.build_env_content(backend="gemini", gemini_key="mykey")
        assert "MODEL_BACKEND=gemini" in content

    def test_gemini_backend_writes_api_key(self):
        content = setup_helper.build_env_content(backend="gemini", gemini_key="mykey")
        assert "GEMINI_API_KEY=mykey" in content

    def test_local_backend_sets_model_backend(self):
        content = setup_helper.build_env_content(backend="local", local_model="gemma3:4b")
        assert "MODEL_BACKEND=local" in content

    def test_local_backend_writes_local_model(self):
        content = setup_helper.build_env_content(backend="local", local_model="llama3.1:latest")
        assert "LOCAL_MODEL=llama3.1:latest" in content

    def test_local_backend_uses_default_model(self):
        content = setup_helper.build_env_content(backend="local")
        assert "LOCAL_MODEL=gemma3:4b" in content

    def test_gemini_backend_omits_local_model(self):
        content = setup_helper.build_env_content(backend="gemini", gemini_key="mykey")
        assert "LOCAL_MODEL" not in content

    def test_local_backend_omits_gemini_key(self):
        content = setup_helper.build_env_content(backend="local", local_model="gemma3:4b")
        assert "GEMINI_API_KEY" not in content

    def test_send_digest_false_by_default(self):
        """Local users should not get digest emails during setup by default."""
        content = setup_helper.build_env_content(backend="gemini", gemini_key="k")
        assert "SEND_DIGEST=false" in content

    def test_content_is_string(self):
        content = setup_helper.build_env_content(backend="gemini", gemini_key="k")
        assert isinstance(content, str)


# ============================================================================
# File presence checks
# ============================================================================

class TestFileChecks:
    def test_credentials_file_found(self, tmp_path):
        f = tmp_path / "credentials.json"
        f.write_text("{}")
        assert setup_helper.credentials_file_exists(str(tmp_path)) is True

    def test_credentials_file_missing(self, tmp_path):
        assert setup_helper.credentials_file_exists(str(tmp_path)) is False

    def test_token_file_found(self, tmp_path):
        f = tmp_path / "token.json"
        f.write_text("{}")
        assert setup_helper.token_file_exists(str(tmp_path)) is True

    def test_token_file_missing(self, tmp_path):
        assert setup_helper.token_file_exists(str(tmp_path)) is False

    def test_env_file_found(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("MODEL_BACKEND=gemini\n")
        assert setup_helper.env_file_exists(str(tmp_path)) is True

    def test_env_file_missing(self, tmp_path):
        assert setup_helper.env_file_exists(str(tmp_path)) is False


# ============================================================================
# interpret_smoke_result
# ============================================================================

class TestInterpretSmokeResult:
    def test_exit_0_passes(self):
        passed, msg = setup_helper.interpret_smoke_result(0)
        assert passed is True
        assert "passed" in msg.lower()

    def test_nonzero_exit_fails(self):
        passed, msg = setup_helper.interpret_smoke_result(1)
        assert passed is False
        assert "failed" in msg.lower()

    def test_any_nonzero_fails(self):
        for code in (1, 2, 127):
            passed, _ = setup_helper.interpret_smoke_result(code)
            assert passed is False


# ============================================================================
# should_pull  (sprint 21 — auto-pull model on startup)
# ============================================================================

class TestShouldPull:
    """Parse the Y/n/Enter confirmation before ollama pull."""

    def test_empty_string_means_yes(self):
        # User just pressed Enter — default is Y
        assert setup_helper.should_pull("") is True

    def test_y_means_yes(self):
        assert setup_helper.should_pull("y") is True

    def test_capital_y_means_yes(self):
        assert setup_helper.should_pull("Y") is True

    def test_yes_means_yes(self):
        assert setup_helper.should_pull("yes") is True

    def test_n_means_no(self):
        assert setup_helper.should_pull("n") is False

    def test_capital_n_means_no(self):
        assert setup_helper.should_pull("N") is False

    def test_no_means_no(self):
        assert setup_helper.should_pull("no") is False

    def test_whitespace_stripped(self):
        assert setup_helper.should_pull("  y  ") is True
        assert setup_helper.should_pull("  n  ") is False


# ============================================================================
# interpret_pull_result  (sprint 21 — auto-pull model on startup)
# ============================================================================

class TestInterpretPullResult:
    """Map ollama pull exit code to (success, message)."""

    def test_exit_0_is_success(self):
        ok, msg = setup_helper.interpret_pull_result(0, "gemma3:4b")
        assert ok is True
        assert "gemma3:4b" in msg
        assert "pulled and ready" in msg

    def test_nonzero_is_failure(self):
        ok, msg = setup_helper.interpret_pull_result(1, "gemma3:4b")
        assert ok is False
        assert "gemma3:4b" in msg
        assert "Failed to pull" in msg

    def test_any_nonzero_is_failure(self):
        for code in (1, 2, 127):
            ok, _ = setup_helper.interpret_pull_result(code, "llama3.1:latest")
            assert ok is False

    def test_tagged_model_name_preserved(self):
        ok, msg = setup_helper.interpret_pull_result(0, "llama3.1:latest")
        assert "llama3.1:latest" in msg


# ============================================================================
# sprint 22 — should_install_daemon
# ============================================================================

class TestShouldInstallDaemon:

    def test_empty_string_defaults_to_yes(self):
        assert setup_helper.should_install_daemon("") is True

    def test_y_is_yes(self):
        assert setup_helper.should_install_daemon("y") is True

    def test_uppercase_Y_is_yes(self):
        assert setup_helper.should_install_daemon("Y") is True

    def test_yes_is_yes(self):
        assert setup_helper.should_install_daemon("yes") is True

    def test_n_is_no(self):
        assert setup_helper.should_install_daemon("n") is False

    def test_uppercase_N_is_no(self):
        assert setup_helper.should_install_daemon("N") is False

    def test_no_is_no(self):
        assert setup_helper.should_install_daemon("no") is False

    def test_whitespace_trimmed_before_check(self):
        assert setup_helper.should_install_daemon("  y  ") is True
        assert setup_helper.should_install_daemon("  n  ") is False


# ============================================================================
# sprint 22 — validate_account_alias (multi-account OAuth)
# ============================================================================

class TestValidateAccountAlias:

    def test_simple_alias_valid(self):
        ok, normalised, _ = setup_helper.validate_account_alias("work")
        assert ok is True
        assert normalised == "work"

    def test_alias_with_numbers_valid(self):
        ok, normalised, _ = setup_helper.validate_account_alias("account2")
        assert ok is True
        assert normalised == "account2"

    def test_alias_with_hyphen_valid(self):
        ok, normalised, _ = setup_helper.validate_account_alias("mom-biz")
        assert ok is True
        assert normalised == "mom-biz"

    def test_uppercase_normalised_to_lowercase(self):
        ok, normalised, _ = setup_helper.validate_account_alias("Work")
        assert ok is True
        assert normalised == "work"

    def test_empty_string_invalid(self):
        ok, _, msg = setup_helper.validate_account_alias("")
        assert ok is False
        assert "empty" in msg.lower()

    def test_space_in_alias_invalid(self):
        ok, _, msg = setup_helper.validate_account_alias("work account")
        assert ok is False
        assert "letters" in msg.lower() or "hyphens" in msg.lower()

    def test_at_sign_invalid(self):
        ok, _, msg = setup_helper.validate_account_alias("work@home")
        assert ok is False

    def test_underscore_invalid(self):
        ok, _, msg = setup_helper.validate_account_alias("work_home")
        assert ok is False


# ============================================================================
# sprint 22 — build_account_entry (multi-account OAuth)
# ============================================================================

class TestBuildAccountEntry:

    def test_id_prefixed_with_gmail(self):
        entry = setup_helper.build_account_entry("work")
        assert entry["id"] == "gmail-work"

    def test_provider_is_gmail(self):
        entry = setup_helper.build_account_entry("work")
        assert entry["provider"] == "gmail"

    def test_active_is_true(self):
        entry = setup_helper.build_account_entry("work")
        assert entry["active"] is True

    def test_token_file_uses_alias(self):
        entry = setup_helper.build_account_entry("work")
        assert entry["token_file"] == "token_work.json"

    def test_token_secret_empty_for_local_setup(self):
        entry = setup_helper.build_account_entry("work")
        assert entry["token_secret"] == ""

    def test_credentials_secret_empty_for_local_setup(self):
        entry = setup_helper.build_account_entry("work")
        assert entry["credentials_secret"] == ""


# ============================================================================
# sprint 22 — add_account_to_json (multi-account OAuth)
# ============================================================================

class TestAddAccountToJson:

    def test_adds_entry_to_existing_array(self, tmp_path):
        accounts_path = tmp_path / "accounts.json"
        accounts_path.write_text('[{"id": "gmail-existing", "provider": "gmail", "active": true, "token_file": "token.json", "token_secret": "", "credentials_secret": ""}]')
        ok, _ = setup_helper.add_account_to_json(str(accounts_path), "work")
        assert ok is True
        import json
        data = json.loads(accounts_path.read_text())
        assert len(data) == 2
        assert data[-1]["id"] == "gmail-work"

    def test_creates_file_if_not_exists(self, tmp_path):
        accounts_path = tmp_path / "accounts.json"
        ok, _ = setup_helper.add_account_to_json(str(accounts_path), "work")
        assert ok is True
        import json
        data = json.loads(accounts_path.read_text())
        assert len(data) == 1
        assert data[0]["id"] == "gmail-work"

    def test_rejects_duplicate_alias(self, tmp_path):
        accounts_path = tmp_path / "accounts.json"
        accounts_path.write_text('[{"id": "gmail-work", "provider": "gmail", "active": true, "token_file": "token_work.json", "token_secret": "", "credentials_secret": ""}]')
        ok, msg = setup_helper.add_account_to_json(str(accounts_path), "work")
        assert ok is False
        assert "gmail-work" in msg
        assert "already exists" in msg

    def test_entry_schema_is_correct(self, tmp_path):
        accounts_path = tmp_path / "accounts.json"
        accounts_path.write_text("[]")
        setup_helper.add_account_to_json(str(accounts_path), "mombiz")
        import json
        data = json.loads(accounts_path.read_text())
        entry = data[0]
        assert entry["id"] == "gmail-mombiz"
        assert entry["token_file"] == "token_mombiz.json"
        assert entry["provider"] == "gmail"
        assert entry["active"] is True
        assert "token_secret" in entry
        assert "credentials_secret" in entry

    def test_write_failure_returns_false(self, tmp_path):
        # Make the path a directory so open() fails
        bad_path = tmp_path / "accounts.json"
        bad_path.mkdir()
        ok, msg = setup_helper.add_account_to_json(str(bad_path), "work")
        assert ok is False
