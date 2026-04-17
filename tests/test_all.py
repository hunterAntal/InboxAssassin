"""Comprehensive unit tests for the email agent project."""

import pytest
import json
import os
import sys
import base64
from datetime import datetime
from unittest import mock
from pathlib import Path

# Import the modules to test
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from fetch_emails import _get_header, _decode_body, fetch_unprocessed_emails
from analyze_emails import (
    _normalize_time,
    _parse_response,
    _calendar_event_fields,
    _read_json,
    _write_json,
    _append_to_json_file,
    log_emails,
    log_activity,
    analyze_emails,
    create_entries,
    manage_inbox,
    _get_or_create_label,
    _event_exists,
    _task_exists,
)
from run_all import cloud_init


# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def tmp_path_env(tmp_path, monkeypatch):
    """Fixture that sets up a temporary directory and clears GCS_BUCKET env var."""
    monkeypatch.setenv("GCS_BUCKET", "")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def mock_gmail_service():
    """Fixture providing a mock Gmail API service."""
    service = mock.MagicMock()
    return service


@pytest.fixture
def sample_email():
    """Fixture providing a sample email dict."""
    return {
        "message_id": "msg_123",
        "sender": "alice@example.com",
        "subject": "Meeting tomorrow at 2 PM",
        "date": "2026-03-25T10:00:00Z",
        "snippet": "Let's meet to discuss the project.",
        "body": "Let's meet to discuss the project.\n\nTime: 2:00 PM\nLocation: Conference Room A",
    }


@pytest.fixture
def sample_analysis():
    """Fixture providing a sample analysis result."""
    return {
        "priority": 4,
        "tldr": "Meeting scheduled for tomorrow at 2 PM to discuss the project.",
        "action_required": True,
        "event_date": "2026-03-26",
        "event_time": "14:00",
        "event_title": "Project Discussion",
        "action_type": "event",
    }


# ============================================================================
# Tests for fetch_emails.py
# ============================================================================


class TestGetHeader:
    """Tests for _get_header function."""

    def test_get_header_found(self):
        """Test finding a header with exact case."""
        headers = [
            {"name": "From", "value": "alice@example.com"},
            {"name": "Subject", "value": "Test"},
        ]
        result = _get_header(headers, "From")
        assert result == "alice@example.com"

    def test_get_header_case_insensitive(self):
        """Test finding a header with different case."""
        headers = [
            {"name": "From", "value": "alice@example.com"},
        ]
        result = _get_header(headers, "from")
        assert result == "alice@example.com"

    def test_get_header_not_found(self):
        """Test when header is not present."""
        headers = [
            {"name": "Subject", "value": "Test"},
        ]
        result = _get_header(headers, "From")
        assert result == ""

    def test_get_header_empty_list(self):
        """Test with empty header list."""
        result = _get_header([], "From")
        assert result == ""


class TestDecodeBody:
    """Tests for _decode_body function."""

    def test_decode_body_single_part(self):
        """Test decoding a single-part email with body data."""
        text = "This is the email body"
        encoded = base64.urlsafe_b64encode(text.encode()).decode()
        payload = {
            "body": {"data": encoded}
        }
        result = _decode_body(payload)
        assert result == text

    def test_decode_body_multipart(self):
        """Test decoding a multipart email with text/plain part."""
        text = "This is the plain text part"
        encoded = base64.urlsafe_b64encode(text.encode()).decode()
        payload = {
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": ""}
                },
                {
                    "mimeType": "text/plain",
                    "body": {"data": encoded}
                }
            ]
        }
        result = _decode_body(payload)
        assert result == text

    def test_decode_body_no_data(self):
        """Test when payload has no body data."""
        payload = {"body": {}}
        result = _decode_body(payload)
        assert result == ""

    def test_decode_body_empty_payload(self):
        """Test with empty payload."""
        result = _decode_body({})
        assert result == ""

    def test_decode_body_empty_parts(self):
        """Test with parts but no matching text/plain."""
        payload = {
            "parts": [
                {"mimeType": "text/html", "body": {"data": ""}},
            ]
        }
        result = _decode_body(payload)
        assert result == ""


class TestFetchUnprocessedEmails:
    """Tests for fetch_unprocessed_emails function."""

    def test_fetch_unprocessed_emails_with_sample_data(self, monkeypatch, tmp_path_env):
        """Test fetching from sample file when USE_SAMPLE_DATA=true."""
        monkeypatch.setenv("USE_SAMPLE_DATA", "true")

        sample_data = [
            {
                "message_id": "msg_001",
                "sender": "test@example.com",
                "subject": "Test",
                "date": "2026-03-25T10:00:00Z",
                "snippet": "Test snippet",
                "body": "Test body",
            }
        ]

        with open("sample_emails.json", "w") as f:
            json.dump(sample_data, f)

        service = mock.MagicMock()
        result = fetch_unprocessed_emails(service)

        assert result == sample_data
        # Verify API was not called
        service.users().messages().list.assert_not_called()

    def test_fetch_unprocessed_emails_no_messages(self, mock_gmail_service, monkeypatch):
        """Test when no processable messages are found."""
        monkeypatch.setenv("USE_SAMPLE_DATA", "false")

        mock_gmail_service.users().messages().list().execute.return_value = {
            "messages": []
        }

        result = fetch_unprocessed_emails(mock_gmail_service)
        assert result == []

    def test_fetch_unprocessed_emails_one_message(self, mock_gmail_service, monkeypatch):
        """Test fetching a single processable email."""
        monkeypatch.setenv("USE_SAMPLE_DATA", "false")

        text = "Test email body"
        encoded = base64.urlsafe_b64encode(text.encode()).decode()

        mock_gmail_service.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg_123"}]
        }
        mock_gmail_service.users().messages().get().execute.return_value = {
            "payload": {
                "headers": [
                    {"name": "From", "value": "alice@example.com"},
                    {"name": "Subject", "value": "Test Subject"},
                    {"name": "Date", "value": "2026-03-25T10:00:00Z"},
                ],
                "body": {"data": encoded}
            },
            "snippet": "fallback"
        }

        result = fetch_unprocessed_emails(mock_gmail_service)

        assert len(result) == 1
        assert result[0]["message_id"] == "msg_123"
        assert result[0]["sender"] == "alice@example.com"
        assert result[0]["subject"] == "Test Subject"
        assert result[0]["body"] == text

    def test_fetch_unprocessed_emails_uses_label_query(self, mock_gmail_service, monkeypatch):
        """Test that the fetch uses q= to exclude AI Processed, not labelIds UNREAD."""
        monkeypatch.setenv("USE_SAMPLE_DATA", "false")

        mock_gmail_service.users().messages().list().execute.return_value = {"messages": []}

        fetch_unprocessed_emails(mock_gmail_service)

        call_kwargs = mock_gmail_service.users().messages().list.call_args
        assert "q" in call_kwargs.kwargs
        assert "in:all" in call_kwargs.kwargs["q"]
        assert "AI Processed" in call_kwargs.kwargs["q"]
        assert "labelIds" not in call_kwargs.kwargs


# ============================================================================
# Tests for analyze_emails.py
# ============================================================================


class TestNormalizeTime:
    """Tests for _normalize_time function."""

    def test_normalize_time_single_digit_hour(self):
        """Test normalizing time with single-digit hour."""
        result = _normalize_time("9:00")
        assert result == "09:00"

    def test_normalize_time_already_normalized(self):
        """Test time already in correct format."""
        result = _normalize_time("09:00")
        assert result == "09:00"

    def test_normalize_time_afternoon(self):
        """Test normalizing afternoon time."""
        result = _normalize_time("14:30")
        assert result == "14:30"

    def test_normalize_time_none_input(self):
        """Test with None input."""
        result = _normalize_time(None)
        assert result is None

    def test_normalize_time_empty_string(self):
        """Test with empty string."""
        result = _normalize_time("")
        assert result is None

    def test_normalize_time_invalid_format(self):
        """Test with invalid time format."""
        result = _normalize_time("bad")
        assert result is None

    def test_normalize_time_single_digit_minute(self):
        """Test with single-digit minute."""
        result = _normalize_time("14:5")
        assert result == "14:05"


class TestParseResponse:
    """Tests for _parse_response function."""

    def test_parse_response_valid_json(self):
        """Test parsing valid JSON response."""
        response = json.dumps({
            "priority": 4,
            "tldr": "Meeting tomorrow",
            "action_required": True,
            "event_date": "2026-03-26",
            "event_time": "14:00",
            "event_title": "Meeting",
            "action_type": "event",
        })

        result = _parse_response(response)

        assert result["priority"] == 4
        assert result["tldr"] == "Meeting tomorrow"
        assert result["action_required"] is True
        assert result["event_date"] == "2026-03-26"
        assert result["event_time"] == "14:00"

    def test_parse_response_with_markdown_fence(self):
        """Test parsing JSON wrapped in markdown code fence."""
        response = """```json
{
    "priority": 3,
    "tldr": "Informational email",
    "action_required": false,
    "event_date": null,
    "event_time": null,
    "event_title": null,
    "action_type": null
}
```"""

        result = _parse_response(response)

        assert result["priority"] == 3
        assert result["action_required"] is False
        assert result["event_date"] is None

    def test_parse_response_missing_fields_use_defaults(self):
        """Test that missing fields use defaults."""
        response = json.dumps({
            "priority": 1,
        })

        result = _parse_response(response)

        assert result["priority"] == 1
        assert result["tldr"] == ""
        assert result["action_required"] is False
        assert result["event_date"] is None

    def test_parse_response_deadline_fallback(self):
        """Test that deadline falls back to event_date."""
        response = json.dumps({
            "priority": 2,
            "deadline": "2026-03-27",
        })

        result = _parse_response(response)

        assert result["event_date"] == "2026-03-27"

    def test_parse_response_normalizes_time(self):
        """Test that event_time is normalized."""
        response = json.dumps({
            "priority": 1,
            "event_time": "9:30",
        })

        result = _parse_response(response)

        assert result["event_time"] == "09:30"


class TestCalendarEventFields:
    """Tests for _calendar_event_fields function."""

    def test_calendar_event_fields_timed_event(self):
        """Test creating a timed event (before 22:00)."""
        info = {
            "event_date": "2026-03-26",
            "event_time": "09:00",
        }

        start, end, time_label = _calendar_event_fields(info)

        assert "dateTime" in start
        assert "dateTime" in end
        assert "timeZone" in start
        assert time_label == "09:00"

    def test_calendar_event_fields_all_day_event_late_time(self):
        """Test creating an all-day event (time at or after 22:00)."""
        info = {
            "event_date": "2026-03-26",
            "event_time": "23:00",
        }

        start, end, time_label = _calendar_event_fields(info)

        assert start == {"date": "2026-03-26"}
        assert end == {"date": "2026-03-26"}
        assert time_label == "all day"

    def test_calendar_event_fields_all_day_no_time(self):
        """Test creating an all-day event (no time specified)."""
        info = {
            "event_date": "2026-03-26",
            "event_time": None,
        }

        start, end, time_label = _calendar_event_fields(info)

        assert start == {"date": "2026-03-26"}
        assert end == {"date": "2026-03-26"}
        assert time_label == "all day"


class TestReadWriteJson:
    """Tests for _read_json and _write_json functions."""

    def test_read_json_local_file_exists(self, tmp_path_env):
        """Test reading existing JSON file from local disk."""
        data = {"key": "value", "items": [1, 2, 3]}

        with open("test.json", "w") as f:
            json.dump(data, f)

        result = _read_json("test.json")
        assert result == data

    def test_read_json_local_file_not_exists(self, tmp_path_env):
        """Test reading non-existent JSON file returns empty list."""
        result = _read_json("nonexistent.json")
        assert result == []

    def test_write_json_local_file(self, tmp_path_env):
        """Test writing JSON file to local disk."""
        data = {"key": "value"}

        _write_json("output.json", data)

        with open("output.json") as f:
            written = json.load(f)

        assert written == data

    @mock.patch("analyze_emails._gcs_client")
    def test_read_json_gcs(self, mock_gcs, tmp_path_env, monkeypatch):
        """Test reading JSON from GCS."""
        monkeypatch.setenv("GCS_BUCKET", "my-bucket")

        data = {"key": "gcs_value"}
        mock_blob = mock.MagicMock()
        mock_blob.exists.return_value = True
        mock_blob.download_as_text.return_value = json.dumps(data)

        mock_client = mock.MagicMock()
        mock_client.bucket().blob.return_value = mock_blob
        mock_gcs.return_value = mock_client

        result = _read_json("test.json")
        assert result == data

    @mock.patch("analyze_emails._gcs_client")
    def test_write_json_gcs(self, mock_gcs, tmp_path_env, monkeypatch):
        """Test writing JSON to GCS."""
        monkeypatch.setenv("GCS_BUCKET", "my-bucket")

        data = {"key": "gcs_value"}
        mock_blob = mock.MagicMock()

        mock_client = mock.MagicMock()
        mock_client.bucket().blob.return_value = mock_blob
        mock_gcs.return_value = mock_client

        _write_json("test.json", data)

        mock_blob.upload_from_string.assert_called_once()
        call_args = mock_blob.upload_from_string.call_args[0]
        assert json.loads(call_args[0]) == data


class TestAppendToJsonFile:
    """Tests for _append_to_json_file function."""

    def test_append_to_existing_file(self, tmp_path_env):
        """Test appending to an existing JSON array file."""
        initial = [{"id": 1}]
        with open("log.json", "w") as f:
            json.dump(initial, f)

        new_entry = {"id": 2}
        _append_to_json_file("log.json", new_entry)

        with open("log.json") as f:
            result = json.load(f)

        assert result == [{"id": 1}, {"id": 2}]

    def test_append_to_nonexistent_file(self, tmp_path_env):
        """Test appending to a file that doesn't exist yet."""
        new_entry = {"id": 1}
        _append_to_json_file("new.json", new_entry)

        with open("new.json") as f:
            result = json.load(f)

        assert result == [{"id": 1}]

    def test_append_multiple_entries(self, tmp_path_env):
        """Test appending multiple entries at once."""
        initial = [{"id": 1}]
        with open("log.json", "w") as f:
            json.dump(initial, f)

        new_entries = [{"id": 2}, {"id": 3}]
        _append_to_json_file("log.json", new_entries)

        with open("log.json") as f:
            result = json.load(f)

        assert result == [{"id": 1}, {"id": 2}, {"id": 3}]


class TestLogEmails:
    """Tests for log_emails function."""

    def test_log_emails_creates_record(self, tmp_path_env, sample_email, sample_analysis):
        """Test that log_emails creates proper records in email_log.json."""
        results = [(sample_email, sample_analysis)]

        log_emails(results)

        with open("email_log.json") as f:
            logged = json.load(f)

        assert len(logged) == 1
        record = logged[0]
        assert record["message_id"] == "msg_123"
        assert record["sender"] == "alice@example.com"
        assert record["subject"] == "Meeting tomorrow at 2 PM"
        assert record["analysis"] == sample_analysis
        assert "logged_at" in record

    def test_log_emails_with_none_analysis(self, tmp_path_env, sample_email):
        """Test logging email with None analysis."""
        results = [(sample_email, None)]

        log_emails(results)

        with open("email_log.json") as f:
            logged = json.load(f)

        assert logged[0]["analysis"] is None

    def test_log_emails_pre_filtered_flag_true(self, tmp_path_env, sample_email):
        """Pre-filtered emails get pre_filtered=True at top level, stripped from analysis."""
        info = {
            "priority": 1, "tldr": "Auto-filtered", "action_required": False,
            "event_date": None, "event_time": None, "event_title": None,
            "action_type": None, "pre_filtered": True,
        }
        log_emails([(sample_email, info)])
        with open("email_log.json") as f:
            record = json.load(f)[0]
        assert record["pre_filtered"] is True
        assert "pre_filtered" not in record["analysis"]

    def test_log_emails_pre_filtered_flag_false(self, tmp_path_env, sample_email, sample_analysis):
        """Normal AI-analyzed emails get pre_filtered=False at top level."""
        log_emails([(sample_email, sample_analysis)])
        with open("email_log.json") as f:
            record = json.load(f)[0]
        assert record["pre_filtered"] is False


class TestLogActivity:
    """Tests for log_activity function."""

    def test_log_activity_creates_record(self, tmp_path_env):
        """Test that log_activity creates proper records."""
        log_activity(
            entry_type="event",
            title="Project Meeting",
            event_date="2026-03-26",
            event_time="14:00",
            sender="alice@example.com",
            subject="Meeting tomorrow",
            tldr="Discuss project timeline"
        )

        with open("activity_log.json") as f:
            logged = json.load(f)

        assert len(logged) == 1
        record = logged[0]
        assert record["type"] == "event"
        assert record["title"] == "Project Meeting"
        assert record["date"] == "2026-03-26"
        assert record["time"] == "14:00"
        assert "logged_at" in record


class TestAnalyzeEmails:
    """Tests for analyze_emails function."""

    @mock.patch("analyze_emails._analyze_with_ollama")
    def test_analyze_emails_with_ollama(self, mock_ollama, monkeypatch, sample_email, sample_analysis):
        """Test analyzing emails with Ollama backend."""
        monkeypatch.setenv("MODEL_BACKEND", "local")

        mock_ollama.return_value = sample_analysis

        results = analyze_emails([sample_email])

        assert len(results) == 1
        em, info = results[0]
        assert em == sample_email
        assert info == sample_analysis
        mock_ollama.assert_called_once_with(sample_email, None, None, account_id=None)

    @mock.patch("analyze_emails._analyze_with_gemini")
    @mock.patch("time.sleep")
    def test_analyze_emails_with_gemini(self, mock_sleep, mock_gemini, monkeypatch, sample_email, sample_analysis):
        """Test analyzing emails with Gemini backend."""
        monkeypatch.setenv("MODEL_BACKEND", "gemini")

        mock_gemini.return_value = sample_analysis

        results = analyze_emails([sample_email])

        assert len(results) == 1
        em, info = results[0]
        assert em == sample_email
        assert info == sample_analysis
        assert mock_gemini.call_args[0][0] == sample_email

    @mock.patch("analyze_emails._analyze_with_ollama")
    def test_analyze_emails_handles_exception(self, mock_ollama, monkeypatch, sample_email):
        """Test that exceptions are caught and None is returned."""
        monkeypatch.setenv("MODEL_BACKEND", "local")

        mock_ollama.side_effect = Exception("API error")

        results = analyze_emails([sample_email])

        assert len(results) == 1
        em, info = results[0]
        assert em == sample_email
        assert info is None


class TestCreateEntries:
    """Tests for create_entries function."""

    @mock.patch("analyze_emails._task_exists", return_value=False)
    @mock.patch("analyze_emails.get_tasks_service")
    @mock.patch("analyze_emails.get_calendar_service")
    def test_create_entries_creates_task(self, mock_cal_svc, mock_task_svc, mock_task_exists, tmp_path_env, sample_email):
        """Test creating a task from an actionable email."""
        analysis = {
            "priority": 3,
            "tldr": "Submit assignment",
            "action_required": True,
            "event_date": "2026-03-28",
            "event_time": None,
            "event_title": "Assignment Due",
            "action_type": "task",
        }

        results = [(sample_email, analysis)]

        create_entries(results)

        mock_task_svc.return_value.tasks().insert.assert_called_once()
        call_args = mock_task_svc.return_value.tasks().insert.call_args
        assert call_args[1]["tasklist"] == "@default"

    @mock.patch("analyze_emails._event_exists", return_value=False)
    @mock.patch("analyze_emails.get_tasks_service")
    @mock.patch("analyze_emails.get_calendar_service")
    def test_create_entries_creates_event(self, mock_cal_svc, mock_task_svc, mock_event_exists, tmp_path_env, sample_email, sample_analysis):
        """Test creating a calendar event from an actionable email."""
        results = [(sample_email, sample_analysis)]

        create_entries(results)

        mock_cal_svc.return_value.events().insert.assert_called_once()
        call_args = mock_cal_svc.return_value.events().insert.call_args
        assert call_args[1]["calendarId"] == "primary"

    def test_create_entries_no_actionable_items(self, tmp_path_env, sample_email):
        """Test when there are no actionable items."""
        analysis = {
            "priority": 1,
            "tldr": "Spam",
            "action_required": False,
            "event_date": None,
            "action_type": None,
        }

        results = [(sample_email, analysis)]

        # Should not raise an error
        create_entries(results)

    @mock.patch("analyze_emails.get_tasks_service")
    @mock.patch("analyze_emails.get_calendar_service")
    def test_create_entries_skips_duplicate_event(self, mock_cal_svc, mock_task_svc, tmp_path_env, sample_email, sample_analysis):
        """Duplicate calendar event (same message_id already in calendar) is skipped."""
        with mock.patch("analyze_emails._event_exists", return_value=True) as mock_exists:
            results = [(sample_email, sample_analysis)]
            create_entries(results)
            mock_exists.assert_called_once()
            mock_cal_svc.return_value.events.return_value.insert.assert_not_called()

    @mock.patch("analyze_emails.get_tasks_service")
    @mock.patch("analyze_emails.get_calendar_service")
    def test_create_entries_embeds_source_tag(self, mock_cal_svc, mock_task_svc, tmp_path_env, sample_email, sample_analysis):
        """Newly created calendar event description contains [source:<message_id>]."""
        with mock.patch("analyze_emails._event_exists", return_value=False):
            results = [(sample_email, sample_analysis)]
            create_entries(results)
            insert_call = mock_cal_svc.return_value.events.return_value.insert.call_args
            description = insert_call[1]["body"]["description"]
            assert f"[source:{sample_email['message_id']}]" in description

    @mock.patch("analyze_emails.get_tasks_service")
    @mock.patch("analyze_emails.get_calendar_service")
    def test_create_entries_skips_duplicate_task(self, mock_cal_svc, mock_task_svc, tmp_path_env, sample_email, sample_analysis):
        """Duplicate task (same message_id already in tasks) is skipped."""
        task_analysis = {**sample_analysis, "action_type": "task"}
        with mock.patch("analyze_emails._task_exists", return_value=True) as mock_exists:
            results = [(sample_email, task_analysis)]
            create_entries(results)
            mock_exists.assert_called_once()
            mock_task_svc.return_value.tasks.return_value.insert.assert_not_called()

    @mock.patch("analyze_emails.get_tasks_service")
    @mock.patch("analyze_emails.get_calendar_service")
    def test_create_entries_embeds_source_tag_in_task(self, mock_cal_svc, mock_task_svc, tmp_path_env, sample_email, sample_analysis):
        """Newly created task notes contains [source:<message_id>]."""
        task_analysis = {**sample_analysis, "action_type": "task"}
        with mock.patch("analyze_emails._task_exists", return_value=False):
            results = [(sample_email, task_analysis)]
            create_entries(results)
            insert_call = mock_task_svc.return_value.tasks.return_value.insert.call_args
            notes = insert_call[1]["body"]["notes"]
            assert f"[source:{sample_email['message_id']}]" in notes


class TestManageInbox:
    """Tests for manage_inbox function."""

    def test_manage_inbox_marks_read(self, mock_gmail_service, sample_email, sample_analysis):
        """Test that manage_inbox marks emails as read."""
        results = [(sample_email, sample_analysis)]

        mock_gmail_service.users().labels().list().execute.return_value = {
            "labels": []
        }

        manage_inbox(mock_gmail_service, results)

        # Should call modify on the message
        mock_gmail_service.users().messages().modify.assert_called()
        call_args = mock_gmail_service.users().messages().modify.call_args
        assert call_args[1]["id"] == "msg_123"
        body = call_args[1]["body"]
        assert "UNREAD" in body["removeLabelIds"]

    def test_manage_inbox_adds_ai_processed_label(self, mock_gmail_service, sample_email, sample_analysis):
        """Test that AI Processed label is added."""
        results = [(sample_email, sample_analysis)]

        mock_gmail_service.users().labels().list().execute.return_value = {
            "labels": [
                {"name": "AI Processed", "id": "label_ai"}
            ]
        }

        manage_inbox(mock_gmail_service, results)

        call_args = mock_gmail_service.users().messages().modify.call_args
        body = call_args[1]["body"]
        assert "label_ai" in body["addLabelIds"]

    def test_manage_inbox_archives_low_priority(self, mock_gmail_service, sample_email):
        """Test that low-priority emails (priority <= 2) are archived."""
        analysis = {
            "priority": 1,
            "tldr": "Spam",
            "action_required": False,
            "event_date": None,
        }

        results = [(sample_email, analysis)]

        mock_gmail_service.users().labels().list().execute.return_value = {
            "labels": []
        }

        manage_inbox(mock_gmail_service, results)

        call_args = mock_gmail_service.users().messages().modify.call_args
        body = call_args[1]["body"]
        # INBOX should be removed for low-priority emails
        assert "INBOX" in body["removeLabelIds"]

    def test_manage_inbox_adds_action_required_label(self, mock_gmail_service, sample_email, sample_analysis):
        """Test that Action Required label is added when action_required=True."""
        results = [(sample_email, sample_analysis)]

        mock_gmail_service.users().labels().list().execute.return_value = {
            "labels": [
                {"name": "Action Required", "id": "label_action"}
            ]
        }

        manage_inbox(mock_gmail_service, results)

        call_args = mock_gmail_service.users().messages().modify.call_args
        body = call_args[1]["body"]
        assert "label_action" in body["addLabelIds"]

    def test_manage_inbox_skips_none_analysis(self, mock_gmail_service, sample_email):
        """Test that emails with None analysis are skipped."""
        results = [(sample_email, None)]

        manage_inbox(mock_gmail_service, results)

        # modify should not be called
        mock_gmail_service.users().messages().modify.assert_not_called()


class TestGetOrCreateLabel:
    """Tests for _get_or_create_label function."""

    def test_get_or_create_label_finds_existing(self, mock_gmail_service):
        """Test finding an existing label."""
        mock_gmail_service.users().labels().list().execute.return_value = {
            "labels": [
                {"name": "AI Processed", "id": "label_123"}
            ]
        }

        result = _get_or_create_label(mock_gmail_service, "AI Processed")

        assert result == "label_123"
        mock_gmail_service.users().labels().create.assert_not_called()

    def test_get_or_create_label_creates_new(self, mock_gmail_service):
        """Test creating a new label when it doesn't exist."""
        mock_gmail_service.users().labels().list().execute.return_value = {
            "labels": []
        }
        mock_gmail_service.users().labels().create().execute.return_value = {
            "id": "label_new"
        }

        result = _get_or_create_label(mock_gmail_service, "New Label")

        assert result == "label_new"
        # Verify create was called with correct parameters
        mock_gmail_service.users().labels().create.assert_called()


# ============================================================================
# Tests for run_all.py
# ============================================================================


class TestCloudInit:
    """Tests for cloud_init function."""

    def test_cloud_init_no_gcp_project(self, monkeypatch):
        """Test cloud_init does nothing when GCP_PROJECT is not set."""
        monkeypatch.delenv("GCP_PROJECT", raising=False)

        # Should not raise an error
        cloud_init({"id": "gmail-personal"})

    def test_cloud_init_fetches_secrets(self, monkeypatch, tmp_path_env):
        """Test cloud_init fetches secrets from Secret Manager."""
        monkeypatch.setenv("GCP_PROJECT", "test-project")

        # Mock the entire secretmanager module
        mock_secret_client = mock.MagicMock()
        mock_client = mock.MagicMock()
        mock_secret_client.SecretManagerServiceClient.return_value = mock_client

        mock_client.access_secret_version.side_effect = lambda **kwargs: mock.MagicMock(
            payload=mock.MagicMock(
                data=b'{"test": "json"}'
            )
        )

        with mock.patch.dict("sys.modules", {"google.cloud.secretmanager": mock_secret_client}):
            cloud_init({"id": "gmail-personal"})

        # Verify it tried to fetch the secrets (token + credentials; gemini key is conditional)
        assert mock_client.access_secret_version.call_count >= 2

    def test_cloud_init_writes_files(self, monkeypatch, tmp_path_env):
        """Test cloud_init writes token.json and credentials.json."""
        monkeypatch.setenv("GCP_PROJECT", "test-project")

        # Mock the entire secretmanager module
        mock_secret_client = mock.MagicMock()
        mock_client = mock.MagicMock()
        mock_secret_client.SecretManagerServiceClient.return_value = mock_client

        def side_effect(**kwargs):
            secret_name = kwargs['name']
            if 'gmail-token' in secret_name:
                return mock.MagicMock(payload=mock.MagicMock(data=b'{"token": "test"}'))
            elif 'gmail-credentials' in secret_name:
                return mock.MagicMock(payload=mock.MagicMock(data=b'{"creds": "test"}'))
            elif 'gemini-api-key' in secret_name:
                return mock.MagicMock(payload=mock.MagicMock(data=b'api-key-value'))
            return mock.MagicMock(payload=mock.MagicMock(data=b'{}'))

        mock_client.access_secret_version.side_effect = side_effect

        with mock.patch.dict("sys.modules", {"google.cloud.secretmanager": mock_secret_client}):
            cloud_init({"id": "gmail-personal"})

        assert os.path.exists("token.json")
        assert os.path.exists("credentials.json")


# ============================================================================
# Integration-like tests
# ============================================================================


class TestEndToEnd:
    """End-to-end style tests combining multiple components."""

    def test_email_analysis_workflow(self, tmp_path_env, sample_email):
        """Test a complete email analysis workflow."""
        # Parse response
        response_text = json.dumps({
            "priority": 3,
            "tldr": "Meeting tomorrow",
            "action_required": True,
            "event_date": "2026-03-26",
            "event_time": "9:00",
            "event_title": "Team Meeting",
            "action_type": "event",
        })

        analysis = _parse_response(response_text)

        # Log the email
        log_emails([(sample_email, analysis)])

        # Log activity
        log_activity(
            "event",
            analysis["event_title"],
            analysis["event_date"],
            analysis["event_time"],
            sample_email["sender"],
            sample_email["subject"],
            analysis["tldr"]
        )

        # Verify logs
        with open("email_log.json") as f:
            email_log = json.load(f)
        assert len(email_log) == 1

        with open("activity_log.json") as f:
            activity_log = json.load(f)
        assert len(activity_log) == 1

# ============================================================================
# Tests for _event_exists and _task_exists
# ============================================================================


class TestEventExists:
    """Tests for _event_exists() helper."""

    def test_returns_false_when_no_events(self):
        mock_svc = mock.MagicMock()
        mock_svc.events.return_value.list.return_value.execute.return_value = {"items": []}
        assert _event_exists(mock_svc, "msg_123", "2026-03-30") is False

    def test_returns_true_when_tag_found_in_description(self):
        mock_svc = mock.MagicMock()
        mock_svc.events.return_value.list.return_value.execute.return_value = {
            "items": [{"description": "From: alice@example.com\n\nMeeting.\n\n[source:msg_123]"}]
        }
        assert _event_exists(mock_svc, "msg_123", "2026-03-30") is True

    def test_returns_false_when_different_message_id(self):
        mock_svc = mock.MagicMock()
        mock_svc.events.return_value.list.return_value.execute.return_value = {
            "items": [{"description": "From: alice@example.com\n\nMeeting.\n\n[source:msg_999]"}]
        }
        assert _event_exists(mock_svc, "msg_123", "2026-03-30") is False

    def test_returns_false_when_description_is_none(self):
        mock_svc = mock.MagicMock()
        mock_svc.events.return_value.list.return_value.execute.return_value = {
            "items": [{"description": None}]
        }
        assert _event_exists(mock_svc, "msg_123", "2026-03-30") is False

    def test_uses_one_year_time_window(self):
        mock_svc = mock.MagicMock()
        mock_svc.events.return_value.list.return_value.execute.return_value = {"items": []}
        _event_exists(mock_svc, "msg_123", "2026-03-30")
        call_kwargs = mock_svc.events.return_value.list.call_args[1]
        assert "timeMin" in call_kwargs
        assert "timeMax" in call_kwargs
        assert call_kwargs["q"] == "msg_123"


class TestTaskExists:
    """Tests for _task_exists() helper."""

    def test_returns_false_when_no_tasks(self):
        mock_svc = mock.MagicMock()
        mock_svc.tasks.return_value.list.return_value.execute.return_value = {"items": []}
        assert _task_exists(mock_svc, "msg_123") is False

    def test_returns_true_when_tag_found_in_notes(self):
        mock_svc = mock.MagicMock()
        mock_svc.tasks.return_value.list.return_value.execute.return_value = {
            "items": [{"notes": "From: alice@example.com\n\nDo the thing.\n\n[source:msg_123]"}]
        }
        assert _task_exists(mock_svc, "msg_123") is True

    def test_returns_false_when_different_message_id(self):
        mock_svc = mock.MagicMock()
        mock_svc.tasks.return_value.list.return_value.execute.return_value = {
            "items": [{"notes": "From: alice@example.com\n\nDo the thing.\n\n[source:msg_999]"}]
        }
        assert _task_exists(mock_svc, "msg_123") is False

    def test_returns_false_when_notes_is_none(self):
        mock_svc = mock.MagicMock()
        mock_svc.tasks.return_value.list.return_value.execute.return_value = {
            "items": [{"notes": None}]
        }
        assert _task_exists(mock_svc, "msg_123") is False

    def test_paginates_through_all_tasks(self):
        mock_svc = mock.MagicMock()
        page1 = {"items": [{"notes": "irrelevant"}], "nextPageToken": "tok1"}
        page2 = {"items": [{"notes": "[source:msg_123]"}]}
        mock_svc.tasks.return_value.list.return_value.execute.side_effect = [page1, page2]
        assert _task_exists(mock_svc, "msg_123") is True
        assert mock_svc.tasks.return_value.list.call_count == 2

# ============================================================================
# Token sync to Secret Manager
# ============================================================================


class TestSyncTokenToSecretManager:
    """Tests for _sync_token_to_secret_manager in fetch_emails.py."""

    def test_does_nothing_when_no_gcp_project(self, monkeypatch):
        """Should be a no-op when GCP_PROJECT is not set."""
        monkeypatch.delenv("GCP_PROJECT", raising=False)
        from fetch_emails import _sync_token_to_secret_manager
        # Should not raise even without the Secret Manager library
        _sync_token_to_secret_manager('{"token": "data"}')

    def test_pushes_new_version_on_gcp(self, monkeypatch):
        """Should add a new secret version when GCP_PROJECT is set."""
        import sys
        monkeypatch.setenv("GCP_PROJECT", "test-project")
        mock_sm = mock.MagicMock()
        mock_client_instance = mock.MagicMock()
        mock_sm.SecretManagerServiceClient.return_value = mock_client_instance
        monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", mock_sm)

        from fetch_emails import _sync_token_to_secret_manager
        _sync_token_to_secret_manager('{"token": "refreshed"}')

        mock_client_instance.add_secret_version.assert_called_once()
        call_kwargs = mock_client_instance.add_secret_version.call_args
        assert "projects/test-project/secrets/gmail-token" in str(call_kwargs)

    def test_warns_on_secret_manager_error(self, monkeypatch, capsys):
        """Should print a warning and not raise if Secret Manager call fails."""
        import sys
        monkeypatch.setenv("GCP_PROJECT", "test-project")
        mock_sm = mock.MagicMock()
        mock_client_instance = mock.MagicMock()
        mock_client_instance.add_secret_version.side_effect = Exception("permission denied")
        mock_sm.SecretManagerServiceClient.return_value = mock_client_instance
        monkeypatch.setitem(sys.modules, "google.cloud.secretmanager", mock_sm)

        from fetch_emails import _sync_token_to_secret_manager
        _sync_token_to_secret_manager('{"token": "data"}')  # must not raise

        assert "warning" in capsys.readouterr().out.lower()


class TestEventExists:
    """Tests for _event_exists() helper."""

    def test_returns_false_when_no_events(self):
        mock_svc = mock.MagicMock()
        mock_svc.events.return_value.list.return_value.execute.return_value = {"items": []}
        assert _event_exists(mock_svc, "msg_123", "2026-03-30") is False

    def test_returns_true_when_tag_found_in_description(self):
        mock_svc = mock.MagicMock()
        mock_svc.events.return_value.list.return_value.execute.return_value = {
            "items": [{"description": "From: alice@example.com\n\nMeeting.\n\n[source:msg_123]"}]
        }
        assert _event_exists(mock_svc, "msg_123", "2026-03-30") is True

    def test_returns_false_when_different_message_id(self):
        mock_svc = mock.MagicMock()
        mock_svc.events.return_value.list.return_value.execute.return_value = {
            "items": [{"description": "From: alice@example.com\n\nMeeting.\n\n[source:msg_999]"}]
        }
        assert _event_exists(mock_svc, "msg_123", "2026-03-30") is False

    def test_returns_false_when_description_is_none(self):
        mock_svc = mock.MagicMock()
        mock_svc.events.return_value.list.return_value.execute.return_value = {
            "items": [{"description": None}]
        }
        assert _event_exists(mock_svc, "msg_123", "2026-03-30") is False

    def test_uses_one_year_time_window(self):
        mock_svc = mock.MagicMock()
        mock_svc.events.return_value.list.return_value.execute.return_value = {"items": []}
        _event_exists(mock_svc, "msg_123", "2026-03-30")
        call_kwargs = mock_svc.events.return_value.list.call_args[1]
        assert "timeMin" in call_kwargs
        assert "timeMax" in call_kwargs
        assert call_kwargs["q"] == "msg_123"


class TestTaskExists:
    """Tests for _task_exists() helper."""

    def test_returns_false_when_no_tasks(self):
        mock_svc = mock.MagicMock()
        mock_svc.tasks.return_value.list.return_value.execute.return_value = {"items": []}
        assert _task_exists(mock_svc, "msg_123") is False

    def test_returns_true_when_tag_found_in_notes(self):
        mock_svc = mock.MagicMock()
        mock_svc.tasks.return_value.list.return_value.execute.return_value = {
            "items": [{"notes": "From: alice@example.com\n\nDo the thing.\n\n[source:msg_123]"}]
        }
        assert _task_exists(mock_svc, "msg_123") is True

    def test_returns_false_when_different_message_id(self):
        mock_svc = mock.MagicMock()
        mock_svc.tasks.return_value.list.return_value.execute.return_value = {
            "items": [{"notes": "From: alice@example.com\n\nDo the thing.\n\n[source:msg_999]"}]
        }
        assert _task_exists(mock_svc, "msg_123") is False

    def test_returns_false_when_notes_is_none(self):
        mock_svc = mock.MagicMock()
        mock_svc.tasks.return_value.list.return_value.execute.return_value = {
            "items": [{"notes": None}]
        }
        assert _task_exists(mock_svc, "msg_123") is False

    def test_paginates_through_all_tasks(self):
        mock_svc = mock.MagicMock()
        page1 = {"items": [{"notes": "irrelevant"}], "nextPageToken": "tok1"}
        page2 = {"items": [{"notes": "[source:msg_123]"}]}
        mock_svc.tasks.return_value.list.return_value.execute.side_effect = [page1, page2]
        assert _task_exists(mock_svc, "msg_123") is True
        assert mock_svc.tasks.return_value.list.call_count == 2


class TestAuthResilience:
    """Tests for auth error handling added to fetch_emails and run_all."""

    def test_refresh_error_local_clears_token_and_reauths(self, monkeypatch, tmp_path):
        """Revoked token locally: stale file deleted, OAuth flow triggered, fresh token written."""
        from google.auth.exceptions import RefreshError
        from fetch_emails import get_gmail_service

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("GCP_PROJECT", raising=False)
        token_file = tmp_path / "token_test.json"
        token_file.write_text('{"token": "stale"}')

        stale_creds = mock.MagicMock()
        stale_creds.valid = False
        stale_creds.expired = True
        stale_creds.refresh_token = "rt"
        stale_creds.refresh.side_effect = RefreshError("invalid_grant")

        fresh_creds = mock.MagicMock()
        fresh_creds.valid = True
        fresh_creds.to_json.return_value = '{"token": "fresh"}'

        with mock.patch("fetch_emails.Credentials.from_authorized_user_file", return_value=stale_creds), \
             mock.patch("fetch_emails.InstalledAppFlow.from_client_secrets_file") as mock_flow, \
             mock.patch("fetch_emails.build"), \
             mock.patch("fetch_emails._sync_token_to_secret_manager"):
            mock_flow.return_value.run_local_server.return_value = fresh_creds
            get_gmail_service(str(token_file))

        mock_flow.return_value.run_local_server.assert_called_once()
        assert token_file.read_text() == '{"token": "fresh"}'

    def test_refresh_error_gcp_raises_runtime_error(self, monkeypatch, tmp_path):
        """Revoked token on GCP: raises RuntimeError instead of trying interactive re-auth."""
        from google.auth.exceptions import RefreshError
        from fetch_emails import get_gmail_service

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GCP_PROJECT", "my-project")
        token_file = tmp_path / "token_test.json"
        token_file.write_text('{"token": "stale"}')

        stale_creds = mock.MagicMock()
        stale_creds.valid = False
        stale_creds.expired = True
        stale_creds.refresh_token = "rt"
        stale_creds.refresh.side_effect = RefreshError("invalid_grant")

        with mock.patch("fetch_emails.Credentials.from_authorized_user_file", return_value=stale_creds):
            with pytest.raises(RuntimeError, match="expired/revoked on GCP"):
                get_gmail_service(str(token_file))

    def test_cloud_init_writes_to_account_token_file(self, monkeypatch, tmp_path):
        """cloud_init writes the secret to the account-specific token_file, not token.json."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GCP_PROJECT", "my-project")
        (tmp_path / "credentials.json").write_text("{}")

        account = {
            "id": "gmail-work",
            "token_file": "token_work.json",
            "token_secret": "gmail-token-work",
            "credentials_secret": "gmail-credentials",
        }

        mock_sm = mock.MagicMock()
        mock_client = mock_sm.SecretManagerServiceClient.return_value
        mock_client.access_secret_version.side_effect = lambda **kwargs: mock.MagicMock(
            payload=mock.MagicMock(data=b'{"token": "account-specific"}')
        )

        with mock.patch.dict("sys.modules", {"google.cloud.secretmanager": mock_sm}):
            import importlib
            import run_all as run_all_mod
            importlib.reload(run_all_mod)
            run_all_mod.cloud_init(account)

        assert (tmp_path / "token_work.json").read_text() == '{"token": "account-specific"}'
        assert not (tmp_path / "token.json").exists(), "cloud_init must not write to the default token.json"

    def test_main_continues_after_account_error(self, monkeypatch, capsys):
        """An exception in one account's pipeline is caught; subsequent accounts still run."""
        import run_all as run_all_mod

        accounts = [
            {"id": "gmail-bad",  "provider": "gmail", "active": True},
            {"id": "gmail-good", "provider": "gmail", "active": True},
        ]
        call_log = []

        def fake_process(account):
            if account["id"] == "gmail-bad":
                raise RuntimeError("auth failed")
            call_log.append(account["id"])

        with mock.patch.object(run_all_mod, "load_accounts", return_value=accounts), \
             mock.patch.object(run_all_mod, "_process_account", side_effect=fake_process):
            run_all_mod.main()

        assert "gmail-good" in call_log
        assert "ERROR" in capsys.readouterr().out
