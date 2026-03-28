# Community Branch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a clean, history-free `community` branch with no personal data, no internal AI tooling references, example config files, and a preflight check that guides any Mac or Windows user through first-time setup.

**Architecture:** Orphan branch from current working tree. Personal data files deleted. `run_all.py` gains a `preflight()` function that runs before any pipeline work and exits with OS-aware install instructions if prerequisites are missing. README rewritten from scratch.

**Tech Stack:** Python 3.12, standard library (`platform`, `shutil`, `sys`, `glob`, `urllib`), existing pipeline unchanged.

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `run_all.py` | Modify | Add `preflight()` called at top of `main()` |
| `.gitignore` | Modify | Add personal/tooling files |
| `README.md` | Rewrite | Public-facing docs, no internal AI tool mentions |
| `COMMANDS.txt` | Modify | Replace personal GCP project IDs with `YOUR_PROJECT_ID`, update Ollama section |
| `accounts.example.json` | Create | Gmail + Outlook entry shapes with placeholders |
| `filter_config.example.json` | Create | Generic blocked domains/keywords |
| Personal data files (15+) | Delete | No personal data in public branch |

---

## Task 1: Create the orphan `community` branch

**Files:** none changed yet

- [ ] **Step 1: Create orphan branch and clear the index**

```bash
git checkout --orphan community
git rm -rf --cached .
```

Expected output: many lines of `rm '...'`. The files still exist on disk — only the git index is cleared.

- [ ] **Step 2: Verify you are on the community branch with no commits**

```bash
git status
git log --oneline 2>&1 || echo "No commits yet"
```

Expected: branch is `community`, all files shown as untracked, log prints "No commits yet".

---

## Task 2: Delete personal data and internal tooling files

**Files:** delete from disk

- [ ] **Step 1: Delete personal data files**

```bash
rm -f email_log.json activity_log.json processed.json
rm -f tally_log*.csv tally_graph.png
rm -f token*.json credentials.json gemini_api_key.txt
rm -f "Gabe.txt" "app info.txt" input.txt output.txt
rm -f CLAUDE.md
rm -rf graphs/
```

- [ ] **Step 2: Delete personal client_secret file**

```bash
rm -f client_secret_*.json
```

- [ ] **Step 3: Delete personal accounts.json**

```bash
rm -f accounts.json
```

- [ ] **Step 4: Verify deletions**

```bash
ls *.json 2>/dev/null; ls *.csv 2>/dev/null; ls *.txt 2>/dev/null
```

Expected: only `filter_config.json`, `sample_emails.json`, `docker-compose.yml` (no personal files), `requirements.txt`, `COMMANDS.txt` remain. No `token*.json`, no `tally_log*.csv`, no `Gabe.txt`.

---

## Task 3: Update `.gitignore`

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Read current .gitignore**

Current `.gitignore` content (from repo):
```
credentials.json
token.json
token_outlook.json
token_antalhunter.json
processed.json
activity_log.json
email_log.json
.env
gemini_api_key.txt
client_secret_*.json
input.txt

.venv/
__pycache__/
*.pyc
.pytest_cache/
*.log
*.text
tally_log.csv
rate_limit_state.json
graphs/
```

- [ ] **Step 2: Replace .gitignore with expanded version**

Write the following as the complete `.gitignore`:

```
# Credentials & tokens
credentials.json
token*.json
client_secret_*.json
gemini_api_key.txt
.env

# Personal data / runtime state
accounts.json
processed.json
activity_log.json
email_log.json
rate_limit_state.json
input.txt
output.txt

# Performance logs & charts
tally_log*.csv
tally_graph.png
graphs/

# Internal AI tooling
CLAUDE.md
.claude/

# Python
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.log
*.text
```

---

## Task 4: Create `accounts.example.json`

**Files:**
- Create: `accounts.example.json`

- [ ] **Step 1: Create the file**

```json
[
  {
    "id": "gmail-personal",
    "provider": "gmail",
    "active": true,
    "token_file": "token.json"
  },
  {
    "id": "gmail-work",
    "provider": "gmail",
    "active": false,
    "token_file": "token_work.json"
  },
  {
    "id": "hotmail-personal",
    "provider": "outlook",
    "active": false,
    "client_id": "YOUR_AZURE_APP_CLIENT_ID",
    "tenant_id": "consumers"
  }
]
```

---

## Task 5: Create `filter_config.example.json`

**Files:**
- Create: `filter_config.example.json`

- [ ] **Step 1: Create the file**

```json
{
  "blocked_domains": [
    "newsletters.example.com",
    "promo.somestore.com",
    "noreply.marketing.com"
  ],
  "blocked_keywords": [
    "unsubscribe",
    "weekly digest",
    "limited time offer"
  ]
}
```

---

## Task 6: Update `COMMANDS.txt`

**Files:**
- Modify: `COMMANDS.txt`

- [ ] **Step 1: Replace the DOCKER / OLLAMA section and scrub personal GCP project IDs**

Replace the entire file content with:

```
EMAIL AGENT — COMMAND REFERENCE
================================

LOCAL DEVELOPMENT
-----------------
python3 fetch_emails.py          Authenticate with Gmail and print the latest unread emails
python3 analyze_emails.py        Run the full pipeline once (fetch → analyze → calendar/tasks → inbox)
python3 run_all.py               Loop the pipeline until the inbox is empty

TESTING
-------
python3 -m pytest tests/test_all.py -v       Run all unit tests (verbose)
python3 -m pytest tests/test_all.py -q       Run all unit tests (summary only)
python3 -m pytest tests/test_all.py -x       Stop on first failure

SAMPLE DATA (no Gmail API calls)
---------------------------------
USE_SAMPLE_DATA=true python3 analyze_emails.py    Run pipeline against sample_emails.json

SWITCH AI BACKEND
-----------------
Edit MODEL_BACKEND in .env:
  MODEL_BACKEND=local     Use local Ollama (no rate limits, good for inbox cleanup)
  MODEL_BACKEND=gemini    Use Google Gemini API (requires GEMINI_API_KEY, good for maintenance)

OLLAMA (local mode)
--------------------
ollama serve                                 Start the Ollama server (run in a separate terminal)
ollama pull gemma3:4b                        Pull the recommended model
ollama list                                  List downloaded models

BUILD & TEST DOCKER IMAGE LOCALLY
----------------------------------
docker build -t email-agent .                Build the container image
docker run --env-file .env email-agent       Run the container locally

GCP DEPLOYMENT (build → update → execute)
------------------------------------------
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/email-agent --project=YOUR_PROJECT_ID
gcloud run jobs update email-agent --image=gcr.io/YOUR_PROJECT_ID/email-agent:latest --region=us-central1 --project=YOUR_PROJECT_ID
gcloud run jobs execute email-agent --region=us-central1 --wait --project=YOUR_PROJECT_ID

GCP ONE-TIME SETUP
------------------
PROJECT_ID=YOUR_PROJECT_ID REGION=us-central1 bash gcp_setup.sh

GCP JOB MANAGEMENT
-------------------
gcloud run jobs executions list --job=email-agent --region=us-central1 --project=YOUR_PROJECT_ID
gcloud scheduler jobs pause  email-agent-schedule --location=us-central1 --project=YOUR_PROJECT_ID
gcloud scheduler jobs resume email-agent-schedule --location=us-central1 --project=YOUR_PROJECT_ID

GCP LOGS
--------
gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name="email-agent"' --project=YOUR_PROJECT_ID --limit=30 --format="value(textPayload)" --order=asc --freshness=5m

GCP AUTH
--------
gcloud auth login
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
rm token.json && python3 fetch_emails.py     Force Gmail re-authentication (e.g. after scope changes)

UPDATE SECRETS ON GCP
----------------------
gcloud secrets versions add gmail-token       --data-file=token.json        Re-upload Gmail token
gcloud secrets versions add gmail-credentials --data-file=credentials.json  Re-upload OAuth credentials
```

---

## Task 7: Add `preflight()` to `run_all.py`

**Files:**
- Modify: `run_all.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_all.py`:

```python
# ── Preflight tests ──────────────────────────────────────────────────────────

class TestPreflight:
    def test_creates_filter_config_when_missing(self, tmp_path, monkeypatch):
        """_preflight_check_configs auto-creates filter_config.json with empty defaults."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "client_secret.json").write_text("{}")
        (tmp_path / "accounts.json").write_text('[{"id":"x","provider":"gmail","active":true}]')
        import run_all
        run_all._preflight_check_configs()
        assert (tmp_path / "filter_config.json").exists()
        import json
        data = json.loads((tmp_path / "filter_config.json").read_text())
        assert data == {"blocked_domains": [], "blocked_keywords": []}

    def test_creates_accounts_json_interactively(self, tmp_path, monkeypatch):
        """_preflight_check_configs creates accounts.json using user-supplied email."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "client_secret.json").write_text("{}")
        (tmp_path / "filter_config.json").write_text('{"blocked_domains":[],"blocked_keywords":[]}')
        monkeypatch.setattr("builtins.input", lambda _: "user@gmail.com")
        import run_all
        run_all._preflight_check_configs()
        import json
        accounts = json.loads((tmp_path / "accounts.json").read_text())
        assert accounts[0]["id"] == "gmail-user"
        assert accounts[0]["provider"] == "gmail"
        assert accounts[0]["active"] is True
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_all.py::TestPreflight -v
```

Expected: FAIL — `run_all` has no `_preflight_check_system`, `_preflight_check_packages`, `_preflight_check_ollama`, or `_preflight_check_configs`.

- [ ] **Step 3: Add preflight helpers and `preflight()` to `run_all.py`**

Add the following imports at the top of `run_all.py` (after existing imports):

```python
import glob as _glob
import platform
import shutil
import urllib.request
```

Add the following functions after the imports and before `load_accounts()`:

```python
def _preflight_check_system() -> None:
    """Check Homebrew (Mac), Python version, and pip. Exits with instructions if missing."""
    is_windows = platform.system() == "Windows"

    if not is_windows and shutil.which("brew") is None:
        print("\n[setup] Homebrew is required on Mac.")
        print('  Install: /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
        sys.exit(1)

    if sys.version_info < (3, 12):
        if is_windows:
            print("\n[setup] Python 3.12+ required.")
            print("  Install: Download from https://python.org/downloads")
            print("           Check 'Add Python to PATH' and 'Install pip' during install.")
        else:
            print("\n[setup] Python 3.12+ required.")
            print("  Install: brew install python@3.12")
        sys.exit(1)

    pip_cmd = "pip" if is_windows else "pip3"
    if shutil.which(pip_cmd) is None:
        if is_windows:
            print(f"\n[setup] {pip_cmd} not found.")
            print("  Fix: Reinstall Python from python.org and check 'Install pip'")
        else:
            print(f"\n[setup] {pip_cmd} not found.")
            print("  Fix: brew reinstall python@3.12")
        sys.exit(1)


def _preflight_check_packages() -> None:
    """Check that required Python packages are installed. Exits with pip hint if not."""
    is_windows = platform.system() == "Windows"
    pip_cmd = "pip" if is_windows else "pip3"
    try:
        import google.auth  # noqa: F401
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401
        import dotenv  # noqa: F401
    except ImportError as e:
        print(f"\n[setup] Missing Python packages: {e}")
        print(f"  Install: {pip_cmd} install -r requirements.txt")
        sys.exit(1)


def _preflight_check_ollama() -> None:
    """Check Ollama binary and server when MODEL_BACKEND=local. Exits with instructions if missing."""
    if os.environ.get("MODEL_BACKEND", "gemini") != "local":
        return
    is_windows = platform.system() == "Windows"
    if shutil.which("ollama") is None:
        if is_windows:
            print("\n[setup] Ollama not found.")
            print("  Install: Download from https://ollama.ai")
            print("           Or run: winget install Ollama.Ollama")
        else:
            print("\n[setup] Ollama not found.")
            print("  Install: brew install ollama")
        sys.exit(1)
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=2)
    except Exception:
        print("\n[setup] Ollama is not running.")
        print("  Fix: Open a new terminal and run: ollama serve")
        sys.exit(1)


def _preflight_check_configs() -> None:
    """Check client_secret, accounts.json, and filter_config.json. Creates missing configs."""
    if not _glob.glob("client_secret*.json"):
        print("\n[setup] Gmail OAuth credentials not found (client_secret*.json).")
        print("  1. Go to https://console.cloud.google.com and create a free project")
        print("  2. Enable: Gmail API, Google Calendar API, Google Tasks API")
        print("  3. APIs & Services → OAuth consent screen → External → add your Gmail as Test user")
        print("  4. Credentials → Create Credentials → OAuth client ID → Desktop app → Download JSON")
        print("  5. Rename the file to client_secret.json and place it in this folder, then re-run.")
        sys.exit(1)

    if not os.path.exists("accounts.json"):
        print("\n[setup] No accounts.json found. Let's create one.")
        email = input("  Enter your Gmail address: ").strip()
        username = email.split("@")[0]
        accounts = [{"id": f"gmail-{username}", "provider": "gmail", "active": True, "token_file": "token.json"}]
        with open("accounts.json", "w") as f:
            json.dump(accounts, f, indent=2)
        print(f"  Created accounts.json for {email}\n")

    if not os.path.exists("filter_config.json"):
        with open("filter_config.json", "w") as f:
            json.dump({"blocked_domains": [], "blocked_keywords": []}, f, indent=2)


def preflight() -> None:
    """Run all pre-flight checks. Exits with actionable instructions if anything is missing."""
    _preflight_check_system()
    _preflight_check_packages()
    _preflight_check_ollama()
    _preflight_check_configs()
```

- [ ] **Step 4: Call `preflight()` at the top of `main()`**

In `run_all.py`, modify `main()`:

```python
def main():
    preflight()
    accounts = load_accounts()
    for account in accounts:
        print(f"\n{'#'*60}")
        print(f"  ACCOUNT: {account['id']}  [{account.get('provider','gmail').upper()}]")
        print(f"{'#'*60}\n")
        _process_account(account)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_all.py::TestPreflight -v
```

Expected: both tests PASS.

- [ ] **Step 6: Run full test suite to verify nothing is broken**

```bash
python3 -m pytest tests/test_all.py -q
```

Expected: all existing tests still PASS.

---

## Task 8: Write new `README.md`

**Files:**
- Rewrite: `README.md`

- [ ] **Step 1: Replace README.md with the following content**

```markdown
# Email Agent

An AI-powered email assistant that automatically processes your Gmail inbox. It reads your emails, understands what needs action, creates calendar events and tasks, and keeps your inbox organised.

## What it does

| Feature | Detail |
|---------|--------|
| **Reads & prioritises** | AI ranks every email 1–5 by urgency |
| **Creates calendar events** | Meetings and appointments added to Google Calendar automatically |
| **Creates tasks** | Deadlines and to-dos added to Google Tasks |
| **Cleans your inbox** | Low-priority emails archived; important ones labelled |
| **Smart labels** | Receipts, invoices, shipping, subscriptions, and travel bookings tagged automatically |
| **Filters spam** | Known promotional senders caught before AI sees them |
| **Self-learning filter** | Emails marked as spam auto-block that sender for future runs |
| **Multi-account** | Supports multiple Gmail (and Outlook) accounts |
| **Inbox digest** | After every batch, a summary email lands in your inbox |
| **Performance tracking** | Per-account CSV log appended after every batch |

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| Python 3.12+ | See setup below |
| Gmail OAuth credentials | Free Google Cloud project required — see [Google API Setup](#google-api-setup) |
| Ollama | Local cleanup mode only — `brew install ollama` (Mac) / [ollama.ai](https://ollama.ai) (Windows) |
| Gemini API key | Ongoing maintenance mode only — free at [aistudio.google.com](https://aistudio.google.com) |

---

## Quick Start

```bash
# 1. Clone the repo
git clone <repo-url>
cd email-agent

# 2. Install dependencies
pip3 install -r requirements.txt        # Mac
pip install -r requirements.txt         # Windows

# 3. Drop your client_secret.json in the folder (see Google API Setup below)

# 4. Run — preflight will guide you through anything that's missing
python3 run_all.py       # Mac
python run_all.py        # Windows
```

On first run, a browser window will open asking you to sign in to Google. After that it runs silently.

---

## Two modes of use

### Mode 1: Inbox cleanup (Ollama — local, no rate limits)

Use this to blast through a large backlog. No API keys, no rate limits — runs entirely on your machine.

```bash
# Mac
brew install ollama
ollama pull gemma3:4b
ollama serve &         # start server in background

MODEL_BACKEND=local python3 run_all.py

# Windows
# Download Ollama from https://ollama.ai, then:
ollama pull gemma3:4b
# Open a separate terminal: ollama serve
set MODEL_BACKEND=local
python run_all.py
```

### Mode 2: Ongoing maintenance (Gemini — cloud, runs on a schedule)

Use this for day-to-day processing. Requires a free Gemini API key.

```bash
# Get a free API key at https://aistudio.google.com
# Add to .env:
echo "GEMINI_API_KEY=your_key_here" >> .env

python3 run_all.py      # Mac
python run_all.py       # Windows
```

For fully automated maintenance, deploy to Google Cloud Run — see [Cloud Deployment](#deploying-to-google-cloud).

---

## Mac Setup

```bash
# 1. Install Homebrew (if not already installed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. Install Python 3.12 (pip3 is included)
brew install python@3.12

# 3. Install Ollama (cleanup mode only)
brew install ollama
```

## Windows Setup

1. Download Python 3.12 from [python.org/downloads](https://python.org/downloads)
   - During install: check **"Add Python to PATH"** and **"Install pip"**
   - Or: `winget install Python.Python.3.12`
2. Download Ollama from [ollama.ai](https://ollama.ai) or run `winget install Ollama.Ollama` (cleanup mode only)
3. Install Git from [git-scm.com](https://git-scm.com) or `winget install Git.Git`

> Run commands in **PowerShell** or **Command Prompt**.

---

## Google API Setup

### One-time setup (~5 minutes)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a new project (free)
2. Go to **APIs & Services → Library** and enable:
   - Gmail API
   - Google Calendar API
   - Google Tasks API
3. Go to **APIs & Services → OAuth consent screen**
   - Choose **External**
   - Fill in any app name (e.g. "My Email Agent")
   - Add your Gmail address as a **Test user** → Save
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app** → Create → **Download JSON**
5. Rename the downloaded file to `client_secret.json` and place it in the project folder

### First run

A browser window will open asking you to sign in to Google. You may see an **"App isn't verified"** warning — this is normal for personal apps. Click **Advanced → Go to [app name] (unsafe)** → **Allow**.

A `token.json` file is saved locally. You won't need to do this again.

---

## Multi-account setup

Copy `accounts.example.json` as a reference. The `accounts.json` file (auto-created on first run) controls which accounts are active:

```json
[
  { "id": "gmail-personal", "provider": "gmail", "active": true,  "token_file": "token.json" },
  { "id": "gmail-work",     "provider": "gmail", "active": false, "token_file": "token_work.json" }
]
```

Set `"active": false` to skip an account without removing it. Each account gets its own token file and tally CSV.

---

## Environment variables

Create a `.env` file in the project folder:

| Variable | Default | Purpose |
|----------|---------|---------|
| `MODEL_BACKEND` | `gemini` | `gemini` or `local` (Ollama) |
| `GEMINI_API_KEY` | — | Your Gemini API key (required for gemini mode) |
| `USE_SAMPLE_DATA` | `false` | `true` to run without hitting Gmail |
| `MAX_EMAILS` | `50` | Emails fetched per batch |
| `MAX_BATCHES` | `0` | Stop after N batches — `0` = unlimited |
| `PRE_FILTER` | `true` | `false` to disable spam pre-filter |
| `SEND_DIGEST` | `true` | `false` to suppress batch summary email |
| `TZ` | `UTC` | Timezone for digest timestamps e.g. `America/Toronto` |

---

## Deploying to Google Cloud

For fully automated, hands-free maintenance (runs every 4 hours):

```bash
# One-time setup
PROJECT_ID=your-project-id REGION=us-central1 bash gcp_setup.sh

# Build and deploy
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/email-agent
gcloud run jobs update email-agent --region=us-central1 --image=gcr.io/YOUR_PROJECT_ID/email-agent
gcloud run jobs execute email-agent --region=us-central1 --wait
```

---

## Project structure

```
├── run_all.py                  # Orchestrator — preflight + loops until inbox is empty
├── fetch_emails.py             # Gmail auth + email fetching
├── fetch_emails_outlook.py     # Outlook/Hotmail auth + email fetching
├── analyze_emails.py           # AI analysis, Calendar/Tasks, inbox management
├── pre_filter.py               # Spam pre-filter (pattern matching + auto-learning)
├── plot_tally.py               # Generates per-account performance charts → graphs/
├── accounts.example.json       # Example account configuration
├── filter_config.example.json  # Example spam filter configuration
├── sample_emails.json          # Sample data for testing without Gmail API
├── gcp_setup.sh                # One-time GCP infrastructure setup
├── Dockerfile                  # Container image for Cloud Run
├── docker-compose.yml          # Local Ollama container
└── tests/                      # Unit tests (all external APIs mocked)
```

## Priority rubric

| Priority | Meaning | Inbox action |
|----------|---------|-------------|
| 5 | Urgent — immediate action needed | Kept, labelled Action Required |
| 4 | Important — deadline this week | Kept, labelled Action Required |
| 3 | Relevant — worth reading | Kept |
| 2 | Low value — informational | Archived |
| 1 | Spam / promotions | Archived + Spam Bucket label |
```

---

## Task 9: Stage all changes and make the initial commit

**Files:** all modified/created/deleted files

- [ ] **Step 1: Verify the working tree looks clean (no personal data)**

```bash
ls *.json
```

Expected: only `filter_config.json`, `sample_emails.json`, `accounts.example.json`, `filter_config.example.json` — no `email_log.json`, no `token*.json`, no `credentials.json`.

```bash
ls *.csv 2>/dev/null || echo "No CSVs — good"
```

Expected: "No CSVs — good"

- [ ] **Step 2: Stage all files**

```bash
git add \
  run_all.py \
  .gitignore \
  README.md \
  COMMANDS.txt \
  accounts.example.json \
  filter_config.example.json \
  analyze_emails.py \
  fetch_emails.py \
  fetch_emails_outlook.py \
  pre_filter.py \
  plot_tally.py \
  run_all.py \
  sample_emails.json \
  filter_config.json \
  requirements.txt \
  Dockerfile \
  docker-compose.yml \
  gcp_setup.sh \
  tests/ \
  docs/
```

- [ ] **Step 3: Verify staged files contain no personal data**

```bash
git diff --cached --name-only
```

Expected: list of source files only — no `token*.json`, no `email_log.json`, no `tally_log*.csv`, no `client_secret_*.json`.

- [ ] **Step 4: Make the initial commit**

```bash
git commit -m "Initial public release"
```

- [ ] **Step 5: Verify final state**

```bash
git log --oneline
git branch
```

Expected: exactly one commit "Initial public release" on branch `community`.
