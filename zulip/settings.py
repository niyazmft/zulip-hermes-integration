"""Environment and configuration resolution for the Zulip adapter.

Every environment setting the adapter reads is resolved here into typed,
validated values, together with the module-level defaults and pacing
constants those resolvers fall back to. The module is pure: it reads the
process environment only through :mod:`zulip.runtime_scope` -- and, for the
knobs the recommended profile decides, through that module's preset gate, so
``ZULIP_PROFILE=recommended`` can supply a value for a knob the operator did not
set (epic #211, child #213).
"""

from __future__ import annotations

import json
import logging
from typing import Any, overload

from . import runtime_scope
from .text_utils import resolve_onchar_prefixes

logger = logging.getLogger(__name__)


def _preset_value(name: str, default: str = "") -> str:
    """Read a setting through the preset gate (epic #211, child #213).

    An explicit env value still wins and an install with no marker is
    unaffected -- ``effective_value`` is a strict superset of
    ``runtime_scope.get_setting`` -- so this is a change of *source*, never of
    precedence.

    The parsing at each call site below is deliberately left alone, because
    unifying it would change what an unrecognised value means:
    ``ZULIP_REQUIRE_MENTION=maybe`` is truthy today (its test is "not one of the
    falsey spellings"), while ``ZULIP_SOFT_GATE=maybe`` is false. Those are
    documented quirks, and the migration contract forbids quietly fixing them
    here.
    """
    return runtime_scope.effective_value(name, default) or ""

# Max input string length to prevent DoS via huge query strings
MAX_INPUT_LENGTH = 10000
_MAX_JSON_OVERRIDES_BYTES = 10240  # 10KB max for ZULIP_STREAM_OVERRIDES

# Chunking defaults (overridable via env)
DEFAULT_CHUNK_LIMIT = 10000  # Hermes registry max_message_length
DEFAULT_CHUNK_MODE = "length"

# Hard cap on a single outbound message, applied before chunking. Mirrors the
# sibling OpenClaw plugin's ``maxMessageLength`` (default 20000): downstream
# consumers (e.g. memory plugins) can fail on very long content. 0 disables.
DEFAULT_MAX_MESSAGE_LENGTH = 20000

# Timeout defaults (seconds) — Issue #62
DEFAULT_CONNECT_TIMEOUT = 30.0
DEFAULT_READ_TIMEOUT = 60.0
DEFAULT_SEND_TIMEOUT = 90.0

# --- Event polling (Issue #146) ---
#
# A /events long-poll that returns faster than this did not hold the poll, so
# the server is answering immediately and re-polling at full speed would spin
# (and, on the constrained hosts this runs on, fill the log with poll noise).
# A response at or above it was really held, meaning the server is pacing us,
# so it must gain no added delay.
POLL_FAST_RETURN_SECONDS = 2.5
POLL_BACKOFF_START = 1.0
POLL_BACKOFF_MAX = 5.0
# Zulip documents ``event_queue_longpoll_timeout_seconds`` as 1-90s, and only
# returns the field when /register asks for ``fetch_event_types: ["realm"]``.
LONGPOLL_MIN_SECONDS = 1.0
LONGPOLL_MAX_SECONDS = 90.0
# Headroom over the server's own budget, so our client-side abort can never
# pre-empt a healthy long-poll.
LONGPOLL_GRACE_SECONDS = 10.0

#: The soft-gate/observe conflict, stated once (epic #211).
#:
#: ``ZULIP_SOFT_GATE`` dispatches every monitored stream message, so nothing ever
#: reaches the drop path where observation happens and ``ZULIP_OBSERVE_GROUP``
#: silently does nothing. The adapter logs this at startup and ``zulip config``
#: reports it, and both read the *same* string: prose duplicated between a log
#: line and a CLI would drift, and the one that drifted would be the one nobody
#: was reading.
SOFT_GATE_OBSERVE_CONFLICT = (
    "ZULIP_SOFT_GATE and ZULIP_OBSERVE_GROUP are both enabled -- the soft gate "
    "dispatches every message, so nothing reaches the drop path where "
    "observation happens and ZULIP_OBSERVE_GROUP is a no-op. Turn the soft gate "
    "off to observe stream traffic without replying."
)

# Parsed ZULIP_STREAM_OVERRIDES, keyed on the raw environment string so the
# value stays live-reloadable while a busy stream does not re-parse JSON on
# every inbound message.
_stream_overrides_cache: tuple[str, dict[str, dict[str, Any]]] = ("", {})


def resolve_max_message_length() -> int:
    """Read the hard outbound message-length cap from the environment.

    ``ZULIP_MAX_MESSAGE_LENGTH`` (default 20000, ``0`` disables). Applied in
    :meth:`ZulipAdapter.send` and :func:`_standalone_send` before chunking.
    """
    raw = runtime_scope.get_setting("ZULIP_MAX_MESSAGE_LENGTH", "").strip()
    if not raw:
        return DEFAULT_MAX_MESSAGE_LENGTH
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_MESSAGE_LENGTH


def resolve_chunk_config() -> tuple[int, str]:
    """Read chunking config from environment."""
    limit_raw = runtime_scope.get_setting("ZULIP_TEXT_CHUNK_LIMIT", "").strip()
    limit = int(limit_raw) if limit_raw.isdigit() else DEFAULT_CHUNK_LIMIT
    mode = runtime_scope.get_setting("ZULIP_CHUNK_MODE", DEFAULT_CHUNK_MODE).strip()
    if mode not in ("length", "newline"):
        mode = DEFAULT_CHUNK_MODE
    return limit, mode


def resolve_timeouts() -> tuple[float, float, float]:
    """Read timeout config from environment.

    Returns (connect_timeout, read_timeout, send_timeout) in seconds.
    """
    def _parse(val: str, default: float) -> float:
        try:
            return float(val.strip())
        except (ValueError, AttributeError):
            return default

    connect = _parse(runtime_scope.get_setting("ZULIP_CONNECT_TIMEOUT", ""), DEFAULT_CONNECT_TIMEOUT)
    read = _parse(runtime_scope.get_setting("ZULIP_READ_TIMEOUT", ""), DEFAULT_READ_TIMEOUT)
    send = _parse(runtime_scope.get_setting("ZULIP_SEND_TIMEOUT", ""), DEFAULT_SEND_TIMEOUT)
    return connect, read, send


def clamp_longpoll_budget(value: Any) -> float | None:
    """Clamp a server long-poll budget to Zulip's documented 1-90s window.

    Returns ``None`` when the value is missing or unusable, which leaves the
    caller's configured timeout in place rather than guessing. (Issue #146)
    """
    try:
        budget = float(value)
    except (TypeError, ValueError):
        return None
    if budget <= 0:
        return None
    return min(max(budget, LONGPOLL_MIN_SECONDS), LONGPOLL_MAX_SECONDS)


def next_poll_backoff(elapsed: float, had_message: bool, current: float) -> float:
    """Return the delay before the next /events poll (0.0 = poll at once).

    Latency-gated (Issue #146): the question is not "were there events?" but
    "did the server hold the long-poll?". A poll that came back quickly did
    not, so the loop has to insert its own delay or it will spin as fast as
    the API answers; a poll that was genuinely held is already paced by the
    server and must not gain any latency.
    """
    if had_message or elapsed >= POLL_FAST_RETURN_SECONDS:
        return 0.0
    return min(max(current * 2, POLL_BACKOFF_START), POLL_BACKOFF_MAX)


def resolve_streams_filter() -> set[str] | None:
    """Read stream filtering config from environment.

    Returns None if all streams are allowed (default), or a set of
    lowercase stream names to monitor.
    """
    raw = runtime_scope.get_setting("ZULIP_STREAMS", "").strip()
    if not raw or raw == "*":
        return None
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def resolve_response_prefix() -> str:
    """Read outbound response prefix from environment."""
    return runtime_scope.get_setting("ZULIP_RESPONSE_PREFIX", "")


def resolve_stream_overrides() -> dict[str, dict[str, Any]]:
    """Read per-stream trigger overrides from the environment.

    ``ZULIP_STREAM_OVERRIDES`` is a JSON object mapping stream name to a
    settings object, overriding ``ZULIP_CHATMODE`` for that stream::

        ZULIP_STREAM_OVERRIDES='{
          "bot lab":       {"chatmode": "onmessage"},
          "team: general": {"chatmode": "oncall"}
        }'

    Only ``chatmode`` is supported. ``requireMention`` is deliberately not
    overridable: in the current gate it is inert in every mode.

    JSON is used rather than delimited pairs because Zulip stream names may
    legitimately contain both colons and commas.

    Stream names and setting keys are both matched case-insensitively.
    Unrecognised setting keys are warned about. Malformed configuration is
    logged and ignored rather than raised.
    """
    raw = runtime_scope.get_setting("ZULIP_STREAM_OVERRIDES", "").strip()
    if len(raw.encode("utf-8")) > _MAX_JSON_OVERRIDES_BYTES:
        logger.warning(
            "ZULIP_STREAM_OVERRIDES exceeds max size (%d > %d bytes); ignoring overrides",
            len(raw.encode("utf-8")),
            _MAX_JSON_OVERRIDES_BYTES,
        )
        return _remember({})
    cached_raw, cached = _stream_overrides_cache
    if raw == cached_raw:
        return cached

    def _remember(value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        global _stream_overrides_cache
        _stream_overrides_cache = (raw, value)
        return value

    if not raw:
        return _remember({})

    try:
        parsed = json.loads(raw)
    except ValueError:
        logger.warning("ZULIP_STREAM_OVERRIDES is not valid JSON; ignoring overrides")
        return _remember({})

    if not isinstance(parsed, dict):
        logger.warning(
            "ZULIP_STREAM_OVERRIDES must be a JSON object mapping stream name "
            "to a settings object; ignoring overrides"
        )
        return _remember({})

    overrides: dict[str, dict[str, Any]] = {}
    for name, settings in parsed.items():
        if not isinstance(settings, dict):
            logger.warning(
                "ZULIP_STREAM_OVERRIDES[%r] must be an object, e.g. "
                '{"chatmode": "onmessage"}; ignoring entry',
                name,
            )
            continue

        entry: dict[str, Any] = {}

        # Setting keys are matched case-insensitively
        normalised = {str(k).strip().lower(): v for k, v in settings.items()}

        # Warn about unrecognised keys
        unknown = sorted(
            k for k in normalised
            if k not in ("chatmode", "requiremention", "require_mention")
        )
        if unknown:
            logger.warning(
                "ZULIP_STREAM_OVERRIDES[%r]: ignoring unrecognised key(s) %s; "
                "the only supported key is 'chatmode'",
                name, ", ".join(unknown),
            )

        mode = normalised.get("chatmode")
        if mode is not None:
            mode = str(mode).strip().lower()
            if mode in ("onmessage", "oncall", "onchar"):
                entry["chatmode"] = mode
            else:
                logger.warning(
                    "ZULIP_STREAM_OVERRIDES[%r].chatmode=%r is not one of "
                    "onmessage/oncall/onchar; ignoring it",
                    name, mode,
                )

        if entry:
            overrides[str(name).strip().lower()] = entry

    return _remember(overrides)


@overload
def resolve_chatmode() -> tuple[str, list[str], bool]:
    ...


@overload
def resolve_chatmode(stream_name: str) -> tuple[str, list[str], bool]:
    ...


def resolve_chatmode(stream_name: str | None = None) -> tuple[str, list[str], bool]:
    """Read stream trigger mode config from environment.

    When ``stream_name`` is supplied, a matching entry in
    ``ZULIP_STREAM_OVERRIDES`` takes precedence over the global
    ``ZULIP_CHATMODE`` for that stream only.
    """
    mode = _preset_value("ZULIP_CHATMODE", "onmessage").strip().lower()
    if mode not in ("onmessage", "oncall", "onchar"):
        mode = "onmessage"
    prefixes = resolve_onchar_prefixes(runtime_scope.get_setting("ZULIP_ONCHAR_PREFIXES", ""))
    require_mention = _preset_value("ZULIP_REQUIRE_MENTION", "true").strip().lower() not in ("false", "0", "no", "off")

    if stream_name:
        override = resolve_stream_overrides().get(stream_name.strip().lower())
        if override:
            mode = override.get("chatmode", mode)

    return mode, prefixes, require_mention


def resolve_soft_gate() -> bool:
    """Whether monitored stream messages are all dispatched (issue #153).

    Off by default. When on, a stream message is dispatched like
    ``ZULIP_CHATMODE=onmessage`` but its metadata carries ``addressed=False``
    unless the bot was mentioned or an onchar prefix fired, so the agent can
    watch a busy stream without answering all of it.
    """
    return _preset_value("ZULIP_SOFT_GATE").strip().lower() in (
        "true", "1", "yes", "on",
    )


def resolve_observe_group() -> bool:
    """Whether non-addressed stream messages are recorded as topic context.

    Off by default (issue #153). Observed messages are kept in a bounded
    in-adapter buffer and never dispatched; the buffer is prepended to the
    agent-facing text the next time the bot is addressed in the same topic.
    """
    return _preset_value("ZULIP_OBSERVE_GROUP").strip().lower() in (
        "true", "1", "yes", "on",
    )


def resolve_session_queue() -> bool:
    """Whether an inbound message waits for its session's run in flight (#151).

    Off by default. When on, a message arriving while its topic (or DM) is
    mid-run joins that session's FIFO instead of being steered into the running
    turn, so one person in a shared topic cannot redirect another's work.
    Separate sessions are untouched and still run in parallel.
    """
    return _preset_value("ZULIP_SESSION_QUEUE").strip().lower() in (
        "true", "1", "yes", "on",
    )


def resolve_history_mode() -> str:
    """How much real stream/topic history is quoted into the prompt (#148).

    ``off`` (default) never harvests, so no extra Zulip round-trip is made;
    ``on-demand`` harvests only for a message that reads like a "do we already
    know this?" question; ``always`` harvests for every inbound stream message.
    Anything unrecognised falls back to ``off`` with a warning.
    """
    raw = _preset_value("ZULIP_HISTORY_MODE").strip().lower()
    if raw in ("off", "on-demand", "always"):
        return raw
    if raw:
        logger.warning(
            "ZULIP_HISTORY_MODE=%r is not one of off/on-demand/always; using off",
            raw,
        )
    return "off"


#: What an install promises when an exec approval is never answered (#222).
#: ``allow`` is the pre-existing behaviour (the gateway's own outcome stands and
#: the bot adds nothing); ``deny`` is the fail-closed statement — the bot says in
#: the room that the request was refused and records ``timeout`` as the decider.
APPROVAL_ON_TIMEOUT_ALLOW = "allow"
APPROVAL_ON_TIMEOUT_DENY = "deny"
_APPROVAL_ON_TIMEOUT_VALUES = frozenset(
    {APPROVAL_ON_TIMEOUT_ALLOW, APPROVAL_ON_TIMEOUT_DENY}
)


def resolve_approval_on_timeout() -> str:
    """What silence means for an unanswered exec approval (#222).

    The **gateway owns the timeout**: it refuses an unanswered approval on every
    host this plugin supports (0.18.2 through the current release), and a plugin
    cannot pre-answer, veto or extend that wait. So this key does not decide
    whether silence runs a command — nothing can. It decides what the install
    *promises* about silence, and therefore what the bot says and records:

    * ``allow`` *(default)* — today's behaviour exactly. Nothing is added to the
      room; the gateway's own outcome stands. This is the migration contract:
      an install that never heard of the key behaves as it always did.
    * ``deny`` — the refusal is stated and audited: one line in the prompt's
      route where the host posts none, and an audit entry naming ``timeout`` as
      the decider. The recommended profile selects this.

    Read at call time through the preset gate, so an explicit ``ZULIP_*`` value
    wins, ``ZULIP_PROFILE=recommended`` can supply ``deny``, and a patch of this
    resolver still takes effect. Anything unrecognised falls back to ``allow``
    with a warning — never to the strict value, because a typo must not change
    what the install promises.
    """
    raw = _preset_value("ZULIP_APPROVAL_ON_TIMEOUT", APPROVAL_ON_TIMEOUT_ALLOW)
    mode = raw.strip().lower()
    if mode in _APPROVAL_ON_TIMEOUT_VALUES:
        return mode
    logger.warning(
        "ZULIP_APPROVAL_ON_TIMEOUT=%r is not one of %s; using %s",
        raw,
        sorted(_APPROVAL_ON_TIMEOUT_VALUES),
        APPROVAL_ON_TIMEOUT_ALLOW,
    )
    return APPROVAL_ON_TIMEOUT_ALLOW


def resolve_int_setting(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive integer setting, warning and falling back when invalid."""
    raw = (runtime_scope.get_setting(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value < minimum:
        logger.warning(
            "%s=%d is below the minimum %d; using %d", name, value, minimum, default
        )
        return default
    return value


def topic_sessions_enabled() -> bool:
    """Whether each Zulip topic should get its own conversation session.

    Off by default. When enabled, the topic is passed to ``build_source`` as
    ``thread_id``, which is what Hermes scopes session state by — so each topic
    in a stream becomes an independent conversation instead of all topics
    sharing one.

    This is opt-in because turning it on splits an existing stream's history
    into per-topic sessions, which changes what an agent remembers.
    """
    return _preset_value("ZULIP_TOPIC_SESSIONS").strip().lower() in ("true", "1", "yes", "on")
