"""
Tests for Sprint 16 — twice-daily Agent Report digest.

Critical sections:
  1. should_send_digest      — branching on time + state
  2. append_run_to_buffer    — mutates log buffer (GCS/local)
  3. build_digest_body       — empty buffer produces "no output" message
  4. send_agent_report       — Gmail send + label + star
  5. archive_read_agent_reports — only read (not unread) emails archived
  6. load_digest_state       — missing/corrupt → safe default
"""

import sys
import json
import base64
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from analyze_emails import (
    should_send_digest,
    append_run_to_buffer,
    build_digest_body,
    send_agent_report,
    archive_read_agent_reports,
    load_digest_state,
    save_digest_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dt(hour: int, minute: int = 0, date: str = "2026-04-12") -> datetime:
    """Return a UTC datetime for the given date and hour."""
    y, m, d = map(int, date.split("-"))
    return datetime(y, m, d, hour, minute, tzinfo=timezone.utc)


def _make_service(messages=None, label_id="label_agent_report"):
    """Return a mock Gmail service."""
    svc = mock.MagicMock()
    svc.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "me@gmail.com"
    }
    svc.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": "sent_001"
    }
    svc.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
    svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": messages or []
    }
    svc.users.return_value.labels.return_value.list.return_value.execute.return_value = {
        "labels": [{"name": "Agent Report", "id": label_id}]
    }
    return svc


# ---------------------------------------------------------------------------
# Critical section 1: should_send_digest
# ---------------------------------------------------------------------------

class TestShouldSendDigest:

    def test_first_ever_run_returns_morning_regardless_of_time(self):
        """No prior state → morning digest due immediately, even at midnight."""
        state = {"last_morning": None, "last_evening": None}
        now = _dt(hour=0)  # midnight
        assert should_send_digest(state, now, "UTC") == "morning"

    def test_morning_sent_today_returns_none_before_evening(self):
        """Morning already sent today and it's 10:00 → neither due."""
        state = {"last_morning": "2026-04-12", "last_evening": None}
        now = _dt(hour=10)
        assert should_send_digest(state, now, "UTC") is None

    def test_morning_not_sent_today_at_09_00_returns_morning(self):
        """Morning not sent today and it's exactly 09:00 → morning due."""
        state = {"last_morning": "2026-04-11", "last_evening": "2026-04-11"}
        now = _dt(hour=9)
        assert should_send_digest(state, now, "UTC") == "morning"

    def test_before_morning_threshold_returns_none(self):
        """08:59 → morning not yet due, evening not yet due."""
        state = {"last_morning": "2026-04-11", "last_evening": "2026-04-11"}
        now = _dt(hour=8, minute=59)
        assert should_send_digest(state, now, "UTC") is None

    def test_evening_due_when_time_past_threshold_and_not_sent_today(self):
        """20:01, morning sent today, evening not sent → evening due."""
        state = {"last_morning": "2026-04-12", "last_evening": "2026-04-11"}
        now = _dt(hour=20, minute=1)
        assert should_send_digest(state, now, "UTC") == "evening"

    def test_evening_not_due_before_threshold(self):
        """17:59, evening not sent → not yet due."""
        state = {"last_morning": "2026-04-12", "last_evening": "2026-04-11"}
        now = _dt(hour=17, minute=59)
        assert should_send_digest(state, now, "UTC") is None

    def test_both_sent_today_returns_none(self):
        """Both morning and evening already sent today → None."""
        state = {"last_morning": "2026-04-12", "last_evening": "2026-04-12"}
        now = _dt(hour=21)
        assert should_send_digest(state, now, "UTC") is None

    def test_timezone_respected(self):
        """15:00 UTC = 11:00 EDT — morning should be due if not sent in EDT context."""
        state = {"last_morning": "2026-04-11", "last_evening": "2026-04-11"}
        # 15:00 UTC = 11:00 America/Toronto (EDT = UTC-4)
        now = _dt(hour=15)
        result = should_send_digest(state, now, "America/Toronto")
        # 11:00 EDT >= 08:00 → morning due
        assert result == "morning"

    def test_evening_preferred_over_morning_at_20_00(self):
        """At 20:00, if morning was already sent but evening not → evening."""
        state = {"last_morning": "2026-04-12", "last_evening": "2026-04-11"}
        now = _dt(hour=20)
        assert should_send_digest(state, now, "UTC") == "evening"


# ---------------------------------------------------------------------------
# Critical section 2: append_run_to_buffer
# ---------------------------------------------------------------------------

class TestAppendRunToBuffer:

    def test_first_append_writes_content(self):
        """Empty buffer → run log written."""
        written = {}
        def read_fn(): return ""
        def write_fn(content): written["data"] = content

        append_run_to_buffer("Run output here", read_fn=read_fn, write_fn=write_fn)
        assert "Run output here" in written["data"]

    def test_second_append_accumulates(self):
        """Second call appends to existing content, not overwrites."""
        storage = {"data": "First run\n"}
        def read_fn(): return storage["data"]
        def write_fn(content): storage["data"] = content

        append_run_to_buffer("Second run\n", read_fn=read_fn, write_fn=write_fn)
        assert "First run" in storage["data"]
        assert "Second run" in storage["data"]

    def test_write_failure_prints_warning_does_not_raise(self, capsys):
        """GCS write error → warning printed, no exception."""
        def read_fn(): return ""
        def write_fn(content): raise OSError("GCS unavailable")

        append_run_to_buffer("log", read_fn=read_fn, write_fn=write_fn)  # must not raise

        out = capsys.readouterr().out
        assert "[digest]" in out
        assert "Warning" in out


# ---------------------------------------------------------------------------
# Critical section 3: build_digest_body
# ---------------------------------------------------------------------------

class TestBuildDigestBody:

    def _now(self):
        return datetime(2026, 4, 12, 8, 0, tzinfo=timezone.utc)

    def test_empty_buffer_contains_no_output_message(self):
        """Empty/whitespace buffer → body says 'No log output recorded'."""
        body = build_digest_body("", "morning", self._now())
        assert "No log output recorded" in body

    def test_empty_buffer_still_has_header(self):
        """Empty buffer digest still shows period and date in header."""
        body = build_digest_body("   ", "morning", self._now())
        assert "Morning" in body
        assert "Apr 12, 2026" in body

    def test_non_empty_buffer_included_in_body(self):
        """Non-empty buffer text appears in output."""
        body = build_digest_body("Run: 2026-04-12 00:03 UTC\nsome output\n", "evening", self._now())
        assert "some output" in body

    def test_period_capitalised_in_header(self):
        """Period word appears capitalised in the header."""
        body = build_digest_body("content", "morning", self._now())
        assert "Morning" in body

    def test_run_count_in_header(self):
        """Header shows number of runs included (count of 'Run:' occurrences)."""
        buf = "Run: 2026-04-12 00:03 UTC\noutput\nRun: 2026-04-12 04:07 UTC\noutput\n"
        body = build_digest_body(buf, "morning", self._now())
        assert "2" in body  # 2 runs


# ---------------------------------------------------------------------------
# Critical section 4: send_agent_report
# ---------------------------------------------------------------------------

class TestSendAgentReport:

    def test_gmail_send_called_once(self):
        """Gmail send() is called exactly once."""
        svc = _make_service()
        send_agent_report(svc, "morning", "body text", "Apr 12, 2026")
        svc.users.return_value.messages.return_value.send.assert_called_once()

    def test_subject_starts_with_agent_report_prefix(self):
        """Subject starts with [Agent Report]."""
        from email import message_from_bytes
        from email.header import decode_header

        svc = _make_service()
        captured = {}

        def capture_send(*args, **kwargs):
            captured["raw"] = kwargs["body"]["raw"]
            return mock.MagicMock(**{"execute.return_value": {"id": "x"}})

        svc.users.return_value.messages.return_value.send.side_effect = capture_send
        send_agent_report(svc, "morning", "body text", "Apr 12, 2026")

        raw_bytes = base64.urlsafe_b64decode(captured["raw"])
        msg = message_from_bytes(raw_bytes)
        parts = decode_header(msg["subject"])
        subject = "".join(
            p.decode(e or "utf-8") if isinstance(p, bytes) else p
            for p, e in parts
        )
        assert subject.startswith("[Agent Report]")

    def test_email_starred_after_send(self):
        """Email is starred via modify() after send."""
        svc = _make_service()
        send_agent_report(svc, "morning", "body text", "Apr 12, 2026")
        modify_call = svc.users.return_value.messages.return_value.modify.call_args
        assert "STARRED" in modify_call.kwargs["body"]["addLabelIds"]

    def test_send_failure_prints_warning_does_not_raise(self, capsys):
        """Gmail API error → warning printed, no exception raised."""
        from googleapiclient.errors import HttpError

        svc = _make_service()
        svc.users.return_value.messages.return_value.send.return_value.execute.side_effect = \
            HttpError(resp=mock.Mock(status=500), content=b"error")

        send_agent_report(svc, "morning", "body", "Apr 12, 2026")  # must not raise
        out = capsys.readouterr().out
        assert "[digest]" in out


# ---------------------------------------------------------------------------
# Critical section 5: archive_read_agent_reports
# ---------------------------------------------------------------------------

class TestArchiveReadAgentReports:

    def test_read_message_gets_inbox_removed(self):
        """A read Agent Report email (no UNREAD) → INBOX removed via modify()."""
        svc = _make_service(messages=[{"id": "msg_001"}])
        archive_read_agent_reports(svc)

        modify_call = svc.users.return_value.messages.return_value.modify.call_args
        assert "INBOX" in modify_call.kwargs["body"]["removeLabelIds"]

    def test_no_messages_means_no_modify_called(self):
        """Zero read Agent Report emails → modify() never called."""
        svc = _make_service(messages=[])
        archive_read_agent_reports(svc)
        svc.users.return_value.messages.return_value.modify.assert_not_called()

    def test_query_excludes_unread(self):
        """The Gmail search query includes -is:unread to skip unread messages."""
        svc = _make_service(messages=[])
        archive_read_agent_reports(svc)
        list_call = svc.users.return_value.messages.return_value.list.call_args
        query = list_call.kwargs.get("q", "") or list_call.args[0] if list_call.args else ""
        # Check via kwargs
        query = svc.users.return_value.messages.return_value.list.call_args.kwargs.get("q", "")
        assert "unread" in query.lower()

    def test_multiple_messages_all_archived(self):
        """Multiple read Agent Report emails → each gets modify() call."""
        svc = _make_service(messages=[{"id": "a"}, {"id": "b"}, {"id": "c"}])
        archive_read_agent_reports(svc)
        assert svc.users.return_value.messages.return_value.modify.call_count == 3

    def test_gmail_error_prints_warning_does_not_raise(self, capsys):
        """Gmail API error in archive sweep → warning printed, no exception."""
        from googleapiclient.errors import HttpError

        svc = _make_service()
        svc.users.return_value.messages.return_value.list.return_value.execute.side_effect = \
            HttpError(resp=mock.Mock(status=403), content=b"forbidden")

        archive_read_agent_reports(svc)  # must not raise
        out = capsys.readouterr().out
        assert "[digest]" in out


# ---------------------------------------------------------------------------
# Critical section 6: load_digest_state
# ---------------------------------------------------------------------------

class TestLoadDigestState:

    def test_missing_file_returns_safe_default(self, tmp_path, monkeypatch):
        """No state file → {'last_morning': None, 'last_evening': None}."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        state = load_digest_state()
        assert state == {"last_morning": None, "last_evening": None}

    def test_valid_state_file_returns_contents(self, tmp_path, monkeypatch):
        """Valid JSON state file → contents returned."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        data = {"last_morning": "2026-04-12", "last_evening": "2026-04-11"}
        (tmp_path / "agent_digest_state.json").write_text(json.dumps(data))
        state = load_digest_state()
        assert state["last_morning"] == "2026-04-12"
        assert state["last_evening"] == "2026-04-11"

    def test_corrupt_file_returns_safe_default(self, tmp_path, monkeypatch):
        """Corrupt JSON → safe default, no exception raised."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        (tmp_path / "agent_digest_state.json").write_text("{corrupt json{{")
        state = load_digest_state()
        assert state == {"last_morning": None, "last_evening": None}


# ---------------------------------------------------------------------------
# Sprint 19 — digest threshold changes (9:00 / 18:00)
# ---------------------------------------------------------------------------

class TestDigestThresholdsSprint19:

    def test_morning_not_due_at_08_59(self):
        """08:59 is before the 09:00 morning threshold — no digest due."""
        state = {"last_morning": "2026-04-11", "last_evening": "2026-04-11"}
        now = _dt(hour=8, minute=59)
        assert should_send_digest(state, now, "UTC") is None

    def test_morning_due_at_09_00(self):
        """09:00 exactly meets the morning threshold — morning digest due."""
        state = {"last_morning": "2026-04-11", "last_evening": "2026-04-11"}
        now = _dt(hour=9, minute=0)
        assert should_send_digest(state, now, "UTC") == "morning"

    def test_evening_due_at_18_00(self):
        """18:00 meets the evening threshold — evening digest due."""
        state = {"last_morning": "2026-04-12", "last_evening": "2026-04-11"}
        now = _dt(hour=18, minute=0)
        assert should_send_digest(state, now, "UTC") == "evening"

    def test_evening_due_at_19_59(self):
        """19:59 is past the 18:00 evening threshold — evening digest due."""
        state = {"last_morning": "2026-04-12", "last_evening": "2026-04-11"}
        now = _dt(hour=19, minute=59)
        assert should_send_digest(state, now, "UTC") == "evening"


# ---------------------------------------------------------------------------
# Sprint 19 — per-account state file isolation
# ---------------------------------------------------------------------------

class TestDigestStatePerAccount:

    def test_load_reads_from_specified_state_file(self, tmp_path, monkeypatch):
        """load_digest_state uses the state_file argument, not the default filename."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        custom_file = "agent_digest_state_gmail-personal.json"
        data = {"last_morning": "2026-04-12", "last_evening": None}
        (tmp_path / custom_file).write_text(json.dumps(data))
        state = load_digest_state(custom_file)
        assert state["last_morning"] == "2026-04-12"

    def test_load_default_file_not_used_when_custom_specified(self, tmp_path, monkeypatch):
        """When custom state_file given, default file is NOT read even if it exists."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        (tmp_path / "agent_digest_state.json").write_text(
            json.dumps({"last_morning": "2026-04-12", "last_evening": "2026-04-12"})
        )
        # Custom file doesn't exist → safe default returned (not the default file's contents)
        state = load_digest_state("agent_digest_state_gmail-work.json")
        assert state == {"last_morning": None, "last_evening": None}

    def test_save_writes_to_specified_state_file(self, tmp_path, monkeypatch):
        """save_digest_state writes to the state_file argument, not the default filename."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        custom_file = "agent_digest_state_gmail-work.json"
        save_digest_state({"last_morning": "2026-04-12", "last_evening": None}, custom_file)
        written = json.loads((tmp_path / custom_file).read_text())
        assert written["last_morning"] == "2026-04-12"

    def test_save_does_not_write_to_default_file_when_custom_specified(self, tmp_path, monkeypatch):
        """When custom state_file given, default file is NOT created or modified."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)
        custom_file = "agent_digest_state_gmail-personal.json"
        save_digest_state({"last_morning": "2026-04-12", "last_evening": None}, custom_file)
        assert not (tmp_path / "agent_digest_state.json").exists()

    def test_two_accounts_state_files_are_independent(self, tmp_path, monkeypatch):
        """personal's state showing morning sent today does NOT block work."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCS_BUCKET", raising=False)

        personal_file = "agent_digest_state_gmail-personal.json"
        work_file = "agent_digest_state_gmail-work.json"

        # personal already sent morning today
        (tmp_path / personal_file).write_text(
            json.dumps({"last_morning": "2026-04-12", "last_evening": None})
        )
        # work has never sent a digest
        # (file does not exist)

        personal_state = load_digest_state(personal_file)
        work_state = load_digest_state(work_file)

        now = _dt(hour=10)  # 10:00 UTC, past the 09:00 threshold
        assert should_send_digest(personal_state, now, "UTC") is None      # already sent
        assert should_send_digest(work_state, now, "UTC") == "morning" # due
