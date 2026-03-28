"""Unit tests for pre_filter.py."""

import json
import pytest
from unittest import mock
from pre_filter import (
    load_filter_config,
    _extract_domain,
    matches_filter,
    make_filtered_result,
    learn_from_results,
    FILTER_TLDR,
)

SAMPLE_CONFIG = {
    "sender_domains": ["chess.com", "deals.dominos.ca"],
    "subject_keywords": ["% off", "flash sale"],
}


class TestExtractDomain:
    def test_display_name_format(self):
        assert _extract_domain("Deals <noreply@deals.dominos.ca>") == "deals.dominos.ca"

    def test_bare_address(self):
        assert _extract_domain("noreply@chess.com") == "chess.com"

    def test_case_insensitive(self):
        assert _extract_domain("Name <User@Chess.COM>") == "chess.com"

    def test_malformed_returns_empty(self):
        assert _extract_domain("not-an-email") == ""

    def test_empty_string(self):
        assert _extract_domain("") == ""


class TestMatchesFilter:
    def test_matches_sender_domain(self):
        email = {"sender": "Chess <noreply@chess.com>", "subject": "Your game"}
        assert matches_filter(email, SAMPLE_CONFIG) is True

    def test_matches_subject_keyword(self):
        email = {"sender": "Unknown <info@unknown.com>", "subject": "50% off today only"}
        assert matches_filter(email, SAMPLE_CONFIG) is True

    def test_no_match(self):
        email = {"sender": "Prof Smith <smith@university.ca>", "subject": "Assignment due"}
        assert matches_filter(email, SAMPLE_CONFIG) is False

    def test_subject_keyword_case_insensitive(self):
        email = {"sender": "Shop <a@b.com>", "subject": "FLASH SALE now on"}
        assert matches_filter(email, SAMPLE_CONFIG) is True

    def test_empty_config_never_matches(self):
        email = {"sender": "noreply@chess.com", "subject": "flash sale"}
        assert matches_filter(email, {"sender_domains": [], "subject_keywords": []}) is False

    def test_missing_sender_key(self):
        email = {"subject": "Your game"}
        assert matches_filter(email, SAMPLE_CONFIG) is False


class TestLoadFilterConfig:
    def test_returns_config_when_file_exists(self, tmp_path, monkeypatch):
        config = {"sender_domains": ["spam.com"], "subject_keywords": ["buy now"]}
        config_file = tmp_path / "filter_config.json"
        config_file.write_text(json.dumps(config))
        monkeypatch.chdir(tmp_path)
        result = load_filter_config()
        assert result == config

    def test_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = load_filter_config()
        assert result == {"sender_domains": [], "subject_keywords": []}

    def test_returns_empty_on_invalid_json(self, tmp_path, monkeypatch):
        config_file = tmp_path / "filter_config.json"
        config_file.write_text("not valid json {{{")
        monkeypatch.chdir(tmp_path)
        result = load_filter_config()
        assert result == {"sender_domains": [], "subject_keywords": []}


class TestMakeFilteredResult:
    def test_returns_correct_shape(self):
        email = {"sender": "a@b.com", "subject": "test"}
        result = make_filtered_result(email)
        assert result["priority"] == 1
        assert result["action_required"] is False
        assert result["tldr"] == FILTER_TLDR
        assert result["event_date"] is None
        assert result["event_time"] is None
        assert result["event_title"] is None
        assert result["action_type"] is None
        assert result["pre_filtered"] is True

    def test_all_required_keys_present(self):
        result = make_filtered_result({})
        expected_keys = {
            "priority", "tldr", "action_required",
            "event_date", "event_time", "event_title",
            "action_type", "pre_filtered",
        }
        assert set(result.keys()) == expected_keys


class TestLearnFromResults:
    def _make_result(self, sender, priority, pre_filtered=False):
        email = {"sender": sender, "subject": "test"}
        info = {
            "priority": priority, "pre_filtered": pre_filtered,
            "tldr": "", "action_required": False,
            "event_date": None, "event_time": None,
            "event_title": None, "action_type": None,
        }
        return (email, info)

    def test_adds_new_domain_when_ai_assigns_priority_1(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        results = [self._make_result("Spam <noreply@newspam.com>", priority=1)]
        count = learn_from_results(results)
        assert count == 1
        config = json.loads((tmp_path / "filter_config.json").read_text())
        assert "newspam.com" in config["sender_domains"]

    def test_does_not_add_pre_filtered_domains(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        results = [self._make_result("Spam <noreply@chess.com>", priority=1, pre_filtered=True)]
        count = learn_from_results(results)
        assert count == 0

    def test_does_not_add_priority_above_1(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        results = [self._make_result("Prof <prof@university.ca>", priority=3)]
        count = learn_from_results(results)
        assert count == 0

    def test_does_not_add_duplicate_domain(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        config = {"sender_domains": ["existing.com"], "subject_keywords": []}
        (tmp_path / "filter_config.json").write_text(json.dumps(config))
        results = [self._make_result("A <a@existing.com>", priority=1)]
        count = learn_from_results(results)
        assert count == 0

    def test_returns_zero_when_nothing_to_learn(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        results = [self._make_result("Prof <prof@school.ca>", priority=4)]
        assert learn_from_results(results) == 0
