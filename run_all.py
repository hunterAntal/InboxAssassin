"""
Runs the full email pipeline in a loop until there are no unread emails left.

Each pass fetches up to MAX_EMAILS unread emails, processes them, and marks
them as read. The loop exits when a fetch returns an empty inbox.
"""

import csv
import json
import os
import sys
import time
from datetime import datetime
from dotenv import load_dotenv

from fetch_emails import get_gmail_service, fetch_unprocessed_emails, load_label_rules, get_authenticated_address, apply_routing_labels, load_time_travel_commands, execute_time_travel, load_priority_overrides, load_pause_state, fetch_status_commands, get_pause_description, fetch_ignore_commands, fetch_digest_commands, fetch_model_commands, TOKEN_FILE, CREDENTIALS_FILE
from fetch_emails_outlook import get_outlook_service, fetch_unread_emails_outlook
from analyze_emails import (
    analyze_emails, print_digest, log_emails, create_entries, manage_inbox,
    load_digest_state, save_digest_state, should_send_digest,
    format_run_log, append_run_to_buffer, build_digest_body,
    send_agent_report, archive_read_agent_reports,
    handle_status_commands,
    handle_ignore_commands,  # sprint 22 — [AGENT IGNORE] / [AGENT UNIGNORE]
    handle_digest_command,   # sprint 22 — [AGENT DIGEST] on-demand report
    handle_model_command,    # sprint 22 — [AGENT MODEL] per-account model switching
)
import pre_filter

ACCOUNTS_FILE = "accounts.json"


def load_accounts() -> list[dict]:
    """Return active accounts from accounts.json. Falls back to a single default Gmail account.

    sprint 22 — active:false accounts are skipped with a visible message so the
    user knows why an account isn't running without editing JSON.
    """
    if not os.path.exists(ACCOUNTS_FILE):
        return [{"id": "gmail-personal", "provider": "gmail", "active": True}]
    with open(ACCOUNTS_FILE) as f:
        accounts = json.load(f)
    active = []
    for a in accounts:
        # sprint 22 — explicit active:false is the only opt-out; print so user can see it
        if a.get("active", True) is False:
            print(f"[{a['id']}] active: false — skipping")
        else:
            active.append(a)
    if not active:
        print("[accounts] No active accounts to process — nothing to do.")
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
    token_file         = account.get("token_file", TOKEN_FILE)

    with open(token_file, "w") as f:
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
        token_file = account.get("token_file", TOKEN_FILE)
        # sprint 22 — guard against missing token file so daemon doesn't hang on OAuth prompt
        if not os.path.exists(token_file):
            print(f"[{account_id}] {token_file} not found — skipping")
            return
        service       = get_gmail_service(token_file)
        fetch_fn      = fetch_unprocessed_emails
    else:
        service       = get_outlook_service(account)
        fetch_fn      = lambda svc: fetch_unread_emails_outlook(svc)
    print(f"[{account_id}] Authenticated.\n")

    label_rules: dict = {}
    excluded_ids: set = set()
    own_address: str = ""
    if provider == "gmail":
        try:
            own_address = get_authenticated_address(service)
            label_rules, excluded_ids = load_label_rules(service, own_address)
        except Exception as e:
            print(f"  [warning] Could not resolve authenticated address — label rules disabled: {e}")

    priority_overrides: list = []
    if provider == "gmail" and own_address:
        try:
            paused, pause_excluded = load_pause_state(service, own_address)
            excluded_ids |= pause_excluded
            if paused:
                return
        except Exception as e:
            print(f"  [warning] Could not check pause state — continuing: {e}")

        try:
            tt_commands, tt_excluded = load_time_travel_commands(service, own_address)
            excluded_ids |= tt_excluded
            for cmd in tt_commands:
                execute_time_travel(service, cmd, own_address, label_rules)
        except Exception as e:
            print(f"  [warning] Time travel commands failed — skipping: {e}")

        try:
            po_overrides, po_excluded = load_priority_overrides(service, own_address)
            priority_overrides = po_overrides
            excluded_ids |= po_excluded
        except Exception as e:
            print(f"  [warning] Priority overrides failed — skipping: {e}")

        try:
            status_ids, status_excluded = fetch_status_commands(service, own_address)
            excluded_ids |= status_excluded
            if status_ids:
                pause_desc = get_pause_description(service, own_address) if paused else "Active  (runs every 4 hours)"
                handle_status_commands(
                    service=service,
                    own_address=own_address,
                    status_msg_ids=status_ids,
                    is_paused=paused,
                    pause_description=pause_desc,
                    accounts=load_accounts(),
                )
        except Exception as e:
            print(f"  [warning] STATUS command failed — skipping: {e}")

        # sprint 22 — [AGENT IGNORE] / [AGENT UNIGNORE] dispatch
        try:
            ignore_cmds, unignore_cmds, ignore_excluded = fetch_ignore_commands(service, own_address)
            excluded_ids |= ignore_excluded
            if ignore_cmds or unignore_cmds:
                handle_ignore_commands(service, ignore_cmds, unignore_cmds, account_id=account_id)
        except Exception as e:
            print(f"  [warning] IGNORE/UNIGNORE command failed — skipping: {e}")

        # sprint 22 — [AGENT DIGEST] on-demand report dispatch
        try:
            digest_ids, digest_excluded = fetch_digest_commands(service, own_address)
            excluded_ids |= digest_excluded
            if digest_ids:
                handle_digest_command(service, digest_ids, account_id=account_id)
        except Exception as e:
            print(f"  [warning] DIGEST command failed — skipping: {e}")

        # sprint 22 — [AGENT MODEL] per-account model switching dispatch
        try:
            model_cmds, model_excluded = fetch_model_commands(service, own_address)
            excluded_ids |= model_excluded
            if model_cmds:
                handle_model_command(service, model_cmds, account_id=account_id)
        except Exception as e:
            print(f"  [warning] MODEL command failed — skipping: {e}")

        try:
            archive_read_agent_reports(service)
        except Exception as e:
            print(f"  [warning] Agent Report archive sweep failed: {e}")

    tz_name = os.environ.get("TZ", "UTC")
    digest_state_file = f"agent_digest_state_{account_id}.json"
    digest_state = load_digest_state(digest_state_file)
    all_run_results: list = []

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

        emails = fetch_fn(service, exclude_ids=excluded_ids) if provider == "gmail" else fetch_fn(service)
        if not emails:
            print(f"\nInbox clear. {total} email(s) processed across {batch - 1} batch(es).")
            break

        print(f"Found {len(emails)} email(s) to process.\n")

        if provider == "gmail" and label_rules:
            apply_routing_labels(service, emails, label_rules)

        results = analyze_emails(emails, label_rules, priority_overrides)
        print_digest(results)
        # sprint 22 — per-account data file isolation
        log_emails(results, account_id=account_id)
        create_entries(results, account_id=account_id)
        manage_inbox(service, results)
        pre_filter.learn_from_results(results, account_id=account_id)
        all_run_results.extend(results)

        counts = _batch_counts(results)
        _update_totals(counts)
        _print_tally(counts)
        total += len(emails)
        batch += 1
        if MAX_BATCHES and batch > MAX_BATCHES:
            print(f"\nMAX_BATCHES={MAX_BATCHES} reached. Stopping.")
            break
        time.sleep(BATCH_DELAY_SECONDS)

    # ── Twice-daily Agent Report digest ─────────────────────────────────────
    if provider == "gmail" and os.environ.get("SEND_DIGEST", "true").lower() != "false":
        run_log = format_run_log(all_run_results, account_id, datetime.now(), tz_name)
        # sprint 22 — per-account log buffer
        append_run_to_buffer(run_log, account_id=account_id)

        period = should_send_digest(digest_state, datetime.now(), tz_name)
        if period:
            from analyze_emails import _read_text, _write_text, AGENT_LOG_BUFFER
            from pre_filter import account_file_path
            buf_path = account_file_path(AGENT_LOG_BUFFER, account_id)
            buffer = _read_text(buf_path)
            body = build_digest_body(buffer, period, datetime.now())
            date_str = datetime.now().strftime("%b %d, %Y")
            send_agent_report(service, period, body, date_str)
            _write_text(buf_path, "")
            digest_state[f"last_{period}"] = datetime.now().date().isoformat()
            save_digest_state(digest_state, digest_state_file)


def _preflight_checks() -> bool:
    """Validate required files, env vars, and runtime deps before processing.

    Runs ALL checks before returning — does not stop on first failure.
    Returns True only if every check passes.
    """
    import subprocess
    import urllib.request
    import json as _json

    ok = True
    backend = os.environ.get("MODEL_BACKEND", "gemini")

    # ── 1. Python version ────────────────────────────────────────────────────
    major, minor, micro = sys.version_info[:3]
    ver_str = f"{major}.{minor}.{micro}"
    if major < 3 or (major == 3 and minor < 10):
        print(f"  ✗ Python {ver_str} — requires 3.10+")
        print("      → brew install python@3.12")
        ok = False
    else:
        print(f"  ✓ Python {ver_str}")

    # ── 2. .env file ─────────────────────────────────────────────────────────
    if not os.path.exists(".env"):
        print("  ✗ .env not found")
        print("      → cp .env.mac.example .env  then edit it")
        ok = False
    else:
        print("  ✓ .env")

    # ── 3. credentials.json ──────────────────────────────────────────────────
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"  ✗ {CREDENTIALS_FILE} not found")
        print("      → Download from GCP Console → APIs & Services → Credentials → OAuth 2.0 Client → Download JSON")
        ok = False
    else:
        print(f"  ✓ {CREDENTIALS_FILE}")

    # ── 4. Token files per active Gmail account ──────────────────────────────
    accounts = load_accounts()
    active_gmail = [
        a for a in accounts
        if a.get("provider", "gmail") == "gmail"
    ]
    for acc in active_gmail:
        tf = acc.get("token_file", TOKEN_FILE)
        acc_id = acc.get("id", "?")
        if not os.path.exists(tf):
            # sprint 22 — warn-and-skip instead of aborting; _process_account guards too
            print(f"  ✗ {tf} not found  ({acc_id}) — skipping")
        else:
            print(f"  ✓ {tf}  ({acc_id})")

    # ── 5. Gemini API key (gemini backend only) ──────────────────────────────
    if backend == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            print("  ✗ GEMINI_API_KEY not set — add it to your .env file")
            ok = False
        else:
            print("  ✓ GEMINI_API_KEY")

    # ── 6. pip packages ──────────────────────────────────────────────────────
    pip_check = subprocess.run(
        [sys.executable, "-m", "pip", "show",
         "google-auth", "google-api-python-client", "python-dotenv"],
        capture_output=True,
    )
    if pip_check.returncode != 0:
        print("  ✗ Missing packages")
        print("      → pip install -r requirements.txt")
        ok = False
    else:
        print("  ✓ pip packages")

    # ── 7–9. Ollama checks (local backend only) ──────────────────────────────
    if backend == "local":
        # 7. Ollama installed
        try:
            ollama_ver = subprocess.run(
                ["ollama", "--version"], capture_output=True, text=True, timeout=5,
            )
            if ollama_ver.returncode != 0:
                raise FileNotFoundError
            version = ollama_ver.stdout.strip().replace("ollama version ", "")
            print(f"  ✓ Ollama {version}")
        except (FileNotFoundError, OSError):
            print("  ✗ Ollama not found")
            print("      → brew install ollama")
            ok = False
        else:
            # 8. Ollama running
            try:
                with urllib.request.urlopen(
                    "http://localhost:11434/api/tags", timeout=3
                ) as resp:
                    data = _json.loads(resp.read())
                print("  ✓ Ollama running")

                # 9. Required model available
                # Sprint 21 — auto-pull model on startup if missing
                model_name = os.environ.get("LOCAL_MODEL", "llama3.1:latest")
                available = [m["name"] for m in data.get("models", [])]
                if model_name not in available:
                    print(f"\n  Model {model_name} not found locally.")
                    print()
                    print("  Downloading it will use several GB of data (exact size varies by model).")
                    print("  Make sure you are on Wi-Fi and have your laptop plugged in.")
                    print()
                    answer = input("  Download now? [Y/n]: ")
                    import setup_helper as _sh
                    if _sh.should_pull(answer):
                        print(f"\n  Pulling {model_name} — this may take a few minutes...")
                        pull = subprocess.run(["ollama", "pull", model_name])
                        pulled_ok, pull_msg = _sh.interpret_pull_result(pull.returncode, model_name)
                        if pulled_ok:
                            print(f"  ✓ {pull_msg}")
                        else:
                            print(f"  ✗ {pull_msg}")
                            print("      → Check the model name and your internet connection")
                            ok = False
                    else:
                        print(f"  ✗ Model {model_name} required to run in local mode.")
                        print("      → When ready, re-run: python3 run_all.py")
                        sys.exit(0)
                else:
                    print(f"  ✓ Model {model_name} available")

            except OSError:
                print("  ✗ Ollama not running")
                print("      → ollama serve")
                ok = False

    # ── 10. Active accounts ──────────────────────────────────────────────────
    if not accounts:
        print("  ✗ No active accounts in accounts.json")
        print('      → Set "active": true for at least one account')
        ok = False
    else:
        print(f"  ✓ accounts.json — {len(accounts)} active account(s)")

    return ok


def main():
    print("Pre-flight checks...")
    if not _preflight_checks():
        print("\nPre-flight failed — fix the issues above and retry.")
        return
    print()

    accounts = load_accounts()
    for account in accounts:
        print(f"\n{'#'*60}")
        print(f"  ACCOUNT: {account['id']}  [{account.get('provider','gmail').upper()}]")
        print(f"{'#'*60}\n")
        try:
            _process_account(account)
        except Exception as e:
            print(f"[{account['id']}] ERROR — skipping account: {e}")


if __name__ == "__main__":
    main()
