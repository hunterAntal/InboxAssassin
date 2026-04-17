"""
sprint 22 — tests for multi-account OAuth additions.

Covers:
  - fetch_emails.main() --token-file flag
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================================
# sprint 22 — fetch_emails main() --token-file flag
# ============================================================================

class TestFetchEmailsTokenFileFlag:

    def test_default_token_file_used_when_no_flag(self, tmp_path, monkeypatch):
        """Calling main() with no args uses TOKEN_FILE (token.json)."""
        from fetch_emails import main, TOKEN_FILE

        monkeypatch.chdir(tmp_path)
        # Provide a credentials.json so the credentials check passes
        (tmp_path / "credentials.json").write_text("{}")

        captured = {}
        def fake_get_gmail_service(token_file=TOKEN_FILE):
            captured["token_file"] = token_file
            return mock.MagicMock()

        with mock.patch("fetch_emails.get_gmail_service", side_effect=fake_get_gmail_service), \
             mock.patch("fetch_emails.fetch_unprocessed_emails", return_value=[]):
            main()

        assert captured["token_file"] == TOKEN_FILE

    def test_custom_token_file_passed_through(self, tmp_path, monkeypatch):
        """Calling main(token_file='token_work.json') passes it to get_gmail_service."""
        from fetch_emails import main, TOKEN_FILE

        monkeypatch.chdir(tmp_path)
        (tmp_path / "credentials.json").write_text("{}")

        captured = {}
        def fake_get_gmail_service(token_file=TOKEN_FILE):
            captured["token_file"] = token_file
            return mock.MagicMock()

        with mock.patch("fetch_emails.get_gmail_service", side_effect=fake_get_gmail_service), \
             mock.patch("fetch_emails.fetch_unprocessed_emails", return_value=[]):
            main(token_file="token_work.json")

        assert captured["token_file"] == "token_work.json"

    def test_missing_credentials_exits_early(self, tmp_path, monkeypatch):
        """main() returns early (no crash) when credentials.json is missing."""
        from fetch_emails import main

        monkeypatch.chdir(tmp_path)
        # No credentials.json in tmp_path
        with mock.patch("fetch_emails.get_gmail_service") as mock_svc:
            main()
        mock_svc.assert_not_called()
