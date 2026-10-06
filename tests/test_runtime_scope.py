"""Regression coverage for profile-scoped Hermes runtime resolution.

Simulates two Hermes profiles served by one process and asserts that settings,
the data dir and any snapshot resolution are keyed by the active profile —
never by the process environment — with a deliberate standalone fallback.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from zulip.runtime_scope import (
    PROFILE_DATA_DIR_EXTRA_KEY,
    SCOPED_SETTINGS_EXTRA_KEY,
    get_profile_data_dir,
    get_setting,
    has_active_profile_scope,
    is_unscoped_multiplexer,
    should_snapshot_settings,
    snapshot_settings,
)


class _FakeHermes:
    """Small in-process stand-in for Hermes' ContextVar-facing API."""

    def __init__(self):
        self.scope: dict[str, str] | None = None
        self.home = ""
        self.multiplex = True

    def install(self, values: dict[str, str], home: Path) -> None:
        self.scope = dict(values)
        self.home = str(home)

    def clear(self) -> None:
        self.scope = None

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


@pytest.fixture
def standalone(monkeypatch):
    """Remove every Hermes module so the standalone path is exercised."""
    for name in ("agent", "agent.secret_scope", "hermes_constants"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return monkeypatch


def test_settings_resolve_through_the_active_profile(hermes, tmp_path):
    hermes.install(
        {"ZULIP_EMAIL": "alice@example.test", "ZULIP_API_KEY": "alice-key"},
        tmp_path / "alice",
    )
    assert get_setting("ZULIP_EMAIL") == "alice@example.test"

    hermes.install(
        {"ZULIP_EMAIL": "bob@example.test", "ZULIP_API_KEY": "bob-key"},
        tmp_path / "bob",
    )
    assert get_setting("ZULIP_EMAIL") == "bob@example.test"
    assert get_setting("ZULIP_API_KEY") == "bob-key"


def test_scoped_setting_miss_never_leaks_the_process_environment(
    hermes, tmp_path, monkeypatch
):
    # The process environment may belong to whichever profile loaded first.
    monkeypatch.setenv("ZULIP_API_KEY", "process-global-key")

    hermes.install({"ZULIP_EMAIL": "alice@example.test"}, tmp_path / "alice")
    assert get_setting("ZULIP_API_KEY", "") == ""

    hermes.install({"ZULIP_API_KEY": "bob-key"}, tmp_path / "bob")
    assert get_setting("ZULIP_API_KEY") == "bob-key"


def test_profile_data_dir_state_does_not_cross_read(hermes, tmp_path):
    alice, bob = tmp_path / "alice", tmp_path / "bob"

    hermes.install({}, alice)
    assert get_profile_data_dir() == str(alice)

    hermes.install({}, bob)
    assert get_profile_data_dir() == str(bob)

    # Switching back must observe alice's home, not a stale resolution.
    hermes.install({}, alice)
    assert get_profile_data_dir() == str(alice)


def test_profile_predicates_track_the_active_scope(hermes, tmp_path):
    hermes.install({"ZULIP_EMAIL": "alice@example.test"}, tmp_path / "alice")
    assert should_snapshot_settings() is True
    assert has_active_profile_scope() is True
    assert is_unscoped_multiplexer() is False

    # Multiplexing continues, but this call carries no profile scope.
    hermes.clear()
    hermes.multiplex = True
    assert should_snapshot_settings() is True
    assert has_active_profile_scope() is False
    assert is_unscoped_multiplexer() is True

    # No scope and no multiplexing: plain single-profile Hermes.
    hermes.multiplex = False
    assert should_snapshot_settings() is False
    assert has_active_profile_scope() is False
    assert is_unscoped_multiplexer() is False


def test_snapshot_settings_are_keyed_by_the_active_profile(hermes, tmp_path):
    names = ("ZULIP_EMAIL", "ZULIP_API_KEY")

    hermes.install(
        {"ZULIP_EMAIL": "alice@example.test", "ZULIP_API_KEY": "alice-key"},
        tmp_path / "alice",
    )
    alice = snapshot_settings(names)

    hermes.install(
        {"ZULIP_EMAIL": "bob@example.test", "ZULIP_API_KEY": "bob-key"},
        tmp_path / "bob",
    )
    bob = snapshot_settings(names)

    assert alice == {"ZULIP_EMAIL": "alice@example.test", "ZULIP_API_KEY": "alice-key"}
    assert bob == {"ZULIP_EMAIL": "bob@example.test", "ZULIP_API_KEY": "bob-key"}

    # A snapshot taken under a profile is immune to a later scope swap.
    hermes.install(
        {"ZULIP_EMAIL": "carol@example.test", "ZULIP_API_KEY": "carol-key"},
        tmp_path / "carol",
    )
    assert alice == {"ZULIP_EMAIL": "alice@example.test", "ZULIP_API_KEY": "alice-key"}


def test_standalone_fallback_is_deliberate_and_documented(
    standalone, tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZULIP_EMAIL", "standalone@example.test")

    assert get_profile_data_dir() == str(tmp_path / "data")
    assert get_setting("ZULIP_EMAIL") == "standalone@example.test"
    assert get_setting("ZULIP_MISSING", "fallback") == "fallback"
    assert should_snapshot_settings() is False
    assert has_active_profile_scope() is False
    assert is_unscoped_multiplexer() is False

    # No profile, no override: the documented legacy default applies.
    monkeypatch.delenv("HERMES_DATA_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert get_profile_data_dir() == str(tmp_path / "home" / ".hermes")

    # An empty override is treated as unset, never as the cwd.
    monkeypatch.setenv("HERMES_DATA_DIR", "")
    assert get_profile_data_dir() == str(tmp_path / "home" / ".hermes")


def test_the_suite_cannot_reach_the_real_hermes_home():
    """The isolation fixture keeps tests out of the developer's real home (#243).

    Without it, ``get_profile_data_dir()`` falls back to ``~/.hermes`` and the
    adapter writes audit logs, dedupe state and queue state there for real --
    invisibly, outside the repository, and possibly into a live install.
    """
    assert os.environ.get("HERMES_DATA_DIR"), (
        "HERMES_DATA_DIR should be pinned per test by the autouse fixture in "
        "tests/conftest.py; without it tests write into the real ~/.hermes"
    )
    assert get_profile_data_dir() != str(Path.home() / ".hermes"), (
        "a test resolved the developer's real ~/.hermes, so the isolation "
        "fixture is missing or was bypassed"
    )


def test_extra_keys_match_the_adapter_contract():
    assert SCOPED_SETTINGS_EXTRA_KEY == "_zulip_scoped_settings"
    assert PROFILE_DATA_DIR_EXTRA_KEY == "_zulip_profile_data_dir"
