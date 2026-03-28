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


def _gcs_client():
    from google.cloud import storage
    return storage.Client()


def load_filter_config() -> dict:
    """Read filter_config.json from local disk or GCS (when GCS_BUCKET is set)."""
    try:
        bucket = os.environ.get("GCS_BUCKET")
        if bucket:
            blob = _gcs_client().bucket(bucket).blob(FILTER_CONFIG_FILE)
            if blob.exists():
                return json.loads(blob.download_as_text())
        elif os.path.exists(FILTER_CONFIG_FILE):
            with open(FILTER_CONFIG_FILE) as f:
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


def matches_filter(email: dict, config: dict) -> bool:
    """Return True if the email matches any configured sender domain or subject keyword."""
    domain = _extract_domain(email.get("sender", ""))
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


def learn_from_results(results: list[tuple[dict, dict | None]]) -> int:
    """Add sender domains to filter_config.json when AI assigns priority 1.

    Only learns from AI-analyzed emails (not already pre-filtered ones).
    Returns the number of new domains added.
    """
    config = load_filter_config()
    existing = set(config.get("sender_domains", []))
    new_domains = []

    for email, info in results:
        if not info:
            continue
        if info.get("pre_filtered"):
            continue  # already in the filter, nothing to learn
        if info.get("priority") == 1:
            domain = _extract_domain(email.get("sender", ""))
            if domain and domain not in existing:
                new_domains.append(domain)
                existing.add(domain)

    if not new_domains:
        return 0

    config["sender_domains"] = sorted(existing)
    try:
        bucket = os.environ.get("GCS_BUCKET")
        if bucket:
            _gcs_client().bucket(bucket).blob(FILTER_CONFIG_FILE).upload_from_string(
                json.dumps(config, indent=2), content_type="application/json"
            )
        else:
            with open(FILTER_CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
        print(f"  [pre-filter] Learned {len(new_domains)} new domain(s): {', '.join(new_domains)}")
    except (OSError, Exception) as e:
        print(f"  [warning] Could not update filter config: {e}")

    return len(new_domains)
