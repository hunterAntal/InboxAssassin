"""
sprint 22 — tests for [AGENT DIGEST] on-demand report command.

Critical sections:
  - fetch_digest_commands(): self-sent security gate
  - handle_digest_command(): send + buffer clear, failure safety
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import fetch_emails
import analyze_emails


# ============================================================================
# sprint 22 — fetch_digest_commands(): self-sent security gate
# ============================================================================

class TestFetchDigestCommands:

    def _make_msg(self, msg_id: str, sender: str) -> dict:
        return {
            "id": msg_id,
            "payload": {
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "Subject", "value": "[AGENT DIGEST]"},
                ]
            },
        }

    def test_self_sent_digest_returned(self):
        service = mock.MagicMock()
        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg1"}]
        }
        service.users().messages().get().execute.return_value = self._make_msg(
            "msg1", "<me@gmail.com>"
        )
        ids, excluded = fetch_emails.fetch_digest_commands(service, "me@gmail.com")
        assert "msg1" in ids
        assert "msg1" in excluded

    def test_non_self_sent_ignored(self):
        service = mock.MagicMock()
        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg1"}]
        }
        service.users().messages().get().execute.return_value = self._make_msg(
            "msg1", "<other@gmail.com>"
        )
        ids, excluded = fetch_emails.fetch_digest_commands(service, "me@gmail.com")
        assert ids == []
        assert excluded == set()

    def test_empty_inbox_returns_empty(self):
        service = mock.MagicMock()
        service.users().messages().list().execute.return_value = {"messages": []}
        ids, excluded = fetch_emails.fetch_digest_commands(service, "me@gmail.com")
        assert ids == []
        assert excluded == set()

    def test_gmail_api_error_returns_empty(self):
        service = mock.MagicMock()
        service.users().messages().list().execute.side_effect = Exception("API error")
        ids, excluded = fetch_emails.fetch_digest_commands(service, "me@gmail.com")
        assert ids == []
        assert excluded == set()


# ============================================================================
# sprint 22 — handle_digest_command(): send + buffer clear, failure safety
# ============================================================================

class TestHandleDigestCommand:

    def _make_service(self):
        svc = mock.MagicMock()
        svc.users().getProfile().execute.return_value = {"emailAddress": "me@gmail.com"}
        svc.users().messages().send().execute.return_value = {"id": "sent1"}
        svc.users().messages().modify().execute.return_value = {}
        svc.users().labels().list().execute.return_value = {"labels": []}
        svc.users().labels().create().execute.return_value = {"id": "label1"}
        return svc

    def test_buffer_sent_as_agent_report(self, tmp_path):
        buf = tmp_path / "agent_log_buffer_test.txt"
        buf.write_text("Run: batch 1\n  Archived 3 emails\n")
        svc = self._make_service()

        with mock.patch("analyze_emails._read_text", return_value=buf.read_text()), \
             mock.patch("analyze_emails._write_text") as mock_write, \
             mock.patch("analyze_emails.send_agent_report") as mock_send, \
             mock.patch("analyze_emails._consume_command") as mock_consume:
            analyze_emails.handle_digest_command(
                service=svc,
                msg_ids=["msg1"],
                account_id="test",
            )

        mock_send.assert_called_once()
        call_kwargs = mock_send.call_args
        assert "on-demand" in str(call_kwargs).lower()

    def test_buffer_cleared_after_successful_send(self, tmp_path):
        svc = self._make_service()
        written = {}

        def fake_write(path, content):
            written[path] = content

        with mock.patch("analyze_emails._read_text", return_value="some log"), \
             mock.patch("analyze_emails._write_text", side_effect=fake_write), \
             mock.patch("analyze_emails.send_agent_report"), \
             mock.patch("analyze_emails._consume_command"):
            analyze_emails.handle_digest_command(
                service=svc,
                msg_ids=["msg1"],
                account_id="test",
            )

        assert any(v == "" for v in written.values()), "Buffer should be cleared after send"

    def test_buffer_not_cleared_on_send_failure(self, tmp_path):
        svc = self._make_service()
        written = {}

        def fake_write(path, content):
            written[path] = content

        with mock.patch("analyze_emails._read_text", return_value="some log"), \
             mock.patch("analyze_emails._write_text", side_effect=fake_write), \
             mock.patch("analyze_emails.send_agent_report", side_effect=Exception("send failed")), \
             mock.patch("analyze_emails._consume_command"):
            analyze_emails.handle_digest_command(
                service=svc,
                msg_ids=["msg1"],
                account_id="test",
            )

        assert not any(v == "" for v in written.values()), "Buffer must NOT be cleared on send failure"

    def test_empty_buffer_sends_no_activity_report(self):
        svc = self._make_service()
        sent_body = {}

        def capture_send(service, period, body, date_str):
            sent_body["body"] = body

        with mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._write_text"), \
             mock.patch("analyze_emails.send_agent_report", side_effect=capture_send), \
             mock.patch("analyze_emails._consume_command"):
            analyze_emails.handle_digest_command(
                service=svc,
                msg_ids=["msg1"],
                account_id="test",
            )

        assert "no log output" in sent_body.get("body", "").lower() or \
               "no activity" in sent_body.get("body", "").lower()

    def test_command_consumed_after_send(self):
        svc = self._make_service()

        with mock.patch("analyze_emails._read_text", return_value="log data"), \
             mock.patch("analyze_emails._write_text"), \
             mock.patch("analyze_emails.send_agent_report"), \
             mock.patch("analyze_emails._consume_command") as mock_consume:
            analyze_emails.handle_digest_command(
                service=svc,
                msg_ids=["msg1"],
                account_id="test",
            )

        mock_consume.assert_called_once()
