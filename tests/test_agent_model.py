"""
sprint 22 — tests for [AGENT MODEL] per-account model switching.

Critical sections:
  - fetch_model_commands(): self-sent security gate + subject parsing
  - handle_model_command(): Ollama check/pull, config write, buffer append, Gemini guard
  - _get_account_model(): per-account config read with env fallback
"""

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import fetch_emails
import analyze_emails


# ============================================================================
# sprint 22 — fetch_model_commands(): self-sent security gate + subject parsing
# ============================================================================

class TestFetchModelCommands:

    def _make_msg(self, msg_id: str, sender: str, subject: str) -> dict:
        return {
            "id": msg_id,
            "payload": {
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "Subject", "value": subject},
                ]
            },
        }

    def test_self_sent_model_command_returned(self):
        service = mock.MagicMock()
        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg1"}]
        }
        service.users().messages().get().execute.return_value = self._make_msg(
            "msg1", "<me@gmail.com>", "[AGENT MODEL] llama3.2"
        )
        cmds, excluded = fetch_emails.fetch_model_commands(service, "me@gmail.com")
        assert len(cmds) == 1
        assert cmds[0][0] == "msg1"
        assert cmds[0][1] == "llama3.2"
        assert "msg1" in excluded

    def test_non_self_sent_ignored(self):
        service = mock.MagicMock()
        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg1"}]
        }
        service.users().messages().get().execute.return_value = self._make_msg(
            "msg1", "<other@gmail.com>", "[AGENT MODEL] llama3.2"
        )
        cmds, excluded = fetch_emails.fetch_model_commands(service, "me@gmail.com")
        assert cmds == []
        assert excluded == set()

    def test_empty_inbox_returns_empty(self):
        service = mock.MagicMock()
        service.users().messages().list().execute.return_value = {"messages": []}
        cmds, excluded = fetch_emails.fetch_model_commands(service, "me@gmail.com")
        assert cmds == []
        assert excluded == set()

    def test_api_error_returns_empty(self):
        service = mock.MagicMock()
        service.users().messages().list().execute.side_effect = Exception("API error")
        cmds, excluded = fetch_emails.fetch_model_commands(service, "me@gmail.com")
        assert cmds == []
        assert excluded == set()

    def test_model_name_trimmed(self):
        service = mock.MagicMock()
        service.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg1"}]
        }
        service.users().messages().get().execute.return_value = self._make_msg(
            "msg1", "<me@gmail.com>", "[AGENT MODEL]  llama3.2  "
        )
        cmds, excluded = fetch_emails.fetch_model_commands(service, "me@gmail.com")
        assert cmds[0][1] == "llama3.2"


# ============================================================================
# sprint 22 — handle_model_command(): Ollama check/pull, config write, buffer
# ============================================================================

class TestHandleModelCommand:

    def _make_service(self):
        svc = mock.MagicMock()
        svc.users().labels().list().execute.return_value = {"labels": []}
        svc.users().labels().create().execute.return_value = {"id": "label1"}
        svc.users().messages().modify().execute.return_value = {}
        return svc

    def _make_ollama_client(self, installed_models=None, pull_raises=None):
        client = mock.MagicMock()
        models = installed_models or []
        client.list.return_value = mock.MagicMock(
            models=[mock.MagicMock(model=m) for m in models]
        )
        if pull_raises:
            client.pull.side_effect = pull_raises
        return client

    def test_model_already_installed_writes_config(self, tmp_path):
        svc = self._make_service()
        written = {}

        with mock.patch("analyze_emails._write_model_config", side_effect=lambda p, m: written.update({"model": m})), \
             mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._write_text"), \
             mock.patch("analyze_emails._consume_command"), \
             mock.patch("analyze_emails._make_ollama_client",
                        return_value=self._make_ollama_client(installed_models=["llama3.2"])):
            analyze_emails.handle_model_command(
                service=svc,
                cmds=[("msg1", "llama3.2")],
                account_id="test",
            )

        assert written.get("model") == "llama3.2"

    def test_model_already_installed_no_pull(self, tmp_path):
        svc = self._make_service()

        with mock.patch("analyze_emails._write_model_config"), \
             mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._write_text"), \
             mock.patch("analyze_emails._consume_command"), \
             mock.patch("analyze_emails._make_ollama_client",
                        return_value=self._make_ollama_client(installed_models=["llama3.2"])) as mock_client_fn:
            analyze_emails.handle_model_command(
                service=svc,
                cmds=[("msg1", "llama3.2")],
                account_id="test",
            )

        client = mock_client_fn.return_value
        client.pull.assert_not_called()

    def test_model_not_installed_pulls_then_writes_config(self):
        svc = self._make_service()
        written = {}

        with mock.patch("analyze_emails._write_model_config", side_effect=lambda p, m: written.update({"model": m})), \
             mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._write_text"), \
             mock.patch("analyze_emails._consume_command"), \
             mock.patch("analyze_emails._make_ollama_client",
                        return_value=self._make_ollama_client(installed_models=[])):
            analyze_emails.handle_model_command(
                service=svc,
                cmds=[("msg1", "llama3.2")],
                account_id="test",
            )

        assert written.get("model") == "llama3.2"

    def test_pull_failure_no_config_written(self):
        svc = self._make_service()
        written = {}

        with mock.patch("analyze_emails._write_model_config", side_effect=lambda p, m: written.update({"model": m})), \
             mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._write_text"), \
             mock.patch("analyze_emails._consume_command"), \
             mock.patch("analyze_emails._make_ollama_client",
                        return_value=self._make_ollama_client(
                            installed_models=[],
                            pull_raises=Exception("model not found")
                        )):
            analyze_emails.handle_model_command(
                service=svc,
                cmds=[("msg1", "llama3.2")],
                account_id="test",
            )

        assert "model" not in written

    def test_pull_failure_error_appended_to_buffer(self):
        svc = self._make_service()
        appended = {}

        def fake_write(path, content):
            appended[path] = content

        with mock.patch("analyze_emails._write_model_config"), \
             mock.patch("analyze_emails._read_text", return_value="existing"), \
             mock.patch("analyze_emails._write_text", side_effect=fake_write), \
             mock.patch("analyze_emails._consume_command"), \
             mock.patch("analyze_emails._make_ollama_client",
                        return_value=self._make_ollama_client(
                            installed_models=[],
                            pull_raises=Exception("model not found")
                        )):
            analyze_emails.handle_model_command(
                service=svc,
                cmds=[("msg1", "llama3.2")],
                account_id="test",
            )

        # buffer should have been updated with error content
        assert any("error" in str(v).lower() or "llama3.2" in str(v) for v in appended.values())

    def test_empty_model_name_no_config_written(self):
        svc = self._make_service()
        written = {}

        with mock.patch("analyze_emails._write_model_config", side_effect=lambda p, m: written.update({"model": m})), \
             mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._write_text"), \
             mock.patch("analyze_emails._consume_command"), \
             mock.patch("analyze_emails._make_ollama_client",
                        return_value=self._make_ollama_client()):
            analyze_emails.handle_model_command(
                service=svc,
                cmds=[("msg1", "")],
                account_id="test",
            )

        assert "model" not in written

    def test_gemini_backend_no_config_written(self):
        svc = self._make_service()
        written = {}

        with mock.patch("analyze_emails._write_model_config", side_effect=lambda p, m: written.update({"model": m})), \
             mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._write_text"), \
             mock.patch("analyze_emails._consume_command"), \
             mock.patch.dict(os.environ, {"MODEL_BACKEND": "gemini"}):
            analyze_emails.handle_model_command(
                service=svc,
                cmds=[("msg1", "llama3.2")],
                account_id="test",
            )

        assert "model" not in written

    def test_command_consumed_on_success(self):
        svc = self._make_service()

        with mock.patch("analyze_emails._write_model_config"), \
             mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._write_text"), \
             mock.patch("analyze_emails._consume_command") as mock_consume, \
             mock.patch("analyze_emails._make_ollama_client",
                        return_value=self._make_ollama_client(installed_models=["llama3.2"])):
            analyze_emails.handle_model_command(
                service=svc,
                cmds=[("msg1", "llama3.2")],
                account_id="test",
            )

        mock_consume.assert_called_once()

    def test_command_consumed_on_failure(self):
        """Command must be consumed even when pull fails."""
        svc = self._make_service()

        with mock.patch("analyze_emails._write_model_config"), \
             mock.patch("analyze_emails._read_text", return_value=""), \
             mock.patch("analyze_emails._write_text"), \
             mock.patch("analyze_emails._consume_command") as mock_consume, \
             mock.patch("analyze_emails._make_ollama_client",
                        return_value=self._make_ollama_client(
                            installed_models=[],
                            pull_raises=Exception("bad model")
                        )):
            analyze_emails.handle_model_command(
                service=svc,
                cmds=[("msg1", "llama3.2")],
                account_id="test",
            )

        mock_consume.assert_called_once()


# ============================================================================
# sprint 22 — _get_account_model(): per-account config read with env fallback
# ============================================================================

class TestGetAccountModel:

    def test_returns_model_from_config(self, tmp_path):
        config = tmp_path / "model_config_test.json"
        config.write_text(json.dumps({"model": "llama3.2"}))

        with mock.patch("analyze_emails.account_file_path", return_value=str(config)):
            result = analyze_emails._get_account_model("test")

        assert result == "llama3.2"

    def test_falls_back_to_env_when_no_config(self, tmp_path):
        missing = str(tmp_path / "model_config_none.json")

        with mock.patch("analyze_emails.account_file_path", return_value=missing), \
             mock.patch.dict(os.environ, {"LOCAL_MODEL": "mistral:latest"}):
            result = analyze_emails._get_account_model("test")

        assert result == "mistral:latest"

    def test_falls_back_to_default_when_no_config_no_env(self, tmp_path):
        missing = str(tmp_path / "model_config_none.json")
        env = {k: v for k, v in os.environ.items() if k != "LOCAL_MODEL"}

        with mock.patch("analyze_emails.account_file_path", return_value=missing), \
             mock.patch.dict(os.environ, env, clear=True):
            result = analyze_emails._get_account_model("test")

        assert result == "llama3.1:latest"
