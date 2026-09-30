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
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


#: Extra-config keys the adapter uses to carry a profile-scoped settings
#: snapshot and data dir between construction and the gateway's config load.
SCOPED_SETTINGS_EXTRA_KEY = "_zulip_scoped_settings"
PROFILE_DATA_DIR_EXTRA_KEY = "_zulip_profile_data_dir"

#: Legacy Hermes home, used only when Hermes exposes no profile home and
#: ``HERMES_DATA_DIR`` is unset.  Kept identical to the pre-multiplexing
#: directory so standalone installs do not move their existing state.
LEGACY_HERMES_HOME = ".hermes"


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
