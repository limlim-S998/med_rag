"""The operator client keeps credentials local and reuses the intended identity."""

import stat
import sys
from types import SimpleNamespace

import pytest

from scripts.demo_run import acquire_token


def identity_state(**config):
    return {"applications": {"api": {"appId": "api"}, "client": {"appId": "client"}},
            "config": {"tenant_id": "tenant", "writer_object_id": "writer", **config}}


def install_msal(monkeypatch, accounts, *, silent=None, interactive=None):
    calls = []

    class Cache:
        has_state_changed = False

        def deserialize(self, value):
            calls.append(("cache-read", value))

        def serialize(self):
            return "private-test-cache"

    class Client:
        def __init__(self, client_id, *, authority, token_cache):
            self.cache = token_cache
            calls.append(("authority", authority))

        def get_accounts(self):
            return accounts

        def acquire_token_silent(self, scopes, *, account):
            calls.append(("silent", account["local_account_id"]))
            return silent

        def acquire_token_interactive(self, **kwargs):
            calls.append(("interactive", kwargs))
            self.cache.has_state_changed = True
            return interactive

    monkeypatch.setitem(sys.modules, "msal", SimpleNamespace(
        SerializableTokenCache=Cache, PublicClientApplication=Client))
    return calls


def test_private_cache_reuses_only_configured_writer(monkeypatch, tmp_path, capsys):
    path = tmp_path / "cache.json"
    path.write_text("existing-cache")
    path.chmod(0o644)
    calls = install_msal(monkeypatch, [{"local_account_id": "another-user"},
                                     {"local_account_id": "writer"}],
                         silent={"access_token": "private-token"})
    assert acquire_token(identity_state(), path) == "private-token"
    assert ("silent", "writer") in calls and ("silent", "another-user") not in calls
    assert not any(call[0] == "interactive" for call in calls)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not capsys.readouterr().out


def test_browser_login_caches_privately_without_printing_credentials(monkeypatch, tmp_path, capsys):
    path = tmp_path / "operator" / "cache.json"
    calls = install_msal(monkeypatch, [], interactive={"access_token": "private-token"})
    assert acquire_token(identity_state(), path) == "private-token"
    browser = next(call[1] for call in calls if call[0] == "interactive")
    assert browser["scopes"] == ["api://api/access"] and browser["port"] == 8400
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == "private-test-cache"
    output = capsys.readouterr().out
    assert "http://localhost:8400" in output
    assert "private-token" not in output and "private-test-cache" not in output


def test_browser_auth_failure_does_not_expose_server_description(monkeypatch, tmp_path, capsys):
    install_msal(monkeypatch, [], interactive={"error": "access_denied",
                                              "error_description": "private-server-detail"})
    with pytest.raises(RuntimeError, match="API sign-in failed: access_denied"):
        acquire_token(identity_state(), tmp_path / "cache.json")
    assert "private-server-detail" not in capsys.readouterr().out
