"""
Pre-filter for known low-value emails.

Pattern-matches sender domains and subject keywords BEFORE sending to the AI,
saving Gemini API quota and eliminating 13-second delays for known spam/promos.

Controlled via PRE_FILTER env var (default: true). Config is read from
filter_config.json — edit that file to add/remove patterns.
"""

import re
import json
import os
from typing import Optional

FILTER_CONFIG_FILE = "filter_config.json"
FILTER_TLDR        = "Auto-filtered"


# sprint 22 — per-account data file isolation
def account_file_path(basename: str, account_id: str | None) -> str:
    """Return a per-account filename if account_id is given, else basename unchanged.

    e.g. account_file_path("filter_config.json", "gmail-personal")
         → "filter_config_gmail-personal.json"
    """
    if not account_id:
        return basename
    stem, _, ext = basename.rpartition(".")
    return f"{stem}_{account_id}.{ext}"


def _gcs_client():
    from google.cloud import storage
    return storage.Client()


def load_filter_config(account_id: str | None = None) -> dict:
    """Read filter_config from local disk or GCS (when GCS_BUCKET is set).

    When account_id is given, reads filter_config_<account_id>.json.
    """
    # sprint 22 — per-account filter config path
    path = account_file_path(FILTER_CONFIG_FILE, account_id)
    try:
        bucket = os.environ.get("GCS_BUCKET")
        if bucket:
            blob = _gcs_client().bucket(bucket).blob(path)
            if blob.exists():
                return json.loads(blob.download_as_text())
        elif os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError, Exception):
        pass
    return {"sender_domains": [], "subject_keywords": []}


def _extract_domain(sender: str) -> str:
    """Extract lowercased domain from 'Display Name <user@domain.com>' or 'user@domain.com'."""
    if not sender:
        return ""
    match = re.search(r"<[^@]+@([^>]+)>", sender)
    if match:
        return match.group(1).lower()
    match = re.search(r"[^@\s]+@([^\s>]+)", sender)
    if match:
        return match.group(1).lower()
    return ""


def _extract_address(sender: str) -> str:
    """Extract lowercased full email address from 'Display Name <user@domain.com>' or 'user@domain.com'."""
    if not sender:
        return ""
    match = re.search(r"<([^@]+@[^>]+)>", sender)
    if match:
        return match.group(1).lower()
    match = re.search(r"[^@\s]+@[^\s>]+", sender)
    if match:
        return match.group(0).lower()
    return ""


def matches_filter(email: dict, config: dict) -> bool:
    """Return True if the email matches any configured sender address, domain, or subject keyword."""
    sender = email.get("sender", "")

    address = _extract_address(sender)
    if address and address in config.get("sender_addresses", []):
        return True

    domain = _extract_domain(sender)
    if domain and domain in config.get("sender_domains", []):
        return True

    subject = email.get("subject", "").lower()
    for keyword in config.get("subject_keywords", []):
        if keyword.lower() in subject:
            return True

    return False


def make_filtered_result(email: dict) -> dict:
    """Return a standard analysis dict for a pre-filtered email (no AI call needed)."""
    return {
        "priority":        1,
        "tldr":            FILTER_TLDR,
        "action_required": False,
        "event_date":      None,
        "event_time":      None,
        "event_title":     None,
        "action_type":     None,
        "pre_filtered":    True,
    }


def learn_from_results(results: list[tuple[dict, dict | None]], account_id: str | None = None) -> int:
    """Add sender addresses to filter config when AI assigns priority 1.

    Only learns from AI-analyzed emails (not already pre-filtered ones).
    When account_id is given, updates filter_config_<account_id>.json.
    Returns the number of new addresses added.
    """
    # sprint 22 — per-account filter config learning
    config = load_filter_config(account_id)
    existing = set(config.get("sender_addresses", []))
    new_addresses = []

    for email, info in results:
        if not info:
            continue
        if info.get("pre_filtered"):
            continue  # already in the filter, nothing to learn
        if info.get("priority") == 1:
            address = _extract_address(email.get("sender", ""))
            if address and address not in existing:
                new_addresses.append(address)
                existing.add(address)

    if not new_addresses:
        return 0

    config["sender_addresses"] = sorted(existing)
    try:
        _write_filter_config(config, account_id)
        print(f"  [pre-filter] Learned {len(new_addresses)} new address(es): {', '.join(new_addresses)}")
    except (OSError, Exception) as e:
        print(f"  [warning] Could not update filter config: {e}")

    return len(new_addresses)


# sprint 22 — [AGENT IGNORE] / [AGENT UNIGNORE] command support

def parse_ignore_address(subject: str, prefix: str) -> str:
    """Extract and normalise the email address from an IGNORE/UNIGNORE subject line.

    e.g. "[AGENT IGNORE]  Foo@Bar.COM" → "foo@bar.com"
    Returns empty string if no address is found.
    """
    remainder = subject[len(prefix):].strip().lower()
    return remainder


def _write_filter_config(config: dict, account_id: str | None = None) -> None:
    """Write filter_config to local disk or GCS.

    When account_id is given, writes filter_config_<account_id>.json.
    """
    # sprint 22 — per-account filter config path
    path = account_file_path(FILTER_CONFIG_FILE, account_id)
    bucket = os.environ.get("GCS_BUCKET")
    if bucket:
        _gcs_client().bucket(bucket).blob(path).upload_from_string(
            json.dumps(config, indent=2), content_type="application/json"
        )
    else:
        with open(path, "w") as f:
            json.dump(config, f, indent=2)


def block_sender(address: str, account_id: str | None = None) -> str:
    """Add address to sender_addresses in the filter config.

    When account_id is given, updates filter_config_<account_id>.json.
    Returns a human-readable status string.
    Idempotent — if address already present, returns 'already blocked' message.
    """
    # sprint 22 — per-account block list
    config = load_filter_config(account_id)
    existing = set(config.get("sender_addresses", []))

    if address in existing:
        return f"{address} already blocked — no change"

    existing.add(address)
    config["sender_addresses"] = sorted(existing)
    try:
        _write_filter_config(config, account_id)
    except (OSError, Exception) as e:
        print(f"  [warning] Could not update filter config: {e}")
    return f"{address} added to block list"


def unblock_sender(address: str, account_id: str | None = None) -> str:
    """Remove address from sender_addresses in the filter config.

    When account_id is given, updates filter_config_<account_id>.json.
    Returns a human-readable status string.
    Safe — if address not present, returns 'not in list' message without error.
    """
    # sprint 22 — per-account block list
    config = load_filter_config(account_id)
    existing = set(config.get("sender_addresses", []))

    if address not in existing:
        return f"{address} not in block list — no change"

    existing.discard(address)
    config["sender_addresses"] = sorted(existing)
    try:
        _write_filter_config(config, account_id)
    except (OSError, Exception) as e:
        print(f"  [warning] Could not update filter config: {e}")
    return f"{address} removed from block list"
