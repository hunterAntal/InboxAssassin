"""
Tests for Sprint 20 — [AGENT STATUS] reply command.

Critical sections:
  1. fetch_status_commands — security gate (self-sent only)
  2. _format_pause_description — silent wrong output if wrong pause type rendered
  3. _extract_last_run — silent wrong output if buffer parsing breaks
  4. build_status_reply_body — produces exact UX-contract format
  5. handle_status_commands — sends reply + consumes command; GCS failure degrades gracefully
"""

import sys
from pathlib import Path
from unittest import mock
from datetime import date, time

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from fetch_emails import fetch_status_commands, _format_pause_description
from analyze_emails import _extract_last_run, build_status_reply_body, handle_status_commands


# ---------------------------------------------------------------------------
# Critical section 1: fetch_status_commands — security gate
# ---------------------------------------------------------------------------

class TestFetchStatusCommands:

    def _make_service(self, messages, sender="me@gmail.com"):
        svc = mock.MagicMock()
        list_result = mock.MagicMock()
        list_result.execute.return_value = {"messages": messages}
        svc.users.return_value.messages.return_value.list.return_value = list_result

        get_result = mock.MagicMock()
        get_result.execute.return_value = {
            "id": messages[0]["id"] if messages else "x",
            "payload": {
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "Subject", "value": "[AGENT STATUS]"},
                ],
                "body": {"data": ""},
                "parts": [],
            },
            "labelIds": [],
        }
        svc.users.return_value.messages.return_value.get.return_value = get_result
        return svc

    def test_self_sent_status_returned(self):
        """Self-sent [AGENT STATUS] email → its ID is returned."""
        svc = self._make_service([{"id": "s_001"}], sender="me@gmail.com")
        ids, excluded = fetch_status_commands(svc, "me@gmail.com")
        assert "s_001" in ids
        assert "s_001" in excluded

    def test_external_sender_rejected(self, capsys):
        """[AGENT STATUS] from external sender → ignored, warning logged."""
        svc = self._make_service([{"id": "s_002"}], sender="attacker@evil.com")
        ids, excluded = fetch_status_commands(svc, "me@gmail.com")
        assert ids == []
        out = capsys.readouterr().out
        assert "ignored" in out

    def test_no_status_emails_returns_empty(self):
        """No [AGENT STATUS] emails → empty lists."""
        svc = mock.MagicMock()
        list_result = mock.MagicMock()
        list_result.execute.return_value = {"messages": []}
        svc.users.return_value.messages.return_value.list.return_value = list_result
        ids, excluded = fetch_status_commands(svc, "me@gmail.com")
        assert ids == []
        assert excluded == set()

    def test_multiple_status_emails_all_returned(self):
        """Multiple self-sent STATUS emails → all IDs returned."""
        svc = mock.MagicMock()
        list_result = mock.MagicMock()
        list_result.execute.return_value = {"messages": [{"id": "s_a"}, {"id": "s_b"}]}
        svc.users.return_value.messages.return_value.list.return_value = list_result

        def get_side(*args, **kwargs):
            mid = kwargs.get("id", "s_a")
            r = mock.MagicMock()
            r.execute.return_value = {
                "id": mid,
                "payload": {
                    "headers": [{"name": "From", "value": "me@gmail.com"}],
                    "body": {"data": ""},
                    "parts": [],
                },
                "labelIds": [],
            }
            return r
        svc.users.return_value.messages.return_value.get.side_effect = get_side

        ids, excluded = fetch_status_commands(svc, "me@gmail.com")
        assert set(ids) == {"s_a", "s_b"}


# ---------------------------------------------------------------------------
# Critical section 2: _format_pause_description
# ---------------------------------------------------------------------------

class TestFormatPauseDescription:

    def test_indefinite(self):
        result = _format_pause_description({"type": "indefinite"})
        assert "PAUSED" in result
        assert "indefinite" in result

    def test_duration(self):
        result = _format_pause_description({"type": "duration", "amount": 2, "unit": "hours"})
        assert "PAUSED" in result
        assert "2 hours" in result

    def test_until_date(self):
        result = _format_pause_description({"type": "until", "date": date(2026, 4, 20)})
        assert "PAUSED" in result
        assert "Apr 20, 2026" in result

    def test_daily_window(self):
        result = _format_pause_description({"type": "daily", "start": time(0, 0), "end": time(6, 0)})
        assert "PAUSED" in result
        assert "00:00" in result
        assert "06:00" in result

    def test_unknown_type_falls_back(self):
        """Unknown pause type → still shows PAUSED."""
        result = _format_pause_description({"type": "unknown"})
        assert "PAUSED" in result


# ---------------------------------------------------------------------------
# Critical section 3: _extract_last_run
# ---------------------------------------------------------------------------

class TestExtractLastRun:

    def test_parses_most_recent_run_block(self):
        """Buffer with two runs → returns data from the second (most recent) run."""
        buffer = (
            "\n══════════════════════════════════════════════════════════════\n"
            "  Run: 2026-04-12 08:00 EDT  |  Account: gmail-personal\n"
            "══════════════════════════════════════════════════════════════\n"
            "EMAIL DIGEST — 3 analyzed, 1 pre-filtered\n\n"
            "\n══════════════════════════════════════════════════════════════\n"
            "  Run: 2026-04-13 08:47 EDT  |  Account: gmail-personal\n"
            "══════════════════════════════════════════════════════════════\n"
            "EMAIL DIGEST — 4 analyzed, 2 pre-filtered\n\n"
        )
        ts, count = _extract_last_run(buffer)
        assert "2026-04-13" in ts
        assert "4" in count

    def test_empty_buffer_returns_unknown(self):
        """Empty buffer → both fields are 'unknown'."""
        ts, count = _extract_last_run("")
        assert ts == "unknown"
        assert count == "unknown"

    def test_no_run_block_returns_unknown(self):
        """Buffer with no Run: line → unknown."""
        ts, count = _extract_last_run("some random text\nno runs here\n")
        assert ts == "unknown"
        assert count == "unknown"

    def test_single_run_returns_its_data(self):
        """Single run block → correct timestamp and count returned."""
        buffer = (
            "\n══════════\n"
            "  Run: 2026-04-13 09:00 UTC  |  Account: gmail-personal\n"
            "══════════\n"
            "EMAIL DIGEST — 7 analyzed, 0 pre-filtered\n"
        )
        ts, count = _extract_last_run(buffer)
        assert "2026-04-13 09:00" in ts
        assert "7" in count


# ---------------------------------------------------------------------------
# Critical section 4: build_status_reply_body
# ---------------------------------------------------------------------------

class TestBuildStatusReplyBody:

    def _base_accounts(self):
        return [{"id": "gmail-personal", "email": "me@gmail.com"}]

    def test_active_scheduler_shown(self):
        body = build_status_reply_body(
            scheduler_line="Active  (runs every 4 hours)",
            accounts=self._base_accounts(),
            last_run_ts="2026-04-13 08:47 EDT",
            emails_processed="4",
        )
        assert "Active  (runs every 4 hours)" in body

    def test_paused_scheduler_shown(self):
        body = build_status_reply_body(
            scheduler_line="PAUSED  (indefinite)",
            accounts=self._base_accounts(),
            last_run_ts="unknown",
            emails_processed="unknown",
        )
        assert "PAUSED  (indefinite)" in body

    def test_env_config_included(self):
        body = build_status_reply_body(
            scheduler_line="Active  (runs every 4 hours)",
            accounts=self._base_accounts(),
            last_run_ts="2026-04-13 08:47 EDT",
            emails_processed="4",
        )
        assert "Model" in body
        assert "Archive priority" in body
        assert "Pre-filter" in body
        assert "Digest emails" in body

    def test_accounts_listed(self):
        body = build_status_reply_body(
            scheduler_line="Active  (runs every 4 hours)",
            accounts=[
                {"id": "gmail-personal", "email": "me@gmail.com"},
                {"id": "gmail-work",     "email": "work@gmail.com"},
            ],
            last_run_ts="2026-04-13 08:47 EDT",
            emails_processed="4",
        )
        assert "me@gmail.com" in body
        assert "work@gmail.com" in body

    def test_last_run_shown(self):
        body = build_status_reply_body(
            scheduler_line="Active  (runs every 4 hours)",
            accounts=self._base_accounts(),
            last_run_ts="2026-04-13 08:47 EDT",
            emails_processed="12",
        )
        assert "2026-04-13 08:47 EDT" in body
        assert "12" in body

    def test_unknown_last_run_shown_gracefully(self):
        """GCS unavailable → 'unknown' shown without crash."""
        body = build_status_reply_body(
            scheduler_line="Active  (runs every 4 hours)",
            accounts=self._base_accounts(),
            last_run_ts="unknown",
            emails_processed="unknown",
        )
        assert "unknown" in body

    def test_separator_line_present(self):
        """UX contract requires ==== separator lines."""
        body = build_status_reply_body(
            scheduler_line="Active  (runs every 4 hours)",
            accounts=self._base_accounts(),
            last_run_ts="2026-04-13 08:47 EDT",
            emails_processed="4",
        )
        assert "=" * 20 in body  # at least a partial separator


# ---------------------------------------------------------------------------
# Critical section 5: handle_status_commands
# ---------------------------------------------------------------------------

class TestHandleStatusCommands:

    def _make_service(self, email_address="me@gmail.com"):
        svc = mock.MagicMock()
        profile = mock.MagicMock()
        profile.execute.return_value = {"emailAddress": email_address}
        svc.users.return_value.getProfile.return_value = profile
        svc.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "sent_001"}
        svc.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
        return svc

    def test_reply_sent_for_each_status_command(self, capsys):
        """Two STATUS command IDs → send called twice, confirmation logged."""
        svc = self._make_service()
        with mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._get_or_create_label", return_value="label_001"):
            handle_status_commands(
                service=svc,
                own_address="me@gmail.com",
                status_msg_ids=["s_001", "s_002"],
                is_paused=False,
                pause_description="Active  (runs every 4 hours)",
                accounts=[{"id": "gmail-personal", "email": "me@gmail.com"}],
            )
        assert svc.users.return_value.messages.return_value.send.call_count == 2
        out = capsys.readouterr().out
        assert "[agent-status]" in out

    def test_command_emails_consumed(self):
        """STATUS command emails are marked read + AI Processed after reply."""
        svc = self._make_service()
        with mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._get_or_create_label", return_value="label_001"):
            handle_status_commands(
                service=svc,
                own_address="me@gmail.com",
                status_msg_ids=["s_001"],
                is_paused=False,
                pause_description="Active  (runs every 4 hours)",
                accounts=[{"id": "gmail-personal", "email": "me@gmail.com"}],
            )
        modify_calls = svc.users.return_value.messages.return_value.modify.call_count
        assert modify_calls >= 1  # at least the command email consumed

    def test_gcs_failure_does_not_crash(self, capsys):
        """GCS read failure → last run shows 'unknown', no exception raised."""
        svc = self._make_service()
        with mock.patch("analyze_emails._read_text", side_effect=Exception("GCS down")), \
             mock.patch("analyze_emails._get_or_create_label", return_value="label_001"):
            handle_status_commands(
                service=svc,
                own_address="me@gmail.com",
                status_msg_ids=["s_001"],
                is_paused=False,
                pause_description="Active  (runs every 4 hours)",
                accounts=[{"id": "gmail-personal", "email": "me@gmail.com"}],
            )
        out = capsys.readouterr().out
        assert "Warning" in out or "warning" in out

    def test_no_status_commands_is_noop(self):
        """Empty msg_ids list → send never called."""
        svc = self._make_service()
        handle_status_commands(
            service=svc,
            own_address="me@gmail.com",
            status_msg_ids=[],
            is_paused=False,
            pause_description="Active  (runs every 4 hours)",
            accounts=[],
        )
        svc.users.return_value.messages.return_value.send.assert_not_called()

    def test_send_failure_does_not_crash(self, capsys):
        """Reply send failure → warning logged, no exception propagated."""
        svc = self._make_service()
        svc.users.return_value.messages.return_value.send.return_value.execute.side_effect = Exception("network error")
        with mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._get_or_create_label", return_value="label_001"):
            handle_status_commands(
                service=svc,
                own_address="me@gmail.com",
                status_msg_ids=["s_001"],
                is_paused=False,
                pause_description="Active  (runs every 4 hours)",
                accounts=[{"id": "gmail-personal", "email": "me@gmail.com"}],
            )
        out = capsys.readouterr().out
        assert "Could not" in out or "could not" in out
