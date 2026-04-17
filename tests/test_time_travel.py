"""
Tests for Sprint 12 — [AGENT TIMETRAVEL] command.

Critical sections:
  1. parse_time_travel_command — wrong parse = wrong emails targeted silently
  2. _iso_to_gmail_date — format conversion (Gmail uses YYYY/M/D not YYYY-MM-DD)
  3. load_time_travel_commands — sender security gate
  4. execute_time_travel — max cap, one-shot marking, label ID lookup
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from fetch_emails import parse_time_travel_command, _iso_to_gmail_date, load_time_travel_commands, execute_time_travel


# ---------------------------------------------------------------------------
# Critical section 1 & 2: parse_time_travel_command + _iso_to_gmail_date
# ---------------------------------------------------------------------------

class TestParseTimeTravelCommand:

    def test_full_command_parsed_correctly(self):
        """Happy path: all fields present and parsed."""
        body = """[AGENT TIMETRAVEL]

from: @lakeheadu.ca, @osap.gov.on.ca
after: 2023-01-01
before: 2025-01-01
apply-label: Lakehead University
max: 200
mode: label-only
"""
        result = parse_time_travel_command(body)

        assert result["from"] == ["@lakeheadu.ca", "@osap.gov.on.ca"]
        assert result["after"] == "2023-01-01"
        assert result["before"] == "2025-01-01"
        assert result["apply-label"] == "Lakehead University"
        assert result["max"] == 200
        assert result["mode"] == "label-only"

    def test_max_defaults_to_500_when_absent(self):
        """max: field is optional — defaults to 500 if not provided."""
        body = "from: @example.com\napply-label: Test\n"

        result = parse_time_travel_command(body)

        assert result["max"] == 500

    def test_mode_defaults_to_label_only_when_absent(self):
        """mode: field defaults to 'label-only' if not specified."""
        body = "from: @example.com\napply-label: Test\n"

        result = parse_time_travel_command(body)

        assert result["mode"] == "label-only"

    def test_missing_from_returns_none(self):
        """Required field: from: must be present, else command is invalid."""
        body = "apply-label: Lakehead University\n"

        result = parse_time_travel_command(body)

        assert result is None

    def test_missing_apply_label_returns_none(self):
        """Required field: apply-label: must be present, else command is invalid."""
        body = "from: @example.com\n"

        result = parse_time_travel_command(body)

        assert result is None

    def test_from_list_split_correctly(self):
        """Multiple from: values comma-separated, stripped of whitespace."""
        body = "from:  @a.com ,  @b.com  \napply-label: X\n"

        result = parse_time_travel_command(body)

        assert result["from"] == ["@a.com", "@b.com"]


class TestIsoToGmailDate:

    def test_converts_iso_to_gmail_format(self):
        """YYYY-MM-DD → YYYY/M/D (Gmail query format)."""
        assert _iso_to_gmail_date("2023-01-01") == "2023/1/1"

    def test_converts_double_digit_month_and_day(self):
        """Month and day without leading zeros when > 9."""
        assert _iso_to_gmail_date("2024-12-31") == "2024/12/31"

    def test_converts_single_digit_month(self):
        """January (01) → 1."""
        assert _iso_to_gmail_date("2021-01-15") == "2021/1/15"


# ---------------------------------------------------------------------------
# Critical section 3: load_time_travel_commands — sender security gate
# ---------------------------------------------------------------------------

class TestLoadTimeTravelCommands:

    def _make_service(self, messages, msg_headers, msg_body=""):
        svc = mock.MagicMock()
        svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": messages
        }
        svc.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "id": messages[0]["id"] if messages else "x",
            "payload": {
                "headers": msg_headers,
                "body": {"data": _encode(msg_body)},
                "parts": [],
            },
            "labelIds": [],
        }
        return svc

    def test_valid_self_sent_command_loaded(self):
        """Self-sent [AGENT TIMETRAVEL] email is loaded and parsed."""
        svc = self._make_service(
            messages=[{"id": "tt_001"}],
            msg_headers=[
                {"name": "From", "subject": "me@gmail.com"},
                {"name": "Subject", "value": "[AGENT TIMETRAVEL]"},
            ],
            msg_body="from: @example.com\napply-label: Test Label\n",
        )

        # Patch _get_header to return expected values
        with mock.patch("fetch_emails._get_header") as mock_hdr:
            mock_hdr.side_effect = lambda headers, name: {
                "From": "me@gmail.com",
                "Subject": "[AGENT TIMETRAVEL]",
            }.get(name, "")
            with mock.patch("fetch_emails._decode_body") as mock_body:
                mock_body.return_value = "from: @example.com\napply-label: Test Label\n"
                commands, excluded = load_time_travel_commands(svc, "me@gmail.com")

        assert len(commands) == 1
        assert "tt_001" in excluded

    def test_external_sender_rejected(self):
        """[AGENT TIMETRAVEL] from external sender is ignored — security gate."""
        svc = self._make_service(
            messages=[{"id": "tt_ext"}],
            msg_headers=[],
            msg_body="from: @example.com\napply-label: Test\n",
        )

        with mock.patch("fetch_emails._get_header") as mock_hdr:
            mock_hdr.side_effect = lambda headers, name: {
                "From": "attacker@evil.com",
                "Subject": "[AGENT TIMETRAVEL]",
            }.get(name, "")
            with mock.patch("fetch_emails._decode_body") as mock_body:
                mock_body.return_value = "from: @example.com\napply-label: Test\n"
                commands, excluded = load_time_travel_commands(svc, "me@gmail.com")

        assert commands == []
        assert "tt_ext" not in excluded

    def test_no_commands_returns_empty(self):
        """No [AGENT TIMETRAVEL] emails → empty list."""
        svc = mock.MagicMock()
        svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": []
        }

        commands, excluded = load_time_travel_commands(svc, "me@gmail.com")

        assert commands == []
        assert excluded == set()


# ---------------------------------------------------------------------------
# Critical section 4: execute_time_travel — max cap, one-shot, label ID lookup
# ---------------------------------------------------------------------------

class TestExecuteTimeTravel:

    def _make_service(self, search_messages):
        svc = mock.MagicMock()
        svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": search_messages
        }
        svc.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
        return svc

    def test_max_cap_respected(self, monkeypatch):
        """Only up to max: emails are labeled — no more."""
        import fetch_emails
        monkeypatch.setattr(fetch_emails, "_LABEL_ID_CACHE", {"Label_99": "Test Label"})

        svc = self._make_service(
            search_messages=[{"id": f"msg_{i}"} for i in range(10)]
        )
        command = {
            "from": ["@example.com"],
            "after": None,
            "before": None,
            "apply-label": "Test Label",
            "max": 3,
            "mode": "label-only",
            "message_id": "cmd_001",
        }

        execute_time_travel(svc, command, own_address="me@gmail.com", label_rules={})

        # modify called only 3 times for emails + 1 for marking command done = 4 total
        modify_calls = svc.users.return_value.messages.return_value.modify.call_count
        assert modify_calls == 4  # 3 emails + 1 one-shot mark

    def test_unknown_label_aborts_without_marking_done(self, monkeypatch, capsys):
        """Label not found in cache → warning, command NOT marked done (no phantom label)."""
        import fetch_emails
        monkeypatch.setattr(fetch_emails, "_LABEL_ID_CACHE", {})  # empty

        svc = self._make_service(search_messages=[{"id": "msg_001"}])
        command = {
            "from": ["@example.com"],
            "after": None,
            "before": None,
            "apply-label": "Nonexistent Label",
            "max": 500,
            "mode": "label-only",
            "message_id": "cmd_001",
        }

        execute_time_travel(svc, command, own_address="me@gmail.com", label_rules={})

        out = capsys.readouterr().out
        assert "not found" in out
        # modify should NOT have been called (no label applied, command not marked done)
        svc.users.return_value.messages.return_value.modify.assert_not_called()

    def test_successful_execution_marks_command_done(self, monkeypatch):
        """After successful labeling, the command email gets AI Processed label."""
        import fetch_emails
        monkeypatch.setattr(fetch_emails, "_LABEL_ID_CACHE", {"Label_99": "Test Label", "AI_PROC": "AI Processed"})

        svc = self._make_service(search_messages=[{"id": "msg_001"}])
        command = {
            "from": ["@example.com"],
            "after": None,
            "before": None,
            "apply-label": "Test Label",
            "max": 500,
            "mode": "label-only",
            "message_id": "cmd_001",
        }

        execute_time_travel(svc, command, own_address="me@gmail.com", label_rules={})

        # Last modify call should be on cmd_001 (marking command done)
        calls = svc.users.return_value.messages.return_value.modify.call_args_list
        last_call_kwargs = calls[-1].kwargs
        assert last_call_kwargs["id"] == "cmd_001"

    def test_zero_search_results_still_marks_done(self, monkeypatch, capsys):
        """No matching emails found → still marks command done (one-shot)."""
        import fetch_emails
        monkeypatch.setattr(fetch_emails, "_LABEL_ID_CACHE", {"Label_99": "Test Label", "AI_PROC": "AI Processed"})

        svc = self._make_service(search_messages=[])
        command = {
            "from": ["@example.com"],
            "after": None,
            "before": None,
            "apply-label": "Test Label",
            "max": 500,
            "mode": "label-only",
            "message_id": "cmd_001",
        }

        execute_time_travel(svc, command, own_address="me@gmail.com", label_rules={})

        # Should still mark command done even if 0 emails matched
        modify_calls = svc.users.return_value.messages.return_value.modify.call_count
        assert modify_calls == 1  # only the one-shot mark


def _encode(text: str) -> str:
    """Helper: base64-encode text for fake Gmail payload."""
    import base64
    return base64.urlsafe_b64encode(text.encode()).decode()
