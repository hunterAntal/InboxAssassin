# InboxAssassin

> *Keeping your inbox organized, or else.*

An AI-powered email assistant that automatically processes your Gmail inbox. It reads your emails, understands what needs action, creates calendar events and tasks, and keeps your inbox organised.

## What it does

| Feature | Detail |
|---------|--------|
| **Reads & prioritises** | AI ranks every email 1-5 by urgency |
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
| Gmail OAuth credentials | Free Google Cloud project required - see [Google API Setup](#google-api-setup) |
| Ollama | Local cleanup mode only - `brew install ollama` (Mac) / [ollama.ai](https://ollama.ai) (Windows) |
| Gemini API key | Ongoing maintenance mode only - free at [aistudio.google.com](https://aistudio.google.com) |

---

## Quick Start

```bash
# 1. Clone the repo
git clone <repo-url>
cd inbox-assassin

# 2. Install dependencies
pip3 install -r requirements.txt        # Mac
pip install -r requirements.txt         # Windows

# 3. Drop your client_secret.json in the folder (see Google API Setup below)

# 4. Run - preflight will guide you through anything that's missing
python3 run_all.py       # Mac
python run_all.py        # Windows
```

On first run, a browser window will open asking you to sign in to Google. After that it runs silently.

---

## Two modes of use

### Mode 1: Inbox cleanup (Ollama - local, no rate limits)

Use this to blast through a large backlog. No API keys, no rate limits - runs entirely on your machine.

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

### Mode 2: Ongoing maintenance (Gemini - cloud, runs on a schedule)

Use this for day-to-day processing. Requires a free Gemini API key.

```bash
# Get a free API key at https://aistudio.google.com
# Add to .env:
echo "GEMINI_API_KEY=your_key_here" >> .env

python3 run_all.py      # Mac
python run_all.py       # Windows
```

For fully automated maintenance, deploy to Google Cloud Run - see [Cloud Deployment](#deploying-to-google-cloud).

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
2. Go to **APIs & Services -> Library** and enable:
   - Gmail API
   - Google Calendar API
   - Google Tasks API
3. Go to **APIs & Services -> OAuth consent screen**
   - Choose **External**
   - Fill in any app name (e.g. "InboxAssassin")
   - Add your Gmail address as a **Test user** -> Save
4. Go to **APIs & Services -> Credentials -> Create Credentials -> OAuth client ID**
   - Application type: **Desktop app** -> Create -> **Download JSON**
5. Rename the downloaded file to `client_secret.json` and place it in the project folder

### First run

A browser window will open asking you to sign in to Google. You may see an **"App isn't verified"** warning - this is normal for personal apps. Click **Advanced -> Go to [app name] (unsafe)** -> **Allow**.

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
| `GEMINI_API_KEY` | - | Your Gemini API key (required for gemini mode) |
| `USE_SAMPLE_DATA` | `false` | `true` to run without hitting Gmail |
| `MAX_EMAILS` | `50` | Emails fetched per batch |
| `MAX_BATCHES` | `0` | Stop after N batches - `0` = unlimited |
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
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/inbox-assassin
gcloud run jobs update inbox-assassin --region=us-central1 --image=gcr.io/YOUR_PROJECT_ID/inbox-assassin
gcloud run jobs execute inbox-assassin --region=us-central1 --wait
```

---

## Project structure

```
├── run_all.py                  # Orchestrator - preflight + loops until inbox is empty
├── fetch_emails.py             # Gmail auth + email fetching
├── fetch_emails_outlook.py     # Outlook/Hotmail auth + email fetching
├── analyze_emails.py           # AI analysis, Calendar/Tasks, inbox management
├── pre_filter.py               # Spam pre-filter (pattern matching + auto-learning)
├── plot_tally.py               # Generates per-account performance charts -> graphs/
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
| 5 | Urgent - immediate action needed | Kept, labelled Action Required |
| 4 | Important - deadline this week | Kept, labelled Action Required |
| 3 | Relevant - worth reading | Kept |
| 2 | Low value - informational | Archived |
| 1 | Spam / promotions | Archived + Spam Bucket label |
