"""The ``ZULIP_PROFILE`` preset gate (epic #211, child #212).

Two claims are pinned here:

1. **The preset works.**  ``ZULIP_PROFILE=recommended`` resolves its table for
   knobs that are not explicitly set, an explicit env value always wins, and the
   resolver reports which of ``env`` / ``profile`` / ``default`` supplied the
   value -- provenance child #216's ``zulip config`` depends on.

2. **Migration safety.**  With the marker absent the gate is inert and every
   existing resolver still returns exactly what it returned before the flag
   existed.  That is issue #153's contract, and it is asserted twice: the gate
   itself reports ``default`` for every preset knob, **and** the real legacy
   resolvers (settings, policy, trace, reaction triggers) are called and
   compared against their pre-flag values.  The second half stays meaningful
   once child #213 wires those resolvers through the gate: if a wiring ever
   consults the preset without the marker, this file goes red.
"""

from __future__ import annotations

import json

import pytest

from zulip import activity_trace, reaction_triggers, runtime_scope, settings
from zulip.policy import PolicyEngine
from zulip.runtime_scope import (
    KNOWN_PROFILES,
    PROFILE_RECOMMENDED,
    RECOMMENDED_PRESET,
    RECOMMENDED_REACTION_TRIGGERS,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SOURCE_PROFILE,
    active_profile,
    effective_value,
    preset_for_profile,
    resolve_setting,
)

#: Every knob the recommended profile decides, and the value it decides, as the
#: epic's locked-design table states them.  Spelled out rather than derived from
#: ``RECOMMENDED_PRESET`` so a silent edit to the table fails here.
EXPECTED_PRESET = {
    "ZULIP_REQUIRE_MENTION": "true",
    "ZULIP_GROUP_POLICY": "open",
    "ZULIP_DM_POLICY": "allowlist",
    "ZULIP_ACTIVITY_TRACE": "true",
    "ZULIP_HISTORY_MODE": "on-demand",
    "ZULIP_SESSION_QUEUE": "true",
    "ZULIP_OBSERVE_GROUP": "true",
    "ZULIP_TOPIC_SESSIONS": "true",
    "ZULIP_SOFT_GATE": "false",
    "ZULIP_REACTION_TRIGGERS": json.dumps(RECOMMENDED_REACTION_TRIGGERS),
}

#: The knobs the preset deliberately leaves alone.  Sticky engagement and
#: actionable refs stay off under the recommended profile.
NOT_IN_PRESET = (
    "ZULIP_ENGAGEMENT_MODE",
    "ZULIP_ENGAGEMENT_SCOPE",
    "ZULIP_ENGAGEMENT_TTL_MINUTES",
)


@pytest.fixture(autouse=True)
def _isolate_profile_env(monkeypatch):
    """No profile marker and no preset knob leaks in from the dev environment."""
    monkeypatch.setattr(runtime_scope, "_unknown_profiles_warned", set())
    monkeypatch.delenv("ZULIP_PROFILE", raising=False)
    for name in (*EXPECTED_PRESET, *NOT_IN_PRESET):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------- the preset


def test_preset_table_is_exactly_the_epic_table():
    assert RECOMMENDED_PRESET == EXPECTED_PRESET


def test_preset_leaves_sticky_engagement_and_refs_off():
    for name in NOT_IN_PRESET:
        assert name not in RECOMMENDED_PRESET


def test_recommended_reaction_triggers_are_the_locked_five():
    assert RECOMMENDED_REACTION_TRIGGERS == {
        "+1": "Proceed with the proposed step.",
        "repeat": "Try a different approach.",
        "arrows_counterclockwise": "Try a different approach.",
        "test_tube": "Write tests for this.",
        "question": "Explain your reasoning in more detail.",
    }
    # The preset value must round-trip through the parser child #213 hands it to.
    config = reaction_triggers.ReactionTriggerConfig.from_mapping(
        RECOMMENDED_PRESET["ZULIP_REACTION_TRIGGERS"]
    )
    assert config.triggers == RECOMMENDED_REACTION_TRIGGERS
    # Reactions remain limited to the bot's own messages; the *sender* is
    # unrestricted, since anyone who can mention the bot can already trigger it.
    assert config.any_message is False


def test_only_recommended_is_a_known_profile():
    assert KNOWN_PROFILES == frozenset({PROFILE_RECOMMENDED})
    assert preset_for_profile(None) is None
    assert preset_for_profile("nope") is None
    assert preset_for_profile(PROFILE_RECOMMENDED) is RECOMMENDED_PRESET


# ----------------------------------------------------- precedence and source


def test_profile_supplies_every_preset_knob(monkeypatch):
    monkeypatch.setenv("ZULIP_PROFILE", "recommended")
    assert active_profile() == PROFILE_RECOMMENDED
    for name, expected in EXPECTED_PRESET.items():
        resolved = resolve_setting(name)
        assert resolved.value == expected, name
        assert resolved.source == SOURCE_PROFILE, name


def test_profile_marker_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("ZULIP_PROFILE", "  Recommended  ")
    assert active_profile() == PROFILE_RECOMMENDED


def test_explicit_env_beats_the_preset(monkeypatch):
    monkeypatch.setenv("ZULIP_PROFILE", "recommended")
    monkeypatch.setenv("ZULIP_HISTORY_MODE", "always")
    monkeypatch.setenv("ZULIP_SOFT_GATE", "1")

    resolved = resolve_setting("ZULIP_HISTORY_MODE", "off")
    assert (resolved.value, resolved.source) == ("always", SOURCE_ENV)
    # The value is handed back byte-for-byte, not normalised: interpretation
    # stays with the owning resolver.
    assert resolve_setting("ZULIP_SOFT_GATE").value == "1"


def test_empty_env_var_is_not_explicit_and_yields_to_the_preset(monkeypatch):
    monkeypatch.setenv("ZULIP_PROFILE", "recommended")
    monkeypatch.setenv("ZULIP_HISTORY_MODE", "")

    resolved = resolve_setting("ZULIP_HISTORY_MODE", "off")
    assert (resolved.value, resolved.source) == ("on-demand", SOURCE_PROFILE)


def test_default_is_used_when_nothing_supplies_the_value(monkeypatch):
    monkeypatch.delenv("ZULIP_STREAMS", raising=False)
    resolved = resolve_setting("ZULIP_STREAMS", "*")
    assert (resolved.value, resolved.source) == ("*", SOURCE_DEFAULT)
    assert resolve_setting("ZULIP_STREAMS").value is None


def test_effective_value_matches_the_resolver(monkeypatch):
    assert effective_value("ZULIP_CHATMODE", "onmessage") == "onmessage"
    monkeypatch.setenv("ZULIP_PROFILE", "recommended")
    assert effective_value("ZULIP_DM_POLICY", "open") == "allowlist"


# -------------------------------------------------- unknown marker handling


def test_unknown_profile_warns_once_and_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("ZULIP_PROFILE", "recomended")

    with caplog.at_level("WARNING", logger="zulip.runtime_scope"):
        assert active_profile() is None
        resolved = resolve_setting("ZULIP_DM_POLICY", "open")
        assert (resolved.value, resolved.source) == ("open", SOURCE_DEFAULT)

    assert len(caplog.records) == 1
    assert "recomended" in caplog.records[0].getMessage()
    # Keyed by the offending value, so a second distinct typo is still reported,
    # while repeating the first one is not.
    monkeypatch.setenv("ZULIP_PROFILE", "recommendedd")
    with caplog.at_level("WARNING", logger="zulip.runtime_scope"):
        assert active_profile() is None
    assert len(caplog.records) == 2
    monkeypatch.setenv("ZULIP_PROFILE", "recomended")
    with caplog.at_level("WARNING", logger="zulip.runtime_scope"):
        assert active_profile() is None
    assert len(caplog.records) == 2


def test_blank_profile_marker_is_ignored_silently(monkeypatch, caplog):
    monkeypatch.setenv("ZULIP_PROFILE", "   ")
    with caplog.at_level("WARNING", logger="zulip.runtime_scope"):
        assert active_profile() is None
    assert caplog.records == []


# --------------------------------------------------------- migration safety


def test_preset_is_inert_without_the_marker(monkeypatch):
    """Marker absent => no knob resolves through the preset, ever."""
    for name in EXPECTED_PRESET:
        resolved = resolve_setting(name, "sentinel")
        assert (resolved.value, resolved.source) == ("sentinel", SOURCE_DEFAULT), name


def test_resolve_setting_matches_get_setting_without_the_marker(monkeypatch):
    """Without the marker the resolver is a strict superset of ``get_setting``."""
    monkeypatch.setenv("ZULIP_RESPONSE_PREFIX", "[bot] ")
    for name in (*EXPECTED_PRESET, "ZULIP_RESPONSE_PREFIX", "ZULIP_NEVER_SET_ANYWHERE"):
        expected = runtime_scope.get_setting(name, "fallback")
        resolved = resolve_setting(name, "fallback")
        assert resolved.value == expected, name
        if runtime_scope.get_setting(name, None) is not None:
            assert resolved.source == SOURCE_ENV, name
        else:
            assert resolved.source == SOURCE_DEFAULT, name


#: Each preset knob's pre-flag behaviour, taken from the resolver that owns it
#: rather than from a hardcoded constant -- so this table cannot drift from the
#: code it is asserting about.
LEGACY_RESOLVERS = {
    "ZULIP_REQUIRE_MENTION": (lambda: settings.resolve_chatmode()[2], True),
    "ZULIP_GROUP_POLICY": (PolicyEngine._resolve_group_mode, "open"),
    "ZULIP_DM_POLICY": (PolicyEngine._resolve_dm_mode, "open"),
    "ZULIP_ACTIVITY_TRACE": (lambda: activity_trace.TraceConfig.from_env().enabled, False),
    "ZULIP_HISTORY_MODE": (settings.resolve_history_mode, "off"),
    "ZULIP_SESSION_QUEUE": (settings.resolve_session_queue, False),
    "ZULIP_OBSERVE_GROUP": (settings.resolve_observe_group, False),
    "ZULIP_TOPIC_SESSIONS": (settings.topic_sessions_enabled, False),
    "ZULIP_SOFT_GATE": (settings.resolve_soft_gate, False),
    "ZULIP_REACTION_TRIGGERS": (
        lambda: reaction_triggers.ReactionTriggerConfig.from_env().enabled,
        False,
    ),
}


def test_legacy_snapshot_covers_every_preset_knob():
    assert set(LEGACY_RESOLVERS) == set(EXPECTED_PRESET)


@pytest.mark.parametrize("name", sorted(LEGACY_RESOLVERS))
def test_legacy_resolvers_are_unchanged_without_the_marker(name):
    resolve, expected = LEGACY_RESOLVERS[name]
    assert resolve() == expected, (
        f"{name} no longer resolves to its pre-flag value without ZULIP_PROFILE; "
        "the preset must not silently change behaviour on upgrade (#153)."
    )


@pytest.mark.parametrize("name", sorted(LEGACY_RESOLVERS))
def test_legacy_resolvers_are_unchanged_with_an_unknown_marker(name, monkeypatch):
    monkeypatch.setenv("ZULIP_PROFILE", "not-a-profile")
    resolve, expected = LEGACY_RESOLVERS[name]
    assert resolve() == expected, name
