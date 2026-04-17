#!/bin/bash
# Local setup for the Email Agent.
#
# Walks a new user through:
#   1. Python version check
#   2. Dependency install
#   3. AI backend selection (Gemini or local Ollama)
#   4. Google OAuth (Gmail, Calendar, Tasks)
#   5. .env generation
#   6. Smoke test
#
# Usage:
#   bash setup.sh

set -euo pipefail

HR="============================================================"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Helpers ──────────────────────────────────────────────────────────────────

pass() { echo "  ✓ $*"; }
fail() { echo "  ✗ $*"; }
info() { echo "    $*"; }
arrow() { echo "    → $*"; }

# ── Banner ───────────────────────────────────────────────────────────────────

echo "$HR"
echo "  Email Agent — Local Setup"
echo "$HR"
echo ""

# ── Step 1: Python version ───────────────────────────────────────────────────

echo "Checking Python..."
PY_RESULT=$(python3 -c "
import setup_helper, sys
sys.path.insert(0, '.')
import setup_helper
ok, ver = setup_helper.check_python_version()
print(ok)
print(ver)
" 2>/dev/null) || true

PY_OK=$(echo "$PY_RESULT" | sed -n '1p')
PY_VER=$(echo "$PY_RESULT" | sed -n '2p')

if [[ "$PY_OK" != "True" ]]; then
  fail "Python ${PY_VER} — requires 3.10+"
  arrow "Install Python 3.10+: https://python.org/downloads"
  exit 1
fi
pass "Python ${PY_VER}"
echo ""

# ── Step 2: Dependencies ─────────────────────────────────────────────────────

echo "Installing dependencies..."
if python3 -m pip install -r "$SCRIPT_DIR/requirements.txt" -q; then
  pass "Dependencies installed"
else
  fail "Dependency install failed. See above for details."
  exit 1
fi
echo ""

# ── Step 3: Backend selection ─────────────────────────────────────────────────

echo "Which AI backend do you want to use?"
echo "  [1] Gemini  (requires API key — free tier available)"
echo "  [2] Local   (requires Ollama — fully offline)"
echo ""

BACKEND_CHOICE=""
while true; do
  read -rp "Enter 1 or 2: " BACKEND_CHOICE
  VALID=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
print(setup_helper.validate_backend('$BACKEND_CHOICE'))
")
  if [[ "$VALID" == "True" ]]; then
    break
  fi
  echo "  Invalid choice. Enter 1 or 2:"
done
echo ""

GEMINI_KEY=""
LOCAL_MODEL="gemma3:4b"

# ── Step 4a: Gemini ───────────────────────────────────────────────────────────

if [[ "$BACKEND_CHOICE" == "1" ]]; then
  while true; do
    read -rp "Enter your Gemini API key: " GEMINI_KEY
    VALID=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
print(setup_helper.validate_api_key('''$GEMINI_KEY'''))
")
    if [[ "$VALID" == "True" ]]; then
      break
    fi
    fail "API key cannot be empty. Please enter a valid key."
  done
  pass "Gemini API key saved"
  echo ""
  BACKEND_NAME="gemini"

# ── Step 4b: Local / Ollama ───────────────────────────────────────────────────

else
  BACKEND_NAME="local"

  # Ollama installed check
  echo "Checking Ollama..."
  while ! command -v ollama &>/dev/null; do
    fail "Ollama not found."
    echo ""
    info "Install it from: https://ollama.com/download"
    echo ""
    read -rp "    Just installed Ollama? Press Enter to check again... " _
  done
  pass "Ollama found"
  echo ""

  # Ollama daemon running check
  while ! ollama list &>/dev/null 2>&1; do
    fail "Ollama is installed but not running."
    arrow "Start it with: ollama serve"
    read -rp "    Press Enter to check again... " _
  done
  pass "Ollama running"
  echo ""

  # Model selection
  read -rp "Which model do you want to use? (default: gemma3:4b)
  Press Enter to use default, or type a model name: " MODEL_INPUT
  echo ""

  if [[ -n "$MODEL_INPUT" ]]; then
    LOCAL_MODEL="$MODEL_INPUT"
  fi

  # Check if model is already pulled
  if ollama list 2>/dev/null | grep -q "^${LOCAL_MODEL}"; then
    pass "${LOCAL_MODEL} already available"
  else
    echo "Pulling ${LOCAL_MODEL} from Ollama (this may take a few minutes)..."
    if ollama pull "$LOCAL_MODEL"; then
      pass "${LOCAL_MODEL} ready"
    else
      fail "Failed to pull ${LOCAL_MODEL}. Check the model name and your internet connection."
      exit 1
    fi
  fi
  echo ""
fi

# ── Step 5: credentials.json ─────────────────────────────────────────────────

echo "Checking for Google OAuth credentials..."
while ! python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
exit(0 if setup_helper.credentials_file_exists('$SCRIPT_DIR') else 1)
" 2>/dev/null; do
  fail "credentials.json not found."
  echo ""
  info "You need a Google OAuth client secret to authorise this agent to"
  info "access your Gmail, Calendar, and Tasks."
  echo ""
  info "1. Go to: https://console.cloud.google.com/apis/credentials"
  info "2. Create a project (or select an existing one)"
  info "3. Enable the APIs: Gmail, Google Calendar, Google Tasks"
  info "4. Create an OAuth 2.0 Client ID  (type: Desktop app)"
  info "5. Download the JSON and rename it to: credentials.json"
  info "   then place it in: $SCRIPT_DIR/"
  echo ""
  read -rp "    Press Enter once credentials.json is in place... " _
  echo ""
done
pass "credentials.json found"
echo ""

# ── Step 6: token.json / OAuth ────────────────────────────────────────────────

echo "Checking for existing Gmail token..."
if python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
exit(0 if setup_helper.token_file_exists('$SCRIPT_DIR') else 1)
" 2>/dev/null; then
  pass "token.json found — skipping OAuth"
  info "(delete token.json and re-run setup.sh to re-authenticate)"
else
  echo "Authenticating with Google (Gmail, Calendar, Tasks)..."
  info "A browser window will open. Sign in and grant all requested access."
  echo ""
  if python3 "$SCRIPT_DIR/fetch_emails.py" 2>&1 | grep -q "Authenticated\|token"; then
    pass "Authenticated — token.json created"
  else
    python3 "$SCRIPT_DIR/fetch_emails.py"
    pass "Authenticated — token.json created"
  fi
fi
echo ""

# ── Step 7: Write .env ────────────────────────────────────────────────────────

echo "Writing .env..."

ENV_PATH="$SCRIPT_DIR/.env"
WRITE_ENV=true

if python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
exit(0 if setup_helper.env_file_exists('$SCRIPT_DIR') else 1)
" 2>/dev/null; then
  read -rp "  .env already exists. Overwrite? [y/N]: " OVERWRITE
  if [[ "${OVERWRITE,,}" != "y" ]]; then
    WRITE_ENV=false
    info "Keeping existing .env"
  fi
fi

if [[ "$WRITE_ENV" == "true" ]]; then
  python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
content = setup_helper.build_env_content(
    backend='$BACKEND_NAME',
    gemini_key='$GEMINI_KEY',
    local_model='$LOCAL_MODEL',
)
with open('$ENV_PATH', 'w') as f:
    f.write(content)
"
  pass ".env written"
fi
echo ""

# ── Step 8: Smoke test ────────────────────────────────────────────────────────

echo "Running smoke test..."
set +e
USE_SAMPLE_DATA=true python3 "$SCRIPT_DIR/analyze_emails.py" > /tmp/email_agent_smoke.log 2>&1
SMOKE_CODE=$?
set -e

SMOKE_RESULT=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
passed, msg = setup_helper.interpret_smoke_result($SMOKE_CODE)
print(passed)
print(msg)
")

SMOKE_OK=$(echo "$SMOKE_RESULT" | sed -n '1p')
SMOKE_MSG=$(echo "$SMOKE_RESULT" | sed -n '2p')

if [[ "$SMOKE_OK" == "True" ]]; then
  pass "$SMOKE_MSG"
else
  fail "$SMOKE_MSG. See below for errors."
  echo ""
  cat /tmp/email_agent_smoke.log
  echo ""
  arrow "Fix the issue above, then re-run: bash setup.sh"
  exit 1
fi
echo ""

# ── Step 9: Daemon install ────────────────────────────────────────────────────
# sprint 22 — offer to install the polling daemon after a successful setup

DAEMON_INSTALLED=false

read -rp "Run the agent automatically every 5 minutes? [Y/n] " DAEMON_ANSWER
echo ""

SHOULD_INSTALL=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
print(setup_helper.should_install_daemon('${DAEMON_ANSWER}'))
")

if [[ "$SHOULD_INSTALL" == "True" ]]; then
  bash "$SCRIPT_DIR/daemon.sh" install
  DAEMON_INSTALLED=true
else
  echo "  You can run it manually with: python3 run_all.py"
fi
echo ""

# ── Step 10: Add more accounts ───────────────────────────────────────────────
# sprint 22 — multi-account OAuth: loop until user declines

ACCOUNTS_ADDED=0

while true; do
  read -rp "Add another Gmail account? [y/N] " ADD_ACCOUNT
  echo ""

  SHOULD_ADD=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
# Treat empty / y / yes as yes; anything else as no
ans = '''${ADD_ACCOUNT}'''.strip().lower()
print('True' if ans in ('', 'y', 'yes') else 'False')
")

  if [[ "$SHOULD_ADD" != "True" ]]; then
    break
  fi

  # Prompt for alias and validate
  ALIAS=""
  while true; do
    read -rp "Enter a short alias for this account (e.g. work, mombiz): " ALIAS_INPUT
    VALIDATE_RESULT=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
ok, normalised, msg = setup_helper.validate_account_alias('''$ALIAS_INPUT''')
print(ok)
print(normalised)
print(msg)
")
    ALIAS_OK=$(echo "$VALIDATE_RESULT" | sed -n '1p')
    ALIAS=$(echo "$VALIDATE_RESULT" | sed -n '2p')
    ALIAS_MSG=$(echo "$VALIDATE_RESULT" | sed -n '3p')

    if [[ "$ALIAS_OK" == "True" ]]; then
      break
    fi
    fail "$ALIAS_MSG"
  done

  # Check for duplicate before running OAuth
  DUPE_CHECK=$(python3 -c "
import sys, json; sys.path.insert(0, '$SCRIPT_DIR')
from pathlib import Path
accounts_path = '$SCRIPT_DIR/accounts.json'
data = json.loads(Path(accounts_path).read_text()) if Path(accounts_path).exists() else []
exists = any(e.get('id') == 'gmail-$ALIAS' for e in data)
print(exists)
")
  if [[ "$DUPE_CHECK" == "True" ]]; then
    fail "Account 'gmail-${ALIAS}' already exists. Try a different alias."
    echo ""
    continue
  fi

  # Run OAuth for the new account
  echo "Authenticating account 'gmail-${ALIAS}' (Gmail, Calendar, Tasks)..."
  info "A browser window will open. Sign in with the account you want to add."
  echo ""

  TOKEN_PATH="$SCRIPT_DIR/token_${ALIAS}.json"
  set +e
  python3 "$SCRIPT_DIR/fetch_emails.py" --token-file "$TOKEN_PATH"
  AUTH_CODE=$?
  set -e

  if [[ $AUTH_CODE -ne 0 ]]; then
    fail "Authentication failed for 'gmail-${ALIAS}'. Skipping."
    echo ""
    continue
  fi
  pass "gmail-${ALIAS} — token_${ALIAS}.json created"

  # Write entry to accounts.json
  WRITE_RESULT=$(python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import setup_helper
ok, msg = setup_helper.add_account_to_json('$SCRIPT_DIR/accounts.json', '$ALIAS')
print(ok)
print(msg)
")
  WRITE_OK=$(echo "$WRITE_RESULT" | sed -n '1p')
  WRITE_MSG=$(echo "$WRITE_RESULT" | sed -n '2p')

  if [[ "$WRITE_OK" == "True" ]]; then
    echo "$WRITE_MSG"
    ACCOUNTS_ADDED=$((ACCOUNTS_ADDED + 1))
  else
    fail "$WRITE_MSG"
  fi
  echo ""
done

# ── Summary ───────────────────────────────────────────────────────────────────

echo "$HR"
echo "  Setup complete!"
echo "$HR"
echo ""

if [[ "$BACKEND_NAME" == "gemini" ]]; then
  echo "  Backend       : gemini"
else
  echo "  Backend       : local (${LOCAL_MODEL})"
fi

echo "  Gmail access  : ✓"
echo "  Calendar      : ✓"
echo "  Tasks         : ✓"

if [[ "$DAEMON_INSTALLED" == "true" ]]; then
  echo "  Auto-run      : ✓ every 5 min"
else
  echo "  Auto-run      : not configured"
fi

# sprint 22 — show total configured accounts in summary
ACCOUNT_COUNT=$(python3 -c "
import sys, json; sys.path.insert(0, '$SCRIPT_DIR')
from pathlib import Path
accounts_path = '$SCRIPT_DIR/accounts.json'
data = json.loads(Path(accounts_path).read_text()) if Path(accounts_path).exists() else []
print(len(data))
" 2>/dev/null || echo "1")
echo "  Accounts      : ${ACCOUNT_COUNT} configured"

echo ""
echo "  Run the agent:"
echo "    python3 run_all.py"
echo ""
echo "  Dry run (no Gmail API calls):"
echo "    USE_SAMPLE_DATA=true python3 analyze_emails.py"
echo ""
echo "$HR"
