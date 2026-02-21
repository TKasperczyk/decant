"""Anthropic client creation with full auth chain.

Priority:
  1. ANTHROPIC_API_KEY / OPENCODE_API_KEY env var
  2. ANTHROPIC_AUTH_TOKEN env var (OAuth)
  3. Claude Code OAuth credentials:
     - macOS: macOS Keychain (service "Claude Code-credentials")
     - Linux/other: ~/.claude/.credentials.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import anthropic

OAUTH_BETAS_WITH_CLAUDE_CODE = (
    "oauth-2025-04-20,claude-code-20250219,interleaved-thinking-2025-05-14"
)
OAUTH_USER_AGENT = "claude-cli/2.1.2 (external, cli)"

# OAuth token refresh constants
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
TOKEN_REFRESH_BUFFER_MS = 5 * 60 * 1000  # 5 min before expiry

# Required system prompt prefix for OAuth requests
CLAUDE_CODE_SYSTEM_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude."

# macOS Keychain service names (current first, then legacy)
_KEYCHAIN_SERVICE_NAMES = [
    "Claude Code-credentials",
    "Claude Code - credentials",
    "Claude Code",
]


def _load_keychain_credentials() -> tuple[dict, str] | None:
    """Load credentials from macOS Keychain. Returns (creds, source) or None."""
    for service in _KEYCHAIN_SERVICE_NAMES:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                continue
            data = json.loads(result.stdout.strip())
            if not isinstance(data, dict):
                continue
            oauth = data.get("claudeAiOauth")
            if isinstance(oauth, dict) and oauth:
                return oauth, "claude-code-keychain"
        except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError):
            continue
    return None


def _load_file_credentials() -> tuple[dict, str] | None:
    """Load credentials from ~/.claude/.credentials.json. Returns (creds, source) or None."""
    cc_path = Path.home() / ".claude" / ".credentials.json"
    if cc_path.exists():
        try:
            data = json.loads(cc_path.read_text())
            if oauth := data.get("claudeAiOauth"):
                return oauth, "claude-code"
        except (json.JSONDecodeError, KeyError):
            pass
    return None


def _load_credentials() -> tuple[dict, str] | None:
    """Load OAuth credentials. Checks Keychain on macOS, file on Linux."""
    if sys.platform == "darwin":
        result = _load_keychain_credentials()
        if result:
            return result
    return _load_file_credentials()


def _save_credentials(creds: dict, source: str) -> None:
    """Persist refreshed credentials back to their original store."""
    if source == "claude-code-keychain":
        _save_keychain_credentials(creds)
    else:
        _save_file_credentials(creds)


def _save_keychain_credentials(creds: dict) -> None:
    """Write refreshed credentials back to macOS Keychain."""
    try:
        service = _KEYCHAIN_SERVICE_NAMES[0]
        existing = {}
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout.strip())
                if isinstance(data, dict):
                    existing = data
        except Exception:
            pass

        existing["claudeAiOauth"] = creds
        payload = json.dumps(existing)

        # -U updates in place if the item exists, creates if it doesn't
        subprocess.run(
            ["security", "add-generic-password", "-s", service,
             "-a", os.getenv("USER", ""), "-w", payload, "-U"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except Exception as e:
        print(f"[decant] Warning: failed to save credentials to Keychain: {e}")


def _save_file_credentials(creds: dict) -> None:
    """Write refreshed credentials back to ~/.claude/.credentials.json."""
    try:
        path = Path.home() / ".claude" / ".credentials.json"
        existing = {}
        try:
            existing = json.loads(path.read_text())
        except Exception:
            pass
        existing["claudeAiOauth"] = creds
        path.write_text(json.dumps(existing, indent=2))
    except Exception as e:
        print(f"[decant] Warning: failed to save refreshed credentials: {e}")


def _refresh_token(refresh_token: str) -> dict | None:
    """Refresh an OAuth access token."""
    import urllib.request

    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return {
                "accessToken": data["access_token"],
                "refreshToken": data["refresh_token"],
                "expiresAt": int(time.time() * 1000) + data["expires_in"] * 1000,
            }
    except Exception as e:
        print(f"[decant] Token refresh failed: {e}")
        return None


def _get_fresh_credentials() -> tuple[dict, str] | None:
    """Load credentials, refreshing if needed."""
    loaded = _load_credentials()
    if not loaded:
        return None

    creds, source = loaded

    # API key credentials don't need refresh
    if creds.get("apiKey"):
        return creds, source

    # Refresh if expired or near expiry
    expires_at = creds.get("expiresAt", 0)
    if expires_at < time.time() * 1000 + TOKEN_REFRESH_BUFFER_MS:
        refreshed = _refresh_token(creds["refreshToken"])
        if not refreshed:
            return None
        _save_credentials(refreshed, source)
        creds = refreshed

    return creds, source


def _create_oauth_client(token: str) -> anthropic.Anthropic:
    """Create an Anthropic client with OAuth auth."""
    return anthropic.Anthropic(
        auth_token=token,
        default_headers={
            "anthropic-beta": OAUTH_BETAS_WITH_CLAUDE_CODE,
            "user-agent": OAUTH_USER_AGENT,
        },
    )


def create_client() -> anthropic.Anthropic:
    """Create an authenticated Anthropic client.

    Tries env vars first, then credential files with auto-refresh.
    Raises RuntimeError if no auth method is available.
    """
    # 1. API key from env
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENCODE_API_KEY")
    if api_key:
        return anthropic.Anthropic(api_key=api_key)

    # 2. Explicit auth token from env
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if auth_token:
        return _create_oauth_client(auth_token)

    # 3. Credential files with auto-refresh
    fresh = _get_fresh_credentials()
    if fresh:
        creds, _source = fresh
        if creds.get("apiKey"):
            return anthropic.Anthropic(api_key=creds["apiKey"])
        return _create_oauth_client(creds["accessToken"])

    raise RuntimeError(
        "No authentication available. Set ANTHROPIC_API_KEY, "
        "ANTHROPIC_AUTH_TOKEN, or ensure Claude Code credentials exist."
    )


def is_oauth_client(client: anthropic.Anthropic) -> bool:
    """Check if a client was created via OAuth (needs system prompt prefix)."""
    headers = getattr(client, "_custom_headers", {}) or {}
    if not headers:
        headers = getattr(client, "default_headers", {}) or {}
    return "oauth" in str(headers.get("anthropic-beta", "")).lower()
