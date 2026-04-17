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

from fetch_emails import get_gmail_service, fetch_unprocessed_emails, TOKEN_FILE, gmail_execute
import pre_filter
from pre_filter import account_file_path  # sprint 22 — per-account data file isolation

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
TMMC_LABEL_NAME         = "TMMC"
AGENT_REPORT_LABEL_NAME = "Agent Report"
ACTIVITY_LOG_FILE    = "activity_log.json"
EMAIL_LOG_FILE       = "email_log.json"

DIGEST_MORNING_HOUR = int(os.environ.get("DIGEST_MORNING_HOUR", 9))
DIGEST_EVENING_HOUR = int(os.environ.get("DIGEST_EVENING_HOUR", 18))
DIGEST_STATE_FILE   = "agent_digest_state.json"
AGENT_LOG_BUFFER    = "agent_log_buffer.txt"
# sprint 22 — [AGENT MODEL] per-account model config file
MODEL_CONFIG_FILE   = "model_config.json"

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

def _build_prompt(
    email: dict,
    label_rules: dict | None = None,
    priority_overrides: list | None = None,
) -> str:
    """Build the AI prompt for an email, injecting label rules and priority overrides."""
    prompt = PROMPT_TEMPLATE.format(
        sender=email.get("sender", ""),
        subject=email.get("subject", ""),
        body=email.get("body", ""),
        today=date.today().isoformat(),
    )

    if label_rules:
        matching_rules = [
            rule for name, rule in label_rules.items()
            if name in email.get("label_names", [])
        ]
        if matching_rules:
            injected = "\n".join(f"- {r}" for r in matching_rules)
            prompt += f"\n\nAdditional instructions for this email's label:\n{injected}"

    if priority_overrides:
        searchable = (
            (email.get("subject", "") + " " + email.get("body", "")).lower()
        )
        for override in priority_overrides:
            if override["keyword"] in searchable:
                prompt += (
                    f'\n\nPriority override: this email contains the keyword '
                    f'"{override["keyword"]}" — score it at priority {override["priority"]} '
                    f'regardless of your normal assessment.'
                )

    return prompt


def _analyze_with_gemini(email: dict, limiter: "RateLimiter", label_rules: dict | None = None, priority_overrides: list | None = None) -> dict:
    from google import genai
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY not set in .env")
    client = genai.Client(api_key=api_key)
    prompt = _build_prompt(email, label_rules, priority_overrides)

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


def _analyze_with_ollama(email: dict, label_rules: dict | None = None, priority_overrides: list | None = None, account_id: str | None = None) -> dict:
    # sprint 22 — use per-account model if set, else env var / default
    import ollama
    client = ollama.Client(host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    prompt = _build_prompt(email, label_rules, priority_overrides)
    response = client.chat(
        model=_get_account_model(account_id),
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


def analyze_emails(emails: list[dict], label_rules: dict | None = None, priority_overrides: list | None = None, account_id: str | None = None) -> list[tuple[dict, Optional[dict]]]:
    """Run every email through the configured AI backend. Returns (email, analysis) pairs."""
    backend       = os.environ.get("MODEL_BACKEND", "gemini").lower()
    use_gemini    = backend == "gemini"
    use_filter    = os.environ.get("PRE_FILTER", "true").lower() != "false"
    # sprint 22 — use per-account model for display and analysis
    model_name    = GEMINI_MODEL if use_gemini else _get_account_model(account_id)
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
                info = _analyze_with_gemini(em, limiter, label_rules, priority_overrides)
                print(f"         [{limiter.status()}]")
            else:
                info = _analyze_with_ollama(em, label_rules, priority_overrides, account_id=account_id)
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


def log_emails(results: list[tuple[dict, Optional[dict]]], account_id: str | None = None) -> None:
    """Append every fetched email + analysis to the email log for training data.

    When account_id is given, writes to email_log_<account_id>.json.
    """
    # sprint 22 — per-account email log path
    path = account_file_path(EMAIL_LOG_FILE, account_id)
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
    _append_to_json_file(path, records)
    print(f"  {len(records)} email(s) logged to {path}")


def log_activity(entry_type: str, title: str, event_date: str, event_time: Optional[str], sender: str, subject: str, tldr: str, account_id: str | None = None) -> None:
    """Append one created event/task to the activity log.

    When account_id is given, writes to activity_log_<account_id>.json.
    """
    # sprint 22 — per-account activity log path
    path = account_file_path(ACTIVITY_LOG_FILE, account_id)
    _append_to_json_file(path, {
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
    """Return the local IANA timezone name (e.g. 'America/Toronto'), falling back to UTC offset."""
    try:
        import zoneinfo
        return str(datetime.now().astimezone().tzinfo)
    except Exception:
        tz_offset = datetime.now().astimezone().strftime("%z")
        return f"UTC{tz_offset[:3]}:{tz_offset[3:]}"


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


def _event_exists(calendar_service: Any, message_id: str, event_date: str) -> bool:
    """Return True if a calendar event tagged [source:<message_id>] already exists."""
    try:
        ref = datetime.strptime(event_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        ref = datetime.today()
    time_min = (ref - timedelta(days=365)).isoformat() + "Z"
    time_max = (ref + timedelta(days=365)).isoformat() + "Z"
    result = gmail_execute(calendar_service.events().list(
        calendarId="primary",
        q=message_id,
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
    ))
    tag = f"[source:{message_id}]"
    return any(tag in (ev.get("description") or "") for ev in result.get("items", []))


def _task_exists(tasks_service: Any, message_id: str) -> bool:
    """Return True if a Google Task tagged [source:<message_id>] already exists."""
    tag = f"[source:{message_id}]"
    page_token = None
    while True:
        kwargs: dict = {"tasklist": "@default", "showCompleted": True, "showHidden": True}
        if page_token:
            kwargs["pageToken"] = page_token
        result = gmail_execute(tasks_service.tasks().list(**kwargs))
        for task in result.get("items", []):
            if tag in (task.get("notes") or ""):
                return True
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return False


def create_entries(results: list[tuple[dict, Optional[dict]]], account_id: str | None = None) -> None:
    """Create a Calendar event or Google Task for each actionable email.

    When account_id is given, activity is logged to activity_log_<account_id>.json.
    """
    actionable = [
        (em, info) for em, info in results
        if info and info.get("event_date") and info.get("action_type")
    ]

    if not actionable:
        print("No events or tasks to create.")
        return

    calendar_service = get_calendar_service()
    tasks_service    = get_tasks_service()
    new_count        = 0

    print(f"\n{'='*60}")
    print(f"  CALENDAR EVENTS & TASKS")
    print(f"{'='*60}\n")

    for em, info in actionable:
        title      = info["event_title"] or em["subject"]
        message_id = em["message_id"]
        source_tag = f"\n\n[source:{message_id}]"

        try:
            if info["action_type"] == "task":
                if _task_exists(tasks_service, message_id):
                    print(f"  [skip]  already in tasks: {em['subject'][:50]}")
                    continue
                gmail_execute(tasks_service.tasks().insert(
                    tasklist="@default",
                    body={
                        "title": title,
                        "notes": f"From: {em['sender']}\n\n{info['tldr']}{source_tag}",
                        "due":   f"{info['event_date']}T00:00:00.000Z",
                    },
                ))
                # sprint 22 — pass account_id for per-account activity log
                log_activity("task", title, info["event_date"], None,
                             em["sender"], em["subject"], info["tldr"], account_id=account_id)
                print(f"  [task]  {info['event_date']} — {title[:50]}")

            else:
                if _event_exists(calendar_service, message_id, info["event_date"]):
                    print(f"  [skip]  already in calendar: {em['subject'][:50]}")
                    continue
                start, end, time_label = _calendar_event_fields(info)
                gmail_execute(calendar_service.events().insert(
                    calendarId="primary",
                    body={
                        "summary":     title,
                        "description": f"From: {em['sender']}\n\n{info['tldr']}{source_tag}",
                        "start":       start,
                        "end":         end,
                    },
                ))
                # sprint 22 — pass account_id for per-account activity log
                log_activity("event", title, info["event_date"], info["event_time"],
                             em["sender"], em["subject"], info["tldr"], account_id=account_id)
                print(f"  [event] {info['event_date']} {time_label} — {title[:50]}")

            new_count += 1

        except HttpError as e:
            print(f"  [error] {em['subject'][:50]}: {e}")

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
    tmmc_label_id         = _get_or_create_label(gmail_service, TMMC_LABEL_NAME)

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
            + ([tmmc_label_id]         if "tmmc-email.toyota.com" in em["sender"].lower() else [])
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
        f"Email Agent — {timestamp} — Batch {batch_num}: {total} processed"
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
# Sprint 16 — Twice-daily Agent Report digest
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    """Read a text file from local disk or GCS. Returns empty string if missing."""
    try:
        bucket = os.environ.get("GCS_BUCKET")
        if bucket:
            blob = _gcs_client().bucket(bucket).blob(path)
            return blob.download_as_text() if blob.exists() else ""
        if os.path.exists(path):
            with open(path) as f:
                return f.read()
    except Exception as e:
        print(f"[warning] Could not read {path}: {e}")
    return ""


def _write_text(path: str, content: str) -> None:
    """Write a text file to local disk or GCS."""
    bucket = os.environ.get("GCS_BUCKET")
    if bucket:
        _gcs_client().bucket(bucket).blob(path).upload_from_string(
            content, content_type="text/plain"
        )
    else:
        with open(path, "w") as f:
            f.write(content)


def load_digest_state(state_file: str = DIGEST_STATE_FILE) -> dict:
    """Load last digest send dates from disk/GCS. Missing or corrupt → safe default."""
    _default = {"last_morning": None, "last_evening": None}
    try:
        bucket = os.environ.get("GCS_BUCKET")
        if bucket:
            blob = _gcs_client().bucket(bucket).blob(state_file)
            if blob.exists():
                return json.loads(blob.download_as_text())
            return _default
        if os.path.exists(state_file):
            with open(state_file) as f:
                return json.load(f)
    except Exception:
        pass
    return _default


def save_digest_state(state: dict, state_file: str = DIGEST_STATE_FILE) -> None:
    """Persist digest send-date state."""
    _write_json(state_file, state)


def should_send_digest(state: dict, now: datetime, tz_name: str = "UTC") -> str | None:
    """Return 'morning', 'evening', or None based on current time and last-sent state."""
    import zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")

    local_now = now.astimezone(tz)
    today = local_now.date().isoformat()

    # First-ever run — no state at all → send morning immediately
    if state.get("last_morning") is None and state.get("last_evening") is None:
        return "morning"

    # Evening threshold check (20:00+)
    if local_now.hour >= DIGEST_EVENING_HOUR and state.get("last_evening") != today:
        return "evening"

    # Morning threshold check (08:00+)
    if local_now.hour >= DIGEST_MORNING_HOUR and state.get("last_morning") != today:
        return "morning"

    return None


def format_run_log(results: list, account_id: str, timestamp: datetime, tz_name: str = "UTC") -> str:
    """Format a run's digest as a plain-text log string to store in the buffer."""
    import zoneinfo
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")

    local_ts = timestamp.astimezone(tz)
    ts_str = local_ts.strftime("%Y-%m-%d %H:%M %Z")
    sep = "\u2550" * 60  # ══════

    pre_filtered = sum(1 for _, info in results if info and info.get("pre_filtered"))
    valid = sorted(
        [(em, info) for em, info in results if info and not info.get("pre_filtered")],
        key=lambda x: x[1]["priority"],
        reverse=True,
    )

    lines = [
        f"\n{sep}",
        f"  Run: {ts_str}  |  Account: {account_id}",
        f"{sep}",
        f"EMAIL DIGEST \u2014 {len(valid)} analyzed, {pre_filtered} pre-filtered\n",
    ]
    for rank, (em, info) in enumerate(valid, start=1):
        action_label = "ACTION REQUIRED" if info["action_required"] else "no action"
        lines += [
            f"[{rank}] PRIORITY {info['priority']} | {action_label}",
            f"    From:    {em['sender']}",
            f"    Subject: {em['subject']}",
            f"    TL;DR:   {info['tldr']}",
        ]
        if info.get("event_date"):
            lines.append(f"    Event:   {info['event_date']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def append_run_to_buffer(run_log: str, read_fn=None, write_fn=None, account_id: str | None = None) -> None:
    """Append a run log to the text buffer (local disk or GCS).

    When account_id is given, uses agent_log_buffer_<account_id>.txt.
    """
    # sprint 22 — per-account log buffer path
    buf_path = account_file_path(AGENT_LOG_BUFFER, account_id)
    if read_fn is None:
        read_fn = lambda: _read_text(buf_path)
    if write_fn is None:
        write_fn = lambda content: _write_text(buf_path, content)
    try:
        existing = read_fn()
        write_fn(existing + run_log)
    except Exception as e:
        print(f"  [digest] Warning: could not append to log buffer: {e}")


def build_digest_body(buffer_text: str, period: str, send_time: datetime) -> str:
    """Build the Agent Report email body from the accumulated log buffer."""
    date_str = send_time.strftime("%b %d, %Y")
    period_cap = period.capitalize()

    if not buffer_text.strip():
        return (
            f"Agent Log Digest \u2014 {period_cap}, {date_str}\n"
            f"{'=' * 60}\n"
            f"No log output recorded since last digest.\n"
        )

    run_count = buffer_text.count("Run:")
    header = (
        f"Agent Log Digest \u2014 {period_cap}, {date_str}\n"
        f"Runs included: {run_count}\n"
        f"{'=' * 60}\n"
    )
    return header + buffer_text


def send_agent_report(service: Any, period: str, body: str, date_str: str) -> None:
    """Send the Agent Report digest email; apply Agent Report label and star it."""
    import base64
    from email.mime.text import MIMEText

    subject = f"[Agent Report] {period.capitalize()} Digest \u2014 {date_str}"

    try:
        profile = gmail_execute(service.users().getProfile(userId="me"))
        email_address = profile["emailAddress"]

        msg = MIMEText(body)
        msg["to"] = email_address
        msg["subject"] = subject

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = gmail_execute(service.users().messages().send(userId="me", body={"raw": raw}))

        label_id = _get_or_create_label(service, AGENT_REPORT_LABEL_NAME)
        gmail_execute(service.users().messages().modify(
            userId="me", id=sent["id"],
            body={"removeLabelIds": ["UNREAD"], "addLabelIds": ["STARRED", label_id]},
        ))
        print(f"  [digest] Agent Report sent \u2014 {subject}")
    except Exception as e:
        print(f"  [digest] Could not send Agent Report: {e}")


def archive_read_agent_reports(service: Any) -> None:
    """Archive any read (non-UNREAD) Agent Report emails from the inbox."""
    try:
        query = 'label:"Agent Report" in:inbox -is:unread'
        result = gmail_execute(service.users().messages().list(userId="me", q=query))
        messages = result.get("messages", [])
        for msg in messages:
            gmail_execute(service.users().messages().modify(
                userId="me", id=msg["id"],
                body={"removeLabelIds": ["INBOX"], "addLabelIds": []},
            ))
        if messages:
            print(f"  [digest] Archived {len(messages)} read Agent Report email(s)")
    except Exception as e:
        print(f"  [digest] Warning: could not archive Agent Report emails: {e}")


# ---------------------------------------------------------------------------
# Sprint 20 — [AGENT STATUS] reply
# ---------------------------------------------------------------------------

def _extract_last_run(buffer_text: str) -> tuple[str, str]:
    """Parse the most recent Run: block from the log buffer.

    Returns (timestamp_str, emails_processed_str).
    Both are 'unknown' if the buffer is empty or unparseable.
    """
    if not buffer_text.strip():
        return "unknown", "unknown"

    # Find all "Run:" lines — last one is most recent
    run_lines = [ln for ln in buffer_text.splitlines() if ln.strip().startswith("Run:")]
    if not run_lines:
        return "unknown", "unknown"

    last_run_line = run_lines[-1].strip()
    # Format: "Run: 2026-04-13 08:47 EDT  |  Account: gmail-personal"
    ts_part = last_run_line.removeprefix("Run:").split("|")[0].strip()

    # Find the EMAIL DIGEST line that follows the last Run: block
    lines = buffer_text.splitlines()
    last_run_idx = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("Run:") and ln.strip() == last_run_line:
            last_run_idx = i

    emails_processed = "unknown"
    if last_run_idx is not None:
        for ln in lines[last_run_idx:]:
            m = re.search(r"(\d+)\s+analyzed", ln)
            if m:
                emails_processed = m.group(1)
                break

    return ts_part, emails_processed


def build_status_reply_body(
    scheduler_line: str,
    accounts: list,
    last_run_ts: str,
    emails_processed: str,
) -> str:
    """Build the plain-text [AGENT STATUS] reply body per UX contract."""
    from datetime import datetime as _dt
    import os as _os

    sep = "=" * 60
    now_str = _dt.now().strftime("%b %d, %Y %H:%M")

    backend      = _os.environ.get("MODEL_BACKEND", "gemini")
    model        = _os.environ.get("GEMINI_MODEL",  GEMINI_MODEL)
    archive_pri  = _os.environ.get("ARCHIVE_PRIORITY", str(ARCHIVE_PRIORITY))
    pre_filter_  = "on"  if _os.environ.get("PRE_FILTER",   "true").lower()  != "false" else "off"
    digest_on    = "on"  if _os.environ.get("SEND_DIGEST",  "true").lower()  != "false" else "off"
    max_batches  = _os.environ.get("MAX_BATCHES", "0")
    max_batch_str = "unlimited" if max_batches == "0" else max_batches

    account_lines = "\n".join(
        f"  • {a.get('email') or a.get('id', '?')}"
        for a in accounts
    ) if accounts else "  • (unknown)"

    lines = [
        f"Agent Status Report — {now_str}",
        sep,
        f"Scheduler       : {scheduler_line}",
        "",
        f"Model           : {backend} / {model}",
        f"Archive priority: ≤ {archive_pri} (priority 1–{archive_pri} archived after processing)",
        f"Pre-filter      : {pre_filter_}",
        f"Digest emails   : {digest_on}",
        f"Max batches     : {max_batch_str}",
        "",
        f"Accounts monitored ({len(accounts)}):",
        account_lines,
        "",
        f"Last run        : {last_run_ts}",
        f"Emails processed: {emails_processed}",
        "",
        sep,
        f"Reply to [AGENT STATUS] sent at {_dt.now().strftime('%H:%M')}.",
    ]
    return "\n".join(lines)


def send_status_reply(service: Any, body: str) -> None:
    """Send the [AGENT STATUS] reply email; apply Agent Report label and star it."""
    import base64
    from email.mime.text import MIMEText
    from datetime import datetime as _dt

    date_str = _dt.now().strftime("%b %d, %Y")
    subject  = f"[Agent Status] Report — {date_str}"

    try:
        profile = gmail_execute(service.users().getProfile(userId="me"))
        email_address = profile["emailAddress"]

        msg = MIMEText(body)
        msg["to"]      = email_address
        msg["subject"] = subject

        raw  = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = gmail_execute(service.users().messages().send(userId="me", body={"raw": raw}))

        label_id = _get_or_create_label(service, AGENT_REPORT_LABEL_NAME)
        gmail_execute(service.users().messages().modify(
            userId="me", id=sent["id"],
            body={"removeLabelIds": ["UNREAD"], "addLabelIds": ["STARRED", label_id]},
        ))
        print(f"  [agent-status] Status reply sent — {subject}")
    except Exception as e:
        print(f"  [agent-status] Could not send status reply: {e}")


def handle_status_commands(
    service: Any,
    own_address: str,
    status_msg_ids: list,
    is_paused: bool,
    pause_description: str,
    accounts: list,
    read_fn=None,
) -> None:
    """For each [AGENT STATUS] command: build reply, send it, consume the command email.

    read_fn is injectable for testing (replaces _read_text on the log buffer).
    """
    if not status_msg_ids:
        return

    print(f"  [agent-status] STATUS command detected — composing reply")

    # Gather last run data from log buffer
    try:
        buf = read_fn() if read_fn else _read_text(AGENT_LOG_BUFFER)
        last_run_ts, emails_processed = _extract_last_run(buf)
    except Exception as e:
        print(f"  [agent-status] Warning: could not read log buffer: {e}")
        last_run_ts, emails_processed = "unknown", "unknown"

    scheduler_line = pause_description if is_paused else "Active  (runs every 4 hours)"

    body = build_status_reply_body(
        scheduler_line=scheduler_line,
        accounts=accounts,
        last_run_ts=last_run_ts,
        emails_processed=emails_processed,
    )

    label_id = _get_or_create_label(service, AI_LABEL_NAME)

    for msg_id in status_msg_ids:
        send_status_reply(service, body)
        # Consume the command email: mark read + AI Processed + archive
        try:
            gmail_execute(service.users().messages().modify(
                userId="me", id=msg_id,
                body={
                    "removeLabelIds": ["UNREAD", "INBOX"],
                    "addLabelIds":    [label_id] if label_id else [],
                },
            ))
        except Exception as e:
            print(f"  [agent-status] Warning: could not consume command {msg_id}: {e}")


# sprint 22 — [AGENT IGNORE] / [AGENT UNIGNORE] handler
def handle_ignore_commands(
    service: Any,
    ignore_cmds: list[tuple[str, str]],
    unignore_cmds: list[tuple[str, str]],
    account_id: str | None = None,
) -> None:
    """Process IGNORE and UNIGNORE command emails.

    For each command: call block_sender or unblock_sender, then consume the
    command email (mark read, label AI Processed, archive).
    """
    from fetch_emails import AGENT_IGNORE_SUBJECT, AGENT_UNIGNORE_SUBJECT

    if not ignore_cmds and not unignore_cmds:
        return

    label_id = _get_or_create_label(service, AI_LABEL_NAME)

    for msg_id, subject in ignore_cmds:
        address = pre_filter.parse_ignore_address(subject, AGENT_IGNORE_SUBJECT)
        if not address:
            print(f"  [agent-ignore] command malformed — no address found, skipping")
        else:
            print(f"  [agent-ignore] IGNORE command detected — {address}")
            status = pre_filter.block_sender(address, account_id=account_id)
            print(f"  [agent-ignore] {status}")
        _consume_command(service, msg_id, label_id)

    for msg_id, subject in unignore_cmds:
        address = pre_filter.parse_ignore_address(subject, AGENT_UNIGNORE_SUBJECT)
        if not address:
            print(f"  [agent-ignore] command malformed — no address found, skipping")
        else:
            print(f"  [agent-ignore] UNIGNORE command detected — {address}")
            status = pre_filter.unblock_sender(address, account_id=account_id)
            print(f"  [agent-ignore] {status}")
        _consume_command(service, msg_id, label_id)


# sprint 22 — [AGENT MODEL] helpers and handler

def _get_account_model(account_id: str | None) -> str:
    """Return the active model for this account.

    Reads model_config_<id>.json if present; falls back to LOCAL_MODEL env var;
    falls back to the compiled default.
    """
    config_path = account_file_path(MODEL_CONFIG_FILE, account_id)
    try:
        data = _read_json(config_path)
        if data and data.get("model"):
            return data["model"]
    except Exception:
        pass
    return os.environ.get("LOCAL_MODEL", "llama3.1:latest")


def _write_model_config(config_path: str, model_name: str) -> None:
    """Write the per-account model config file."""
    _write_json(config_path, {"model": model_name})


def _make_ollama_client():
    """Return an Ollama client using the configured host."""
    import ollama
    return ollama.Client(host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))


def handle_model_command(
    service: Any,
    cmds: list[tuple[str, str]],
    account_id: str | None = None,
) -> None:
    """Process [AGENT MODEL] command emails.

    For each command: validate model name, check/pull from Ollama, write
    per-account config on success. Errors are appended to the log buffer.
    Command email is always consumed.
    """
    label_id  = _get_or_create_label(service, AI_LABEL_NAME)
    buf_path  = account_file_path(AGENT_LOG_BUFFER, account_id)
    backend   = os.environ.get("MODEL_BACKEND", "gemini").lower()

    for msg_id, model_name in cmds:
        try:
            # Guard: Gemini accounts cannot use this command
            if backend == "gemini":
                print(f"  [agent-model] command ignored — account uses Gemini backend")
                buf = _read_text(buf_path)
                _write_text(buf_path, buf + f"  [AGENT MODEL] error — model switching is Ollama-only\n")
                continue

            # Guard: empty model name
            if not model_name:
                print(f"  [agent-model] command malformed — no model name found")
                buf = _read_text(buf_path)
                _write_text(buf_path, buf + f"  [AGENT MODEL] error — no model name in command\n")
                continue

            print(f"  [agent-model] MODEL command received — checking {model_name}")
            client      = _make_ollama_client()
            installed   = [m.model for m in client.list().models]
            config_path = account_file_path(MODEL_CONFIG_FILE, account_id)

            if model_name in installed:
                print(f"  [agent-model] {model_name} already installed — switching")
            else:
                print(f"  [agent-model] {model_name} not found locally — pulling from Ollama...")
                try:
                    client.pull(model_name)
                    print(f"  [agent-model] {model_name} pulled successfully — switching")
                except Exception as pull_err:
                    print(f"  [agent-model] pull failed — {model_name} not found: {pull_err}")
                    buf = _read_text(buf_path)
                    _write_text(buf_path, buf + f"  [AGENT MODEL] error — {model_name} could not be pulled: {pull_err}\n")
                    continue  # skip config write

            _write_model_config(config_path, model_name)
            print(f"  [agent-model] model config updated")
            buf = _read_text(buf_path)
            _write_text(buf_path, buf + f"  [AGENT MODEL] switched to {model_name}\n")

        except Exception as e:
            print(f"  [agent-model] unexpected error: {e}")
        finally:
            _consume_command(service, msg_id, label_id)


# sprint 22 — [AGENT DIGEST] on-demand report handler
def handle_digest_command(
    service: Any,
    msg_ids: list[str],
    account_id: str | None = None,
) -> None:
    """Send an on-demand Agent Report from the current buffer, then clear it.

    Buffer is only cleared if send succeeds — protects against silent data loss.
    """
    buf_path = account_file_path(AGENT_LOG_BUFFER, account_id)
    label_id = _get_or_create_label(service, AI_LABEL_NAME)

    for msg_id in msg_ids:
        print(f"[{account_id}] DIGEST command received — sending on-demand report")
        buffer = _read_text(buf_path)
        now = datetime.now()
        date_str = now.strftime("%b %d, %Y")
        body = build_digest_body(buffer, "on-demand", now)

        try:
            send_agent_report(service, "on-demand", body, date_str)
            _write_text(buf_path, "")
            print(f"[{account_id}] DIGEST sent — buffer cleared")
        except Exception as e:
            print(f"  [warning] DIGEST command failed — send error: {e}")

        _consume_command(service, msg_id, label_id)


def _consume_command(service: Any, msg_id: str, label_id: str | None) -> None:
    """Mark a command email read, apply AI Processed label, and archive it."""
    try:
        gmail_execute(service.users().messages().modify(
            userId="me", id=msg_id,
            body={
                "removeLabelIds": ["UNREAD", "INBOX"],
                "addLabelIds":    [label_id] if label_id else [],
            },
        ))
        print(f"  [agent-ignore] Command email consumed")
    except Exception as e:
        print(f"  [agent-ignore] Warning: could not consume command {msg_id}: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Authenticating with Gmail...")
    service = get_gmail_service()
    print("Authenticated.\n")

    print("Fetching emails to process...")
    emails = fetch_unprocessed_emails(service)
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
