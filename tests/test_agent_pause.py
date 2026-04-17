"""
Tests for Sprint 14 — [AGENT PAUSE] with duration/schedule and [AGENT RESUME].

Critical sections:
  1. parse_pause_command — wrong parse = wrong pause window silently
  2. _is_pause_active — core run/don't-run decision; silent error in either direction
  3. load_pause_state — security gate + auto-expire side effect
  4. Resume command sender validation — security gate
"""

import sys
from pathlib import Path
from unittest import mock
from datetime import datetime, date, time, timezone, timedelta

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from fetch_emails import parse_pause_command, _is_pause_active, load_pause_state


# ---------------------------------------------------------------------------
# Critical section 1: parse_pause_command
# ---------------------------------------------------------------------------

class TestParsePauseCommand:

    def test_empty_body_returns_indefinite(self):
        """Empty body → indefinite pause."""
        result = parse_pause_command("")
        assert result["type"] == "indefinite"

    def test_no_body_keyword_returns_indefinite(self):
        """Body with no recognised keyword → fallback indefinite."""
        result = parse_pause_command("going on holiday")
        assert result["type"] == "indefinite"

    def test_duration_hours_parsed(self):
        """duration: 2 hours → type=duration, amount=2, unit=hours."""
        result = parse_pause_command("duration: 2 hours")
        assert result == {"type": "duration", "amount": 2, "unit": "hours"}

    def test_duration_days_parsed(self):
        """duration: 3 days → type=duration, amount=3, unit=days."""
        result = parse_pause_command("duration: 3 days")
        assert result == {"type": "duration", "amount": 3, "unit": "days"}

    def test_duration_weeks_parsed(self):
        """duration: 1 week → type=duration, amount=1, unit=weeks."""
        result = parse_pause_command("duration: 1 week")
        assert result == {"type": "duration", "amount": 1, "unit": "weeks"}

    def test_duration_years_parsed(self):
        """duration: 1 year → type=duration, amount=1, unit=years."""
        result = parse_pause_command("duration: 1 year")
        assert result == {"type": "duration", "amount": 1, "unit": "years"}

    def test_until_date_parsed(self):
        """until: 2026-04-20 → type=until, date=date(2026,4,20)."""
        result = parse_pause_command("until: 2026-04-20")
        assert result["type"] == "until"
        assert result["date"] == date(2026, 4, 20)

    def test_daily_window_parsed(self):
        """daily: 12:00 - 17:00 → type=daily, start=time(12,0), end=time(17,0)."""
        result = parse_pause_command("daily: 12:00 - 17:00")
        assert result["type"] == "daily"
        assert result["start"] == time(12, 0)
        assert result["end"] == time(17, 0)

    def test_invalid_until_date_falls_back_to_indefinite(self):
        """Unparseable until: date → fallback indefinite."""
        result = parse_pause_command("until: not-a-date")
        assert result["type"] == "indefinite"

    def test_invalid_daily_window_falls_back_to_indefinite(self):
        """Unparseable daily: window → fallback indefinite."""
        result = parse_pause_command("daily: noon to five")
        assert result["type"] == "indefinite"

    def test_duration_singular_unit_normalised(self):
        """'hour' and 'hours' both accepted."""
        result = parse_pause_command("duration: 1 hour")
        assert result["unit"] == "hours"

    def test_duration_invalid_amount_falls_back(self):
        """Non-numeric amount → fallback indefinite."""
        result = parse_pause_command("duration: many days")
        assert result["type"] == "indefinite"


# ---------------------------------------------------------------------------
# Critical section 2: _is_pause_active
# ---------------------------------------------------------------------------

class TestIsPauseActive:

    def _utc(self, **kwargs):
        return datetime(2026, 4, 12, 13, 0, 0, tzinfo=timezone.utc).replace(**kwargs)

    def test_indefinite_always_active(self):
        """type=indefinite → always active regardless of time."""
        cmd = {"type": "indefinite"}
        assert _is_pause_active(cmd, send_time=self._utc(), now=self._utc()) is True

    def test_duration_active_within_window(self):
        """duration: 3 days — 1 day after send → active."""
        send = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
        now  = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)  # 1 day later
        cmd  = {"type": "duration", "amount": 3, "unit": "days"}
        assert _is_pause_active(cmd, send_time=send, now=now) is True

    def test_duration_expired_after_window(self):
        """duration: 3 days — 4 days after send → expired."""
        send = datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc)
        now  = datetime(2026, 4, 12, 13, 0, tzinfo=timezone.utc)  # 4+ days later
        cmd  = {"type": "duration", "amount": 3, "unit": "days"}
        assert _is_pause_active(cmd, send_time=send, now=now) is False

    def test_duration_zero_is_expired(self):
        """duration: 0 hours → immediately expired."""
        now = datetime(2026, 4, 12, 13, 0, tzinfo=timezone.utc)
        cmd = {"type": "duration", "amount": 0, "unit": "hours"}
        assert _is_pause_active(cmd, send_time=now, now=now) is False

    def test_until_active_before_date(self):
        """until: 2026-04-20 — today is 2026-04-12 → active."""
        cmd = {"type": "until", "date": date(2026, 4, 20)}
        now = datetime(2026, 4, 12, 13, 0, tzinfo=timezone.utc)
        assert _is_pause_active(cmd, send_time=now, now=now) is True

    def test_until_expired_on_date(self):
        """until: 2026-04-12 — today is 2026-04-12 → expired (inclusive end)."""
        cmd = {"type": "until", "date": date(2026, 4, 12)}
        now = datetime(2026, 4, 12, 13, 0, tzinfo=timezone.utc)
        assert _is_pause_active(cmd, send_time=now, now=now) is False

    def test_daily_active_within_window(self):
        """daily: 12:00-17:00 — current time 13:00 → active."""
        cmd = {"type": "daily", "start": time(12, 0), "end": time(17, 0)}
        now = datetime(2026, 4, 12, 13, 0, tzinfo=timezone.utc)
        assert _is_pause_active(cmd, send_time=now, now=now) is True

    def test_daily_inactive_outside_window(self):
        """daily: 12:00-17:00 — current time 18:00 → inactive."""
        cmd = {"type": "daily", "start": time(12, 0), "end": time(17, 0)}
        now = datetime(2026, 4, 12, 18, 0, tzinfo=timezone.utc)
        assert _is_pause_active(cmd, send_time=now, now=now) is False

    def test_daily_inactive_before_window(self):
        """daily: 12:00-17:00 — current time 09:00 → inactive."""
        cmd = {"type": "daily", "start": time(12, 0), "end": time(17, 0)}
        now = datetime(2026, 4, 12, 9, 0, tzinfo=timezone.utc)
        assert _is_pause_active(cmd, send_time=now, now=now) is False


# ---------------------------------------------------------------------------
# Critical section 3 & 4: load_pause_state — security + auto-expire
# ---------------------------------------------------------------------------

class TestLoadPauseState:

    def _make_service(self, pause_messages, resume_messages, pause_body="", pause_sender="me@gmail.com", pause_date="Sat, 12 Apr 2026 01:00:00 +0000"):
        svc = mock.MagicMock()

        def list_side_effect(*args, **kwargs):
            q = kwargs.get("q", "")
            result = mock.MagicMock()
            if "AGENT PAUSE" in q:
                result.execute.return_value = {"messages": pause_messages}
            elif "AGENT RESUME" in q:
                result.execute.return_value = {"messages": resume_messages}
            else:
                result.execute.return_value = {"messages": []}
            return result

        svc.users.return_value.messages.return_value.list.side_effect = list_side_effect
        svc.users.return_value.messages.return_value.modify.return_value.execute.return_value = {}

        get_result = mock.MagicMock()
        get_result.execute.return_value = {
            "id": pause_messages[0]["id"] if pause_messages else "x",
            "payload": {"headers": [], "body": {"data": ""}, "parts": []},
            "labelIds": [],
        }
        svc.users.return_value.messages.return_value.get.return_value = get_result

        return svc

    def test_no_pause_emails_returns_not_paused(self):
        """No [AGENT PAUSE] emails → not paused, empty excluded set."""
        svc = self._make_service(pause_messages=[], resume_messages=[])
        paused, excluded = load_pause_state(svc, "me@gmail.com")
        assert paused is False
        assert excluded == set()

    def test_external_sender_on_pause_rejected(self, capsys):
        """External sender on [AGENT PAUSE] → ignored, warning logged."""
        svc = self._make_service(
            pause_messages=[{"id": "p_001"}],
            resume_messages=[],
        )
        with mock.patch("fetch_emails._get_header") as mock_hdr, \
             mock.patch("fetch_emails._decode_body") as mock_body:
            mock_hdr.side_effect = lambda h, n: {
                "From": "attacker@evil.com", "Date": "Sat, 12 Apr 2026 01:00:00 +0000"
            }.get(n, "")
            mock_body.return_value = ""
            paused, excluded = load_pause_state(svc, "me@gmail.com")

        assert paused is False
        out = capsys.readouterr().out
        assert "ignored" in out

    def test_external_sender_on_resume_rejected(self, capsys):
        """External sender on [AGENT RESUME] → ignored, agent still checks pause."""
        svc = self._make_service(
            pause_messages=[],
            resume_messages=[{"id": "r_001"}],
        )
        with mock.patch("fetch_emails._get_header") as mock_hdr, \
             mock.patch("fetch_emails._decode_body") as mock_body:
            mock_hdr.side_effect = lambda h, n: {
                "From": "attacker@evil.com", "Date": "Sat, 12 Apr 2026 01:00:00 +0000"
            }.get(n, "")
            mock_body.return_value = ""
            paused, excluded = load_pause_state(svc, "me@gmail.com")

        out = capsys.readouterr().out
        assert "ignored" in out

    def test_valid_indefinite_pause_returns_paused(self):
        """Valid self-sent indefinite pause → paused=True, message excluded."""
        svc = self._make_service(
            pause_messages=[{"id": "p_001"}],
            resume_messages=[],
        )
        with mock.patch("fetch_emails._get_header") as mock_hdr, \
             mock.patch("fetch_emails._decode_body") as mock_body, \
             mock.patch("fetch_emails._is_pause_active", return_value=True):
            mock_hdr.side_effect = lambda h, n: {
                "From": "me@gmail.com", "Date": "Sat, 12 Apr 2026 01:00:00 +0000"
            }.get(n, "")
            mock_body.return_value = ""
            paused, excluded = load_pause_state(svc, "me@gmail.com")

        assert paused is True
        assert "p_001" in excluded

    def test_expired_pause_marked_done_not_paused(self):
        """Expired pause → not paused, modify() called to mark AI Processed."""
        svc = self._make_service(
            pause_messages=[{"id": "p_exp"}],
            resume_messages=[],
        )
        with mock.patch("fetch_emails._get_header") as mock_hdr, \
             mock.patch("fetch_emails._decode_body") as mock_body, \
             mock.patch("fetch_emails._is_pause_active", return_value=False):
            mock_hdr.side_effect = lambda h, n: {
                "From": "me@gmail.com", "Date": "Sat, 12 Apr 2026 01:00:00 +0000"
            }.get(n, "")
            mock_body.return_value = "duration: 1 hour"
            paused, excluded = load_pause_state(svc, "me@gmail.com")

        assert paused is False
        svc.users.return_value.messages.return_value.modify.assert_called()

    def test_resume_command_overrides_active_pause(self, capsys):
        """Valid [AGENT RESUME] → not paused even if pause email exists, all marked done."""
        svc = self._make_service(
            pause_messages=[{"id": "p_001"}],
            resume_messages=[{"id": "r_001"}],
        )
        with mock.patch("fetch_emails._get_header") as mock_hdr, \
             mock.patch("fetch_emails._decode_body") as mock_body:
            mock_hdr.side_effect = lambda h, n: {
                "From": "me@gmail.com", "Date": "Sat, 12 Apr 2026 01:00:00 +0000"
            }.get(n, "")
            mock_body.return_value = ""
            paused, excluded = load_pause_state(svc, "me@gmail.com")

        assert paused is False
        out = capsys.readouterr().out
        assert "RESUMED" in out
