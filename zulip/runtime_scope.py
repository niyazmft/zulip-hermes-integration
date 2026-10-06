"""Resolve Zulip settings and state through the active Hermes profile.

Under ``gateway.multiplex_profiles`` a single gateway process serves several
Hermes profiles.  Anything process-global — the environment, a fixed data
directory, an unkeyed cache — belongs to whichever profile happened to load
first, so profile A's allowlist, credentials or dedupe state can gate profile
B's traffic and the leak is silent.  These helpers instead prefer the ambient
profile signal Hermes exposes (the context-local secret scope and
``hermes_constants.get_hermes_home``) so each profile resolves its own values.

The Zulip plugin also runs standalone, so imports of Hermes internals stay
lazy.  When no profile signal is detectable the fallback is deliberate and
documented, not implicit:

* settings: ``os.getenv`` — the pre-multiplexing behaviour, unchanged;
* data dir: ``HERMES_DATA_DIR``, then the legacy ``~/.hermes`` home.

This module keeps no process-global resolution cache.  Every read resolves
against the ambient profile signal, because a cache that is not keyed by the
active profile is exactly how one profile ends up serving another's values.
Callers that need values stable for an adapter's lifetime use
:func:`snapshot_settings`, which is stored per profile by the adapter.

The same module also owns the *preset profile* gate (epic #211):
``ZULIP_PROFILE=recommended`` names a default posture, and
:func:`resolve_setting` resolves one knob as **explicit env > preset > built-in
default**, reporting which layer supplied the value.  The preset is a table of
values for knobs that are not explicitly set; it never rewrites a code default
and it is inert unless the marker is present, so an install without
``ZULIP_PROFILE`` behaves exactly as it did before the flag existed (issue
#153's contract).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


#: Extra-config keys the adapter uses to carry a profile-scoped settings
#: snapshot and data dir between construction and the gateway's config load.
SCOPED_SETTINGS_EXTRA_KEY = "_zulip_scoped_settings"
PROFILE_DATA_DIR_EXTRA_KEY = "_zulip_profile_data_dir"

#: Legacy Hermes home, used only when Hermes exposes no profile home and
#: ``HERMES_DATA_DIR`` is unset.  Kept identical to the pre-multiplexing
#: directory so standalone installs do not move their existing state.
LEGACY_HERMES_HOME = ".hermes"


# ---------------------------------------------------------------------------
# Preset profile gate (epic #211)
# ---------------------------------------------------------------------------

#: The only ``ZULIP_PROFILE`` value that names a preset today.  A profile is a
#: *named default posture*, not a second configuration file: it supplies values
#: for knobs that are not explicitly set, and nothing else.
PROFILE_RECOMMENDED = "recommended"

#: Values of ``ZULIP_PROFILE`` that name a preset.  Anything else is a typo as
#: far as this module is concerned: it is warned about once and then ignored, so
#: the install keeps the built-in defaults rather than a posture nobody chose.
KNOWN_PROFILES = frozenset({PROFILE_RECOMMENDED})

#: Provenance labels returned by :func:`resolve_setting` and consumed by
#: ``zulip config`` (child #216) so a value's origin is inspectable.
SOURCE_ENV = "env"
SOURCE_PROFILE = "profile"
SOURCE_DEFAULT = "default"

#: The reaction-trigger set the recommended profile turns on.  Two spellings
#: share the meaning of "try something else" so a wrong emoji guess degrades
#: instead of silently never firing; it adds no semantics to learn.
RECOMMENDED_REACTION_TRIGGERS: dict[str, str] = {
    "+1": "Proceed with the proposed step.",
    "repeat": "Try a different approach.",
    "arrows_counterclockwise": "Try a different approach.",
    "test_tube": "Write tests for this.",
    "question": "Explain your reasoning in more detail.",
}

#: ``ZULIP_PROFILE=recommended``: the locked posture from epic #211, as values
#: for the knobs that differ from the built-in defaults.  Sticky engagement and
#: actionable refs are deliberately absent -- the preset does not turn them on.
#:
#: The soft-gate/observe pair is a unit: ``ZULIP_SOFT_GATE`` dispatches every
#: stream message, which leaves nothing on the drop path where observation
#: happens, so observe would become a no-op.  The preset therefore picks observe
#: on and soft gate off.  ``zulip config`` must not present the two as
#: independently togglable without that explanation.
RECOMMENDED_PRESET: dict[str, str] = {
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

#: Presets by profile name, so a second profile is a table entry rather than a
#: branch in the resolver.
_PROFILES: dict[str, dict[str, str]] = {
    PROFILE_RECOMMENDED: RECOMMENDED_PRESET,
}

#: ``ZULIP_PROFILE`` values already warned about.  This is the module's only
#: piece of process-global state and it caches no resolved setting -- it exists
#: solely so a typo does not re-log on every resolution.  It is keyed by the
#: offending value, so two profiles misspelling the marker are both reported.
_unknown_profiles_warned: set[str] = set()

#: Distinguishes "the env var is absent" from "the env var is present and
#: empty".  Only the former is missing: an empty value is present, but counts as
#: *not explicitly set* for preset purposes, which is how the existing resolvers
#: already treat it.
_ENV_ABSENT: object = object()


@dataclass(frozen=True)
class ResolvedSetting:
    """A resolved setting together with the layer that supplied it.

    ``source`` is one of :data:`SOURCE_ENV`, :data:`SOURCE_PROFILE` or
    :data:`SOURCE_DEFAULT`.  It exists for ``zulip config`` (child #216), which
    has to answer "what did it decide for me?" for every knob.
    """

    name: str
    value: Optional[str]
    source: str


def active_profile() -> Optional[str]:
    """Return the active preset profile name, or ``None`` when none applies.

    Unset, empty and unrecognised values all mean "no preset", which is what
    keeps an install without the marker on the pre-flag code path.  An
    unrecognised value warns once per distinct value, because silently ignoring
    ``ZULIP_PROFILE=recomended`` would look identical to a preset that does
    nothing.
    """
    raw = (get_setting("ZULIP_PROFILE", "") or "").strip().lower()
    if not raw:
        return None
    if raw in KNOWN_PROFILES:
        return raw
    if raw not in _unknown_profiles_warned:
        _unknown_profiles_warned.add(raw)
        logger.warning(
            "ZULIP_PROFILE=%r is not a known profile (%s); ignoring it and "
            "using the built-in defaults",
            raw,
            ", ".join(sorted(KNOWN_PROFILES)),
        )
    return None


def preset_for_profile(profile: Optional[str]) -> Optional[dict[str, str]]:
    """Return the preset table for ``profile``, or ``None`` if it has none."""
    if profile is None:
        return None
    return _PROFILES.get(profile)


def resolve_setting(name: str, default: Optional[str] = None) -> ResolvedSetting:
    """Resolve one setting as **explicit env > preset > built-in default**.

    This is the single place the preset profile is applied; feature resolvers
    opt in by calling it (child #213) rather than by reading the environment
    themselves, so precedence is stated once and testable once.

    An env var that is present but empty is *not* explicit: it yields to the
    preset exactly as an unset one does.  When no preset applies, the returned
    value is the env value byte-for-byte, so this is a strict superset of
    :func:`get_setting` and the migration contract (marker absent => unchanged
    behaviour) holds by construction.
    """
    raw = get_setting(name, _ENV_ABSENT)
    if raw is not _ENV_ABSENT and raw:
        return ResolvedSetting(name, raw, SOURCE_ENV)

    preset = preset_for_profile(active_profile())
    if preset is not None and name in preset:
        return ResolvedSetting(name, preset[name], SOURCE_PROFILE)

    if raw is not _ENV_ABSENT:
        # Present but empty, and no preset claimed it: hand back exactly what
        # the caller would have read before this gate existed.
        return ResolvedSetting(name, raw, SOURCE_ENV)
    return ResolvedSetting(name, default, SOURCE_DEFAULT)


def effective_value(name: str, default: Optional[str] = None) -> Optional[str]:
    """Value-only form of :func:`resolve_setting` for call sites that do not
    need to display provenance.

    Same resolution, same precedence -- it is not a second policy.
    """
    return resolve_setting(name, default).value


def _secret_scope_module():
    """Return Hermes' secret scope module when this is running inside Hermes."""
    try:
        from agent import secret_scope
    except ImportError:
        return None
    return secret_scope


def _hermes_home() -> Optional[str]:
    """Return Hermes' context-local profile home, or None outside Hermes.

    ``get_hermes_home`` observes the profile the gateway is currently serving,
    unlike ``HERMES_DATA_DIR`` which is process-global.
    """
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return None
    return str(get_hermes_home())


def get_setting(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve a Zulip setting without leaking across Hermes profiles.

    Outside Hermes this is exactly ``os.getenv``.  Inside Hermes, use its
    scoped resolver whenever a profile scope is installed (and let its
    fail-closed guard reject unscoped reads while multiplexing is active), so
    a missing setting never falls back to the process environment, which may
    belong to a different profile.
    """
    scope = _secret_scope_module()
    if scope is not None:
        if scope.current_secret_scope() is not None or scope.is_multiplex_active():
            return scope.get_secret(name, default)
    return os.getenv(name, default)


def should_snapshot_settings() -> bool:
    """Whether construction is happening under Hermes profile isolation."""
    scope = _secret_scope_module()
    return bool(
        scope is not None
        and (scope.current_secret_scope() is not None or scope.is_multiplex_active())
    )


def has_active_profile_scope() -> bool:
    """Return whether a Hermes secret scope makes profile data authoritative."""
    scope = _secret_scope_module()
    return bool(scope is not None and scope.current_secret_scope() is not None)


def is_unscoped_multiplexer() -> bool:
    """Whether Hermes is multiplexing but this call has no active profile."""
    scope = _secret_scope_module()
    return bool(
        scope is not None
        and scope.is_multiplex_active()
        and scope.current_secret_scope() is None
    )


def get_profile_data_dir() -> str:
    """Return the active profile's Hermes home, with documented fallbacks.

    The context-local profile home wins when Hermes provides one, because it
    is per profile.  ``HERMES_DATA_DIR`` is process-global and therefore only
    consulted when there is no profile (standalone use); when even that is
    unset the legacy ``~/.hermes`` home is used, deliberately, so standalone
    installs keep their existing directory.
    """
    home = _hermes_home()
    if home is not None:
        return home
    return os.getenv("HERMES_DATA_DIR") or str(Path.home() / LEGACY_HERMES_HOME)


def snapshot_settings(names: tuple[str, ...]) -> dict[str, str]:
    """Resolve settings once so an adapter cannot observe a later scope swap."""
    return {name: get_setting(name, "") or "" for name in names}
