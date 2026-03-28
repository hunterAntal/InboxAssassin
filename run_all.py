"""
Runs the full email pipeline in a loop until there are no unread emails left.

Each pass fetches up to MAX_EMAILS unread emails, processes them, and marks
them as read. The loop exits when a fetch returns an empty inbox.
"""

import csv
import glob as _glob
import json
import os
import platform
import shutil
import sys
import time
import urllib.request
from datetime import datetime
from dotenv import load_dotenv

from fetch_emails import get_gmail_service, fetch_unread_emails, TOKEN_FILE, CREDENTIALS_FILE
from fetch_emails_outlook import get_outlook_service, fetch_unread_emails_outlook
from analyze_emails import analyze_emails, print_digest, log_emails, create_entries, manage_inbox, send_digest_email
import pre_filter

ACCOUNTS_FILE = "accounts.json"


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


def load_accounts() -> list[dict]:
    """Return active accounts from accounts.json. Falls back to a single default Gmail account."""
    if not os.path.exists(ACCOUNTS_FILE):
        return [{"id": "gmail-personal", "provider": "gmail", "active": True}]
    with open(ACCOUNTS_FILE) as f:
        accounts = json.load(f)
    active = [a for a in accounts if a.get("active", True)]
    if not active:
        print("[accounts] No active accounts in accounts.json — nothing to do.")
    return active

load_dotenv()

BATCH_DELAY_SECONDS = 3  # brief pause between batches
MAX_BATCHES         = int(os.environ.get("MAX_BATCHES", 0))  # 0 = unlimited
TALLY_CSV_FILE      = "tally_log.csv"  # default; overridden per-account in _process_account
_CSV_HEADERS        = [
    "timestamp", "batch",
    "pre_filtered", "archived", "read_only", "action_required", "total",
    "receipt", "invoice", "shipping", "subscription", "travel",
]


def cloud_init(account: dict) -> None:
    """Pull secrets from GCP Secret Manager for a given account.

    Only runs when the GCP_PROJECT env var is set (i.e. running on Cloud Run).
    This keeps credentials out of the container image entirely.
    """
    project = os.environ.get("GCP_PROJECT")
    if not project:
        return  # running locally — secrets already on disk

    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()

    def fetch_secret(name):
        return client.access_secret_version(
            name=f"projects/{project}/secrets/{name}/versions/latest"
        ).payload.data.decode()

    print(f"Fetching secrets for [{account['id']}] from Secret Manager...")

    token_secret       = account.get("token_secret", "gmail-token")
    credentials_secret = account.get("credentials_secret", "gmail-credentials")

    with open(TOKEN_FILE, "w") as f:
        f.write(fetch_secret(token_secret))

    with open(CREDENTIALS_FILE, "w") as f:
        f.write(fetch_secret(credentials_secret))

    # Inject Gemini API key once (same for all accounts)
    if not os.environ.get("GEMINI_API_KEY"):
        os.environ["GEMINI_API_KEY"] = fetch_secret("gemini-api-key")

    print("Secrets loaded.\n")


def _process_account(account: dict) -> None:
    """Run the full pipeline for a single account."""
    provider = account.get("provider", "gmail")
    account_id = account["id"]

    if provider not in ("gmail", "outlook"):
        print(f"[{account_id}] Provider '{provider}' not yet implemented — skipping.")
        return

    cloud_init(account)

    if os.environ.get("USE_SAMPLE_DATA", "").lower() == "true":
        print("Error: run_all.py cannot loop with USE_SAMPLE_DATA=true (sample emails never get marked as read).")
        return

    print(f"[{account_id}] Authenticating with {provider.title()}...")
    if provider == "gmail":
        token_file    = account.get("token_file", TOKEN_FILE)
        service       = get_gmail_service(token_file)
        fetch_fn      = fetch_unread_emails
    else:
        service       = get_outlook_service(account)
        fetch_fn      = lambda svc: fetch_unread_emails_outlook(svc)
    print(f"[{account_id}] Authenticated.\n")

    tally_file = f"tally_log_{account_id}.csv"

    # Continue batch numbering from previous runs
    batch = 1
    if os.path.exists(tally_file):
        with open(tally_file, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if r.get("batch")]
            if rows:
                batch = int(rows[-1]["batch"]) + 1

    total = 0
    _label_keys = ["receipt", "invoice", "shipping", "subscription", "travel"]
    totals = {"pre_filtered": 0, "archived": 0, "read_only": 0, "action_required": 0,
              **{k: 0 for k in _label_keys}}

    def _batch_counts(results: list) -> dict:
        counts = {"pre_filtered": 0, "archived": 0, "read_only": 0, "action_required": 0,
                  **{k: 0 for k in _label_keys}}
        for _, info in results:
            if not info:
                continue
            if info.get("pre_filtered"):
                counts["pre_filtered"] += 1
            elif info.get("action_required"):
                counts["action_required"] += 1
            elif info.get("priority", 3) <= 2:
                counts["archived"] += 1
            else:
                counts["read_only"] += 1
            for k in _label_keys:
                if info.get(f"is_{k}"):
                    counts[k] += 1
        return counts

    def _update_totals(counts: dict) -> None:
        for k in totals:
            totals[k] += counts[k]

    def _print_tally(counts: dict) -> None:
        base_keys = ["pre_filtered", "archived", "read_only", "action_required"]
        processed = sum(totals[k] for k in base_keys)
        print(f"\n{'─'*40}")
        print(f"  RUNNING TALLY  ({processed} emails, {batch} batch(es))")
        print(f"{'─'*40}")
        print(f"  Pre-filtered (spam)  : {totals['pre_filtered']}")
        print(f"  Archived (AI p1-2)   : {totals['archived']}")
        print(f"  Read only  (AI p3)   : {totals['read_only']}")
        print(f"  Action required      : {totals['action_required']}")
        print(f"  ── Labels ──────────")
        for k in _label_keys:
            if totals[k]:
                print(f"  {k.capitalize():<20} : {totals[k]}")
        print(f"{'─'*40}\n")

        write_header = not os.path.exists(tally_file)
        with open(tally_file, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS)
            if write_header:
                writer.writeheader()
            writer.writerow({
                "timestamp":       datetime.now().isoformat(timespec="seconds"),
                "batch":           batch,
                "pre_filtered":    counts["pre_filtered"],
                "archived":        counts["archived"],
                "read_only":       counts["read_only"],
                "action_required": counts["action_required"],
                "total":           sum(counts[k] for k in base_keys),
                **{k: counts[k] for k in _label_keys},
            })

    while True:
        print(f"{'='*60}")
        print(f"  BATCH {batch}")
        print(f"{'='*60}")

        emails = fetch_fn(service)
        if not emails:
            print(f"\nInbox clear. {total} email(s) processed across {batch - 1} batch(es).")
            break

        print(f"Found {len(emails)} unread email(s).\n")

        results = analyze_emails(emails)
        print_digest(results)
        log_emails(results)
        create_entries(results)
        manage_inbox(service, results)
        if os.environ.get("SEND_DIGEST", "true").lower() != "false":
            send_digest_email(service, results, batch)
        pre_filter.learn_from_results(results)

        counts = _batch_counts(results)
        _update_totals(counts)
        _print_tally(counts)
        total += len(emails)
        batch += 1
        if MAX_BATCHES and batch > MAX_BATCHES:
            print(f"\nMAX_BATCHES={MAX_BATCHES} reached. Stopping.")
            break
        time.sleep(BATCH_DELAY_SECONDS)


def main():
    preflight()
    accounts = load_accounts()
    for account in accounts:
        print(f"\n{'#'*60}")
        print(f"  ACCOUNT: {account['id']}  [{account.get('provider','gmail').upper()}]")
        print(f"{'#'*60}\n")
        _process_account(account)


if __name__ == "__main__":
    main()
