"""Zulip SDK handle, connection caches and address parsing.

This module owns the plugin's single connection to the ``zulip`` SDK: the
lazily-resolved SDK handle, the LRU client/target caches, and the pure
address-parsing helpers shared by the send and receive paths. Keeping them
here gives the mutable SDK state exactly one owner, so a test that patches
``ZULIP_AVAILABLE`` or the ``zulip`` handle patches the module the code reads.

Layering: L0 — depends only on the standard library, never on another package
module.
"""

from __future__ import annotations

import importlib
import logging
import sys
from collections import OrderedDict
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Whether the real ``zulip`` SDK has been resolved. Kept mutable because a
# missing SDK is a supported state — the adapter degrades rather than failing
# to import. This module owns the value; patch ``zulip_client``, not ``adapter``.
ZULIP_AVAILABLE = False

# Module-level SDK handle — updated by import_zulip_sdk().
zulip = None  # type: ignore

# This package is itself named ``zulip``, so ``import zulip`` can resolve back
# to us instead of to the real SDK. Own entry file, used to detect that case.
_PLUGIN_PACKAGE_INIT = Path(__file__).resolve().parent / "__init__.py"

# ------------------------------------------------------------------
# Performance: client + target caching (Issue #49)
# ------------------------------------------------------------------
_MAX_CLIENT_CACHE = 50
_MAX_TARGET_CACHE = 500

_client_cache: OrderedDict[str, Any] = OrderedDict()
_target_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()


def get_cached_client(site: str, email: str, api_key: str, *, _zulip_mod: Any = None) -> Any:
    """Return a cached Zulip client or create a new one.

    LRU eviction keeps the most-recently-used clients.
    """
    key = f"{site}\x00{email}\x00{api_key}"
    client = _client_cache.pop(key, None)
    if client is not None:
        _client_cache[key] = client
        return client

    _zulip = _zulip_mod or import_zulip_sdk()
    if _zulip is None:
        raise ImportError("zulip package not installed")

    client = _zulip.Client(email=email, api_key=api_key, site=site)

    # Configure connection pooling for the client's requests session.
    # This reuses TCP connections across API calls, reducing latency.
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        if hasattr(client, "ensure_session"):
            client.ensure_session()
        if hasattr(client, "session") and client.session is not None:
            # Pool up to 10 connections per host, with retry on transient errors
            retry_strategy = Retry(
                total=2,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
            )
            adapter = HTTPAdapter(
                pool_connections=10,
                pool_maxsize=20,
                max_retries=retry_strategy,
            )
            client.session.mount("https://", adapter)
            client.session.mount("http://", adapter)
    except ImportError:
        pass  # requests not available; use default session

    if len(_client_cache) >= _MAX_CLIENT_CACHE:
        oldest = next(iter(_client_cache))
        del _client_cache[oldest]

    _client_cache[key] = client
    return client


def _get_cached_target(chat_id: str) -> dict[str, Any] | None:
    """Return cached target info or None.

    Target info: {"type": "dm", "user_ids": list[int]} |
    {"type": "stream", "stream_id": int}
    """
    info = _target_cache.get(chat_id)
    if info is not None:
        # Move to end (most-recently-used)
        del _target_cache[chat_id]
        _target_cache[chat_id] = info
    return info


def _set_cached_target(chat_id: str, info: dict[str, Any]) -> None:
    """Cache parsed target info with LRU eviction."""
    if chat_id in _target_cache:
        del _target_cache[chat_id]

    if len(_target_cache) >= _MAX_TARGET_CACHE:
        oldest = next(iter(_target_cache))
        del _target_cache[oldest]

    _target_cache[chat_id] = info


def parse_target(chat_id: str) -> dict[str, Any]:
    """Parse chat_id into target info, using cache if available.

    The memo in ``_target_cache`` is keyed on the ``chat_id`` string alone and
    is therefore safe only because this function is a *pure function of that
    string*: the result carries no profile-, account- or connection-dependent
    field. Adding one would let a reply resolved under one profile be served
    from cache to another, so keep it pure.
    """
    cached = _get_cached_target(chat_id)
    if cached is not None:
        return cached

    if chat_id.startswith("dm:"):
        # Session-scoped DM chat_ids include a `:session:N` suffix (e.g.
        # `dm:1032616:session:1`). Strip everything after the recipient list
        # so the send path resolves the correct target. (Issue #111)
        #
        # A group DM carries every recipient, comma-separated
        # (`dm:7,42,99`). Replying with only the sender would start a new
        # one-to-one DM instead of continuing the group conversation, so the
        # complete set is part of the address. (Issue #154)
        recipients = chat_id[3:].split(":", 1)[0]
        user_ids = [int(part) for part in recipients.split(",") if part.strip()]
        if not user_ids:
            raise ValueError("DM target must include at least one recipient")
        info = {"type": "dm", "user_ids": user_ids}
    else:
        info = {"type": "stream", "stream_id": int(chat_id)}

    _set_cached_target(chat_id, info)
    return info


def private_recipient_ids(message: dict[str, Any]) -> list[int]:
    """Return every recipient of an incoming private message, sorted.

    Zulip represents a direct message's ``display_recipient`` as the list of
    participants, the bot included. A one-to-one DM therefore has two entries
    and a group DM has more. Preserving the complete set is what keeps a reply
    inside the original conversation instead of starting a new one-to-one DM
    with just the sender. (Issue #154)

    Older or malformed events may omit the recipient list, so fall back to the
    sender's id. That reproduces the previous behaviour for one-to-one DMs.
    """
    recipient_ids: set[int] = set()

    recipients = message.get("display_recipient")
    if isinstance(recipients, list):
        for recipient in recipients:
            if not isinstance(recipient, dict):
                continue
            try:
                recipient_ids.add(int(recipient["id"]))
            except (KeyError, TypeError, ValueError):
                continue

    if not recipient_ids:
        try:
            recipient_ids.add(int(message["sender_id"]))
        except (KeyError, TypeError, ValueError):
            pass

    return sorted(recipient_ids)


def private_chat_id(message: dict[str, Any]) -> str:
    """Encode a private message's recipient set as the adapter chat id.

    The address has to be self-describing: replies are routed by chat id, so
    the recipient set must survive persistence and a gateway restart.
    """
    recipient_ids = private_recipient_ids(message)
    if not recipient_ids:
        raise ValueError("Private message has no usable recipient IDs")
    return "dm:" + ",".join(str(user_id) for user_id in recipient_ids)


def clear_caches() -> None:
    """Clear all caches. Used by tests and for resource cleanup."""
    _client_cache.clear()
    _target_cache.clear()


def _self_shadowing_path_entries() -> list[tuple[int, str]]:
    """``(index, entry)`` pairs where ``zulip`` resolves back to THIS plugin.

    Hermes puts the plugin's install directory on ``sys.path``, and this package
    is named ``zulip``, so any entry whose ``zulip/__init__.py`` is our own file
    shadows the real SDK. Indices are captured so the original ``sys.path``
    order can be restored exactly -- import precedence depends on it.
    """
    shadowing: list[tuple[int, str]] = []
    for index, entry in enumerate(sys.path):
        try:
            candidate = (Path(entry) if entry else Path.cwd()) / "zulip" / "__init__.py"
            if candidate.is_file() and candidate.resolve() == _PLUGIN_PACKAGE_INIT:
                shadowing.append((index, entry))
        except OSError:
            # Unreadable or unresolvable path entry: cannot be a shadow source.
            continue
    return shadowing


def _resolved_to_this_package(module: Any) -> bool:
    """True when ``module`` is this plugin rather than the real SDK."""
    origin = getattr(module, "__file__", None)
    if not origin:
        return False
    try:
        return Path(origin).resolve() == _PLUGIN_PACKAGE_INIT
    except OSError:
        return False


def import_zulip_sdk() -> Any:
    """Lazily import the real ``zulip`` SDK, bypassing this plugin's shadow.

    This package is named ``zulip`` and its install directory sits on
    ``sys.path``, so a plain ``import zulip`` resolves back to *us*. Dropping
    ``sys.modules["zulip"]`` alone does not help: the name is then re-resolved
    from ``sys.path`` and lands on this same package again. The shadowing path
    entries must be removed for the duration of the import so the real SDK is
    the only remaining candidate.

    Returns the SDK module, or ``None`` when it is genuinely absent. A missing
    SDK is a supported state: the adapter degrades rather than failing to
    import.
    """
    global ZULIP_AVAILABLE, zulip
    if ZULIP_AVAILABLE and zulip is not None:
        return zulip

    shadowed = sys.modules.pop("zulip", None)
    removed = _self_shadowing_path_entries()
    try:
        # Delete high indices first so the remaining ones stay valid.
        for index, _entry in sorted(removed, reverse=True):
            del sys.path[index]
        importlib.invalidate_caches()
        sdk = importlib.import_module("zulip")
    except ImportError:
        zulip = None
        ZULIP_AVAILABLE = False
        return None
    finally:
        # Put every entry back at its original index, then restore the package
        # binding other importers expect. sys.path ordering is import
        # precedence, so this must be exact, not merely set-equal.
        for index, entry in sorted(removed):
            sys.path.insert(index, entry)
        if shadowed is not None:
            sys.modules["zulip"] = shadowed

    if _resolved_to_this_package(sdk):
        # Shadow removal did not take (the plugin is still reachable, e.g. via
        # a .pth or an installed egg-link). Treating the plugin as its own SDK
        # would be far worse than reporting the SDK as absent.
        logger.warning(
            "resolved 'zulip' to this plugin package instead of the SDK; "
            "treating the SDK as unavailable"
        )
        zulip = None
        ZULIP_AVAILABLE = False
        return None

    zulip = sdk
    ZULIP_AVAILABLE = True
    return sdk


def user_lookup_call(
    client: Any, user_id_or_email: Any
) -> tuple[Callable[..., Any] | None, tuple[Any, ...]]:
    """Resolve ``(callable, args)`` for a single-user lookup.

    The ``zulip`` SDK exposes no single consistent method for this: 0.9.1 has
    ``get_user_by_id`` and ``call_endpoint`` but **no** ``get_user``, while some
    other builds do provide ``get_user`` (issue #196). ``GET /users/{value}``
    accepts either a numeric id or an email address, so the raw endpoint covers
    the email case that ``get_user_by_id`` cannot.
    """
    raw = str(user_id_or_email or "").strip()
    if not raw:
        return None, ()
    by_id = getattr(client, "get_user_by_id", None)
    if raw.isdigit() and callable(by_id):
        return by_id, (int(raw),)
    endpoint = getattr(client, "call_endpoint", None)
    if callable(endpoint):
        return partial(endpoint, url=f"users/{raw}", method="GET"), ()
    legacy = getattr(client, "get_user", None)
    if callable(legacy):
        return legacy, (raw,)
    return None, ()
