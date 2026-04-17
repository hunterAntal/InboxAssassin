"""
Gmail authentication and email fetching.

Handles OAuth 2.0 token management and returns a uniform email dict
consumed by analyze_emails.py. Set USE_SAMPLE_DATA=true in .env to
load from sample_emails.json instead of hitting the Gmail API.
"""

import os
import re
import json
import base64
import socket
import time
from typing import Any
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()


def gmail_execute(request, retries: int = 3, backoff: float = 5.0):
    """Execute a Gmail API request, retrying on transient network errors."""
    for attempt in range(retries):
        try:
            return request.execute()
        except (TimeoutError, socket.timeout, OSError) as e:
            if attempt == retries - 1:
                raise
            print(f"  [warning] Network timeout, retrying in {backoff:.0f}s... ({e})")
            time.sleep(backoff)
            backoff *= 2

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/tasks",
]

CREDENTIALS_FILE   = "credentials.json"
TOKEN_FILE         = "token.json"
SAMPLE_FILE        = "sample_emails.json"
MAX_EMAILS         = int(os.environ.get("MAX_EMAILS", 50))
SNIPPET_LENGTH     = 100   # chars shown in digest preview
BODY_LENGTH        = 500   # chars sent to AI for analysis
AGENT_RULE_SUBJECT        = "[AGENT RULE]"
AGENT_TIMETRAVEL_SUBJECT  = "[AGENT TIMETRAVEL]"
AGENT_PRIORITY_SUBJECT    = "[AGENT PRIORITY]"
AGENT_PAUSE_SUBJECT       = "[AGENT PAUSE]"
AGENT_RESUME_SUBJECT      = "[AGENT RESUME]"
AGENT_STATUS_SUBJECT      = "[AGENT STATUS]"
# sprint 22 — [AGENT IGNORE] / [AGENT UNIGNORE] commands
AGENT_IGNORE_SUBJECT      = "[AGENT IGNORE]"
AGENT_UNIGNORE_SUBJECT    = "[AGENT UNIGNORE]"
# sprint 22 — [AGENT DIGEST] on-demand report command
AGENT_DIGEST_SUBJECT      = "[AGENT DIGEST]"
# sprint 22 — [AGENT MODEL] per-account model switching
AGENT_MODEL_SUBJECT       = "[AGENT MODEL]"
TIME_TRAVEL_MAX_DEFAULT   = 500

# Populated by load_label_rules; maps label_id → label_name for fetch_unprocessed_emails.
_LABEL_ID_CACHE: dict[str, str] = {}


def get_gmail_service(token_file: str = TOKEN_FILE) -> Any:
    """Authenticate and return a Gmail API service object."""
    creds = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                print(f"  [auth] Token expired or revoked — clearing {token_file} and re-authenticating...")
                if os.environ.get("GCP_PROJECT"):
                    raise RuntimeError(
                        f"Token in '{token_file}' is expired/revoked on GCP. "
                        "Re-run OAuth locally and push a fresh token to Secret Manager."
                    )
                creds = None
                os.remove(token_file)
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        token_json = creds.to_json()

        with open(token_file, "w") as f:
            f.write(token_json)

        # On GCP, push the refreshed token back to Secret Manager so the
        # next container run doesn't start with a stale/expired token.
        _sync_token_to_secret_manager(token_json)

    return build("gmail", "v1", credentials=creds)


def _sync_token_to_secret_manager(token_json: str) -> None:
    """Write the current token back to Secret Manager (GCP only)."""
    project = os.environ.get("GCP_PROJECT")
    if not project:
        return
    try:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()
        client.add_secret_version(
            parent=f"projects/{project}/secrets/gmail-token",
            payload={"data": token_json.encode()},
        )
    except Exception as e:
        print(f"[warning] Could not sync token to Secret Manager: {e}")


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode_body(payload: dict) -> str:
    """Extract and decode plain text from a Gmail message payload."""
    if "body" in payload and payload["body"].get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part["body"].get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")

    return ""


def parse_route_criteria(body: str) -> dict:
    """Extract the ROUTE: block from a rule body and return parsed criteria.

    Returns {"from": [...], "subject": [...]} with all values lowercased.
    Returns {} if no ROUTE: section exists or criteria are empty.
    """
    route_match = re.search(
        r'ROUTE:\s*\n(.*?)(?:\n[ \t]*\n|\Z)', body, re.DOTALL | re.IGNORECASE
    )
    if not route_match:
        return {}

    criteria: dict[str, list[str]] = {"from": [], "subject": []}
    for line in route_match.group(1).splitlines():
        line = line.strip()
        if line.lower().startswith("from:"):
            criteria["from"] = [v.strip().lower() for v in line[5:].split(",") if v.strip()]
        elif line.lower().startswith("subject:"):
            criteria["subject"] = [v.strip().lower() for v in line[8:].split(",") if v.strip()]

    if not criteria["from"] and not criteria["subject"]:
        return {}
    return criteria


def _email_matches_criteria(email: dict, criteria: dict) -> bool:
    """Return True if the email matches any from: domain/address or subject: keyword."""
    if not criteria:
        return False

    sender  = email.get("sender", "").lower()
    subject = email.get("subject", "").lower()

    for pattern in criteria.get("from", []):
        if pattern in sender:
            return True

    for keyword in criteria.get("subject", []):
        if keyword in subject:
            return True

    return False


def apply_routing_labels(service: Any, emails: list[dict], label_rules: dict) -> None:
    """Apply Gmail labels to emails that match ROUTE: criteria in their rule.

    Mutates each email dict in-place by appending the matched label to label_names.
    Skips emails that already carry the label. Logs warnings on API errors.
    """
    # Build name → id reverse lookup from the existing id → name cache.
    label_name_to_id = {name: lid for lid, name in _LABEL_ID_CACHE.items()}

    for label_name, rule_body in label_rules.items():
        criteria = parse_route_criteria(rule_body)
        if not criteria:
            continue

        label_id = label_name_to_id.get(label_name)
        if not label_id:
            print(f'  [warning] Label "{label_name}" not found — routing skipped')
            continue

        for em in emails:
            if label_name in em.get("label_names", []):
                continue
            if not _email_matches_criteria(em, criteria):
                continue
            try:
                gmail_execute(service.users().messages().modify(
                    userId="me",
                    id=em["message_id"],
                    body={"addLabelIds": [label_id]},
                ))
                em.setdefault("label_names", []).append(label_name)
                print(f'  [label-rule] routed: "{em["subject"][:50]}" → "{label_name}"')
            except Exception as e:
                print(f'  [warning] Could not apply label "{label_name}" to {em["message_id"]}: {e}')


def _iso_to_gmail_date(iso_date: str) -> str:
    """Convert ISO date string (YYYY-MM-DD) to Gmail query format (YYYY/M/D).

    Gmail's after:/before: operators use YYYY/M/D without leading zeros.
    """
    year, month, day = iso_date.split("-")
    return f"{year}/{int(month)}/{int(day)}"


def parse_time_travel_command(body: str) -> dict | None:
    """Parse an [AGENT TIMETRAVEL] email body into a structured command dict.

    Required fields: from:, apply-label:
    Optional fields: after:, before:, max: (default 500), mode: (default label-only)

    Returns None if required fields are missing.
    """
    criteria: dict = {
        "from": [],
        "after": None,
        "before": None,
        "apply-label": None,
        "max": TIME_TRAVEL_MAX_DEFAULT,
        "mode": "label-only",
    }

    for line in body.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith("from:"):
            vals = [v.strip() for v in line[5:].split(",") if v.strip()]
            criteria["from"] = vals
        elif low.startswith("after:"):
            criteria["after"] = line[6:].strip()
        elif low.startswith("before:"):
            criteria["before"] = line[7:].strip()
        elif low.startswith("apply-label:"):
            criteria["apply-label"] = line[12:].strip()
        elif low.startswith("max:"):
            try:
                criteria["max"] = int(line[4:].strip())
            except ValueError:
                pass
        elif low.startswith("mode:"):
            criteria["mode"] = line[5:].strip().lower()

    if not criteria["from"] or not criteria["apply-label"]:
        return None
    return criteria


def load_time_travel_commands(service: Any, own_address: str) -> tuple[list[dict], set]:
    """Scan inbox for self-sent [AGENT TIMETRAVEL] emails and parse them.

    Returns:
        commands     — list of parsed command dicts (with message_id injected)
        excluded_ids — message IDs of command emails (exclude from normal processing)

    Security: only emails where sender == own_address are accepted.
    """
    commands: list[dict] = []
    excluded_ids: set[str] = set()

    try:
        result = gmail_execute(service.users().messages().list(
            userId="me",
            q=f'subject:"{AGENT_TIMETRAVEL_SUBJECT}" -label:"AI Processed"',
        ))
    except Exception as e:
        print(f"  [warning] Could not load time travel commands: {e}")
        return commands, excluded_ids

    messages = result.get("messages", [])
    if not messages:
        return commands, excluded_ids

    for msg_ref in messages:
        try:
            msg     = gmail_execute(service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ))
            headers = msg["payload"]["headers"]
            sender  = _get_header(headers, "From")

            addr_match  = re.search(r"<(.+?)>", sender)
            sender_addr = addr_match.group(1) if addr_match else sender

            if sender_addr.strip().lower() != own_address.strip().lower():
                print(f'  [time-travel] ignored: sender not self ({sender_addr})')
                continue

            body = _decode_body(msg["payload"]).strip()
            if not body:
                print(f'  [time-travel] ignored: empty body (id={msg_ref["id"]})')
                continue

            parsed = parse_time_travel_command(body)
            if parsed is None:
                print(f'  [time-travel] ignored: missing required fields (id={msg_ref["id"]})')
                continue

            parsed["message_id"] = msg_ref["id"]
            commands.append(parsed)
            excluded_ids.add(msg_ref["id"])
            print(f'  [time-travel] loaded: apply-label="{parsed["apply-label"]}" '
                  f'from={parsed["from"]} max={parsed["max"]} mode={parsed["mode"]}')

        except Exception as e:
            print(f"  [warning] Could not parse time travel command {msg_ref['id']}: {e}")

    return commands, excluded_ids


def execute_time_travel(
    service: Any,
    command: dict,
    own_address: str,
    label_rules: dict,
) -> None:
    """Execute a single [AGENT TIMETRAVEL] command.

    Searches Gmail history matching the command criteria, applies the target
    label to up to command['max'] matches, then marks the command email as
    'AI Processed' so it never runs again (one-shot).

    mode='label-only': applies label only (no AI analysis)
    mode='full-pipeline': label + AI analysis (passed back via return value stub;
                          orchestrator handles analysis)
    """
    label_name = command["apply-label"]
    label_name_to_id = {name: lid for lid, name in _LABEL_ID_CACHE.items()}

    label_id = label_name_to_id.get(label_name)
    if not label_id:
        print(f'  [time-travel] label "{label_name}" not found in Gmail — aborting command')
        return

    # Build Gmail search query
    from_clauses = " OR ".join(f"from:{f}" for f in command["from"])
    query_parts  = [f"({from_clauses})"]
    if command.get("after"):
        query_parts.append(f"after:{_iso_to_gmail_date(command['after'])}")
    if command.get("before"):
        query_parts.append(f"before:{_iso_to_gmail_date(command['before'])}")
    # Exclude already-labeled emails
    query_parts.append(f'-label:"{label_name}"')
    query = " ".join(query_parts)

    print(f'  [time-travel] searching: {query}')

    try:
        result = gmail_execute(service.users().messages().list(
            userId="me",
            q=query,
            maxResults=command["max"],
        ))
    except Exception as e:
        print(f"  [warning] time travel search failed: {e}")
        return

    messages = result.get("messages", [])
    print(f'  [time-travel] found {len(messages)} email(s) matching criteria')

    labeled = 0
    for msg_ref in messages[:command["max"]]:
        try:
            gmail_execute(service.users().messages().modify(
                userId="me",
                id=msg_ref["id"],
                body={"addLabelIds": [label_id]},
            ))
            labeled += 1
        except Exception as e:
            print(f"  [warning] could not label {msg_ref['id']}: {e}")

    print(f'  [time-travel] labeled {labeled} email(s) → "{label_name}"')

    # One-shot: mark command email as AI Processed so it never runs again
    ai_proc_id = label_name_to_id.get("AI Processed")
    mark_body: dict = {"addLabelIds": []}
    if ai_proc_id:
        mark_body["addLabelIds"].append(ai_proc_id)
    try:
        gmail_execute(service.users().messages().modify(
            userId="me",
            id=command["message_id"],
            body=mark_body,
        ))
        print(f'  [time-travel] command marked done (one-shot consumed)')
    except Exception as e:
        print(f"  [warning] could not mark time travel command as done: {e}")


def parse_priority_override(body: str) -> dict | None:
    """Parse a single [AGENT PRIORITY] email body into an override dict.

    Expected format: "keyword = N" where N is an integer 1–5.
    Keyword may be multi-word. Whitespace is stripped from both sides.

    Returns {"keyword": str, "priority": int} or None if invalid.
    """
    body = body.strip()
    if "=" not in body:
        return None

    idx = body.rfind("=")
    keyword = body[:idx].strip().lower()
    value   = body[idx + 1:].strip()

    try:
        priority = int(value)
    except ValueError:
        return None

    if not keyword or not (1 <= priority <= 5):
        return None

    return {"keyword": keyword, "priority": priority}


def load_priority_overrides(service: Any, own_address: str) -> tuple[list[dict], set]:
    """Scan inbox for self-sent [AGENT PRIORITY] emails and parse them.

    Returns:
        overrides    — list of {"keyword": str, "priority": int} dicts
        excluded_ids — message IDs of command emails (exclude from normal processing)

    Security: only emails where sender == own_address are accepted.
    """
    overrides: list[dict] = []
    excluded_ids: set[str] = set()

    try:
        result = gmail_execute(service.users().messages().list(
            userId="me",
            q=f'subject:"{AGENT_PRIORITY_SUBJECT}" -label:"AI Processed"',
        ))
    except Exception as e:
        print(f"  [warning] Could not load priority overrides: {e}")
        return overrides, excluded_ids

    messages = result.get("messages", [])
    if not messages:
        return overrides, excluded_ids

    for msg_ref in messages:
        try:
            msg     = gmail_execute(service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ))
            headers = msg["payload"]["headers"]
            sender  = _get_header(headers, "From")

            addr_match  = re.search(r"<(.+?)>", sender)
            sender_addr = addr_match.group(1) if addr_match else sender

            if sender_addr.strip().lower() != own_address.strip().lower():
                print(f'  [priority-override] ignored: sender not self ({sender_addr})')
                continue

            body = _decode_body(msg["payload"]).strip()
            if not body:
                print(f'  [priority-override] ignored: empty body (id={msg_ref["id"]})')
                continue

            parsed = parse_priority_override(body)
            if parsed is None:
                print(f'  [priority-override] ignored: invalid format — expected "keyword = N" (got "{body[:40]}")')
                continue

            overrides.append(parsed)
            excluded_ids.add(msg_ref["id"])
            print(f'  [priority-override] loaded: "{parsed["keyword"]}" → {parsed["priority"]}')

        except Exception as e:
            print(f"  [warning] Could not parse priority override {msg_ref['id']}: {e}")

    return overrides, excluded_ids


def parse_pause_command(body: str) -> dict:
    """Parse an [AGENT PAUSE] email body into a structured command dict.

    Supported formats (first match wins):
      duration: N hour(s)/day(s)/week(s)/year(s)
      until: YYYY-MM-DD
      daily: HH:MM - HH:MM
      (anything else) → indefinite

    Returns a dict with at minimum {"type": "indefinite"|"duration"|"until"|"daily"}.
    Invalid values fall back to indefinite with a warning flag.
    """
    from datetime import date as _date, time as _time

    body = body.strip()
    for line in body.splitlines():
        line = line.strip()
        low  = line.lower()

        if low.startswith("duration:"):
            raw = line[9:].strip()
            parts = raw.split()
            if len(parts) >= 2:
                try:
                    amount = int(parts[0])
                    unit   = parts[1].lower().rstrip("s") + "s"  # normalise to plural
                    if unit in ("hours", "days", "weeks", "years") and amount >= 0:
                        return {"type": "duration", "amount": amount, "unit": unit}
                except ValueError:
                    pass
            print(f'  [agent-pause] invalid duration "{raw}" — pausing indefinitely as fallback')
            return {"type": "indefinite"}

        if low.startswith("until:"):
            raw = line[6:].strip()
            try:
                parsed_date = _date.fromisoformat(raw)
                return {"type": "until", "date": parsed_date}
            except ValueError:
                print(f'  [agent-pause] invalid date "{raw}" — pausing indefinitely as fallback')
                return {"type": "indefinite"}

        if low.startswith("daily:"):
            raw = line[6:].strip()
            try:
                start_str, end_str = [s.strip() for s in raw.split("-", 1)]
                sh, sm = [int(x) for x in start_str.split(":")]
                eh, em = [int(x) for x in end_str.split(":")]
                return {"type": "daily", "start": _time(sh, sm), "end": _time(eh, em)}
            except (ValueError, TypeError):
                print(f'  [agent-pause] invalid window "{raw}" — pausing indefinitely as fallback')
                return {"type": "indefinite"}

    return {"type": "indefinite"}


def _is_pause_active(command: dict, send_time: "datetime", now: "datetime") -> bool:
    """Return True if the pause command is currently in effect.

    Args:
        command:   parsed pause dict from parse_pause_command()
        send_time: when the [AGENT PAUSE] email was sent (aware datetime)
        now:       current time (aware datetime)
    """
    from datetime import timedelta as _td

    kind = command["type"]

    if kind == "indefinite":
        return True

    if kind == "duration":
        unit_map = {
            "hours": _td(hours=1),
            "days":  _td(days=1),
            "weeks": _td(weeks=1),
            "years": _td(days=365),
        }
        delta   = unit_map[command["unit"]] * command["amount"]
        expires = send_time + delta
        return now < expires

    if kind == "until":
        return now.date() < command["date"]

    if kind == "daily":
        now_time = now.timetz().replace(tzinfo=None)
        # Strip tz for comparison since we compare within-day only
        now_naive = now.time().replace(second=0, microsecond=0)
        return command["start"] <= now_naive < command["end"]

    return False


def _parse_email_date(date_str: str) -> "datetime":
    """Parse an RFC 2822 email Date header into an aware UTC datetime."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        from datetime import datetime as _dt, timezone as _tz
        return _dt.now(_tz.utc)


def load_pause_state(service: Any, own_address: str) -> tuple[bool, set]:
    """Check for [AGENT PAUSE] and [AGENT RESUME] commands.

    Logic:
      1. If a valid self-sent [AGENT RESUME] exists → mark all pause + resume emails
         as AI Processed, return (False, set()) — agent runs normally.
      2. Otherwise check [AGENT PAUSE] emails:
         - Expired → mark AI Processed, skip.
         - Active  → return (True, {excluded_ids}).
      3. If none active → return (False, set()).

    Security: only self-sent emails accepted for both commands.
    """
    from datetime import datetime as _dt, timezone as _tz

    now         = _dt.now(_tz.utc)
    excluded    : set[str] = set()
    pause_ids   : list[str] = []
    active_pause: bool = False
    active_log  : str  = ""

    label_name_to_id = {name: lid for lid, name in _LABEL_ID_CACHE.items()}
    ai_proc_id       = label_name_to_id.get("AI Processed")

    def _mark_done(msg_id: str) -> None:
        body: dict = {"addLabelIds": [ai_proc_id]} if ai_proc_id else {}
        try:
            gmail_execute(service.users().messages().modify(
                userId="me", id=msg_id, body=body
            ))
        except Exception as e:
            print(f"  [warning] Could not mark {msg_id} as done: {e}")

    # ── Check for [AGENT RESUME] ────────────────────────────────────────────
    try:
        resume_result = gmail_execute(service.users().messages().list(
            userId="me",
            q=f'subject:"{AGENT_RESUME_SUBJECT}" -label:"AI Processed"',
        ))
        resume_msgs = resume_result.get("messages", [])
    except Exception as e:
        print(f"  [warning] Could not check for resume command: {e}")
        resume_msgs = []

    valid_resume = False
    for msg_ref in resume_msgs:
        try:
            msg     = gmail_execute(service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ))
            sender  = _get_header(msg["payload"]["headers"], "From")
            addr_m  = re.search(r"<(.+?)>", sender)
            sender_addr = addr_m.group(1) if addr_m else sender
            if sender_addr.strip().lower() != own_address.strip().lower():
                print(f'  [agent-resume] ignored: sender not self ({sender_addr})')
                continue
            valid_resume = True
            pause_ids.append(msg_ref["id"])  # resume email — mark done too
        except Exception as e:
            print(f"  [warning] Could not read resume command {msg_ref['id']}: {e}")

    # ── Check for [AGENT PAUSE] ─────────────────────────────────────────────
    try:
        pause_result = gmail_execute(service.users().messages().list(
            userId="me",
            q=f'subject:"{AGENT_PAUSE_SUBJECT}" -label:"AI Processed"',
        ))
        pause_msgs = pause_result.get("messages", [])
    except Exception as e:
        print(f"  [warning] Could not check for pause command: {e}")
        pause_msgs = []

    active_count = 0
    for msg_ref in pause_msgs:
        try:
            msg       = gmail_execute(service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ))
            headers   = msg["payload"]["headers"]
            sender    = _get_header(headers, "From")
            addr_m    = re.search(r"<(.+?)>", sender)
            sender_addr = addr_m.group(1) if addr_m else sender

            if sender_addr.strip().lower() != own_address.strip().lower():
                print(f'  [agent-pause] ignored: sender not self ({sender_addr})')
                continue

            date_str  = _get_header(headers, "Date")
            send_time = _parse_email_date(date_str)
            body      = _decode_body(msg["payload"]).strip()
            command   = parse_pause_command(body)

            if not _is_pause_active(command, send_time, now):
                print(f'  [agent-pause] pause expired — resuming normally')
                _mark_done(msg_ref["id"])
                continue

            if valid_resume:
                # Resume overrides — mark this pause done
                pause_ids.append(msg_ref["id"])
                continue

            active_count += 1
            excluded.add(msg_ref["id"])

        except Exception as e:
            print(f"  [warning] Could not read pause command {msg_ref['id']}: {e}")

    # ── Handle resume outcome ───────────────────────────────────────────────
    if valid_resume:
        for mid in pause_ids:
            _mark_done(mid)
        print(f'  [agent-pause] RESUMED via [AGENT RESUME] command — running normally')
        return False, set()

    if active_count > 0:
        count_str = f" ({active_count} found)" if active_count > 1 else ""
        print(f'  [agent-pause] PAUSED{count_str} — send [AGENT RESUME] or mark "AI Processed" to resume')
        active_pause = True

    return active_pause, excluded


def get_pause_description(service: Any, own_address: str) -> str:
    """Return a human-readable scheduler status string for the active pause, if any.

    Returns "Active  (runs every 4 hours)" if not paused.
    Queries Gmail for the active [AGENT PAUSE] email to extract detail.
    """
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    try:
        result = gmail_execute(service.users().messages().list(
            userId="me",
            q=f'subject:"{AGENT_PAUSE_SUBJECT}" -label:"AI Processed"',
        ))
        messages = result.get("messages", [])
    except Exception:
        return "PAUSED"

    for msg_ref in messages:
        try:
            msg = gmail_execute(service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ))
            headers = msg["payload"]["headers"]
            sender  = _get_header(headers, "From")
            addr_m  = re.search(r"<(.+?)>", sender)
            sender_addr = addr_m.group(1) if addr_m else sender
            if sender_addr.strip().lower() != own_address.strip().lower():
                continue
            date_str  = _get_header(headers, "Date")
            send_time = _parse_email_date(date_str)
            body      = _decode_body(msg["payload"]).strip()
            command   = parse_pause_command(body)
            if _is_pause_active(command, send_time, now):
                return _format_pause_description(command)
        except Exception:
            continue

    return "Active  (runs every 4 hours)"


def _format_pause_description(command: dict) -> str:
    """Return a human-readable scheduler status string from a parsed pause command dict."""
    kind = command.get("type", "indefinite")
    if kind == "indefinite":
        return "PAUSED  (indefinite)"
    if kind == "duration":
        amount = command.get("amount", "?")
        unit   = command.get("unit", "")
        return f"PAUSED  ({amount} {unit})"
    if kind == "until":
        d = command.get("date")
        date_str = d.strftime("%b %d, %Y") if d else "unknown date"
        return f"PAUSED  until {date_str}"
    if kind == "daily":
        start = command.get("start")
        end   = command.get("end")
        s_str = start.strftime("%H:%M") if start else "?"
        e_str = end.strftime("%H:%M") if end else "?"
        return f"PAUSED  daily {s_str}–{e_str}"
    return "PAUSED"


def fetch_status_commands(service: Any, own_address: str) -> tuple[list[str], set[str]]:
    """Scan inbox for self-sent [AGENT STATUS] emails.

    Returns:
        msg_ids      — list of message IDs to process (one reply each)
        excluded_ids — same IDs as a set, to pass into excluded_ids so
                       manage_inbox consumes them via the normal path
    Security: only self-sent emails accepted.
    """
    msg_ids: list[str] = []
    excluded: set[str] = set()

    try:
        result = gmail_execute(service.users().messages().list(
            userId="me",
            q=f'subject:"{AGENT_STATUS_SUBJECT}" -label:"AI Processed"',
        ))
        messages = result.get("messages", [])
    except Exception as e:
        print(f"  [warning] Could not check for STATUS command: {e}")
        return msg_ids, excluded

    for msg_ref in messages:
        try:
            msg = gmail_execute(service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ))
            sender  = _get_header(msg["payload"]["headers"], "From")
            addr_m  = re.search(r"<(.+?)>", sender)
            sender_addr = addr_m.group(1) if addr_m else sender
            if sender_addr.strip().lower() != own_address.strip().lower():
                print(f"  [agent-status] ignored: sender not self ({sender_addr})")
                continue
            msg_ids.append(msg_ref["id"])
            excluded.add(msg_ref["id"])
        except Exception as e:
            print(f"  [warning] Could not read STATUS command {msg_ref['id']}: {e}")

    return msg_ids, excluded


# sprint 22 — [AGENT IGNORE] / [AGENT UNIGNORE] fetch
def fetch_ignore_commands(
    service: Any, own_address: str
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], set[str]]:
    """Scan inbox for self-sent [AGENT IGNORE] and [AGENT UNIGNORE] emails.

    Returns:
        ignore_cmds   — list of (msg_id, subject) for IGNORE commands
        unignore_cmds — list of (msg_id, subject) for UNIGNORE commands
        excluded_ids  — all cmd IDs as a set for excluded_ids pass-through
    Security: only self-sent emails accepted.
    """
    ignore_cmds: list[tuple[str, str]]   = []
    unignore_cmds: list[tuple[str, str]] = []
    excluded: set[str] = set()

    for subject_prefix in (AGENT_IGNORE_SUBJECT, AGENT_UNIGNORE_SUBJECT):
        try:
            result = gmail_execute(service.users().messages().list(
                userId="me",
                q=f'subject:"{subject_prefix}" -label:"AI Processed"',
            ))
            messages = result.get("messages", [])
        except Exception as e:
            print(f"  [warning] Could not check for {subject_prefix} command: {e}")
            continue

        for msg_ref in messages:
            try:
                msg = gmail_execute(service.users().messages().get(
                    userId="me", id=msg_ref["id"], format="full"
                ))
                sender = _get_header(msg["payload"]["headers"], "From")
                addr_m = re.search(r"<(.+?)>", sender)
                sender_addr = addr_m.group(1) if addr_m else sender
                if sender_addr.strip().lower() != own_address.strip().lower():
                    continue  # silently ignore non-self-sent
                subject = _get_header(msg["payload"]["headers"], "Subject")
                entry = (msg_ref["id"], subject)
                if subject_prefix == AGENT_IGNORE_SUBJECT:
                    ignore_cmds.append(entry)
                else:
                    unignore_cmds.append(entry)
                excluded.add(msg_ref["id"])
            except Exception as e:
                print(f"  [warning] Could not read {subject_prefix} command {msg_ref['id']}: {e}")

    return ignore_cmds, unignore_cmds, excluded


# sprint 22 — [AGENT DIGEST] on-demand report
def fetch_digest_commands(
    service: Any, own_address: str
) -> tuple[list[str], set[str]]:
    """Scan inbox for self-sent [AGENT DIGEST] emails.

    Returns:
        msg_ids      — list of message IDs to process
        excluded_ids — same IDs as a set for excluded_ids pass-through
    Security: only self-sent emails accepted.
    """
    msg_ids: list[str] = []
    excluded: set[str] = set()

    try:
        result = gmail_execute(service.users().messages().list(
            userId="me",
            q=f'subject:"{AGENT_DIGEST_SUBJECT}" -label:"AI Processed"',
        ))
        messages = result.get("messages", [])
    except Exception as e:
        print(f"  [warning] Could not check for DIGEST command: {e}")
        return msg_ids, excluded

    for msg_ref in messages:
        try:
            msg = gmail_execute(service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ))
            sender = _get_header(msg["payload"]["headers"], "From")
            addr_m = re.search(r"<(.+?)>", sender)
            sender_addr = addr_m.group(1) if addr_m else sender
            if sender_addr.strip().lower() != own_address.strip().lower():
                continue  # silently ignore non-self-sent
            msg_ids.append(msg_ref["id"])
            excluded.add(msg_ref["id"])
        except Exception as e:
            print(f"  [warning] Could not read DIGEST command {msg_ref['id']}: {e}")

    return msg_ids, excluded


# sprint 22 — [AGENT MODEL] per-account model switching
def fetch_model_commands(
    service: Any, own_address: str
) -> tuple[list[tuple[str, str]], set[str]]:
    """Scan inbox for self-sent [AGENT MODEL] emails.

    Returns:
        cmds         — list of (msg_id, model_name) for valid self-sent commands
        excluded_ids — all cmd IDs as a set for excluded_ids pass-through
    Security: only self-sent emails accepted.
    """
    cmds: list[tuple[str, str]] = []
    excluded: set[str] = set()

    try:
        result = gmail_execute(service.users().messages().list(
            userId="me",
            q=f'subject:"{AGENT_MODEL_SUBJECT}" -label:"AI Processed"',
        ))
        messages = result.get("messages", [])
    except Exception as e:
        print(f"  [warning] Could not check for MODEL command: {e}")
        return cmds, excluded

    for msg_ref in messages:
        try:
            msg = gmail_execute(service.users().messages().get(
                userId="me", id=msg_ref["id"], format="full"
            ))
            headers = msg["payload"]["headers"]
            sender  = _get_header(headers, "From")
            subject = _get_header(headers, "Subject")
            addr_m  = re.search(r"<(.+?)>", sender)
            sender_addr = addr_m.group(1) if addr_m else sender
            if sender_addr.strip().lower() != own_address.strip().lower():
                continue  # silently ignore non-self-sent
            model_name = subject[len(AGENT_MODEL_SUBJECT):].strip()
            cmds.append((msg_ref["id"], model_name))
            excluded.add(msg_ref["id"])
        except Exception as e:
            print(f"  [warning] Could not read MODEL command {msg_ref['id']}: {e}")

    return cmds, excluded


def get_authenticated_address(service: Any) -> str:
    """Return the email address of the authenticated Gmail account."""
    profile = gmail_execute(service.users().getProfile(userId="me"))
    return profile["emailAddress"]


def load_label_rules(service: Any, own_address: str) -> tuple[dict, set]:
    """Scan all Gmail labels for self-sent [AGENT RULE] emails.

    Returns:
        rules       — {label_name: instruction_text} for valid rules found
        excluded_ids — message IDs of instruction emails (exclude from processing)

    Security: only emails where sender address == own_address are accepted.
    Failures are logged and skipped; the agent always continues without rules.
    """
    rules: dict[str, str] = {}
    excluded_ids: set[str] = set()

    try:
        labels_result = gmail_execute(service.users().labels().list(userId="me"))
    except Exception as e:
        print(f"  [warning] Could not load label rules: {e}")
        return rules, excluded_ids

    for label in labels_result.get("labels", []):
        label_id   = label["id"]
        label_name = label["name"]
        _LABEL_ID_CACHE[label_id] = label_name
        try:
            result = gmail_execute(service.users().messages().list(
                userId="me",
                q=f'subject:"{AGENT_RULE_SUBJECT}"',
                labelIds=[label_id],
            ))
            messages = result.get("messages", [])
            if not messages:
                continue

            msg     = gmail_execute(service.users().messages().get(
                userId="me", id=messages[0]["id"], format="full"
            ))
            headers = msg["payload"]["headers"]
            sender  = _get_header(headers, "From")

            # Strip display name: "User <me@gmail.com>" → "me@gmail.com"
            addr_match = re.search(r"<(.+?)>", sender)
            sender_addr = addr_match.group(1) if addr_match else sender

            if sender_addr.strip().lower() != own_address.strip().lower():
                print(f'  [label-rule] ignored: "{AGENT_RULE_SUBJECT}" in "{label_name}" — sender not self')
                continue

            body = _decode_body(msg["payload"]).strip()
            if not body:
                print(f'  [label-rule] ignored: "{AGENT_RULE_SUBJECT}" in "{label_name}" — empty body')
                continue

            rules[label_name] = body
            excluded_ids.add(messages[0]["id"])
            has_routing = bool(parse_route_criteria(body))
            rule_type   = "route + behavior" if has_routing else "behavior only"
            print(f'  [label-rule] loaded: "{label_name}" ({rule_type})')

        except Exception as e:
            print(f"  [warning] Could not check label '{label_name}' for rules: {e}")
            continue

    return rules, excluded_ids


def fetch_unprocessed_emails(service: Any, max_results: int = MAX_EMAILS, exclude_ids: set | None = None) -> list[dict]:
    """Fetch up to max_results emails that don't have the 'AI Processed' label.

    Returns sample data if USE_SAMPLE_DATA=true. In normal operation fetches all
    mail (excluding Spam/Trash) lacking 'AI Processed' — covers both new unread
    mail and emails the user has reset for reprocessing by removing that label.
    """
    if os.environ.get("USE_SAMPLE_DATA", "").lower() == "true":
        print(f"(using sample data from {SAMPLE_FILE})")
        with open(SAMPLE_FILE) as f:
            return json.load(f)

    try:
        result = gmail_execute(service.users().messages().list(
            userId="me",
            q='in:all -label:"AI Processed"',
            maxResults=max_results,
        ))
    except (HttpError, TimeoutError, OSError) as e:
        print(f"[error] Failed to list emails: {e}")
        return []

    messages = result.get("messages", [])
    if not messages:
        print("No emails to process.")
        return []

    exclude_ids = exclude_ids or set()
    emails = []
    for msg_ref in messages:
        if msg_ref["id"] in exclude_ids:
            continue
        try:
            msg     = gmail_execute(service.users().messages().get(userId="me", id=msg_ref["id"], format="full"))
            headers = msg["payload"]["headers"]
            body    = _decode_body(msg["payload"]).strip()

            # Resolve label IDs to names for rule injection downstream.
            label_ids   = msg.get("labelIds", [])
            label_names = [_LABEL_ID_CACHE.get(lid, lid) for lid in label_ids]

            emails.append({
                "message_id":  msg_ref["id"],
                "sender":      _get_header(headers, "From"),
                "subject":     _get_header(headers, "Subject"),
                "date":        _get_header(headers, "Date"),
                "snippet":     body[:SNIPPET_LENGTH] if body else msg.get("snippet", "")[:SNIPPET_LENGTH],
                "body":        body[:BODY_LENGTH] if body else msg.get("snippet", ""),
                "label_names": label_names,
            })
        except (HttpError, KeyError, TimeoutError, OSError) as e:
            print(f"[error] Failed to fetch email {msg_ref['id']}: {e}")

    return emails


def print_emails(emails: list[dict]) -> None:
    print(f"\n{'='*60}")
    print(f"  UNREAD EMAILS ({len(emails)} found)")
    print(f"{'='*60}\n")
    for i, em in enumerate(emails, start=1):
        print(f"[{i}] From:    {em['sender']}")
        print(f"    Subject: {em['subject']}")
        print(f"    Date:    {em['date']}")
        print(f"    Snippet: {em['snippet']}")
        print()


# sprint 22 — accept token_file so setup.sh can OAuth each account into its own file
def main(token_file: str = TOKEN_FILE):
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"Error: {CREDENTIALS_FILE} not found.")
        return

    print("Authenticating with Gmail...")
    service = get_gmail_service(token_file)
    print("Authenticated.\n")

    emails = fetch_unprocessed_emails(service)
    if emails:
        print_emails(emails)


if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser(description="Email Agent — fetch emails")
    # sprint 22 — --token-file lets setup.sh trigger OAuth for any account
    _parser.add_argument("--token-file", default=TOKEN_FILE,
                         help="Path to the OAuth token file (default: token.json)")
    _args = _parser.parse_args()
    main(token_file=_args.token_file)
