"""
Tests for daemon installer logic — sprint 22.

Critical sections:
  CS1 — build_service_file: generates correct systemd unit content
  CS2 — get_service_state: parses systemctl output to active/inactive/not-installed
  CS3 — install_daemon: writes file, enables linger, enables + starts (mocked syscalls)
  CS4 — uninstall_daemon: stop + disable + remove file (mocked syscalls)
  CS5 — run_daemon_command: unknown subcommand prints usage and returns non-zero
"""

import sys
import os
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import daemon_helper


# ============================================================================
# CS1 — build_service_file
# ============================================================================

class TestBuildServiceFile:

    def test_contains_run_all_py_path(self):
        content = daemon_helper.build_service_file("/home/user/Dev/email-agent")
        assert "/home/user/Dev/email-agent/run_all.py" in content

    def test_working_directory_is_script_dir(self):
        content = daemon_helper.build_service_file("/home/user/Dev/email-agent")
        assert "WorkingDirectory=/home/user/Dev/email-agent" in content

    def test_interval_is_five_minutes(self):
        content = daemon_helper.build_service_file("/home/user/Dev/email-agent")
        assert "OnUnitInactiveSec=5min" in content

    def test_type_is_oneshot(self):
        content = daemon_helper.build_service_file("/home/user/Dev/email-agent")
        assert "Type=oneshot" in content

    def test_restart_on_failure_not_set(self):
        # Restart would cause tight loops on crash — must not be present
        content = daemon_helper.build_service_file("/home/user/Dev/email-agent")
        assert "Restart=always" not in content

    def test_uses_python3_executable(self):
        content = daemon_helper.build_service_file("/home/user/Dev/email-agent")
        assert "python3" in content

    # sprint 22 — EnvironmentFile wiring
    def test_environment_file_uses_absolute_path(self):
        content = daemon_helper.build_service_file("/home/user/Dev/email-agent")
        assert "EnvironmentFile" in content
        assert "/home/user/Dev/email-agent/.env" in content

    def test_environment_file_is_optional(self):
        # The '-' prefix makes systemd skip the file if absent instead of failing
        content = daemon_helper.build_service_file("/home/user/Dev/email-agent")
        assert "EnvironmentFile=-" in content

    def test_send_digest_not_hardcoded(self):
        # SEND_DIGEST must come from .env, not be overridden in the unit file
        content = daemon_helper.build_service_file("/home/user/Dev/email-agent")
        assert "SEND_DIGEST" not in content


# ============================================================================
# CS2 — get_service_state
# ============================================================================

class TestGetServiceState:

    def test_active_when_systemctl_says_active(self):
        with mock.patch("daemon_helper._run", return_value=(0, "active")):
            assert daemon_helper.get_service_state() == "active"

    def test_inactive_when_systemctl_says_inactive(self):
        with mock.patch("daemon_helper._run", return_value=(0, "inactive")):
            assert daemon_helper.get_service_state() == "inactive"

    def test_not_installed_when_systemctl_returns_nonzero(self):
        with mock.patch("daemon_helper._run", return_value=(4, "")):
            assert daemon_helper.get_service_state() == "not-installed"

    def test_not_installed_when_unit_file_missing(self):
        with mock.patch("daemon_helper._run", return_value=(1, "could not be found")):
            assert daemon_helper.get_service_state() == "not-installed"


# ============================================================================
# CS3 — install_daemon
# ============================================================================

class TestInstallDaemon:

    def _mock_run(self, returncode=0, output=""):
        return mock.patch("daemon_helper._run", return_value=(returncode, output))

    def test_writes_service_file(self, tmp_path):
        service_dir = tmp_path / ".config" / "systemd" / "user"
        with self._mock_run():
            daemon_helper.install_daemon(
                script_dir=str(tmp_path),
                service_dir=str(service_dir),
            )
        assert (service_dir / "email-agent.service").exists()

    def test_service_file_contains_script_dir(self, tmp_path):
        service_dir = tmp_path / ".config" / "systemd" / "user"
        with self._mock_run():
            daemon_helper.install_daemon(
                script_dir=str(tmp_path),
                service_dir=str(service_dir),
            )
        content = (service_dir / "email-agent.service").read_text()
        assert str(tmp_path) in content

    def test_creates_service_dir_if_missing(self, tmp_path):
        service_dir = tmp_path / "deep" / "nested" / "dir"
        with self._mock_run():
            daemon_helper.install_daemon(
                script_dir=str(tmp_path),
                service_dir=str(service_dir),
            )
        assert service_dir.exists()

    def test_returns_success_true_on_happy_path(self, tmp_path):
        service_dir = tmp_path / ".config" / "systemd" / "user"
        with self._mock_run():
            ok, messages = daemon_helper.install_daemon(
                script_dir=str(tmp_path),
                service_dir=str(service_dir),
            )
        assert ok is True

    def test_returns_messages_list(self, tmp_path):
        service_dir = tmp_path / ".config" / "systemd" / "user"
        with self._mock_run():
            ok, messages = daemon_helper.install_daemon(
                script_dir=str(tmp_path),
                service_dir=str(service_dir),
            )
        assert isinstance(messages, list)
        assert len(messages) > 0

    def test_idempotent_second_install_succeeds(self, tmp_path):
        service_dir = tmp_path / ".config" / "systemd" / "user"
        with self._mock_run():
            daemon_helper.install_daemon(str(tmp_path), str(service_dir))
            ok, _ = daemon_helper.install_daemon(str(tmp_path), str(service_dir))
        assert ok is True


# ============================================================================
# CS4 — uninstall_daemon
# ============================================================================

class TestUninstallDaemon:

    def _mock_run(self, returncode=0, output=""):
        return mock.patch("daemon_helper._run", return_value=(returncode, output))

    def test_removes_service_file(self, tmp_path):
        service_dir = tmp_path / ".config" / "systemd" / "user"
        service_dir.mkdir(parents=True)
        service_file = service_dir / "email-agent.service"
        service_file.write_text("[Unit]\nDescription=test\n")

        with self._mock_run():
            daemon_helper.uninstall_daemon(service_dir=str(service_dir))

        assert not service_file.exists()

    def test_returns_success_true_when_file_removed(self, tmp_path):
        service_dir = tmp_path / ".config" / "systemd" / "user"
        service_dir.mkdir(parents=True)
        (service_dir / "email-agent.service").write_text("[Unit]\n")

        with self._mock_run():
            ok, messages = daemon_helper.uninstall_daemon(service_dir=str(service_dir))

        assert ok is True

    def test_not_installed_returns_false(self, tmp_path):
        service_dir = tmp_path / ".config" / "systemd" / "user"
        service_dir.mkdir(parents=True)
        # no service file

        ok, messages = daemon_helper.uninstall_daemon(service_dir=str(service_dir))

        assert ok is False
        assert any("not installed" in m.lower() for m in messages)


# ============================================================================
# CS5 — run_daemon_command (unknown subcommand)
# ============================================================================

class TestRunDaemonCommand:

    def test_unknown_subcommand_returns_nonzero(self, tmp_path):
        with mock.patch("daemon_helper._run", return_value=(0, "inactive")):
            code = daemon_helper.run_daemon_command("badcmd", script_dir=str(tmp_path))
        assert code != 0

    def test_unknown_subcommand_message_contains_usage(self, tmp_path, capsys):
        with mock.patch("daemon_helper._run", return_value=(0, "inactive")):
            daemon_helper.run_daemon_command("badcmd", script_dir=str(tmp_path))
        out = capsys.readouterr().out
        assert "usage" in out.lower() or "install" in out.lower()

    def test_status_not_installed_returns_zero(self, tmp_path):
        with mock.patch("daemon_helper.get_service_state", return_value="not-installed"):
            code = daemon_helper.run_daemon_command("status", script_dir=str(tmp_path))
        assert code == 0
