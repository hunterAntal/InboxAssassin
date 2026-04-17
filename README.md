# Inbox Assassin

An AI email agent for Gmail. Reads your inbox, prioritises emails, archives the noise, creates calendar events and tasks, and sends you a digest twice a day. Runs locally on a 5-minute polling loop.

---

## Setup

```bash
bash setup.sh
```

Walks you through: Python check, pip install, Gemini or Ollama backend, Gmail OAuth, `.env` file, and optional daemon install.

---

## Run

```bash
python3 run_all.py                       # process inbox until empty
python3 run_all.py MAX_BATCHES=1         # single batch only
USE_SAMPLE_DATA=true python3 run_all.py  # dry run, no Gmail API
```

---

## Daemon (auto-run every 5 minutes)

```bash
bash daemon.sh install    # install and start
bash daemon.sh status     # show state and last run time
bash daemon.sh start      # start a stopped daemon
bash daemon.sh stop       # stop
bash daemon.sh uninstall  # remove completely
```

---

## Email Commands

Send these to yourself. The agent picks them up on the next run. Any other sender is ignored.

---

### `[AGENT RULE]` - Set AI behaviour per label

```
Subject: [AGENT RULE]
Label: University

ROUTE:
  from: @university.ca
  subject: tuition, bursary, deadline

Treat as priority 4-5 if a deadline is mentioned.
```

- `from:` matches sender domains or addresses
- `subject:` matches keywords in the subject line
- Text after the `ROUTE:` block is sent to the AI as instructions
- Multiple values are comma-separated

---

### `[AGENT PRIORITY]` - Boost or lower priority for a keyword

```
Subject: [AGENT PRIORITY]

invoice = 4
newsletter = 1
```

Format: `keyword = N` (1 to 5). Multiple keywords per email. All `[AGENT PRIORITY]` emails are loaded and applied each run.

---

### `[AGENT PAUSE]` / `[AGENT RESUME]` - Suspend the agent

```
Subject: [AGENT PAUSE]

duration: 1 week
```

| Body format | Behaviour |
|-------------|-----------|
| (empty) | Pause indefinitely |
| `duration: N hours/days/weeks` | Pause for that long |
| `until: YYYY-MM-DD` | Pause until that date |
| `daily: HH:MM - HH:MM` | Pause every day in that window |

Send `[AGENT RESUME]` to cancel early.

---

### `[AGENT TIMETRAVEL]` - Label historical emails in bulk

```
Subject: [AGENT TIMETRAVEL]

from: @university.ca
after: 2022-09-01
apply-label: University
max: 500
```

Runs once on the next pass, then self-destructs. `apply-label` must be an existing Gmail label.

---

### `[AGENT IGNORE]` / `[AGENT UNIGNORE]` - Block or unblock a sender

```
Subject: [AGENT IGNORE]

spam@example.com
```

Adds the address to your per-account block list. Send `[AGENT UNIGNORE]` with the same address to remove it.

---

### `[AGENT STATUS]` - Get a status report

```
Subject: [AGENT STATUS]
```

Replies with: model, archive threshold, pre-filter state, digest schedule, accounts monitored, last run stats.

---

### `[AGENT DIGEST]` - Force an immediate digest

```
Subject: [AGENT DIGEST]
```

Sends the current log buffer as a report right now, outside the scheduled runs. Buffer clears on success.

---

### `[AGENT MODEL] <model>` - Switch the active Ollama model

```
Subject: [AGENT MODEL] llama3.2
```

Checks if the model is installed, pulls it if not, and saves it to per-account config. Errors surface in the next digest. Ollama accounts only.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values.

| Variable | Default | Notes |
|----------|---------|-------|
| `MODEL_BACKEND` | `gemini` | `gemini` or `local` (Ollama) |
| `GEMINI_API_KEY` | - | Required for Gemini backend |
| `LOCAL_MODEL` | `llama3.1:latest` | Overridden per account by `[AGENT MODEL]` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL |
| `MAX_EMAILS` | `50` | Emails per batch |
| `MAX_BATCHES` | `0` | 0 is unlimited |
| `PRE_FILTER` | `true` | Skip known spam before AI analysis |
| `SEND_DIGEST` | `true` | Set false to suppress digest emails |
| `DIGEST_MORNING_HOUR` | `8` | 0-23 local time |
| `DIGEST_EVENING_HOUR` | `20` | 0-23 local time |
| `TZ` | `UTC` | Your timezone, e.g. `America/Toronto` |

---

## Multiple Accounts

Copy `accounts.example.json` to `accounts.json` and add each Gmail account:

```json
[
  { "id": "gmail-personal", "provider": "gmail", "active": true, "token_file": "token.json" },
  { "id": "gmail-work",     "provider": "gmail", "active": true, "token_file": "token_work.json" }
]
```

- `active: false` skips an account without removing it
- Each account gets its own filter config, log, and model setting
- Run `bash setup.sh` to add accounts interactively

---

## Priority Scale

| Priority | Meaning | Action |
|----------|---------|--------|
| 5 | Urgent | Kept + Action Required label |
| 4 | Important, deadline this week | Kept + Action Required label |
| 3 | Worth reading | Kept |
| 2 | Low value | Archived |
| 1 | Spam or promo | Archived + Spam Bucket label |

---

## Tests

```bash
python3 -m pytest tests/ -v
python3 -m pytest tests/ -k "ClassName"
```

All external APIs are mocked. No Gmail or Gemini credentials needed to run tests.
