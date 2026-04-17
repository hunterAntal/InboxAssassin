"""
Tests for per-account data file isolation — sprint 22.

Critical sections:
  CS1 — account_file_path: constructs per-account filename from basename + account_id
  CS2 — pre_filter per-account: load/write filter_config_<id>.json; no bleed to other accounts
  CS3 — log_emails per-account: email_log_<id>.json; no bleed to other accounts
  CS4 — append_run_to_buffer per-account: agent_log_buffer_<id>.txt; no bleed to other accounts
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pre_filter
import analyze_emails


# ============================================================================
# Helpers
# ============================================================================

def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))

def _read(path: Path) -> dict:
    return json.loads(path.read_text())

def _make_results(sender="spam@evil.com", priority=1):
    email = {
        "message_id": "msg-001",
        "sender": sender,
        "subject": "Test subject",
        "date": "2026-04-16",
        "body": "body text",
    }
    info = {"priority": priority, "tldr": "test", "action_required": False, "pre_filtered": False}
    return [(email, info)]


# ============================================================================
# CS1 — account_file_path
# ============================================================================

class TestAccountFilePath:

    def test_with_account_id_inserts_id_before_extension(self):
        assert pre_filter.account_file_path("filter_config.json", "gmail-personal") == \
               "filter_config_gmail-personal.json"

    def test_without_account_id_returns_basename_unchanged(self):
        assert pre_filter.account_file_path("filter_config.json", None) == "filter_config.json"

    def test_empty_string_account_id_returns_basename(self):
        assert pre_filter.account_file_path("agent_log_buffer.txt", "") == "agent_log_buffer.txt"

    def test_txt_extension_handled_correctly(self):
        assert pre_filter.account_file_path("agent_log_buffer.txt", "gmail-personal") == \
               "agent_log_buffer_gmail-personal.txt"

    def test_account_id_with_hyphens(self):
        assert pre_filter.account_file_path("email_log.json", "gmail-a-b-c") == \
               "email_log_gmail-a-b-c.json"

    def test_activity_log_path(self):
        assert pre_filter.account_file_path("activity_log.json", "gmail-work") == \
               "activity_log_gmail-work.json"


# ============================================================================
# CS2 — pre_filter per-account
# ============================================================================

class TestLearnFromResultsPerAccount:

    def test_learns_to_per_account_config(self, tmp_path, monkeypatch):
        _write(tmp_path / "filter_config_gmail-personal.json",
               {"sender_addresses": [], "sender_domains": []})
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        pre_filter.learn_from_results(_make_results(), account_id="gmail-personal")

        saved = _read(tmp_path / "filter_config_gmail-personal.json")
        assert "spam@evil.com" in saved["sender_addresses"]

    def test_does_not_touch_other_account_config(self, tmp_path, monkeypatch):
        _write(tmp_path / "filter_config_gmail-personal.json",
               {"sender_addresses": [], "sender_domains": []})
        _write(tmp_path / "filter_config_gmail-work.json",
               {"sender_addresses": ["keep@me.com"], "sender_domains": []})
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        pre_filter.learn_from_results(_make_results(), account_id="gmail-personal")

        saved_b = _read(tmp_path / "filter_config_gmail-work.json")
        assert "spam@evil.com" not in saved_b["sender_addresses"]
        assert "keep@me.com" in saved_b["sender_addresses"]

    def test_without_account_id_writes_global_config(self, tmp_path, monkeypatch):
        _write(tmp_path / "filter_config.json",
               {"sender_addresses": [], "sender_domains": []})
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        pre_filter.learn_from_results(_make_results())

        saved = _read(tmp_path / "filter_config.json")
        assert "spam@evil.com" in saved["sender_addresses"]


class TestBlockSenderPerAccount:

    def test_block_writes_to_per_account_config(self, tmp_path, monkeypatch):
        _write(tmp_path / "filter_config_gmail-personal.json",
               {"sender_addresses": [], "sender_domains": []})
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        pre_filter.block_sender("spam@evil.com", account_id="gmail-personal")

        saved = _read(tmp_path / "filter_config_gmail-personal.json")
        assert "spam@evil.com" in saved["sender_addresses"]

    def test_block_does_not_touch_global_config(self, tmp_path, monkeypatch):
        _write(tmp_path / "filter_config.json",
               {"sender_addresses": ["global@keep.com"], "sender_domains": []})
        _write(tmp_path / "filter_config_gmail-personal.json",
               {"sender_addresses": [], "sender_domains": []})
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        pre_filter.block_sender("spam@evil.com", account_id="gmail-personal")

        saved_global = _read(tmp_path / "filter_config.json")
        assert "spam@evil.com" not in saved_global["sender_addresses"]

    def test_unblock_removes_from_per_account_config(self, tmp_path, monkeypatch):
        _write(tmp_path / "filter_config_gmail-personal.json",
               {"sender_addresses": ["spam@evil.com"], "sender_domains": []})
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        pre_filter.unblock_sender("spam@evil.com", account_id="gmail-personal")

        saved = _read(tmp_path / "filter_config_gmail-personal.json")
        assert "spam@evil.com" not in saved["sender_addresses"]

    def test_unblock_does_not_touch_other_account(self, tmp_path, monkeypatch):
        _write(tmp_path / "filter_config_gmail-personal.json",
               {"sender_addresses": ["spam@evil.com"], "sender_domains": []})
        _write(tmp_path / "filter_config_gmail-work.json",
               {"sender_addresses": ["spam@evil.com"], "sender_domains": []})
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        pre_filter.unblock_sender("spam@evil.com", account_id="gmail-personal")

        saved_b = _read(tmp_path / "filter_config_gmail-work.json")
        assert "spam@evil.com" in saved_b["sender_addresses"]


# ============================================================================
# CS3 — log_emails per-account
# ============================================================================

class TestLogEmailsPerAccount:

    def test_writes_to_per_account_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        analyze_emails.log_emails(_make_results(), account_id="gmail-personal")

        path = tmp_path / "email_log_gmail-personal.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1

    def test_without_account_id_writes_to_global_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        analyze_emails.log_emails(_make_results())

        assert (tmp_path / "email_log.json").exists()

    def test_does_not_write_to_other_account_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        analyze_emails.log_emails(_make_results(), account_id="gmail-personal")

        assert not (tmp_path / "email_log_gmail-work.json").exists()


# ============================================================================
# CS4 — append_run_to_buffer per-account
# ============================================================================

class TestAppendRunToBufferPerAccount:

    def test_uses_per_account_buffer_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        analyze_emails.append_run_to_buffer("Run: test\n", account_id="gmail-personal")

        path = tmp_path / "agent_log_buffer_gmail-personal.txt"
        assert path.exists()
        assert "Run: test" in path.read_text()

    def test_without_account_id_uses_global_buffer(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        analyze_emails.append_run_to_buffer("Run: test\n")

        assert (tmp_path / "agent_log_buffer.txt").exists()

    def test_two_accounts_write_to_separate_buffers(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        analyze_emails.append_run_to_buffer("Run: personal\n", account_id="gmail-personal")
        analyze_emails.append_run_to_buffer("Run: work\n", account_id="gmail-work")

        buf_tree  = (tmp_path / "agent_log_buffer_gmail-personal.txt").read_text()
        buf_antal = (tmp_path / "agent_log_buffer_gmail-work.txt").read_text()
        assert "personal"  in buf_tree
        assert "personal"  not in buf_antal
        assert "work" in buf_antal
