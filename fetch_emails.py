"""
Gmail authentication and email fetching.

Handles OAuth 2.0 token management and returns a uniform email dict
consumed by analyze_emails.py. Set USE_SAMPLE_DATA=true in .env to
load from sample_emails.json instead of hitting the Gmail API.
"""

import os
import json
import base64
import socket
import time
from typing import Any
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()


def gmail_execute(request, retries: int = 4, backoff: float = 5.0):
    """Execute a Google API request, retrying on transient network errors and rate limits."""
    from googleapiclient.errors import HttpError as _HttpError
    for attempt in range(retries):
        try:
            return request.execute()
        except _HttpError as e:
            if e.resp.status in (429, 403) and attempt < retries - 1:
                wait = backoff * (2 ** attempt)
                print(f"  [warning] API rate limit ({e.resp.status}), retrying in {wait:.0f}s...")
                time.sleep(wait)
            else:
                raise
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

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE       = "token.json"
SAMPLE_FILE      = "sample_emails.json"
MAX_EMAILS       = int(os.environ.get("MAX_EMAILS", 50))
SNIPPET_LENGTH   = 100   # chars shown in digest preview
BODY_LENGTH      = 500   # chars sent to AI for analysis


def get_gmail_service(token_file: str = TOKEN_FILE) -> Any:
    """Authenticate and return a Gmail API service object."""
    creds = None

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
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


def fetch_unread_emails(service: Any, max_results: int = MAX_EMAILS) -> list[dict]:
    """Fetch up to max_results unread emails. Returns sample data if USE_SAMPLE_DATA=true."""
    if os.environ.get("USE_SAMPLE_DATA", "").lower() == "true":
        print(f"(using sample data from {SAMPLE_FILE})")
        with open(SAMPLE_FILE) as f:
            return json.load(f)

    try:
        result = gmail_execute(service.users().messages().list(
            userId="me",
            labelIds=["INBOX", "UNREAD"],
            maxResults=max_results,
        ))
    except (HttpError, TimeoutError, OSError) as e:
        print(f"[error] Failed to list emails: {e}")
        return []

    messages = result.get("messages", [])
    if not messages:
        print("No unread emails found.")
        return []

    emails = []
    for msg_ref in messages:
        try:
            msg     = gmail_execute(service.users().messages().get(userId="me", id=msg_ref["id"], format="full"))
            headers = msg["payload"]["headers"]
            body    = _decode_body(msg["payload"]).strip()

            emails.append({
                "message_id": msg_ref["id"],
                "sender":     _get_header(headers, "From"),
                "subject":    _get_header(headers, "Subject"),
                "date":       _get_header(headers, "Date"),
                "snippet":    body[:SNIPPET_LENGTH] if body else msg.get("snippet", "")[:SNIPPET_LENGTH],
                "body":       body[:BODY_LENGTH] if body else msg.get("snippet", ""),
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


def main():
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"Error: {CREDENTIALS_FILE} not found.")
        return

    print("Authenticating with Gmail...")
    service = get_gmail_service()
    print("Authenticated.\n")

    emails = fetch_unread_emails(service)
    if emails:
        print_emails(emails)


if __name__ == "__main__":
    main()
