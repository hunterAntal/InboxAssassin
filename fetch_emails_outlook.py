"""
Outlook / Hotmail email fetching via Microsoft Graph API.

Auth uses MSAL with the device code flow (no browser required — works in
containers and headless environments). Token cache is persisted to
TOKEN_FILE_OUTLOOK so subsequent runs use the refresh token silently.

Environment variables (set in .env or accounts.json):
  OUTLOOK_CLIENT_ID     — Azure app registration client ID
  OUTLOOK_TENANT_ID     — Azure tenant ID ("consumers" for personal accounts)
  OUTLOOK_CLIENT_SECRET — Azure app client secret
"""

import json
import os
import msal
import requests

TOKEN_FILE_OUTLOOK = "token_outlook.json"
GRAPH_BASE         = "https://graph.microsoft.com/v1.0"
SCOPES             = [
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/User.Read",
]
MAX_EMAILS = int(os.environ.get("MAX_EMAILS", 50))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _build_msal_app(client_id: str, tenant_id: str, cache: msal.SerializableTokenCache):
    return msal.PublicClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        token_cache=cache,
    )


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if os.path.exists(TOKEN_FILE_OUTLOOK):
        with open(TOKEN_FILE_OUTLOOK) as f:
            cache.deserialize(f.read())
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        with open(TOKEN_FILE_OUTLOOK, "w") as f:
            f.write(cache.serialize())


def get_outlook_token(client_id: str = None, tenant_id: str = None, client_secret: str = None) -> str:
    """Return a valid access token, refreshing silently or prompting device code if needed."""
    client_id = client_id or os.environ.get("OUTLOOK_CLIENT_ID", "")
    tenant_id = tenant_id or os.environ.get("OUTLOOK_TENANT_ID", "consumers")

    cache = _load_cache()
    app   = _build_msal_app(client_id, tenant_id, cache)

    # Try silent refresh first
    accounts = app.get_accounts()
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]

    # Fall back to device code flow (prints a URL + code for first-time auth)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise RuntimeError(f"Could not initiate device flow: {flow.get('error_description')}")

    print(f"\n[outlook] {flow['message']}\n")
    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        raise RuntimeError(f"Outlook auth failed: {result.get('error_description')}")

    _save_cache(cache)
    return result["access_token"]


def get_outlook_service(account: dict = None) -> dict:
    """Return a dict with token + session, used like a service handle."""
    account       = account or {}
    client_id     = account.get("client_id")     or os.environ.get("OUTLOOK_CLIENT_ID", "")
    tenant_id     = account.get("tenant_id")     or os.environ.get("OUTLOOK_TENANT_ID", "consumers")
    client_secret = account.get("client_secret") or os.environ.get("OUTLOOK_CLIENT_SECRET", "")

    token   = get_outlook_token(client_id, tenant_id)
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return {"session": session, "token": token}


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_unread_emails_outlook(service: dict, max_results: int = MAX_EMAILS) -> list[dict]:
    """Fetch unread emails and return them in the standard pipeline dict format."""
    session  = service["session"]
    endpoint = (
        f"{GRAPH_BASE}/me/messages"
        f"?$filter=isRead eq false"
        f"&$orderby=receivedDateTime desc"
        f"&$top={max_results}"
        f"&$select=id,subject,from,receivedDateTime,bodyPreview,body"
    )

    resp = session.get(endpoint)
    resp.raise_for_status()
    messages = resp.json().get("value", [])

    emails = []
    for msg in messages:
        from_field   = msg.get("from") or {}
        email_addr   = from_field.get("emailAddress") or {}
        sender_name  = email_addr.get("name", "")
        sender_email = email_addr.get("address", "") or sender_name or "unknown"
        sender       = f"{sender_name} <{sender_email}>" if sender_name and sender_email != sender_name else sender_email
        body_text    = msg.get("body", {}).get("content", msg.get("bodyPreview", ""))

        # Strip basic HTML tags from body if content type is HTML
        if msg.get("body", {}).get("contentType") == "html":
            import re
            body_text = re.sub(r"<[^>]+>", " ", body_text)
            body_text = re.sub(r"\s+", " ", body_text).strip()

        emails.append({
            "message_id": msg["id"],
            "sender":     sender,
            "subject":    msg.get("subject", "(no subject)"),
            "date":       msg.get("receivedDateTime", ""),
            "snippet":    msg.get("bodyPreview", "")[:100],
            "body":       body_text[:500],
        })

    return emails


# ---------------------------------------------------------------------------
# Inbox management helpers (used by analyze_emails.manage_inbox)
# ---------------------------------------------------------------------------

def mark_read_outlook(service: dict, message_id: str) -> None:
    service["session"].patch(
        f"{GRAPH_BASE}/me/messages/{message_id}",
        json={"isRead": True},
    )


def archive_outlook(service: dict, message_id: str) -> None:
    """Move message to the Archive folder."""
    service["session"].post(
        f"{GRAPH_BASE}/me/messages/{message_id}/move",
        json={"destinationId": "archive"},
    )


def apply_category_outlook(service: dict, message_id: str, category: str) -> None:
    """Apply an Outlook category (equivalent to Gmail label)."""
    msg = service["session"].get(f"{GRAPH_BASE}/me/messages/{message_id}?$select=categories").json()
    existing = msg.get("categories", [])
    if category not in existing:
        service["session"].patch(
            f"{GRAPH_BASE}/me/messages/{message_id}",
            json={"categories": existing + [category]},
        )


# ---------------------------------------------------------------------------
# First-time auth helper (run manually)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json
    from dotenv import load_dotenv
    load_dotenv()

    # Load the outlook account from accounts.json
    with open("accounts.json") as _f:
        _accounts = _json.load(_f)
    _account = next((a for a in _accounts if a.get("provider") == "outlook"), None)
    if not _account:
        print("No outlook account found in accounts.json")
        raise SystemExit(1)

    # Inject client_secret from env
    _account = {**_account, "client_secret": os.environ.get("OUTLOOK_CLIENT_SECRET", "")}

    print("Authenticating with Outlook...")
    svc = get_outlook_service(_account)
    me  = svc["session"].get(f"{GRAPH_BASE}/me").json()
    print(f"Authenticated as: {me.get('displayName')} <{me.get('mail') or me.get('userPrincipalName')}>")
    print(f"Token saved to {TOKEN_FILE_OUTLOOK}")
