"""Profile isolation for the Zulip adapter's settings and state (Issue #156).

``runtime_scope`` (and its unit tests) prove the resolution helpers work in
isolation. These tests prove the *wiring*: the adapter and the config modules
actually resolve credentials, feature flags and on-disk state through the active
Hermes profile, so two profiles served by one process never cross-read.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

import zulip.adapter as adapter_module
from zulip.accounts import AccountResolver
from zulip.adapter import ZulipAdapter
from zulip.media import DEFAULT_MAX_MB, resolve_media_max_mb
from zulip.policy import PolicyEngine
from zulip.reactions import ReactionConfig
from zulip.runtime_scope import get_setting


class _FakeHermes:
    """In-process stand-in for Hermes' ambient profile signal."""

    def __init__(self):
        self.scope: dict[str, str] | None = None
        self.home = ""
        self.multiplex = True

    def install(self, values: dict[str, str], home: Path) -> None:
        self.scope = dict(values)
        self.home = str(home)

    def current_secret_scope(self):
        return self.scope

    def is_multiplex_active(self):
        return self.multiplex

    def get_secret(self, name, default=None):
        if self.scope is None:
            raise RuntimeError("unscoped secret read")
        return self.scope.get(name, default)


@pytest.fixture
def hermes(monkeypatch):
    """Install a fake Hermes host exposing the ambient profile signal."""
    runtime = _FakeHermes()
    agent = ModuleType("agent")
    secret_scope = ModuleType("agent.secret_scope")
    for name in ("current_secret_scope", "is_multiplex_active", "get_secret"):
        setattr(secret_scope, name, getattr(runtime, name))
    agent.secret_scope = secret_scope
    constants = ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: Path(runtime.home)
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)
    return runtime


@pytest.fixture(autouse=True)
def fake_zulip_sdk(monkeypatch):
    """Let the adapter construct without the real ``zulip`` SDK installed."""

    class _FakeSDK:
        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

    monkeypatch.setattr(adapter_module, "_import_zulip_sdk", lambda: _FakeSDK)
    return _FakeSDK


class _Config:
    """Minimal PlatformConfig stand-in with an empty ``extra``.

    Empty so every credential has to come from the profile, not the config.
    """

    extra: dict = {}


def _alice() -> dict[str, str]:
    return {
        "ZULIP_EMAIL": "alice@example.test",
        "ZULIP_API_KEY": "alice-key",
        "ZULIP_SITE": "https://alice.zulipchat.com",
    }


def _bob() -> dict[str, str]:
    return {
        "ZULIP_EMAIL": "bob@example.test",
        "ZULIP_API_KEY": "bob-key",
        "ZULIP_SITE": "https://bob.zulipchat.com",
    }


def test_two_profiles_resolve_their_own_credentials_and_data_dir(hermes, tmp_path):
    alice_home = tmp_path / "alice"
    bob_home = tmp_path / "bob"

    hermes.install(_alice(), alice_home)
    alice = ZulipAdapter(_Config())
    assert alice.email == "alice@example.test"
    assert alice.api_key == "alice-key"
    assert alice.site == "https://alice.zulipchat.com"
    assert alice._data_dir == str(alice_home)

    hermes.install(_bob(), bob_home)
    bob = ZulipAdapter(_Config())
    assert bob.email == "bob@example.test"
    assert bob.api_key == "bob-key"
    assert bob.site == "https://bob.zulipchat.com"
    assert bob._data_dir == str(bob_home)

    # Switching the active profile must not retroactively change alice.
    assert alice.email == "alice@example.test"
    assert alice._data_dir == str(alice_home)


def test_queue_dedupe_audit_and_policy_state_are_per_profile(hermes, tmp_path):
    alice_home = tmp_path / "alice"
    bob_home = tmp_path / "bob"

    hermes.install(_alice(), alice_home)
    alice = ZulipAdapter(_Config())

    hermes.install(_bob(), bob_home)
    bob = ZulipAdapter(_Config())

    assert alice._queue_mgr._data_dir == alice_home
    assert alice._dedupe._data_dir == alice_home
    assert alice._policy._data_dir == alice_home
    assert alice._audit_logger._log_dir == alice_home / "audit"

    assert bob._queue_mgr._data_dir == bob_home
    assert bob._dedupe._data_dir == bob_home
    assert bob._policy._data_dir == bob_home
    assert bob._audit_logger._log_dir == bob_home / "audit"

    # The persisted allowlist and audit log must never share a path.
    assert alice._policy._persistence_path() != bob._policy._persistence_path()
    assert alice._audit_logger._log_path != bob._audit_logger._log_path


def test_profile_without_zulip_config_does_not_inherit_the_process_env(
    hermes, tmp_path, monkeypatch
):
    # The process environment may belong to whichever profile loaded first.
    monkeypatch.setenv("ZULIP_EMAIL", "leaked@example.test")
    monkeypatch.setenv("ZULIP_API_KEY", "leaked-key")
    monkeypatch.setenv("ZULIP_SITE", "https://leaked.zulipchat.com")

    hermes.install({}, tmp_path / "empty")
    assert get_setting("ZULIP_EMAIL", "") == ""

    adapter = ZulipAdapter(_Config())
    assert adapter.email == ""
    assert adapter.api_key == ""
    assert adapter.site == ""
    assert adapter._data_dir == str(tmp_path / "empty")


def test_module_resolvers_follow_the_active_profile(hermes, tmp_path):
    hermes.install(
        {
            "ZULIP_EMAIL": "alice@example.test",
            "ZULIP_DM_POLICY": "allowlist",
            "ZULIP_ALLOWED_USERS": "alice@example.test",
            "ZULIP_REACTIONS_ENABLED": "false",
            "ZULIP_MEDIA_MAX_MB": "9",
        },
        tmp_path / "alice",
    )
    assert PolicyEngine().mode == "allowlist"
    assert PolicyEngine().can_dm("alice@example.test") is True
    assert ReactionConfig.from_env().enabled is False
    assert resolve_media_max_mb() == 9
    assert AccountResolver().resolve()[0].email == "alice@example.test"

    hermes.install({}, tmp_path / "bob")
    assert PolicyEngine().mode == "open"
    assert PolicyEngine().can_dm("alice@example.test") is True
    assert ReactionConfig.from_env().enabled is True
    assert resolve_media_max_mb() == DEFAULT_MAX_MB
    assert AccountResolver().resolve()[0].email == ""
