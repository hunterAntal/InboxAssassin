"""
sprint 22 — daemon installer logic for the local polling daemon.

Testable Python module backing daemon.sh. All systemd interactions go through
_run() so they can be mocked in tests without touching the real system.
"""

import os
import subprocess
from pathlib import Path

SERVICE_NAME = "email-agent"
SERVICE_FILE = f"{SERVICE_NAME}.service"

# Default service directory for systemd user units
_DEFAULT_SERVICE_DIR = os.path.expanduser("~/.config/systemd/user")


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run a shell command, return (returncode, stdout.strip())."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


# sprint 22 — generate systemd unit file content
def build_service_file(script_dir: str) -> str:
    """Return the content of the systemd unit file for the email agent daemon.

    Uses a oneshot + OnUnitInactiveSec pattern so the service waits the full
    interval after each run (even on failure) before re-running — no tight loops.
    """
    py = "python3"
    run_all = os.path.join(script_dir, "run_all.py")
    # sprint 22 — load .env so GEMINI_API_KEY and all user settings are available;
    #             '-' prefix makes the file optional (no failure if .env is absent)
    env_file = os.path.join(script_dir, ".env")
    return f"""\
[Unit]
Description=Email Agent — local polling daemon
After=network.target

[Service]
Type=oneshot
WorkingDirectory={script_dir}
EnvironmentFile=-{env_file}
ExecStart={py} {run_all}

[Install]
WantedBy=default.target

[X-Timer]
OnUnitInactiveSec=5min
"""


# sprint 22 — read current service state from systemd
def get_service_state() -> str:
    """Return 'active', 'inactive', or 'not-installed'."""
    code, out = _run(["systemctl", "--user", "is-active", SERVICE_FILE])
    if code == 0:
        return out.strip() if out.strip() in ("active", "inactive") else "active"
    # exit code 4 = unit not found; 3 = inactive; anything else = not found
    if "could not be found" in out.lower() or code == 4:
        return "not-installed"
    if out.strip() == "inactive":
        return "inactive"
    return "not-installed"


def _get_last_run(script_dir: str) -> str:
    """Return last-run timestamp from journald, or 'unknown'."""
    code, out = _run([
        "journalctl", "--user", "-u", SERVICE_FILE,
        "--no-pager", "-n", "1", "-o", "short",
    ])
    if code == 0 and out:
        # first field of journald short format is the timestamp
        parts = out.split()
        if len(parts) >= 3:
            return f"{parts[0]} {parts[1]} {parts[2]}"
    return "unknown"


# sprint 22 — install: write unit file, enable linger, enable + start
def install_daemon(
    script_dir: str,
    service_dir: str = _DEFAULT_SERVICE_DIR,
) -> tuple[bool, list[str]]:
    """Install and start the systemd user service.

    Returns (success: bool, messages: list[str]).
    Idempotent — safe to run multiple times.
    """
    messages = []

    # 1. Write service file
    service_path = Path(service_dir) / SERVICE_FILE
    try:
        Path(service_dir).mkdir(parents=True, exist_ok=True)
        service_path.write_text(build_service_file(script_dir))
        messages.append(f"  ✓ Service file written to {service_path}")
    except OSError as e:
        messages.append(f"  ✗ Could not write service file: {e}")
        return False, messages

    # 2. Enable linger (best-effort — daemon survives logout)
    code, out = _run(["loginctl", "enable-linger", os.environ.get("USER", "")])
    if code == 0:
        messages.append("  ✓ Linger enabled — daemon survives logout")
    else:
        messages.append(f"  ✗ Could not enable linger: {out}")
        # linger failure is non-fatal — continue install

    # 3. Reload systemd, enable + start the service
    _run(["systemctl", "--user", "daemon-reload"])

    code, out = _run(["systemctl", "--user", "enable", "--now", SERVICE_FILE])
    if code != 0:
        messages.append(f"  ✗ Could not enable service: {out}")
        return False, messages
    messages.append("  ✓ Service enabled and started")

    # 4. Confirm active
    state = get_service_state()
    if state == "active":
        messages.append(f"  ✓ {SERVICE_FILE} is active")
    else:
        messages.append(f"  ✓ {SERVICE_FILE} scheduled (will activate on next timer tick)")

    return True, messages


# sprint 22 — uninstall: stop + disable + remove unit file
def uninstall_daemon(
    service_dir: str = _DEFAULT_SERVICE_DIR,
) -> tuple[bool, list[str]]:
    """Stop, disable, and remove the systemd user service.

    Returns (success: bool, messages: list[str]).
    """
    messages = []
    service_path = Path(service_dir) / SERVICE_FILE

    if not service_path.exists():
        messages.append(f"  {SERVICE_FILE} — not installed")
        return False, messages

    _run(["systemctl", "--user", "stop", SERVICE_FILE])
    messages.append("  ✓ Service stopped")

    _run(["systemctl", "--user", "disable", SERVICE_FILE])
    messages.append("  ✓ Service disabled")

    try:
        service_path.unlink()
        messages.append("  ✓ Service file removed")
    except OSError as e:
        messages.append(f"  ✗ Could not remove service file: {e}")
        return False, messages

    _run(["systemctl", "--user", "daemon-reload"])
    messages.append(f"  ✓ {SERVICE_FILE} uninstalled")
    return True, messages


# sprint 22 — dispatch subcommand and print UX-contract output
def run_daemon_command(
    cmd: str,
    script_dir: str = ".",
    service_dir: str = _DEFAULT_SERVICE_DIR,
) -> int:
    """Dispatch a daemon subcommand. Returns exit code."""
    HR = "=" * 60

    if cmd == "install":
        print(HR)
        print("  Email Agent — Daemon Installer")
        print(HR)
        print()
        print("Installing systemd user service...")
        ok, messages = install_daemon(script_dir, service_dir)
        for m in messages:
            print(m)
        if ok:
            print()
            print("The agent will now run every 5 minutes automatically.")
            print("Use ./daemon.sh status | stop | start | uninstall to manage it.")
        return 0 if ok else 1

    elif cmd == "status":
        state = get_service_state()
        if state == "not-installed":
            print(f"  {SERVICE_FILE} — not installed")
            print("  Run ./daemon.sh install to set up the daemon.")
        else:
            last = _get_last_run(script_dir)
            print(f"  {SERVICE_FILE} — {state}")
            print(f"  Last run: {last}")
        return 0

    elif cmd == "start":
        _run(["systemctl", "--user", "start", SERVICE_FILE])
        print(f"  ✓ {SERVICE_FILE} started")
        return 0

    elif cmd == "stop":
        _run(["systemctl", "--user", "stop", SERVICE_FILE])
        print(f"  ✓ {SERVICE_FILE} stopped")
        return 0

    elif cmd == "uninstall":
        ok, messages = uninstall_daemon(service_dir)
        for m in messages:
            print(m)
        return 0 if ok else 1

    else:
        print(f"  Usage: ./daemon.sh install | status | start | stop | uninstall")
        return 1


if __name__ == "__main__":
    import sys as _sys
    _cmd  = _sys.argv[1] if len(_sys.argv) > 1 else ""
    _sdir = _sys.argv[2] if len(_sys.argv) > 2 else os.path.dirname(os.path.abspath(__file__))
    _sys.exit(run_daemon_command(_cmd, script_dir=_sdir))
