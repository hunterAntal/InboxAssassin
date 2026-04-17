"""
sprint 22 — tests for load_accounts() auto-activation by token file presence
and _process_account() token file guard.
"""

import json
import sys
from pathlib import Path
from unittest import mock
from io import StringIO

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import run_all


# ============================================================================
# sprint 22 — load_accounts(): active:false skip message
# ============================================================================

class TestLoadAccountsActiveFlag:

    def test_active_false_account_excluded(self, tmp_path, monkeypatch, capsys):
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps([
            {"id": "gmail-work", "provider": "gmail", "active": False,
             "token_file": "token_work.json"},
        ]))
        monkeypatch.setattr(run_all, "ACCOUNTS_FILE", str(accounts_file))
        result = run_all.load_accounts()
        assert result == []

    def test_active_false_prints_skip_message(self, tmp_path, monkeypatch, capsys):
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps([
            {"id": "gmail-work", "provider": "gmail", "active": False,
             "token_file": "token_work.json"},
        ]))
        monkeypatch.setattr(run_all, "ACCOUNTS_FILE", str(accounts_file))
        run_all.load_accounts()
        out = capsys.readouterr().out
        assert "[gmail-work] active: false — skipping" in out

    def test_no_active_field_included(self, tmp_path, monkeypatch, capsys):
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps([
            {"id": "gmail-work", "provider": "gmail",
             "token_file": "token_work.json"},
        ]))
        monkeypatch.setattr(run_all, "ACCOUNTS_FILE", str(accounts_file))
        result = run_all.load_accounts()
        assert len(result) == 1
        assert result[0]["id"] == "gmail-work"

    def test_active_true_included(self, tmp_path, monkeypatch, capsys):
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps([
            {"id": "gmail-work", "provider": "gmail", "active": True,
             "token_file": "token_work.json"},
        ]))
        monkeypatch.setattr(run_all, "ACCOUNTS_FILE", str(accounts_file))
        result = run_all.load_accounts()
        assert len(result) == 1

    def test_mixed_accounts_only_active_returned(self, tmp_path, monkeypatch, capsys):
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps([
            {"id": "gmail-a", "provider": "gmail", "active": True,
             "token_file": "token_a.json"},
            {"id": "gmail-b", "provider": "gmail", "active": False,
             "token_file": "token_b.json"},
            {"id": "gmail-c", "provider": "gmail",
             "token_file": "token_c.json"},
        ]))
        monkeypatch.setattr(run_all, "ACCOUNTS_FILE", str(accounts_file))
        result = run_all.load_accounts()
        ids = [a["id"] for a in result]
        assert "gmail-a" in ids
        assert "gmail-c" in ids
        assert "gmail-b" not in ids

    def test_all_inactive_returns_empty(self, tmp_path, monkeypatch, capsys):
        accounts_file = tmp_path / "accounts.json"
        accounts_file.write_text(json.dumps([
            {"id": "gmail-a", "active": False, "token_file": "t.json"},
            {"id": "gmail-b", "active": False, "token_file": "t2.json"},
        ]))
        monkeypatch.setattr(run_all, "ACCOUNTS_FILE", str(accounts_file))
        result = run_all.load_accounts()
        assert result == []

    def test_no_accounts_file_returns_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_all, "ACCOUNTS_FILE", str(tmp_path / "missing.json"))
        result = run_all.load_accounts()
        assert len(result) == 1
        assert result[0]["provider"] == "gmail"


# ============================================================================
# sprint 22 — _process_account(): token file guard (no OAuth hang in daemon)
# ============================================================================

class TestProcessAccountTokenGuard:

    def test_missing_token_file_skips_without_calling_gmail(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCP_PROJECT", raising=False)
        monkeypatch.setenv("USE_SAMPLE_DATA", "false")

        account = {
            "id": "gmail-work",
            "provider": "gmail",
            "active": True,
            "token_file": str(tmp_path / "token_work.json"),  # does not exist
        }

        with mock.patch("run_all.get_gmail_service") as mock_svc, \
             mock.patch("run_all.cloud_init"):
            run_all._process_account(account)

        mock_svc.assert_not_called()
        out = capsys.readouterr().out
        assert "not found" in out.lower() or "skipping" in out.lower()

    def test_present_token_file_proceeds_to_auth(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCP_PROJECT", raising=False)
        monkeypatch.setenv("USE_SAMPLE_DATA", "false")

        token_file = tmp_path / "token_work.json"
        token_file.write_text('{"token": "fake"}')

        account = {
            "id": "gmail-work",
            "provider": "gmail",
            "active": True,
            "token_file": str(token_file),
        }

        mock_service = mock.MagicMock()
        with mock.patch("run_all.get_gmail_service", return_value=mock_service) as mock_svc, \
             mock.patch("run_all.cloud_init"), \
             mock.patch("run_all.get_authenticated_address", return_value="work@gmail.com"), \
             mock.patch("run_all.load_label_rules", return_value=({}, set())), \
             mock.patch("run_all.load_pause_state", return_value=(False, set())), \
             mock.patch("run_all.load_time_travel_commands", return_value=([], set())), \
             mock.patch("run_all.load_priority_overrides", return_value=([], set())), \
             mock.patch("run_all.fetch_status_commands", return_value=([], set())), \
             mock.patch("run_all.fetch_ignore_commands", return_value=([], [], set())), \
             mock.patch("run_all.archive_read_agent_reports"), \
             mock.patch("run_all.fetch_unprocessed_emails", return_value=[]), \
             mock.patch("run_all.load_digest_state", return_value={}):
            run_all._process_account(account)

        mock_svc.assert_called_once_with(str(token_file))
