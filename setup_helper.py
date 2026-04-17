"""
Testable logic for setup.sh.

setup.sh delegates deterministic decisions here so they can be
unit-tested without interactive prompts or subprocesses.
"""

import json
import re
import sys
from pathlib import Path


def check_python_version() -> tuple[bool, str]:
    """Return (ok, version_str). ok is True when Python >= 3.10."""
    info = sys.version_info
    major, minor, micro = info[0], info[1], info[2]
    version_str = f"{major}.{minor}.{micro}"
    ok = (major, minor) >= (3, 10)
    return ok, version_str


def validate_backend(choice: str) -> bool:
    """Return True when choice is "1" (Gemini) or "2" (Local)."""
    return choice in ("1", "2")


def validate_api_key(key: str) -> bool:
    """Return True when key is a non-empty, non-whitespace string."""
    return bool(key and key.strip())


def build_env_content(
    backend: str,
    gemini_key: str = "",
    local_model: str = "gemma3:4b",
) -> str:
    """
    Return the text content for a .env file.

    backend  : "gemini" or "local"
    gemini_key : required when backend == "gemini"
    local_model: model name for Ollama; defaults to gemma3:4b
    """
    lines = [
        f"MODEL_BACKEND={backend}",
        "USE_SAMPLE_DATA=false",
        "SEND_DIGEST=false",
        "MAX_EMAILS=50",
    ]

    if backend == "gemini":
        lines.append(f"GEMINI_API_KEY={gemini_key}")
    else:
        lines.append(f"LOCAL_MODEL={local_model}")
        lines.append("OLLAMA_HOST=http://localhost:11434")

    return "\n".join(lines) + "\n"


def credentials_file_exists(directory: str = ".") -> bool:
    """Return True when credentials.json is present in directory."""
    return (Path(directory) / "credentials.json").exists()


def token_file_exists(directory: str = ".") -> bool:
    """Return True when token.json is present in directory."""
    return (Path(directory) / "token.json").exists()


def env_file_exists(directory: str = ".") -> bool:
    """Return True when .env is present in directory."""
    return (Path(directory) / ".env").exists()


def interpret_smoke_result(returncode: int) -> tuple[bool, str]:
    """Translate a subprocess return code into (passed, message)."""
    if returncode == 0:
        return True, "Smoke test passed"
    return False, "Smoke test failed"


# ── Sprint 21 — auto-pull model on startup ────────────────────────────────────

def should_pull(answer: str) -> bool:
    """
    Parse a Y/n/Enter confirmation response.

    Empty string or any form of "y"/"yes" returns True (pull).
    "n" / "no" returns False (decline).
    """
    normalised = answer.strip().lower()
    return normalised in ("", "y", "yes")


# sprint 22 — daemon install prompt in setup.sh
def should_install_daemon(answer: str) -> bool:
    """Parse a Y/n/Enter response for the daemon install prompt.

    Empty string or any form of "y"/"yes" returns True (install).
    "n" / "no" returns False (skip).
    """
    return answer.strip().lower() in ("", "y", "yes")


# sprint 22 — multi-account OAuth helpers

_ALIAS_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')


def validate_account_alias(alias: str) -> tuple[bool, str, str]:
    """Validate and normalise a Gmail account alias.

    Returns (valid, normalised_alias, error_message).
    Valid aliases: lowercase letters, digits, hyphens; non-empty.
    Input is lowercased before validation.
    """
    normalised = alias.strip().lower()
    if not normalised:
        return False, "", "Alias cannot be empty."
    if not _ALIAS_RE.match(normalised):
        return False, "", "Alias must contain only letters, numbers, and hyphens."
    return True, normalised, ""


def build_account_entry(alias: str) -> dict:
    """Build an accounts.json entry dict for a new local Gmail account."""
    return {
        "id": f"gmail-{alias}",
        "provider": "gmail",
        "active": True,
        "token_file": f"token_{alias}.json",
        "token_secret": "",
        "credentials_secret": "",
    }


def add_account_to_json(accounts_path: str, alias: str) -> tuple[bool, str]:
    """Append a new Gmail account entry to accounts.json.

    Returns (success, message).
    Rejects duplicates — same id must not already exist.
    Creates the file if it doesn't exist.
    """
    path = Path(accounts_path)
    try:
        if path.exists():
            data = json.loads(path.read_text())
        else:
            data = []
    except (OSError, json.JSONDecodeError) as e:
        return False, f"Could not read accounts.json: {e}"

    new_id = f"gmail-{alias}"
    if any(entry.get("id") == new_id for entry in data):
        return False, f"Account '{new_id}' already exists. Try a different alias."

    data.append(build_account_entry(alias))

    try:
        path.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as e:
        return False, f"Could not write accounts.json: {e}"

    return True, f"  ✓ {new_id} — added to accounts.json"


def interpret_pull_result(returncode: int, model_name: str) -> tuple[bool, str]:
    """
    Map an `ollama pull` exit code to (success, human-readable message).

    Used by run_all.py preflight to decide whether to proceed or abort.
    """
    if returncode == 0:
        return True, f"Model {model_name} pulled and ready"
    return False, f"Failed to pull {model_name}"
