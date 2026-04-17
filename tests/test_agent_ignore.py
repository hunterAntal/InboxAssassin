"""
Tests for [AGENT IGNORE] / [AGENT UNIGNORE] commands — sprint 22.

Critical sections:
  CS1 — parse_ignore_address: strips prefix + whitespace from subject line
  CS2 — block_sender: writes to filter_config, deduplicates, returns status
  CS3 — unblock_sender: removes from filter_config, handles not-found, returns status
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pre_filter


# ============================================================================
# CS1 — parse_ignore_address
# ============================================================================

class TestParseIgnoreAddress:

    def test_extracts_address_after_ignore_prefix(self):
        assert pre_filter.parse_ignore_address(
            "[AGENT IGNORE] foo@bar.com", "[AGENT IGNORE]"
        ) == "foo@bar.com"

    def test_extracts_address_after_unignore_prefix(self):
        assert pre_filter.parse_ignore_address(
            "[AGENT UNIGNORE] foo@bar.com", "[AGENT UNIGNORE]"
        ) == "foo@bar.com"

    def test_strips_extra_whitespace(self):
        assert pre_filter.parse_ignore_address(
            "[AGENT IGNORE]   foo@bar.com  ", "[AGENT IGNORE]"
        ) == "foo@bar.com"

    def test_lowercases_address(self):
        assert pre_filter.parse_ignore_address(
            "[AGENT IGNORE] Foo@Bar.COM", "[AGENT IGNORE]"
        ) == "foo@bar.com"

    def test_returns_empty_string_when_no_address(self):
        assert pre_filter.parse_ignore_address(
            "[AGENT IGNORE]", "[AGENT IGNORE]"
        ) == ""

    def test_returns_empty_string_for_whitespace_only(self):
        assert pre_filter.parse_ignore_address(
            "[AGENT IGNORE]   ", "[AGENT IGNORE]"
        ) == ""


# ============================================================================
# CS2 — block_sender
# ============================================================================

class TestBlockSender:

    def test_adds_new_address(self, tmp_path, monkeypatch):
        cfg = {"sender_addresses": [], "sender_domains": []}
        _write_cfg(tmp_path, cfg)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        status = pre_filter.block_sender("spam@evil.com")

        saved = _read_cfg(tmp_path)
        assert "spam@evil.com" in saved["sender_addresses"]
        assert "added" in status.lower()

    def test_result_is_sorted(self, tmp_path, monkeypatch):
        cfg = {"sender_addresses": ["z@z.com"], "sender_domains": []}
        _write_cfg(tmp_path, cfg)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        pre_filter.block_sender("a@a.com")

        saved = _read_cfg(tmp_path)
        assert saved["sender_addresses"] == sorted(saved["sender_addresses"])

    def test_duplicate_not_added(self, tmp_path, monkeypatch):
        cfg = {"sender_addresses": ["spam@evil.com"], "sender_domains": []}
        _write_cfg(tmp_path, cfg)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        status = pre_filter.block_sender("spam@evil.com")

        saved = _read_cfg(tmp_path)
        assert saved["sender_addresses"].count("spam@evil.com") == 1
        assert "already" in status.lower()

    def test_returns_status_string(self, tmp_path, monkeypatch):
        cfg = {"sender_addresses": [], "sender_domains": []}
        _write_cfg(tmp_path, cfg)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        status = pre_filter.block_sender("x@x.com")
        assert isinstance(status, str)
        assert len(status) > 0


# ============================================================================
# CS3 — unblock_sender
# ============================================================================

class TestUnblockSender:

    def test_removes_existing_address(self, tmp_path, monkeypatch):
        cfg = {"sender_addresses": ["spam@evil.com"], "sender_domains": []}
        _write_cfg(tmp_path, cfg)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        status = pre_filter.unblock_sender("spam@evil.com")

        saved = _read_cfg(tmp_path)
        assert "spam@evil.com" not in saved["sender_addresses"]
        assert "removed" in status.lower()

    def test_not_found_returns_not_in_list_status(self, tmp_path, monkeypatch):
        cfg = {"sender_addresses": [], "sender_domains": []}
        _write_cfg(tmp_path, cfg)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        status = pre_filter.unblock_sender("nobody@nowhere.com")

        assert "not in" in status.lower()

    def test_not_found_does_not_corrupt_config(self, tmp_path, monkeypatch):
        cfg = {"sender_addresses": ["good@keep.com"], "sender_domains": []}
        _write_cfg(tmp_path, cfg)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        pre_filter.unblock_sender("nobody@nowhere.com")

        saved = _read_cfg(tmp_path)
        assert "good@keep.com" in saved["sender_addresses"]

    def test_result_remains_sorted(self, tmp_path, monkeypatch):
        cfg = {"sender_addresses": ["a@a.com", "b@b.com", "c@c.com"], "sender_domains": []}
        _write_cfg(tmp_path, cfg)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        pre_filter.unblock_sender("b@b.com")

        saved = _read_cfg(tmp_path)
        assert saved["sender_addresses"] == sorted(saved["sender_addresses"])


# ============================================================================
# Helpers
# ============================================================================

def _write_cfg(tmp_path: Path, cfg: dict) -> None:
    (tmp_path / "filter_config.json").write_text(json.dumps(cfg))

def _read_cfg(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "filter_config.json").read_text())
