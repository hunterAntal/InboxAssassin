# Community Branch Design

**Date:** 2026-03-28
**Branch:** `community`
**Goal:** Publish a clean, history-free version of email-agent that any Mac user can clone and run locally, with no personal data and no references to internal AI tooling.

---

## 1. Branch & History

- Create `community` as an **orphan branch** — `git checkout --orphan community`
- No commit history carries over; zero risk of personal data in git log
- Single initial commit: "Initial public release"
- `main` / `online_model` branches are untouched

---

## 2. First-Run Preflight (baked into `run_all.py`)

On every run, before doing any work, `run_all.py` executes a `preflight()` function that checks each requirement in order and exits with a clear, actionable message if anything is missing. Once all checks pass, preflight is silent on subsequent runs.

| # | Check | Action if missing |
|---|-------|------------------|
| 1 | **Homebrew** (`brew` on PATH) | Print one-liner install command and exit |
| 2 | **Python 3.12+** | Print `brew install python@3.12` and exit |
| 3 | **pip** (`pip3` available) | Print `brew reinstall python@3.12` and exit |
| 4 | **Python packages** (key imports) | Print `pip3 install -r requirements.txt` and exit |
| 5 | **Ollama binary** (local mode only) | Print `brew install ollama` and exit |
| 6 | **Ollama running** (local mode only) | Print `ollama serve` instructions and exit |
| 7 | **`client_secret*.json`** | Print step-by-step GCP setup guide and exit |
| 8 | **`accounts.json`** | Interactively prompt for Gmail address, write file |
| 9 | **`filter_config.json`** | Auto-create empty default silently |

Checks 5–6 only run when `MODEL_BACKEND=local`. All checks short-circuit on first failure so the user fixes one thing at a time.

---

## 3. File Cleanup

### Delete entirely
| File | Reason |
|------|--------|
| `email_log.json` | Personal email data |
| `activity_log.json` | Personal activity data |
| `processed.json` | Personal message IDs |
| `tally_log*.csv` | Personal performance data |
| `tally_graph.png` | Personal chart |
| `graphs/` | Personal charts directory |
| `token*.json` | Personal OAuth tokens |
| `credentials.json` | Personal OAuth credentials |
| `gemini_api_key.txt` | Personal API key |
| `client_secret_*.json` | Personal OAuth client secret |
| `accounts.json` | Personal account config (generated on first run) |
| `Gabe.txt` | Personal note |
| `app info.txt` | Personal note |
| `input.txt`, `output.txt` | Personal scratch files |
| `CLAUDE.md` | Internal AI tooling guidance |

### Replace with examples
| New file | Purpose |
|----------|---------|
| `accounts.example.json` | Shows Gmail + Outlook entry shapes with placeholder values |
| `filter_config.example.json` | Generic blocked domains/keywords, no personal senders |

### `.gitignore` additions
```
CLAUDE.md
.claude/
accounts.json
token*.json
credentials.json
client_secret_*.json
gemini_api_key.txt
tally_log*.csv
tally_graph.png
graphs/
processed.json
email_log.json
activity_log.json
rate_limit_state.json
```

### Code changes (minimal)
- `COMMANDS.txt` — replace personal docker commands with generic Ollama equivalents
- No changes to pipeline logic (`analyze_emails.py`, `fetch_emails.py`, `pre_filter.py`)

---

## 4. New README

Sections (no references to Claude or internal AI tooling):

1. **What it does** — feature table (kept)
2. **Requirements** — Python 3.12, Ollama (cleanup mode), Gemini API key (maintenance mode), Gmail OAuth credentials
3. **Quick Start** — clone → `pip install -r requirements.txt` → drop in `client_secret.json` → `python3 run_all.py`
4. **Two modes of use**
   - *Inbox cleanup* — `MODEL_BACKEND=local` + Ollama, no rate limits, process backlog fast
   - *Ongoing maintenance* — `MODEL_BACKEND=gemini` + Gemini API key, runs on schedule
5. **Gmail OAuth setup** — step-by-step: GCP project, enable APIs, download `client_secret.json`
6. **Multi-account setup** — reference `accounts.example.json`
7. **Cloud deployment (GCP)** — existing content kept as-is
8. **Environment variables** — cleaned-up table
9. **Project structure** — updated file list

---

## 5. Mac Compatibility

Fresh Mac setup order:

1. **Homebrew** (not included on Mac) — `/ bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`
2. **Python 3.12** — `brew install python@3.12` (pip3 is bundled, no separate install)
3. **Ollama** — `brew install ollama` (cleanup mode only)
4. **Gmail OAuth credentials** — free GCP project required (documented in README)
5. **Python packages** — `pip3 install -r requirements.txt`

The preflight check in `run_all.py` guides users through each of these steps if anything is missing.

---

## 6. Windows Compatibility

Windows is supported. The preflight check must be **OS-aware** — detect `platform.system()` and give platform-appropriate install instructions.

### Fresh Windows setup order

1. **Python 3.12** — Download installer from [python.org/downloads](https://python.org/downloads)
   - During install: check **"Add Python to PATH"** and **"Install pip"** — both required
   - Or via winget: `winget install Python.Python.3.12`
2. **pip** — Bundled with the python.org installer (no separate install needed)
3. **Git** — Needed to clone the repo: `winget install Git.Git` or [git-scm.com](https://git-scm.com)
4. **Ollama** — Download from [ollama.ai](https://ollama.ai) or `winget install Ollama.Ollama` (cleanup mode only)
5. **Gmail OAuth credentials** — same GCP setup as Mac (see Section 7)
6. **Python packages** — `pip install -r requirements.txt` (no `pip3` on Windows, just `pip`)

### Windows-specific preflight messages

| Check | Windows message |
|-------|----------------|
| Python 3.12+ | "Download from python.org/downloads — check 'Add to PATH' during install" |
| pip | "Reinstall Python from python.org and check 'Install pip'" |
| Python packages | "Run `pip install -r requirements.txt`" |
| Ollama (local mode) | "Download from ollama.ai or run `winget install Ollama.Ollama`" |
| Ollama running (local mode) | "Open a new terminal and run `ollama serve`" |

### Notes
- Use `python` not `python3` on Windows — preflight detects OS and uses the right command
- Run commands in **PowerShell** or **Command Prompt** (not WSL, unless intentional)
- No Homebrew check on Windows

---

## 7. Google API Setup (README section script)

This section is the exact content to include in the README under "Google API Setup".

### One-time setup (~5 minutes)

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a new project (free)
2. In the left menu go to **APIs & Services → Library** and enable these three APIs:
   - Gmail API
   - Google Calendar API
   - Google Tasks API
3. Go to **APIs & Services → OAuth consent screen**
   - Choose **External**
   - Fill in an app name (anything, e.g. "My Email Agent")
   - Add your Gmail address as a **Test user**
   - Save
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Click Create, then **Download JSON**
5. Rename the downloaded file to `client_secret.json` and place it in the project folder

### First run

When you run `python3 run_all.py` for the first time, a browser window will open asking you to sign in to Google. You may see an **"App isn't verified"** warning — this is normal for personal apps. Click **Advanced → Go to [app name] (unsafe)** and then **Allow**.

A `token.json` file is saved locally. You won't need to do this again unless you change the OAuth scopes.

---

## 8. Out of Scope

- Refactoring pipeline internals
- Changing the AI model or provider logic
- Any changes to GCP deployment infrastructure
