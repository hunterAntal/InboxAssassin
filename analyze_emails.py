"""
Email analysis pipeline.

Fetches unread emails, analyzes each with an AI model, prints a ranked
digest, creates Calendar events / Tasks, manages the inbox, and logs
everything for training data.

Model backend (MODEL_BACKEND in .env):
  local  — Ollama (default: llama3.1:latest), no rate limits
  gemini — Google Gemini API (gemini-2.0-flash), requires GEMINI_API_KEY
"""

import os
import re
import json
import time
from typing import Any, Optional
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from fetch_emails import get_gmail_service, fetch_unread_emails, TOKEN_FILE, gmail_execute
import pre_filter

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GEMINI_MODEL     = "gemma-3-27b-it"
ARCHIVE_PRIORITY = 2         # emails at or below this priority are archived

# Free-tier limits per model (RPM, TPM, RPD). Used by RateLimiter.
_MODEL_LIMITS: dict[str, dict] = {
    "gemma-3-1b-it":         {"rpm": 30, "tpm": 15_000,    "rpd": 14_400},
    "gemma-3-4b-it":         {"rpm": 30, "tpm": 15_000,    "rpd": 14_400},
    "gemma-3-27b-it":        {"rpm": 30, "tpm": 15_000,    "rpd": 14_400},
    "gemini-2.0-flash":      {"rpm": 15, "tpm": 1_000_000, "rpd":  1_500},
    "gemini-2.0-flash-lite": {"rpm": 30, "tpm": 1_000_000, "rpd":  1_500},
    "gemini-2.5-flash":      {"rpm": 10, "tpm":   250_000, "rpd":    500},
}
_RATE_LIMIT_STATE_FILE = "rate_limit_state.json"
ALL_DAY_THRESHOLD    = "22:00"   # event times at or after this are treated as all-day
AI_LABEL_NAME        = "AI Processed"
ACTION_LABEL_NAME    = "Action Required"
CLOUD_LABEL_NAME     = "Google Cloud"
SPAM_LABEL_NAME      = "Spam Bucket"
EVENT_LABEL_NAME     = "Event"
TASK_LABEL_NAME      = "Task"
RECEIPT_LABEL_NAME      = "Receipt"
INVOICE_LABEL_NAME      = "Invoice"
SHIPPING_LABEL_NAME     = "Shipping"
SUBSCRIPTION_LABEL_NAME = "Subscription"
TRAVEL_LABEL_NAME       = "Travel"
PROCESSED_FILE       = "processed.json"
ACTIVITY_LOG_FILE    = "activity_log.json"
EMAIL_LOG_FILE       = "email_log.json"

PROMPT_TEMPLATE = """Today's date is {today}. Analyze this email and respond ONLY with a valid JSON object — no markdown, no code fences, just raw JSON.

Required fields:
- "priority": integer 1 (low) to 5 (urgent). Use this rubric strictly:
    5 = urgent, time-sensitive, requires immediate action (e.g. exam tomorrow, payment overdue)
    4 = important with a near deadline (e.g. assignment due this week, appointment confirmed)
    3 = relevant and worth reading (e.g. registration reminder, advisor email)
    2 = low-value or informational, no real action needed
    1 = spam, promotions, marketing, newsletters, sales pitches, betting tips, discount codes, or any unsolicited commercial email — always 1, no exceptions
- "tldr": 1-2 sentence summary of the email
- "action_required": true only if the recipient must personally do something meaningful (reply, submit, attend, pay). Newsletters, promotions, and sales emails are always false.
- "event_date": ISO date string (YYYY-MM-DD) if the email mentions a real meeting, appointment, or personal deadline. Ignore artificial urgency in marketing emails (e.g. "sale ends tonight"). Use today's year unless the email specifies otherwise. Null if no genuine event.
- "event_time": 24-hour time string (HH:MM) if a specific time is mentioned in a real event, otherwise null.
- "event_title": a short descriptive title for the calendar entry, or null if no event_date.
- "action_type": "event" if this is a meeting or appointment; "task" if this is a personal to-do or deadline; null if no event_date or if this is spam/promotional.
- "is_receipt": true if this email is a purchase confirmation or order receipt (money already spent). false otherwise.
- "is_invoice": true if this email is a bill or invoice sent to you that requires payment. false otherwise.
- "is_shipping": true if this email contains a shipping notification, tracking number, dispatch notice, or delivery update. false otherwise.
- "is_subscription": true if this email is about a subscription renewal, billing cycle, or recurring charge. false otherwise.
- "is_travel": true if this email is a flight, hotel, train, or car rental booking confirmation or itinerary. false otherwise.

Email:
From: {sender}
Subject: {subject}
Body: {body}
"""


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window rate limiter that tracks RPM, TPM, and RPD."""

    _SAFETY = 0.90  # stay at 90% of each limit

    def __init__(self, model: str):
        limits        = _MODEL_LIMITS.get(model, _MODEL_LIMITS["gemini-2.0-flash"])
        self.rpm_cap  = int(limits["rpm"] * self._SAFETY)
        self.tpm_cap  = int(limits["tpm"] * self._SAFETY)
        self.rpd_cap  = int(limits["rpd"] * self._SAFETY)
        self.model    = model
        self._req_ts: list[float] = []          # request timestamps (last 60s)
        self._tok_ts: list[tuple[float, int]] = []  # (timestamp, tokens) (last 60s)
        self._daily   = self._load_daily()

    # -- persistence ----------------------------------------------------------

    def _load_daily(self) -> int:
        try:
            with open(_RATE_LIMIT_STATE_FILE) as f:
                s = json.load(f)
            if s.get("date") == date.today().isoformat() and s.get("model") == self.model:
                return int(s.get("requests", 0))
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass
        return 0

    def _save_daily(self) -> None:
        with open(_RATE_LIMIT_STATE_FILE, "w") as f:
            json.dump({"date": date.today().isoformat(), "model": self.model, "requests": self._daily}, f)

    # -- public interface -----------------------------------------------------

    def wait(self) -> None:
        """Block until sending the next request is within all three limits."""
        now = time.time()
        self._req_ts  = [t      for t      in self._req_ts  if now - t      < 60]
        self._tok_ts  = [(t, k) for t, k   in self._tok_ts  if now - t      < 60]

        if self._daily >= self.rpd_cap:
            raise RuntimeError(
                f"Daily limit reached ({self._daily}/{self.rpd_cap} requests). "
                "Reset at midnight or switch model."
            )

        # RPM wait
        if len(self._req_ts) >= self.rpm_cap:
            wait = 60 - (now - self._req_ts[0]) + 0.5
            if wait > 0:
                print(f"  [rate limit] RPM {len(self._req_ts)}/{self.rpm_cap} — waiting {wait:.0f}s")
                time.sleep(wait)

        # TPM wait
        recent_tokens = sum(k for _, k in self._tok_ts)
        if recent_tokens >= self.tpm_cap:
            wait = 60 - (now - self._tok_ts[0][0]) + 0.5
            if wait > 0:
                print(f"  [rate limit] TPM {recent_tokens}/{self.tpm_cap} — waiting {wait:.0f}s")
                time.sleep(wait)

        if self._daily >= self.rpd_cap * 0.9:
            print(f"  [rate limit] RPD warning: {self._daily}/{self.rpd_cap} daily requests used")

    def record(self, tokens: int = 0) -> None:
        """Call after a successful API request."""
        now = time.time()
        self._req_ts.append(now)
        if tokens:
            self._tok_ts.append((now, tokens))
        self._daily += 1
        self._save_daily()

    def status(self) -> str:
        now = time.time()
        rpm = len([t for t in self._req_ts if now - t < 60])
        tpm = sum(k for t, k in self._tok_ts if now - t < 60)
        return f"RPM {rpm}/{self.rpm_cap} | TPM {tpm:,}/{self.tpm_cap:,} | RPD {self._daily}/{self.rpd_cap}"


# ---------------------------------------------------------------------------
# AI backends
# ---------------------------------------------------------------------------

def _analyze_with_gemini(email: dict, limiter: "RateLimiter") -> dict:
    from google import genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY not set in .env")
    client = genai.Client(api_key=api_key)
    prompt = PROMPT_TEMPLATE.format(**email, today=date.today().isoformat())

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            limiter.wait()
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            tokens = getattr(getattr(response, "usage_metadata", None), "total_token_count", 0) or 0
            limiter.record(tokens)
            return _parse_response(response.text)
        except RuntimeError:
            raise  # daily limit — propagate immediately
        except Exception as e:
            if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e):
                raise
            match = re.search(r"retryDelay.*?(\d+)s", str(e))
            wait = int(match.group(1)) + 5 if match else 60
            if attempt < max_retries:
                print(f"  [rate limit] waiting {wait}s before retry {attempt}/{max_retries - 1}...")
                time.sleep(wait)
            else:
                raise


def _analyze_with_ollama(email: dict) -> dict:
    import ollama
    client = ollama.Client(host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    prompt = PROMPT_TEMPLATE.format(**email, today=date.today().isoformat())
    response = client.chat(
        model=os.environ.get("LOCAL_MODEL", "llama3.1:latest"),
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_response(response.message.content)


def _normalize_time(t: Optional[str]) -> Optional[str]:
    """Ensure time is zero-padded HH:MM (e.g. '9:00' → '09:00'). Returns None if invalid."""
    if not t:
        return None
    try:
        h, m = t.strip().split(":")
        return f"{int(h):02d}:{int(m):02d}"
    except (ValueError, AttributeError):
        return None


def _parse_response(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    result = json.loads(text)
    return {
        "priority":        int(result.get("priority", 1)),
        "tldr":            result.get("tldr", ""),
        "action_required": bool(result.get("action_required", False)),
        "event_date":      result.get("event_date") or result.get("deadline"),
        "event_time":      _normalize_time(result.get("event_time")),
        "event_title":     result.get("event_title"),
        "action_type":     result.get("action_type"),
        "is_receipt":      bool(result.get("is_receipt", False)),
        "is_invoice":      bool(result.get("is_invoice", False)),
        "is_shipping":     bool(result.get("is_shipping", False)),
        "is_subscription": bool(result.get("is_subscription", False)),
        "is_travel":       bool(result.get("is_travel", False)),
    }


def analyze_emails(emails: list[dict]) -> list[tuple[dict, Optional[dict]]]:
    """Run every email through the configured AI backend. Returns (email, analysis) pairs."""
    backend       = os.environ.get("MODEL_BACKEND", "gemini").lower()
    use_gemini    = backend == "gemini"
    use_filter    = os.environ.get("PRE_FILTER", "true").lower() != "false"
    model_name    = GEMINI_MODEL if use_gemini else os.environ.get("LOCAL_MODEL", "llama3.1:latest")
    filter_config = pre_filter.load_filter_config() if use_filter else {}
    limiter       = RateLimiter(GEMINI_MODEL) if use_gemini else None

    print(f"Analyzing {len(emails)} emails with {model_name}...\n")
    results = []

    for i, em in enumerate(emails, start=1):
        if use_filter and pre_filter.matches_filter(em, filter_config):
            print(f"  [{i}/{len(emails)}] [pre-filtered] {em['subject'][:60]}")
            results.append((em, pre_filter.make_filtered_result(em)))
            continue

        print(f"  [{i}/{len(emails)}] {em['subject'][:60]}")
        try:
            if use_gemini:
                info = _analyze_with_gemini(em, limiter)
                print(f"         [{limiter.status()}]")
            else:
                info = _analyze_with_ollama(em)
        except RuntimeError as e:
            print(f"  [stopped] {e}")
            break
        except Exception as e:
            print(f"  [warning] Could not analyze '{em['subject']}': {e}")
            info = None

        results.append((em, info))

    return results


# ---------------------------------------------------------------------------
# Storage helpers  (local files or GCS when GCS_BUCKET env var is set)
# ---------------------------------------------------------------------------

def _gcs_client():
    from google.cloud import storage
    return storage.Client()


def _read_json(path: str) -> list:
    """Read a JSON file from local disk or GCS."""
    try:
        bucket = os.environ.get("GCS_BUCKET")
        if bucket:
            blob = _gcs_client().bucket(bucket).blob(path)
            return json.loads(blob.download_as_text()) if blob.exists() else []
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        print(f"[warning] Could not read {path}: {e}. Starting fresh.")
    return []


def _write_json(path: str, data: Any) -> None:
    """Write a JSON file to local disk or GCS."""
    bucket = os.environ.get("GCS_BUCKET")
    if bucket:
        _gcs_client().bucket(bucket).blob(path).upload_from_string(
            json.dumps(data, indent=2), content_type="application/json"
        )
    else:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


def _append_to_json_file(path: str, entries: Any) -> None:
    """Append one or more entries to a JSON array (local or GCS)."""
    log = _read_json(path)
    log.extend(entries if isinstance(entries, list) else [entries])
    _write_json(path, log)


def load_processed() -> set:
    return set(_read_json(PROCESSED_FILE))


def save_processed(ids: set) -> None:
    _write_json(PROCESSED_FILE, list(ids))


def log_emails(results: list[tuple[dict, Optional[dict]]]) -> None:
    """Append every fetched email + analysis to email_log.json for training data."""
    now = datetime.now().isoformat(timespec="seconds")
    records = [
        {
            "logged_at":    now,
            "message_id":   em["message_id"],
            "sender":       em["sender"],
            "subject":      em["subject"],
            "date":         em["date"],
            "body":         em["body"],
            "pre_filtered": bool(info and info.get("pre_filtered", False)),
            "analysis":     {k: v for k, v in info.items() if k != "pre_filtered"} if info else None,
        }
        for em, info in results
    ]
    _append_to_json_file(EMAIL_LOG_FILE, records)
    print(f"  {len(records)} email(s) logged to {EMAIL_LOG_FILE}")


def log_activity(entry_type: str, title: str, event_date: str, event_time: Optional[str], sender: str, subject: str, tldr: str) -> None:
    """Append one created event/task to activity_log.json."""
    _append_to_json_file(ACTIVITY_LOG_FILE, {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "type":      entry_type,
        "title":     title,
        "date":      event_date,
        "time":      event_time,
        "sender":    sender,
        "subject":   subject,
        "tldr":      tldr,
    })


# ---------------------------------------------------------------------------
# Google service builders
# ---------------------------------------------------------------------------

def _build_google_service(api: str, version: str) -> Any:
    creds = Credentials.from_authorized_user_file(TOKEN_FILE)
    return build(api, version, credentials=creds)


def get_calendar_service() -> Any:
    return _build_google_service("calendar", "v3")


def get_tasks_service() -> Any:
    return _build_google_service("tasks", "v1")


# ---------------------------------------------------------------------------
# Calendar events & Tasks
# ---------------------------------------------------------------------------

def _local_tz() -> str:
    """Return the local IANA timezone name (e.g. 'America/Toronto'), falling back to UTC."""
    # Prefer explicit TZ env var (set to e.g. America/Toronto in Cloud Run)
    tz_env = os.environ.get("TZ")
    if tz_env:
        return tz_env
    try:
        import zoneinfo
        tz = datetime.now().astimezone().tzinfo
        # tzinfo.key is set on ZoneInfo objects; str() gives abbreviations like "EDT"
        key = getattr(tz, "key", None)
        if key:
            return key
    except Exception:
        pass
    return "UTC"


def _calendar_event_fields(info: dict) -> tuple[dict, dict, str]:
    """Return (start, end, time_label) for a Calendar event."""
    event_date = info["event_date"]
    event_time = info["event_time"]

    try:
        datetime.strptime(event_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return {"date": str(date.today())}, {"date": str(date.today())}, "all day"

    if event_time and event_time < ALL_DAY_THRESHOLD:
        try:
            tz_name   = _local_tz()
            tz_offset = datetime.now().astimezone().strftime("%z")
            tz_str    = f"{tz_offset[:3]}:{tz_offset[3:]}"
            start_dt  = f"{event_date}T{event_time}:00{tz_str}"
            end_dt    = (datetime.fromisoformat(start_dt) + timedelta(hours=1)).isoformat()
            return (
                {"dateTime": start_dt, "timeZone": tz_name},
                {"dateTime": end_dt,   "timeZone": tz_name},
                event_time,
            )
        except (ValueError, TypeError):
            pass

    return {"date": event_date}, {"date": event_date}, "all day"


def _analyzed(results: list[tuple[dict, Optional[dict]]]) -> list[tuple[dict, dict]]:
    """Return only results where AI analysis succeeded."""
    return [(em, info) for em, info in results if info is not None]


def create_entries(results: list[tuple[dict, Optional[dict]]]) -> None:
    """Create a Calendar event or Google Task for each actionable email."""
    actionable = [
        (em, info) for em, info in results
        if info and info.get("event_date") and info.get("action_type")
    ]

    if not actionable:
        print("No events or tasks to create.")
        return

    processed        = load_processed()
    calendar_service = get_calendar_service()
    tasks_service    = get_tasks_service()
    new_count        = 0

    print(f"\n{'='*60}")
    print(f"  CALENDAR EVENTS & TASKS")
    print(f"{'='*60}\n")

    for em, info in actionable:
        if em["message_id"] in processed:
            print(f"  [skip]  {em['subject'][:50]}")
            continue

        title = info["event_title"] or em["subject"]

        try:
            if info["action_type"] == "task":
                gmail_execute(tasks_service.tasks().insert(
                    tasklist="@default",
                    body={
                        "title": title,
                        "notes": f"From: {em['sender']}\n\n{info['tldr']}",
                        "due":   f"{info['event_date']}T00:00:00.000Z",
                    },
                ))
                log_activity("task", title, info["event_date"], None,
                             em["sender"], em["subject"], info["tldr"])
                print(f"  [task]  {info['event_date']} — {title[:50]}")

            else:
                start, end, time_label = _calendar_event_fields(info)
                gmail_execute(calendar_service.events().insert(
                    calendarId="primary",
                    body={
                        "summary":     title,
                        "description": f"From: {em['sender']}\n\n{info['tldr']}",
                        "start":       start,
                        "end":         end,
                    },
                ))
                log_activity("event", title, info["event_date"], info["event_time"],
                             em["sender"], em["subject"], info["tldr"])
                print(f"  [event] {info['event_date']} {time_label} — {title[:50]}")

            processed.add(em["message_id"])
            new_count += 1

        except HttpError as e:
            print(f"  [error] {em['subject'][:50]}: {e}")

    save_processed(processed)
    print(f"\n{new_count} item(s) created.")


# ---------------------------------------------------------------------------
# Inbox management
# ---------------------------------------------------------------------------


def _get_or_create_label(gmail_service: Any, name: str) -> str:
    existing = gmail_execute(gmail_service.users().labels().list(userId="me"))
    for label in existing.get("labels", []):
        if label["name"] == name:
            return label["id"]
    return gmail_execute(gmail_service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ))["id"]


def manage_inbox(gmail_service: Any, results: list[tuple[dict, Optional[dict]]]) -> None:
    """Mark as read, apply labels, and archive low-priority emails."""
    valid = _analyzed(results)
    if not valid:
        return

    ai_label_id      = _get_or_create_label(gmail_service, AI_LABEL_NAME)
    action_label_id  = _get_or_create_label(gmail_service, ACTION_LABEL_NAME)
    spam_label_id    = _get_or_create_label(gmail_service, SPAM_LABEL_NAME)
    event_label_id   = _get_or_create_label(gmail_service, EVENT_LABEL_NAME)
    task_label_id    = _get_or_create_label(gmail_service, TASK_LABEL_NAME)
    receipt_label_id      = _get_or_create_label(gmail_service, RECEIPT_LABEL_NAME)
    invoice_label_id      = _get_or_create_label(gmail_service, INVOICE_LABEL_NAME)
    shipping_label_id     = _get_or_create_label(gmail_service, SHIPPING_LABEL_NAME)
    subscription_label_id = _get_or_create_label(gmail_service, SUBSCRIPTION_LABEL_NAME)
    travel_label_id       = _get_or_create_label(gmail_service, TRAVEL_LABEL_NAME)
    cloud_label_id        = _get_or_create_label(gmail_service, CLOUD_LABEL_NAME) if os.environ.get("GCP_PROJECT") else None

    print(f"\n{'='*60}")
    print(f"  INBOX MANAGEMENT")
    print(f"{'='*60}\n")

    for em, info in valid:
        is_spam       = info.get("pre_filtered", False) or info["priority"] == 1
        action_type   = info.get("action_type")
        add_labels    = (
            [ai_label_id]
            + ([action_label_id]  if info["action_required"] else [])
            + ([spam_label_id]    if is_spam else [])
            + ([event_label_id]   if action_type == "event" else [])
            + ([task_label_id]    if action_type == "task"  else [])
            + ([receipt_label_id]      if info.get("is_receipt") else [])
            + ([invoice_label_id]      if info.get("is_invoice") else [])
            + ([shipping_label_id]     if info.get("is_shipping") else [])
            + ([subscription_label_id] if info.get("is_subscription") else [])
            + ([travel_label_id]       if info.get("is_travel") else [])
            + ([cloud_label_id]        if cloud_label_id else [])
        )
        remove_labels = ["UNREAD"] + (["INBOX"] if info["priority"] <= ARCHIVE_PRIORITY else [])
        action        = "archived" if info["priority"] <= ARCHIVE_PRIORITY else "marked read"

        try:
            gmail_execute(gmail_service.users().messages().modify(
                userId="me",
                id=em["message_id"],
                body={"addLabelIds": add_labels, "removeLabelIds": remove_labels},
            ))
            print(f"  [p{info['priority']}] {action}: {em['subject'][:50]}")
        except (HttpError, TimeoutError, OSError) as e:
            print(f"  [error] {em['subject'][:50]}: {e}")


# ---------------------------------------------------------------------------
# Digest output
# ---------------------------------------------------------------------------

def print_digest(results: list[tuple[dict, Optional[dict]]]) -> None:
    pre_filtered_count = sum(1 for _, info in results if info and info.get("pre_filtered"))
    valid = sorted(
        [(em, info) for em, info in _analyzed(results) if not info.get("pre_filtered")],
        key=lambda x: x[1]["priority"],
        reverse=True,
    )

    print(f"\n{'='*60}")
    print(f"  EMAIL DIGEST — ranked by priority ({len(valid)} analyzed)")
    if pre_filtered_count:
        print(f"  {pre_filtered_count} email(s) skipped by pre-filter (archived)")
    print(f"{'='*60}\n")

    for rank, (em, info) in enumerate(valid, start=1):
        action_label = "ACTION REQUIRED" if info["action_required"] else "no action"
        date_str     = f"    Event:    {info['event_date']}\n" if info["event_date"] else ""

        print(f"[{rank}] PRIORITY {info['priority']} | {action_label}")
        print(f"    From:     {em['sender']}")
        print(f"    Subject:  {em['subject']}")
        print(f"    Date:     {em['date']}")
        print(date_str, end="")
        print(f"    TL;DR:    {info['tldr']}")
        print()


# ---------------------------------------------------------------------------
# Epic 4 — Inbox digest email
# ---------------------------------------------------------------------------

def send_digest_email(service: Any, results: list, batch_num: int) -> None:
    """Send a plain-text batch summary to the authenticated Gmail account."""
    import base64
    from email.mime.text import MIMEText

    analyzed   = [(em, info) for em, info in results if info and not info.get("pre_filtered")]
    pre_filtered = sum(1 for _, info in results if info and info.get("pre_filtered"))
    archived   = sum(1 for _, info in analyzed if info["priority"] <= ARCHIVE_PRIORITY)
    read_only  = sum(1 for _, info in analyzed if not info["action_required"] and info["priority"] > ARCHIVE_PRIORITY)
    action     = [(em, info) for em, info in analyzed if info["action_required"]]
    total      = len(results)

    timestamp = datetime.now().strftime("%b %d, %Y at %I:%M %p")
    subject = (
        f"InboxAssassin — {timestamp} — Batch {batch_num}: {total} processed"
        + (f" ({len(action)} action required)" if action else "")
    )

    lines = [
        f"Batch {batch_num} Summary",
        "=" * 40,
        f"  Total processed   : {total}",
        f"  Pre-filtered spam : {pre_filtered}",
        f"  Archived (p1-2)   : {archived}",
        f"  Read only  (p3)   : {read_only}",
        f"  Action required   : {len(action)}",
        "",
    ]

    ranked = sorted(analyzed, key=lambda x: x[1]["priority"], reverse=True)

    if action:
        lines += ["ACTION REQUIRED", "-" * 40]
        for em, info in sorted(action, key=lambda x: x[1]["priority"], reverse=True):
            lines += [
                f"  [P{info['priority']}] {em['subject']}",
                f"      From:  {em['sender']}",
                f"      TL;DR: {info['tldr']}",
                "",
            ]

    if ranked:
        lines += ["ALL EMAILS — ranked by priority", "-" * 40]
        for i, (em, info) in enumerate(ranked, start=1):
            action_label = "ACTION REQUIRED" if info["action_required"] else "no action"
            lines += [
                f"[{i}] PRIORITY {info['priority']} | {action_label}",
                f"    From:    {em['sender']}",
                f"    Subject: {em['subject']}",
                f"    TL;DR:   {info['tldr']}",
                "",
            ]

    profile = gmail_execute(service.users().getProfile(userId="me"))
    email_address = profile["emailAddress"]

    body = "\n".join(lines)
    msg  = MIMEText(body)
    msg["to"]      = email_address
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    try:
        sent = gmail_execute(service.users().messages().send(userId="me", body={"raw": raw}))
        gmail_execute(service.users().messages().modify(
            userId="me", id=sent["id"],
            body={"removeLabelIds": ["UNREAD"], "addLabelIds": ["STARRED"]}
        ))
        print(f"  [digest] Summary email sent — {subject}")
    except Exception as e:
        print(f"  [digest] Could not send summary email: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Authenticating with Gmail...")
    service = get_gmail_service()
    print("Authenticated.\n")

    print("Fetching unread emails...")
    emails = fetch_unread_emails(service)
    if not emails:
        return

    results = analyze_emails(emails)

    print_digest(results)
    log_emails(results)
    create_entries(results)

    if os.environ.get("USE_SAMPLE_DATA", "").lower() != "true":
        manage_inbox(service, results)


if __name__ == "__main__":
    main()
