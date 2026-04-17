"""
Tests for Sprint 13 — [AGENT PRIORITY] keyword override command.

Critical sections:
  1. parse_priority_override — wrong parse = wrong AI scoring silently
  2. load_priority_overrides — sender security gate
  3. _build_prompt keyword injection — wrong match or no injection = override never applied
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from fetch_emails import parse_priority_override, load_priority_overrides
from analyze_emails import _build_prompt


# ---------------------------------------------------------------------------
# Critical section 1: parse_priority_override
# ---------------------------------------------------------------------------

class TestParsePriorityOverride:

    def test_valid_format_parsed_correctly(self):
        """Happy path: 'keyword = 5' returns correct dict."""
        result = parse_priority_override("osap = 5")
        assert result == {"keyword": "osap", "priority": 5}

    def test_keyword_stored_lowercase(self):
        """Keywords are lowercased for case-insensitive matching."""
        result = parse_priority_override("OSAP = 4")
        assert result["keyword"] == "osap"

    def test_multi_word_keyword_supported(self):
        """Multi-word phrases like 'tax return = 5' are supported."""
        result = parse_priority_override("tax return = 5")
        assert result == {"keyword": "tax return", "priority": 5}

    def test_missing_equals_returns_none(self):
        """Body with no '=' sign is invalid — returns None."""
        result = parse_priority_override("osap")
        assert result is None

    def test_non_numeric_priority_returns_none(self):
        """Priority value 'high' is not a number — returns None."""
        result = parse_priority_override("osap = high")
        assert result is None

    def test_priority_above_5_returns_none(self):
        """Priority 6 is out of range — returns None."""
        result = parse_priority_override("osap = 6")
        assert result is None

    def test_priority_below_1_returns_none(self):
        """Priority 0 is out of range — returns None."""
        result = parse_priority_override("osap = 0")
        assert result is None

    def test_priority_at_boundary_1_valid(self):
        """Priority 1 is the lower boundary — valid."""
        result = parse_priority_override("spam = 1")
        assert result == {"keyword": "spam", "priority": 1}

    def test_priority_at_boundary_5_valid(self):
        """Priority 5 is the upper boundary — valid."""
        result = parse_priority_override("urgent = 5")
        assert result == {"keyword": "urgent", "priority": 5}

    def test_whitespace_stripped_from_keyword_and_value(self):
        """Extra whitespace around keyword and value is stripped."""
        result = parse_priority_override("  osap  =  3  ")
        assert result == {"keyword": "osap", "priority": 3}


# ---------------------------------------------------------------------------
# Critical section 2: load_priority_overrides — security gate
# ---------------------------------------------------------------------------

class TestLoadPriorityOverrides:

    def _make_service(self, messages, sender, body):
        svc = mock.MagicMock()
        svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": messages
        }
        svc.users.return_value.messages.return_value.get.return_value.execute.return_value = {
            "id": messages[0]["id"] if messages else "x",
            "payload": {
                "headers": [],
                "body": {"data": ""},
                "parts": [],
            },
            "labelIds": [],
        }
        return svc

    def test_valid_self_sent_override_loaded(self, capsys):
        """Self-sent [AGENT PRIORITY] email is loaded and logged."""
        svc = self._make_service([{"id": "po_001"}], "me@gmail.com", "osap = 5")

        with mock.patch("fetch_emails._get_header") as mock_hdr, \
             mock.patch("fetch_emails._decode_body") as mock_body:
            mock_hdr.side_effect = lambda h, n: {"From": "me@gmail.com"}.get(n, "")
            mock_body.return_value = "osap = 5"

            overrides, excluded = load_priority_overrides(svc, "me@gmail.com")

        assert len(overrides) == 1
        assert overrides[0]["keyword"] == "osap"
        assert overrides[0]["priority"] == 5
        assert "po_001" in excluded
        out = capsys.readouterr().out
        assert '[priority-override] loaded: "osap" → 5' in out

    def test_external_sender_rejected(self, capsys):
        """[AGENT PRIORITY] from external sender is ignored — security gate."""
        svc = self._make_service([{"id": "po_ext"}], "attacker@evil.com", "osap = 5")

        with mock.patch("fetch_emails._get_header") as mock_hdr, \
             mock.patch("fetch_emails._decode_body") as mock_body:
            mock_hdr.side_effect = lambda h, n: {"From": "attacker@evil.com"}.get(n, "")
            mock_body.return_value = "osap = 5"

            overrides, excluded = load_priority_overrides(svc, "me@gmail.com")

        assert overrides == []
        assert "po_ext" not in excluded
        out = capsys.readouterr().out
        assert "ignored" in out

    def test_invalid_format_skipped_with_warning(self, capsys):
        """Body with invalid format is skipped and warning logged."""
        svc = self._make_service([{"id": "po_bad"}], "me@gmail.com", "osap")

        with mock.patch("fetch_emails._get_header") as mock_hdr, \
             mock.patch("fetch_emails._decode_body") as mock_body:
            mock_hdr.side_effect = lambda h, n: {"From": "me@gmail.com"}.get(n, "")
            mock_body.return_value = "osap"

            overrides, excluded = load_priority_overrides(svc, "me@gmail.com")

        assert overrides == []
        out = capsys.readouterr().out
        assert "ignored" in out

    def test_multiple_overrides_all_loaded(self, capsys):
        """Multiple [AGENT PRIORITY] emails are all loaded."""
        svc = mock.MagicMock()
        svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": [{"id": "po_001"}, {"id": "po_002"}]
        }

        call_count = [0]
        def fake_get(*args, **kwargs):
            i = call_count[0]
            call_count[0] += 1
            bodies = ["osap = 5", "tax return = 4"]
            return mock.MagicMock(**{
                "execute.return_value": {
                    "id": f"po_00{i+1}",
                    "payload": {"headers": [], "body": {"data": ""}, "parts": []},
                    "labelIds": [],
                }
            })
        svc.users.return_value.messages.return_value.get.side_effect = fake_get

        with mock.patch("fetch_emails._get_header") as mock_hdr, \
             mock.patch("fetch_emails._decode_body") as mock_body, \
             mock.patch("fetch_emails.parse_priority_override") as mock_parse:
            mock_hdr.side_effect = lambda h, n: {"From": "me@gmail.com"}.get(n, "")
            mock_body.return_value = "placeholder"
            mock_parse.side_effect = [
                {"keyword": "osap", "priority": 5},
                {"keyword": "tax return", "priority": 4},
            ]

            overrides, excluded = load_priority_overrides(svc, "me@gmail.com")

        assert len(overrides) == 2

    def test_no_commands_returns_empty(self):
        """No [AGENT PRIORITY] emails → empty list, empty excluded set."""
        svc = mock.MagicMock()
        svc.users.return_value.messages.return_value.list.return_value.execute.return_value = {
            "messages": []
        }

        overrides, excluded = load_priority_overrides(svc, "me@gmail.com")

        assert overrides == []
        assert excluded == set()


# ---------------------------------------------------------------------------
# Critical section 3: _build_prompt keyword injection
# ---------------------------------------------------------------------------

class TestBuildPromptPriorityInjection:

    def _email(self, subject="Hello", body="Some content"):
        return {
            "sender": "someone@example.com",
            "subject": subject,
            "body": body,
            "label_names": [],
        }

    def test_keyword_in_subject_injects_override(self):
        """Override injected when keyword found in email subject (case-insensitive)."""
        email = self._email(subject="Your OSAP Assessment is Ready")
        overrides = [{"keyword": "osap", "priority": 5}]

        prompt = _build_prompt(email, priority_overrides=overrides)

        assert 'keyword "osap"' in prompt
        assert "priority 5" in prompt

    def test_keyword_in_body_injects_override(self):
        """Override injected when keyword found in email body."""
        email = self._email(subject="Update", body="Your OSAP application has been received.")
        overrides = [{"keyword": "osap", "priority": 5}]

        prompt = _build_prompt(email, priority_overrides=overrides)

        assert 'keyword "osap"' in prompt

    def test_no_keyword_match_no_injection(self):
        """No override injected when keyword not present in subject or body."""
        email = self._email(subject="Amazon order shipped", body="Your package is on the way.")
        overrides = [{"keyword": "osap", "priority": 5}]

        prompt = _build_prompt(email, priority_overrides=overrides)

        assert "priority override" not in prompt.lower()

    def test_keyword_match_is_case_insensitive(self):
        """Match works regardless of case in email content."""
        email = self._email(subject="OSAP REMINDER")
        overrides = [{"keyword": "osap", "priority": 4}]

        prompt = _build_prompt(email, priority_overrides=overrides)

        assert "priority 4" in prompt

    def test_no_overrides_prompt_unchanged(self):
        """Empty override list leaves prompt identical to baseline."""
        email = self._email()
        baseline = _build_prompt(email)
        with_empty = _build_prompt(email, priority_overrides=[])

        assert baseline == with_empty

    def test_none_overrides_prompt_unchanged(self):
        """None overrides leaves prompt identical to baseline."""
        email = self._email()
        baseline = _build_prompt(email)
        with_none = _build_prompt(email, priority_overrides=None)

        assert baseline == with_none
