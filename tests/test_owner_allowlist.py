"""Seed the DM allowlist from the Zulip bot owner (epic #211, child #214).

The recommended profile sets ``ZULIP_DM_POLICY=allowlist``, but a fresh install
has no address to allow-list.  So the owner is discovered from Zulip
(``GET /users/me`` -> ``bot_owner_id`` -> address), with ``ZULIP_OWNER_EMAIL`` as
the explicit escape hatch.

Three things are pinned here:

1. The discovery works and seeds exactly one address.
2. Every failure path is **loud and non-fatal**: the bot keeps connecting, and
   the warning names the setting that fixes it, because the alternative is an
   allowlist that is empty for a reason nobody can see.
3. It is scoped to the recommended profile. Without the marker an existing
   ``allowlist`` install must not have its allowlist widened on upgrade -- that
   would be a silent authorization change (epic #211's migration contract).
"""

from __future__ import annotations

import pytest

from zulip import runtime_scope
from zulip.policy import (
    OWNER_EMAIL_ENV,
    PolicyEngine,
    bot_owner_address,
    owner_email_from_user,
    owner_email_override,
)

OWNER = "owner@example.com"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """No profile, no owner override, and a data dir that is not the real ~/.hermes.

    The last one matters: seeding persists the allowlist, and the adapter derives
    its data dir from ``HERMES_DATA_DIR`` falling back to the operator's real
    Hermes home. Without this the suite would write into it.
    """
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    for name in (
        "ZULIP_PROFILE",
        OWNER_EMAIL_ENV,
        "ZULIP_DM_POLICY",
        "ZULIP_ALLOWED_USERS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def recommended(monkeypatch):
    monkeypatch.setenv("ZULIP_PROFILE", "recommended")


class FakeLookupClient:
    """A Zulip client whose single-user lookup is scriptable (#196's surface)."""

    def __init__(self, user=None, error=None):
        self._user = user
        self._error = error
        self.calls: list[object] = []

    def get_user_by_id(self, user_id):
        self.calls.append(user_id)
        if self._error is not None:
            raise self._error
        return {"result": "success", "user": self._user or {}}


# ------------------------------------------------------------- pure helpers


def test_owner_email_override_is_normalised(monkeypatch):
    assert owner_email_override() == ""
    monkeypatch.setenv(OWNER_EMAIL_ENV, "  Owner@Example.COM ")
    assert owner_email_override() == "owner@example.com"


@pytest.mark.parametrize(
    "profile,expected",
    [
        ({"bot_owner_id": 7}, "7"),
        ({"bot_owner_id": "7"}, "7"),
        ({"bot_owner": "owner@example.com"}, "owner@example.com"),
        ({"bot_owner_email": "owner@example.com", "bot_owner_id": 7}, "owner@example.com"),
        ({"bot_owner_id": None}, ""),
        ({"bot_owner_id": ""}, ""),
        ({}, ""),
        (None, ""),
        ("not-a-dict", ""),
    ],
)
def test_bot_owner_address(profile, expected):
    assert bot_owner_address(profile) == expected


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"result": "success", "user": {"email": "Owner@Example.com"}}, "owner@example.com"),
        ({"result": "success", "members": [{"email": "owner@example.com"}]}, "owner@example.com"),
        ({"result": "success", "user": [{"email": "owner@example.com"}]}, "owner@example.com"),
        ({"result": "success", "email": "owner@example.com"}, "owner@example.com"),
        ({"result": "error", "user": {"email": "owner@example.com"}}, ""),
        ({"result": "success", "user": {}}, ""),
        ({"result": "success", "user": {"email": "not-an-email"}}, ""),
        ("nonsense", ""),
        (None, ""),
    ],
)
def test_owner_email_from_user(result, expected):
    assert owner_email_from_user(result) == expected


# ------------------------------------------------------- the allowlist seed


def test_seed_dm_allowlist_adds_and_persists(tmp_path):
    engine = PolicyEngine(data_dir=str(tmp_path))
    assert engine.seed_dm_allowlist("Owner@Example.com") is True
    assert engine.can_dm("owner@example.com") is True
    # Idempotent, and still present after a reload from disk.
    assert engine.seed_dm_allowlist("owner@example.com") is False
    assert OWNER in PolicyEngine(data_dir=str(tmp_path)).allowlist


@pytest.mark.parametrize("junk", ["", "   ", "no-at-sign", "@example.com", "owner@"])
def test_seed_dm_allowlist_refuses_a_non_address(junk, tmp_path):
    """An entry that can never match a sender is worse than an absent one."""
    engine = PolicyEngine(data_dir=str(tmp_path))
    assert engine.seed_dm_allowlist(junk) is False
    assert engine.allowlist == set()


def test_explicit_owner_email_is_seeded_on_construction(monkeypatch, tmp_path):
    monkeypatch.setenv("ZULIP_DM_POLICY", "allowlist")
    monkeypatch.setenv(OWNER_EMAIL_ENV, "Owner@Example.com")
    engine = PolicyEngine(data_dir=str(tmp_path))
    assert engine.owner_email == OWNER
    assert engine.can_dm(OWNER) is True


def test_explicit_owner_email_is_honoured_without_the_profile(monkeypatch, tmp_path):
    """Naming an address is an explicit instruction; the profile is not required."""
    assert runtime_scope.active_profile() is None
    monkeypatch.setenv("ZULIP_DM_POLICY", "allowlist")
    monkeypatch.setenv(OWNER_EMAIL_ENV, OWNER)
    assert PolicyEngine(data_dir=str(tmp_path)).can_dm(OWNER) is True


# ------------------------------------------------- who the lookup runs for


def test_lookup_is_needed_under_the_recommended_profile(recommended, tmp_path):
    engine = PolicyEngine(data_dir=str(tmp_path))
    assert engine.mode == "allowlist"  # the preset
    assert engine.needs_bot_owner_lookup() is True


def test_no_lookup_without_the_profile(monkeypatch, tmp_path):
    """An existing allowlist install must not be widened on upgrade."""
    monkeypatch.setenv("ZULIP_DM_POLICY", "allowlist")
    monkeypatch.setenv("ZULIP_ALLOWED_USERS", "alice@example.com")
    engine = PolicyEngine(data_dir=str(tmp_path))
    assert engine.needs_bot_owner_lookup() is False


def test_no_lookup_when_the_owner_is_already_named(recommended, monkeypatch, tmp_path):
    monkeypatch.setenv(OWNER_EMAIL_ENV, OWNER)
    assert PolicyEngine(data_dir=str(tmp_path)).needs_bot_owner_lookup() is False


def test_no_lookup_when_the_policy_ignores_the_allowlist(recommended, monkeypatch, tmp_path):
    monkeypatch.setenv("ZULIP_DM_POLICY", "open")
    assert PolicyEngine(data_dir=str(tmp_path)).needs_bot_owner_lookup() is False


# ------------------------------------------------------- the adapter's path


@pytest.fixture
def make_adapter(mock_platform_config, monkeypatch):
    def _make():
        import zulip.adapter as adapter_module
        import zulip.zulip_client as zulip_client_module

        monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

        from tests.conftest import MockZulipClient

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = MockZulipClient(**kwargs)

                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
        return adapter_module.ZulipAdapter(mock_platform_config)

    return _make


@pytest.mark.asyncio
async def test_adapter_seeds_the_owner_from_the_profile_payload(recommended, make_adapter, caplog):
    adapter = make_adapter()
    profile = {"result": "success", "full_name": "Bot", "bot_owner_id": 42}
    adapter.client = FakeLookupClient(user={"email": OWNER})

    with caplog.at_level("INFO"):
        await adapter.seed_bot_owner_dm_allowlist(profile)

    assert adapter._policy.can_dm(OWNER) is True
    assert adapter.client.calls == [42]
    # The address is logged masked, never raw.
    assert OWNER not in "\n".join(record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_adapter_accepts_an_inlined_owner_address(recommended, make_adapter):
    """An address in the payload needs no second round-trip at all."""
    adapter = make_adapter()
    adapter.client = FakeLookupClient(user={"email": OWNER})

    await adapter.seed_bot_owner_dm_allowlist(
        {"result": "success", "bot_owner": OWNER}
    )

    assert adapter._policy.can_dm(OWNER) is True
    assert adapter.client.calls == []


@pytest.mark.asyncio
async def test_adapter_warns_actionably_when_there_is_no_owner(recommended, make_adapter, caplog):
    adapter = make_adapter()
    adapter.client = FakeLookupClient(user={"email": OWNER})

    with caplog.at_level("WARNING", logger="zulip.policy"):
        await adapter.seed_bot_owner_dm_allowlist({"result": "success"})

    assert adapter._policy.allowlist == set()
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert OWNER_EMAIL_ENV in messages
    assert adapter.client.calls == []


@pytest.mark.asyncio
async def test_adapter_survives_a_failing_lookup(recommended, make_adapter, caplog):
    """A DM-only misconfiguration must not take the bot offline."""
    adapter = make_adapter()
    adapter.client = FakeLookupClient(error=RuntimeError("zulip exploded"))

    with caplog.at_level("WARNING"):
        await adapter.seed_bot_owner_dm_allowlist(
            {"result": "success", "bot_owner_id": 42}
        )

    assert adapter._policy.allowlist == set()
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "bot-owner lookup failed" in messages
    assert OWNER_EMAIL_ENV in messages


@pytest.mark.asyncio
async def test_adapter_warns_when_the_sdk_cannot_look_users_up(recommended, make_adapter, caplog):
    """The unmodified MockZulipClient exposes no supported lookup (#196)."""
    adapter = make_adapter()

    with caplog.at_level("WARNING", logger="zulip.policy"):
        await adapter.seed_bot_owner_dm_allowlist(
            {"result": "success", "bot_owner_id": 42}
        )

    assert adapter._policy.allowlist == set()
    assert OWNER_EMAIL_ENV in "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_adapter_does_not_look_up_without_the_profile(monkeypatch, make_adapter):
    monkeypatch.setenv("ZULIP_DM_POLICY", "allowlist")
    monkeypatch.setenv("ZULIP_ALLOWED_USERS", "alice@example.com")
    adapter = make_adapter()
    adapter.client = FakeLookupClient(user={"email": OWNER})

    await adapter.seed_bot_owner_dm_allowlist(
        {"result": "success", "bot_owner_id": 42}
    )

    assert adapter.client.calls == []
    assert adapter._policy.allowlist == {"alice@example.com"}
    assert adapter._policy.can_dm(OWNER) is False
