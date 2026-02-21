"""Tests for auth credential loading (file + macOS Keychain)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from decant.auth import (
    _load_credentials,
    _load_file_credentials,
    _load_keychain_credentials,
    _save_credentials,
    _save_file_credentials,
    _save_keychain_credentials,
)

FAKE_OAUTH = {
    "accessToken": "ak-ant-test123",
    "refreshToken": "rt-test456",
    "expiresAt": 9999999999999,
}

FAKE_KEYCHAIN_JSON = json.dumps({"claudeAiOauth": FAKE_OAUTH})

FAKE_FILE_JSON = json.dumps({"claudeAiOauth": FAKE_OAUTH})


# -- _load_file_credentials --------------------------------------------------


def test_load_file_credentials(tmp_path, monkeypatch):
    creds_path = tmp_path / ".claude" / ".credentials.json"
    creds_path.parent.mkdir(parents=True)
    creds_path.write_text(FAKE_FILE_JSON)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = _load_file_credentials()
    assert result is not None
    creds, source = result
    assert source == "claude-code"
    assert creds["accessToken"] == "ak-ant-test123"


def test_load_file_credentials_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _load_file_credentials() is None


def test_load_file_credentials_bad_json(tmp_path, monkeypatch):
    creds_path = tmp_path / ".claude" / ".credentials.json"
    creds_path.parent.mkdir(parents=True)
    creds_path.write_text("not json")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert _load_file_credentials() is None


def test_load_file_credentials_missing_oauth_key(tmp_path, monkeypatch):
    creds_path = tmp_path / ".claude" / ".credentials.json"
    creds_path.parent.mkdir(parents=True)
    creds_path.write_text(json.dumps({"someOtherKey": {}}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert _load_file_credentials() is None


# -- _load_keychain_credentials -----------------------------------------------


def _mock_security_run(stdout: str, returncode: int = 0):
    """Create a mock for subprocess.run that simulates `security` output."""
    mock_result = MagicMock()
    mock_result.returncode = returncode
    mock_result.stdout = stdout
    return mock_result


@patch("decant.auth.subprocess.run")
def test_load_keychain_success(mock_run):
    mock_run.return_value = _mock_security_run(FAKE_KEYCHAIN_JSON)

    result = _load_keychain_credentials()
    assert result is not None
    creds, source = result
    assert source == "claude-code-keychain"
    assert creds["accessToken"] == "ak-ant-test123"

    # Should try the first service name
    call_args = mock_run.call_args[0][0]
    assert "Claude Code-credentials" in call_args


@patch("decant.auth.subprocess.run")
def test_load_keychain_fallback_service_name(mock_run):
    """Falls back to legacy service names if primary fails."""
    def side_effect(cmd, **kwargs):
        service = cmd[cmd.index("-s") + 1]
        if service == "Claude Code-credentials":
            return _mock_security_run("", returncode=44)  # not found
        elif service == "Claude Code - credentials":
            return _mock_security_run(FAKE_KEYCHAIN_JSON)
        return _mock_security_run("", returncode=44)

    mock_run.side_effect = side_effect

    result = _load_keychain_credentials()
    assert result is not None
    assert result[1] == "claude-code-keychain"


@patch("decant.auth.subprocess.run")
def test_load_keychain_all_fail(mock_run):
    mock_run.return_value = _mock_security_run("", returncode=44)
    assert _load_keychain_credentials() is None


@patch("decant.auth.subprocess.run")
def test_load_keychain_bad_json(mock_run):
    mock_run.return_value = _mock_security_run("not json at all")
    assert _load_keychain_credentials() is None


@patch("decant.auth.subprocess.run")
def test_load_keychain_timeout(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="security", timeout=5)
    assert _load_keychain_credentials() is None


@patch("decant.auth.subprocess.run")
def test_load_keychain_no_oauth_key(mock_run):
    mock_run.return_value = _mock_security_run(json.dumps({"otherKey": {}}))
    assert _load_keychain_credentials() is None


@patch("decant.auth.subprocess.run")
def test_load_keychain_non_dict_json(mock_run):
    """Keychain returns valid JSON that isn't a dict (e.g. a list)."""
    mock_run.return_value = _mock_security_run(json.dumps([1, 2, 3]))
    assert _load_keychain_credentials() is None


@patch("decant.auth.subprocess.run")
def test_load_keychain_oauth_not_dict(mock_run):
    """claudeAiOauth key exists but is a string, not a dict."""
    mock_run.return_value = _mock_security_run(json.dumps({"claudeAiOauth": "not-a-dict"}))
    assert _load_keychain_credentials() is None


# -- _load_credentials (platform dispatch) ------------------------------------


@patch("decant.auth.sys")
@patch("decant.auth._load_keychain_credentials")
@patch("decant.auth._load_file_credentials")
def test_load_credentials_darwin_keychain_hit(mock_file, mock_keychain, mock_sys):
    mock_sys.platform = "darwin"
    mock_keychain.return_value = (FAKE_OAUTH, "claude-code-keychain")

    result = _load_credentials()
    assert result == (FAKE_OAUTH, "claude-code-keychain")
    mock_file.assert_not_called()


@patch("decant.auth.sys")
@patch("decant.auth._load_keychain_credentials")
@patch("decant.auth._load_file_credentials")
def test_load_credentials_darwin_keychain_miss_falls_back_to_file(mock_file, mock_keychain, mock_sys):
    mock_sys.platform = "darwin"
    mock_keychain.return_value = None
    mock_file.return_value = (FAKE_OAUTH, "claude-code")

    result = _load_credentials()
    assert result == (FAKE_OAUTH, "claude-code")


@patch("decant.auth.sys")
@patch("decant.auth._load_keychain_credentials")
@patch("decant.auth._load_file_credentials")
def test_load_credentials_linux_skips_keychain(mock_file, mock_keychain, mock_sys):
    mock_sys.platform = "linux"
    mock_file.return_value = (FAKE_OAUTH, "claude-code")

    result = _load_credentials()
    assert result == (FAKE_OAUTH, "claude-code")
    mock_keychain.assert_not_called()


# -- _save_credentials (dispatch) ---------------------------------------------


@patch("decant.auth._save_keychain_credentials")
@patch("decant.auth._save_file_credentials")
def test_save_dispatches_to_keychain(mock_file_save, mock_kc_save):
    _save_credentials(FAKE_OAUTH, "claude-code-keychain")
    mock_kc_save.assert_called_once_with(FAKE_OAUTH)
    mock_file_save.assert_not_called()


@patch("decant.auth._save_keychain_credentials")
@patch("decant.auth._save_file_credentials")
def test_save_dispatches_to_file(mock_file_save, mock_kc_save):
    _save_credentials(FAKE_OAUTH, "claude-code")
    mock_file_save.assert_called_once_with(FAKE_OAUTH)
    mock_kc_save.assert_not_called()


# -- _save_file_credentials ---------------------------------------------------


def test_save_file_credentials(tmp_path, monkeypatch):
    creds_path = tmp_path / ".claude" / ".credentials.json"
    creds_path.parent.mkdir(parents=True)
    creds_path.write_text(json.dumps({"existingKey": "keep"}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    _save_file_credentials(FAKE_OAUTH)

    saved = json.loads(creds_path.read_text())
    assert saved["claudeAiOauth"] == FAKE_OAUTH
    assert saved["existingKey"] == "keep"


# -- _save_keychain_credentials ------------------------------------------------


@patch("decant.auth.subprocess.run")
def test_save_keychain_merges_existing(mock_run):
    """Reads existing Keychain data and merges, then writes with -U."""
    existing_data = json.dumps({"claudeAiOauth": {}, "otherKey": "keep"})
    # First call: read existing; second call: add-generic-password
    mock_run.side_effect = [
        _mock_security_run(existing_data),  # read
        _mock_security_run(""),             # add -U
    ]

    _save_keychain_credentials(FAKE_OAUTH)

    assert mock_run.call_count == 2
    add_call = mock_run.call_args_list[1]
    cmd = add_call[0][0]
    assert "add-generic-password" in cmd
    assert "-U" in cmd
    # The payload should contain both keys
    payload_idx = cmd.index("-w") + 1
    payload = json.loads(cmd[payload_idx])
    assert payload["claudeAiOauth"] == FAKE_OAUTH
    assert payload["otherKey"] == "keep"


@patch("decant.auth.subprocess.run")
def test_save_keychain_no_prior_entry(mock_run):
    """If no existing entry, creates a new one with just the oauth data."""
    mock_run.side_effect = [
        _mock_security_run("", returncode=44),  # read fails (not found)
        _mock_security_run(""),                  # add -U
    ]

    _save_keychain_credentials(FAKE_OAUTH)

    assert mock_run.call_count == 2
    add_call = mock_run.call_args_list[1]
    cmd = add_call[0][0]
    payload_idx = cmd.index("-w") + 1
    payload = json.loads(cmd[payload_idx])
    assert payload == {"claudeAiOauth": FAKE_OAUTH}


@patch("decant.auth.subprocess.run")
def test_save_keychain_no_delete_before_add(mock_run):
    """Verify we use -U (update in place) and never call delete-generic-password."""
    mock_run.side_effect = [
        _mock_security_run(json.dumps({"claudeAiOauth": {}})),  # read
        _mock_security_run(""),                                   # add -U
    ]

    _save_keychain_credentials(FAKE_OAUTH)

    for call in mock_run.call_args_list:
        cmd = call[0][0]
        assert "delete-generic-password" not in cmd
