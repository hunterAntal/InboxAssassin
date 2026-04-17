"""
Tests for Sprint 11 — label routing via [AGENT RULE] ROUTE: section.

Critical sections:
  1. parse_route_criteria — wrong parse = wrong emails routed silently
  2. _email_matches_criteria — silent wrong routing
  3. apply_routing_labels — label ID lookup + Gmail API mutation
  4. Already-labeled email skip — avoids duplicate API calls
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from fetch_emails import parse_route_criteria, _email_matches_criteria, apply_routing_labels


# ---------------------------------------------------------------------------
# Critical section 1: parse_route_criteria
# ---------------------------------------------------------------------------

class TestParseRouteCriteria:

    def test_parses_from_and_subject(self):
        """Happy path: both from: and subject: lines parsed correctly."""
        body = """ROUTE:
  from: @lakeheadu.ca, hbantel@lakeheadu.ca
  subject: OSAP, tuition, bursary

Treat as priority 4-5 if deadline related."""

        result = parse_route_criteria(body)

        assert result["from"] == ["@lakeheadu.ca", "hbantel@lakeheadu.ca"]
        assert result["subject"] == ["osap", "tuition", "bursary"]

    def test_subject_criteria_lowercased(self):
        """Subject keywords are stored lowercase for case-insensitive matching."""
        body = "ROUTE:\n  subject: OSAP, Financial Aid, Bursary\n"

        result = parse_route_criteria(body)

        assert all(k == k.lower() for k in result["subject"])

    def test_no_route_section_returns_empty(self):
        """AC5: body with no ROUTE: block returns empty dict."""
        body = "Treat all emails in this label as high priority."

        result = parse_route_criteria(body)

        assert result == {}

    def test_route_section_with_no_criteria_returns_empty(self):
        """Edge: ROUTE: exists but both from: and subject: are absent."""
        body = "ROUTE:\n\nSome behavioral instruction."

        result = parse_route_criteria(body)

        assert result == {}

    def test_from_only_no_subject(self):
        """Partial rule: only from: line present."""
        body = "ROUTE:\n  from: @osap.gov.on.ca\n\nBehavior here."

        result = parse_route_criteria(body)

        assert result["from"] == ["@osap.gov.on.ca"]
        assert result["subject"] == []

    def test_behavioral_instruction_not_included_in_criteria(self):
        """Parsing extracts only ROUTE: block — behavioral text not included."""
        body = "ROUTE:\n  subject: scholarship\n\nAlways mark as priority 5."

        result = parse_route_criteria(body)

        assert "Always mark" not in str(result)


# ---------------------------------------------------------------------------
# Critical section 2: _email_matches_criteria
# ---------------------------------------------------------------------------

class TestEmailMatchesCriteria:

    def _email(self, sender="someone@example.com", subject="Hello"):
        return {
            "sender": sender,
            "subject": subject,
            "label_names": [],
        }

    def test_matches_sender_domain(self):
        """from: @lakeheadu.ca matches sender ending in that domain."""
        email = self._email(sender="User <hbantel@lakeheadu.ca>")
        criteria = {"from": ["@lakeheadu.ca"], "subject": []}

        assert _email_matches_criteria(email, criteria) is True

    def test_matches_full_sender_address(self):
        """from: full address matches exact sender."""
        email = self._email(sender="hbantel@lakeheadu.ca")
        criteria = {"from": ["hbantel@lakeheadu.ca"], "subject": []}

        assert _email_matches_criteria(email, criteria) is True

    def test_matches_subject_keyword_case_insensitive(self):
        """subject: OSAP matches email with subject containing 'osap' or 'OSAP'."""
        email = self._email(subject="Your OSAP Assessment is Ready")
        criteria = {"from": [], "subject": ["osap"]}

        assert _email_matches_criteria(email, criteria) is True

    def test_no_match_returns_false(self):
        """Email from unrelated sender with unrelated subject → no match."""
        email = self._email(sender="promo@amazon.ca", subject="Sale ends tonight")
        criteria = {"from": ["@lakeheadu.ca"], "subject": ["osap", "tuition"]}

        assert _email_matches_criteria(email, criteria) is False

    def test_empty_criteria_never_matches(self):
        """Empty criteria dict → nothing matches."""
        email = self._email(sender="anyone@anywhere.com", subject="Anything")

        assert _email_matches_criteria(email, {}) is False


# ---------------------------------------------------------------------------
# Critical section 3 & 4: apply_routing_labels
# ---------------------------------------------------------------------------

class TestApplyRoutingLabels:

    def _make_service(self):
        svc = mock.MagicMock()
        # Use .return_value chain to avoid calling modify() during setup
        svc.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}
        return svc

    def test_matching_email_gets_label_applied(self, monkeypatch):
        """AC2: email matching from: criteria has label applied via Gmail API."""
        import fetch_emails
        monkeypatch.setattr(fetch_emails, "_LABEL_ID_CACHE", {"Label_99": "Lakehead University"})

        svc = self._make_service()
        emails = [{
            "message_id": "msg_001",
            "sender": "admin@lakeheadu.ca",
            "subject": "Registration Open",
            "label_names": [],
        }]
        label_rules = {"Lakehead University": "ROUTE:\n  from: @lakeheadu.ca\n\nHigh priority."}

        apply_routing_labels(svc, emails, label_rules)

        svc.users.return_value.messages.return_value.modify.assert_called_once()
        assert "Lakehead University" in emails[0]["label_names"]

    def test_already_labeled_email_not_re_applied(self, monkeypatch):
        """AC3: email already carrying the label skips the API call."""
        import fetch_emails
        monkeypatch.setattr(fetch_emails, "_LABEL_ID_CACHE", {"Label_99": "Lakehead University"})

        svc = self._make_service()
        emails = [{
            "message_id": "msg_002",
            "sender": "admin@lakeheadu.ca",
            "subject": "Tuition Due",
            "label_names": ["Lakehead University"],
        }]
        label_rules = {"Lakehead University": "ROUTE:\n  from: @lakeheadu.ca\n\nHigh priority."}

        apply_routing_labels(svc, emails, label_rules)

        svc.users.return_value.messages.return_value.modify.assert_not_called()

    def test_unknown_label_name_logs_warning_no_crash(self, monkeypatch, capsys):
        """Edge: label name in rule not found in cache → warning, no crash."""
        import fetch_emails
        monkeypatch.setattr(fetch_emails, "_LABEL_ID_CACHE", {})  # empty cache

        svc = self._make_service()
        emails = [{
            "message_id": "msg_003",
            "sender": "admin@lakeheadu.ca",
            "subject": "Tuition Due",
            "label_names": [],
        }]
        label_rules = {"Lakehead University": "ROUTE:\n  from: @lakeheadu.ca\n\nHigh priority."}

        apply_routing_labels(svc, emails, label_rules)

        out = capsys.readouterr().out
        assert "not found" in out
        svc.users.return_value.messages.return_value.modify.assert_not_called()

    def test_api_error_logs_warning_continues(self, monkeypatch, capsys):
        """AC4: Gmail API error on one email → warning printed, no crash."""
        import fetch_emails
        from googleapiclient.errors import HttpError
        monkeypatch.setattr(fetch_emails, "_LABEL_ID_CACHE", {"Label_99": "Lakehead University"})

        svc = self._make_service()
        svc.users().messages().modify().execute.side_effect = HttpError(
            resp=mock.Mock(status=403), content=b"forbidden"
        )
        emails = [{
            "message_id": "msg_004",
            "sender": "admin@lakeheadu.ca",
            "subject": "Tuition Due",
            "label_names": [],
        }]
        label_rules = {"Lakehead University": "ROUTE:\n  from: @lakeheadu.ca\n\nHigh priority."}

        apply_routing_labels(svc, emails, label_rules)  # must not raise

        out = capsys.readouterr().out
        assert "[warning]" in out

    def test_rule_without_route_section_skips_routing(self, monkeypatch):
        """AC5: rule body with no ROUTE: block — no API calls made."""
        import fetch_emails
        monkeypatch.setattr(fetch_emails, "_LABEL_ID_CACHE", {"Label_99": "Lakehead University"})

        svc = self._make_service()
        emails = [{
            "message_id": "msg_005",
            "sender": "admin@lakeheadu.ca",
            "subject": "Tuition Due",
            "label_names": [],
        }]
        label_rules = {"Lakehead University": "Treat as high priority. No routing section."}

        apply_routing_labels(svc, emails, label_rules)

        svc.users.return_value.messages.return_value.modify.assert_not_called()
