#!/bin/bash
# sprint 22 — local polling daemon control script
#
# Installs/manages a systemd user service that runs run_all.py every 5 minutes.
#
# Usage:
#   ./daemon.sh install    — install and start the daemon
#   ./daemon.sh status     — show current state and last run time
#   ./daemon.sh start      — start a stopped daemon
#   ./daemon.sh stop       — stop a running daemon
#   ./daemon.sh uninstall  — remove the daemon completely

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD="${1:-}"

if [[ -z "$CMD" ]]; then
  echo "  Usage: ./daemon.sh install | status | start | stop | uninstall"
  exit 1
fi

python3 "$SCRIPT_DIR/daemon_helper.py" "$CMD" "$SCRIPT_DIR"
