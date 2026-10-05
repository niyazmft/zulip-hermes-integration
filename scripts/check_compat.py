#!/usr/bin/env python3
"""Real-host compatibility gate (issue #141).

``tests/stubs/`` exports whatever we wrote there, so it can never notice that a
real Hermes gateway lacks a symbol or changed a contract. This harness loads
the plugin against an *actual* Hermes install on ``sys.path`` and asserts every
gateway symbol, registration kwarg, and behavioural contract the plugin
depends on — failing with a message that names the missing symbol and the host
version.

It is deliberately self-contained (no pytest, no repo fixtures) so it can run
straight from the ``compat`` workflow. It is *not* part of the fast unit lane:
it needs a real Hermes install, which public PyPI may not provide at or above
``__min_hermes__``.

Two kinds of assertion, and the difference matters:

* **Requirements** — a missing one is fatal (exit 1): the module symbols the
  plugin imports unguarded, the ``thread_id`` reply-routing contract, and the
  registration kwargs. Their absence breaks the plugin or silently misroutes
  replies.
* **Capabilities** — reported, never fatal: the version-gated exec-approval
  symbols and the metadata-aware stop-typing helpers. The plugin guards these
  and degrades, which is the whole point of ``__min_hermes__`` being a *floor*
  rather than a "fully featured" version. See ``GATED_BASE_METHODS`` for why
  their absence is not an incompatibility.

Separately, the host version itself is checked against ``__min_hermes__``: a
host *below* the declared floor is a failure, so the floor is asserted in both
directions rather than being a number nothing reads.

Usage
-----
    python scripts/check_compat.py
        Fail (exit 2) if no Hermes host is importable.

    HERMES_COMPAT_PATH=/path/to/hermes python scripts/check_compat.py
        Prepend a Hermes checkout / site-packages dir to ``sys.path`` first
        (``os.pathsep``-separated for more than one).

    HERMES_COMPAT_SKIP=1 python scripts/check_compat.py
        Explicitly skip when no host can be installed: print why and exit 0.

Exit codes
----------
0  host present and compatible, or an explicit skip with no host
1  host present but a required symbol / contract is missing, or the host is
   below ``__min_hermes__`` (see stderr)
2  no host importable and HERMES_COMPAT_SKIP was not set
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "zulip" / "version.py"

# Symbols the plugin imports without a guard, or that the base class must
# provide for registration: their absence is an immediate incompatibility.
REQUIRED_MODULE_SYMBOLS: dict[str, tuple[str, ...]] = {
    "gateway.platforms.base": (
        "BasePlatformAdapter",
        "MessageEvent",
        "MessageType",
        "SendResult",
    ),
    "gateway.config": ("Platform", "PlatformConfig"),
}

# The reply-routing contract (#143): the gateway puts the session origin's
# topic in metadata["thread_id"]. A rename/drop here silently misroutes every
# reply with no error and no log, so it is asserted *behaviourally*.
THREAD_METADATA_ATTR = "_thread_metadata_for_source"

# Metadata-aware stop-typing helpers (#144). These are *capabilities*, not
# requirements, because the plugin does not need them to work — they only make
# typing more precise, and the host is what consumes them:
#
#   * Below 0.19.0 there is no ``_stop_typing_with_metadata`` at all, so the host
#     calls ``stop_typing(chat_id)`` positionally. The plugin's hook takes
#     ``metadata=None`` and clears typing on the stream's last-seen topic.
#   * 0.19.0 - 0.21.2 have ``_stop_typing_with_metadata`` but not the
#     ``_accepts_kwarg`` introspection helper this harness (and the
#     ``tests/stubs`` mirror) is written against, so whether the host forwards
#     metadata is not something we can verify from here.
#   * 0.21.3+ have both, and the run's own topic is used.
#
# Either way stop-typing is a best-effort path (``zulip/outbound.py`` swallows
# send failures) whose fallback is covered by
# ``tests/test_topic_routing.py::test_stop_typing_without_metadata_still_works``.
# Treating this as fatal is what made the declared 0.18.2 floor look false (#230).
GATED_BASE_METHODS = ("_stop_typing_with_metadata", "_accepts_kwarg")

# Version-gated (>= 0.21.3) symbols. The plugin guards these, so their absence
# is not fatal — but when present their contract must hold, and the harness
# reports which capabilities the host offers.
GATED_EXEC_APPROVAL_SYMBOLS = (
    "ExecApprovalPrompt",
    "ProcessingOutcome",
    "_send_exec_approval_prompt",
    "supports_exec_approval_buttons",
)

# The registration kwargs the plugin passes; a typo or a dropped kwarg here is
# as fatal as a missing symbol.
EXPECTED_REGISTER_KWARGS = {
    "name": "zulip",
    "allowed_users_env": "ZULIP_ALLOWED_USERS",
    "allow_all_env": "ZULIP_ALLOW_ALL_USERS",
    "cron_deliver_env_var": "ZULIP_HOME_CHANNEL",
}
REQUIRED_REGISTER_KEYS = (
    "adapter_factory",
    "check_fn",
    "validate_config",
    "required_env",
    "standalone_sender_fn",
    "max_message_length",
)


def _read_min_hermes() -> str:
    """``__min_hermes__`` from zulip/version.py, without importing the plugin."""
    try:
        tree = ast.parse(VERSION_FILE.read_text())
    except OSError:
        return "unknown"
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__min_hermes__":
                    value = node.value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        return value.value
    return "unknown"


def _host_version() -> str:
    for dist in ("hermes-agent", "hermes"):
        try:
            return importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            continue
        except Exception:
            continue
    try:
        import gateway  # type: ignore

        version = getattr(gateway, "__version__", None)
        if version:
            return str(version)
    except Exception:
        pass
    return "unknown"


def _skip_requested() -> bool:
    raw = os.environ.get("HERMES_COMPAT_SKIP", "").strip().lower()
    return raw not in ("", "0", "false", "no", "off")


def _prepend_host_path() -> None:
    raw = os.environ.get("HERMES_COMPAT_PATH", "").strip()
    if not raw:
        return
    for entry in reversed(raw.split(os.pathsep)):
        entry = entry.strip()
        if entry and Path(entry).exists():
            sys.path.insert(0, entry)


def _register_repo_on_path() -> None:
    repo = str(REPO_ROOT)
    if repo not in sys.path:
        sys.path.insert(0, repo)


class _CaptureCtx:
    """Minimal plugin context: records registration, tolerates the rest."""

    def __init__(self) -> None:
        self.platform_calls: list[dict] = []
        self.hook_calls: list[tuple] = []
        self.tool_calls: list[dict] = []

    def register_platform(self, **kwargs):
        self.platform_calls.append(kwargs)

    def register_hook(self, *args, **kwargs):
        self.hook_calls.append((args, kwargs))

    def register_tool(self, **kwargs):
        self.tool_calls.append(kwargs)


def _synthetic_source(topic: str):
    """A Zulip stream source carrying ``topic`` as its session origin."""
    source_cls = None
    try:
        source_cls = importlib.import_module(
            "gateway.platforms.base"
        ).MessageSource
    except Exception:
        source_cls = None

    if source_cls is not None:
        for kwargs in (
            {"chat_id": "573423", "chat_type": "stream", "thread_id": topic},
            {"chat_type": "stream"},
            {},
        ):
            try:
                source = source_cls(**kwargs)
            except Exception:  # noqa: BLE001 - try the next shape
                continue
            try:
                source.thread_id = topic
            except Exception:
                pass
            return source

    class _Source:
        pass

    source = _Source()
    source.chat_id = "573423"
    source.chat_type = "stream"
    source.thread_id = topic
    return source


def _missing_required_symbols() -> list[str]:
    missing: list[str] = []
    for module_name, names in REQUIRED_MODULE_SYMBOLS.items():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - report the import error verbatim
            missing.append(f"{module_name} (import failed: {exc})")
            continue
        for name in names:
            if not hasattr(module, name):
                missing.append(f"{module_name}.{name}")
    return missing


def _check_thread_metadata_contract() -> list[str]:
    try:
        base = importlib.import_module("gateway.platforms.base")
    except Exception as exc:  # noqa: BLE001
        return [f"gateway.platforms.base import failed: {exc}"]

    helper = getattr(base, THREAD_METADATA_ATTR, None)
    if helper is None:
        return [
            f"gateway.platforms.base.{THREAD_METADATA_ATTR} is missing; the "
            "plugin routes replies by the metadata thread_id it returns, so "
            "replies would silently land in the wrong topic (#143)."
        ]

    topic = "compat-probe-topic"
    try:
        metadata = helper(_synthetic_source(topic))
    except Exception as exc:  # noqa: BLE001
        return [
            f"gateway.platforms.base.{THREAD_METADATA_ATTR} raised "
            f"{exc!r} for a synthetic stream source."
        ]

    if not isinstance(metadata, dict) or metadata.get("thread_id") != topic:
        return [
            f"gateway.platforms.base.{THREAD_METADATA_ATTR} no longer puts the "
            f"origin topic in metadata['thread_id'] (got {metadata!r} for topic "
            f"{topic!r}); replies would silently misroute (#143)."
        ]
    return []


def _report_gated_base_methods() -> None:
    """Print the metadata-aware stop-typing capabilities. Never fatal (#230)."""
    try:
        base = importlib.import_module("gateway.platforms.base")
    except Exception:  # noqa: BLE001
        return
    adapter_cls = getattr(base, "BasePlatformAdapter", None)
    if adapter_cls is None:
        return
    for name in GATED_BASE_METHODS:
        if hasattr(adapter_cls, name):
            print(f"  gated hook BasePlatformAdapter.{name}: present")
        else:
            print(
                f"  gated hook BasePlatformAdapter.{name}: absent "
                "(typing falls back to the stream's last-seen topic)"
            )


def _parse_version(raw: str) -> tuple[int, ...] | None:
    """Numeric parts of a version string, or None when unparseable.

    Deliberately dependency-free (no ``packaging``): this harness runs before
    the plugin's own dependencies are guaranteed to be installed.
    """
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or None


def _check_host_meets_floor() -> list[str]:
    """Fail when the host is below the declared ``__min_hermes__``.

    The capability checks cannot assert the floor — by design they pass on old
    hosts. Without this, ``__min_hermes__`` is a number that nothing reads and
    the claim can drift either way: it read 0.18.2 while the harness failed
    every host below 0.21.3 (#230), and nothing would have noticed the reverse.
    """
    floor = _read_min_hermes()
    host = _host_version()
    parsed_host = _parse_version(host)
    parsed_floor = _parse_version(floor)
    if parsed_host is None or parsed_floor is None:
        # An unparseable host version is not evidence of an unsupported host:
        # HERMES_COMPAT_PATH pointed at a bare checkout reports "unknown".
        return []
    if parsed_host >= parsed_floor:
        return []
    return [
        f"host Hermes {host} is below the declared floor "
        f"__min_hermes__={floor}: the plugin does not claim to support it, so "
        "this run proves nothing. Test at or above the floor, or lower "
        "__min_hermes__ in zulip/version.py."
    ]


def _check_registration(adapter) -> list[str]:
    ctx = _CaptureCtx()
    try:
        adapter.register(ctx)
    except Exception as exc:  # noqa: BLE001
        return [f"zulip.register(ctx) raised {exc!r} against this host"]

    failures: list[str] = []
    if len(ctx.platform_calls) != 1:
        failures.append(
            "zulip.register() must register exactly one platform entry; "
            f"got {len(ctx.platform_calls)}"
        )
        return failures

    kwargs = ctx.platform_calls[0]
    for key, expected in EXPECTED_REGISTER_KWARGS.items():
        if key not in kwargs:
            failures.append(f"register_platform() is missing the {key!r} kwarg")
        elif kwargs[key] != expected:
            failures.append(
                f"register_platform({key}=...) is {kwargs[key]!r}, expected {expected!r}"
            )
    for key in REQUIRED_REGISTER_KEYS:
        if key not in kwargs:
            failures.append(f"register_platform() is missing the {key!r} kwarg")
        elif kwargs[key] is None:
            failures.append(f"register_platform({key}=...) must not be None")
    return failures


def _report_gated_symbols() -> None:
    try:
        base = importlib.import_module("gateway.platforms.base")
    except Exception:  # noqa: BLE001
        return
    for name in GATED_EXEC_APPROVAL_SYMBOLS:
        present = hasattr(base, name) or hasattr(
            getattr(base, "BasePlatformAdapter", object), name
        )
        print(f"  gated symbol {name}: {'present' if present else 'absent (guarded)'}")

    prompt = getattr(base, "ExecApprovalPrompt", None)
    if prompt is not None:
        fields = getattr(prompt, "__dataclass_fields__", {})
        has_metadata = "metadata" in fields or hasattr(prompt, "metadata")
        print(
            f"  ExecApprovalPrompt.metadata: {'present' if has_metadata else 'MISSING'}"
        )


def _print_failure(lines: list[str]) -> None:
    host = _host_version()
    minimum = _read_min_hermes()
    print(
        f"check_compat: FAIL — Hermes {host} (plugin floor __min_hermes__={minimum})",
        file=sys.stderr,
    )
    for line in lines:
        print(f"  - {line}", file=sys.stderr)
    print(
        "  Fix the host ref (HERMES_COMPAT_PATH, or the ref a `compat` CI "
        "leg pins) or the plugin, then re-run.",
        file=sys.stderr,
    )


def main() -> int:
    _prepend_host_path()
    _register_repo_on_path()

    try:
        import gateway  # type: ignore  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        if _skip_requested():
            print(
                "check_compat: SKIP — no Hermes host importable and "
                "HERMES_COMPAT_SKIP is set."
            )
            print(f"  import gateway failed: {exc}")
            return 0
        print(
            "check_compat: FAIL — no Hermes gateway importable on sys.path.",
            file=sys.stderr,
        )
        print(f"  import gateway failed: {exc}", file=sys.stderr)
        print(
            "  Install a Hermes host, point HERMES_COMPAT_PATH at one, or set "
            "HERMES_COMPAT_SKIP=1 to skip explicitly.",
            file=sys.stderr,
        )
        return 2

    print(f"check_compat: host Hermes {_host_version()} detected", flush=True)

    floor_failures = _check_host_meets_floor()
    if floor_failures:
        _print_failure(floor_failures)
        return 1

    try:
        from zulip import adapter
    except Exception as exc:  # noqa: BLE001
        _print_failure([f"the plugin failed to import against this host: {exc!r}"])
        return 1

    failures: list[str] = []
    missing = _missing_required_symbols()
    if missing:
        failures.append("missing required gateway symbol(s): " + ", ".join(missing))
    failures.extend(_check_thread_metadata_contract())
    failures.extend(_check_registration(adapter))

    if failures:
        _print_failure(failures)
        return 1

    print("check_compat: plugin registration surface verified")
    _report_gated_base_methods()
    _report_gated_symbols()
    print("check_compat: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
