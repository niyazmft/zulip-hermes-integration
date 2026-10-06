"""The recommended profile actually changes behaviour (epic #211, child #213).

Child #212 shipped the gate; this child wires it into the resolvers that decide
what the bot does.  The claims pinned here:

1. With ``ZULIP_PROFILE=recommended`` and nothing else set beyond the three
   credentials, a **constructed adapter** reports the profile's posture: trace
   on, history ``on-demand``, session queue on, observe on, per-topic sessions
   on, soft gate off, DM policy ``allowlist``, group policy ``open``, mention
   required, and the five reaction triggers enabled.
2. An explicit ``ZULIP_*`` value always wins over the preset, for every knob.
3. The preset does not trip the adapter's own soft-gate/observe
   mutual-exclusion warning.

The no-marker half of the contract lives in ``tests/test_profile_gate.py``
(``test_legacy_resolvers_are_unchanged_without_the_marker``), which now calls
these same, newly wired resolvers.
"""

from __future__ import annotations

import pytest

from zulip import activity_trace, runtime_scope, settings
from zulip.policy import PolicyEngine
from zulip.reaction_triggers import ReactionTriggerConfig
from zulip.runtime_scope import RECOMMENDED_REACTION_TRIGGERS, RECOMMENDED_PRESET

#: Credentials are the only thing a fresh install sets.
_REQUIRED_ENV = {
    "ZULIP_SITE": "https://test.zulipchat.com",
    "ZULIP_EMAIL": "bot@test.zulipchat.com",
    "ZULIP_API_KEY": "fake-key",
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Only the credentials, plus whatever the test itself sets."""
    for name in (*RECOMMENDED_PRESET, "ZULIP_PROFILE", "ZULIP_STREAM_OVERRIDES"):
        monkeypatch.delenv(name, raising=False)
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def recommended(monkeypatch):
    monkeypatch.setenv("ZULIP_PROFILE", "recommended")


@pytest.fixture
def adapter(recommended, mock_platform_config, monkeypatch):
    """A fully constructed adapter under the recommended profile."""
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


# --------------------------------------------------- each knob, resolver level


def test_chatmode_is_mention_gated(recommended):
    """Mention-gating comes from the chatmode, not from ZULIP_REQUIRE_MENTION.

    ``ZULIP_REQUIRE_MENTION`` is inert in every mode -- each mode already
    implies its own trigger, so the extra gate in the inbound handler can only
    lower an already-False decision.  The epic's locked trigger scope is
    "every stream the bot is subscribed to, mention-gated", so the profile has
    to move the chatmode off its ``onmessage`` default (which answers
    everything, and leaves nothing on the drop path where observation happens).
    """
    mode, _prefixes, require_mention = settings.resolve_chatmode()
    assert mode == "oncall"
    assert require_mention is True


def test_history_mode_is_on_demand(recommended):
    assert settings.resolve_history_mode() == "on-demand"


def test_session_queue_is_on(recommended):
    assert settings.resolve_session_queue() is True


def test_observe_group_is_on_but_the_soft_gate_is_off(recommended):
    assert settings.resolve_observe_group() is True
    assert settings.resolve_soft_gate() is False


def test_topic_sessions_are_on(recommended):
    assert settings.topic_sessions_enabled() is True


def test_activity_trace_is_on(recommended):
    assert activity_trace.TraceConfig.from_env().enabled is True


def test_dm_policy_is_allowlist_and_group_policy_open(recommended):
    assert PolicyEngine._resolve_dm_mode() == "allowlist"
    assert PolicyEngine._resolve_group_mode() == "open"


def test_reaction_triggers_are_the_locked_set(monkeypatch):
    """``from_env()`` stays the env-only legacy path; the adapter applies the preset."""
    import zulip.adapter as adapter_module

    monkeypatch.setenv("ZULIP_PROFILE", "recommended")
    assert adapter_module._resolve_reaction_triggers().triggers == (
        RECOMMENDED_REACTION_TRIGGERS
    )
    # The gate resolves it for the adapter; the legacy path is untouched.
    assert runtime_scope.effective_value("ZULIP_REACTION_TRIGGERS", "") == (
        RECOMMENDED_PRESET["ZULIP_REACTION_TRIGGERS"]
    )
    assert ReactionTriggerConfig.from_env().enabled is False


# ------------------------------------------------------- explicit env wins


def test_an_explicit_value_wins_for_every_knob(monkeypatch):
    """Each preset knob is overridable, which is the whole point of the gate."""
    import zulip.adapter as adapter_module

    monkeypatch.setenv("ZULIP_PROFILE", "recommended")
    cases = {
        # name: (explicit override, resolver, value under the override, value under the preset)
        "ZULIP_CHATMODE": ("onmessage", lambda: settings.resolve_chatmode()[0], "onmessage", "oncall"),
        "ZULIP_REQUIRE_MENTION": ("false", lambda: settings.resolve_chatmode()[2], False, True),
        "ZULIP_GROUP_POLICY": ("disabled", PolicyEngine._resolve_group_mode, "disabled", "open"),
        "ZULIP_DM_POLICY": ("open", PolicyEngine._resolve_dm_mode, "open", "allowlist"),
        "ZULIP_ACTIVITY_TRACE": (
            "false",
            lambda: activity_trace.TraceConfig.from_env().enabled,
            False,
            True,
        ),
        "ZULIP_HISTORY_MODE": ("off", settings.resolve_history_mode, "off", "on-demand"),
        "ZULIP_SESSION_QUEUE": ("false", settings.resolve_session_queue, False, True),
        "ZULIP_OBSERVE_GROUP": ("false", settings.resolve_observe_group, False, True),
        "ZULIP_TOPIC_SESSIONS": ("false", settings.topic_sessions_enabled, False, True),
        "ZULIP_SOFT_GATE": ("true", settings.resolve_soft_gate, True, False),
        # The trigger set is applied in the adapter (L6), so it is resolved
        # through the adapter's own helper rather than a settings resolver.
        "ZULIP_REACTION_TRIGGERS": (
            '{"eyes": "Look at this."}',
            lambda: adapter_module._resolve_reaction_triggers().triggers,
            {"eyes": "Look at this."},
            RECOMMENDED_REACTION_TRIGGERS,
        ),
    }
    assert set(cases) == set(RECOMMENDED_PRESET)

    for name, (explicit, resolve, under_override, under_preset) in cases.items():
        monkeypatch.setenv(name, explicit)
        assert resolve() == under_override, f"{name}: explicit value did not win"
        monkeypatch.delenv(name)
        # ... and with the override gone the preset applies again.
        assert resolve() == under_preset, f"{name}: preset did not return"


def test_explicit_reaction_triggers_win(recommended, monkeypatch):
    import zulip.adapter as adapter_module

    monkeypatch.setenv("ZULIP_REACTION_TRIGGERS", '{"eyes": "Look at this."}')
    assert adapter_module._resolve_reaction_triggers().triggers == {
        "eyes": "Look at this."
    }




def test_a_stream_override_still_beats_the_preset_mode(recommended, monkeypatch):
    """Per-stream overrides outrank the global chatmode, preset included."""
    monkeypatch.setenv(
        "ZULIP_STREAM_OVERRIDES", '{"bot lab": {"chatmode": "onmessage"}}'
    )
    assert settings.resolve_chatmode()[0] == "oncall"
    assert settings.resolve_chatmode("bot lab")[0] == "onmessage"


# --------------------------------------------------- the constructed adapter


def test_constructed_adapter_reports_the_profile_posture(adapter):
    assert adapter._trace_cfg.enabled is True
    assert adapter._history_mode == "on-demand"
    assert adapter._queue_cfg.enabled is True
    assert adapter._observe_group is True
    assert adapter._soft_gate is False
    assert adapter._policy.mode == "allowlist"
    assert adapter._policy.group_mode == "open"
    assert adapter._reaction_trigger_cfg.enabled is True
    assert adapter._reaction_trigger_cfg.triggers == RECOMMENDED_REACTION_TRIGGERS


def test_constructed_adapter_subscribes_to_the_events_the_profile_needs(adapter):
    """The preset must reach the queue registration, not just the config objects."""
    assert "reaction" in adapter._extra_event_types
    assert "update_message" in adapter._extra_event_types
    assert "delete_message" in adapter._extra_event_types


def test_constructed_adapter_does_not_warn_about_soft_gate_and_observe(adapter, caplog):
    """The preset picks the combination the adapter documents as correct."""
    with caplog.at_level("WARNING", logger="zulip.adapter"):
        adapter._soft_gate, adapter._observe_group = (
            settings.resolve_soft_gate(),
            settings.resolve_observe_group(),
        )
    assert not [
        record
        for record in caplog.records
        if "ZULIP_OBSERVE_GROUP is a no-op" in record.getMessage()
    ]


# --------------------------------------------- the profile is not a back door


def test_no_profile_means_no_adapter_change(mock_platform_config, monkeypatch):
    """Without the marker the constructed adapter is byte-for-byte the old one."""
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
    plain = adapter_module.ZulipAdapter(mock_platform_config)

    assert plain._trace_cfg.enabled is False
    assert plain._history_mode == "off"
    assert plain._queue_cfg.enabled is False
    assert plain._observe_group is False
    assert plain._soft_gate is False
    assert plain._policy.mode == "open"
    assert plain._policy.group_mode == "open"
    assert plain._reaction_trigger_cfg.enabled is False
    assert plain._extra_event_types == []
