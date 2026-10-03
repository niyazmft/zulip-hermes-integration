"""
Zulip Platform Adapter for Hermes Gateway (Plugin)

Bi-directional integration with Zulip chat platform.
Supports stream messages (with topics) and private messages.
"""

import asyncio
import logging
import re
import weakref
from pathlib import Path
from typing import Optional, Any

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
)

# Native exec-approval buttons (ExecApprovalPrompt + _send_exec_approval_prompt)
# first shipped in Hermes 0.21.3. Import the type defensively so the adapter
# still loads on older gateways: there the gateway never calls the hook and
# keeps using its plain-text approval prompt, instead of the whole plugin
# failing to import.
try:
    from gateway.platforms.base import ExecApprovalPrompt
except ImportError:  # pragma: no cover - gateways < 0.21.3
    ExecApprovalPrompt = Any  # type: ignore[assignment,misc]

# Processing lifecycle hooks (on_processing_start / on_processing_complete) and
# their outcome enum first shipped alongside ExecApprovalPrompt. Guarded for the
# same reason: on an older gateway the hooks are simply never called, so the
# activity trace does not run — rather than the whole plugin failing to import.
# (epic #139 / issue #158)
try:
    from gateway.platforms.base import ProcessingOutcome
except ImportError:  # pragma: no cover - gateways without lifecycle hooks
    ProcessingOutcome = None  # type: ignore[assignment,misc]

from gateway.config import Platform, PlatformConfig

# Use relative imports for internal modules so the plugin works
# regardless of how Hermes loads it (bundled, user path, etc.)
from .logger import mask_pii
from .text_utils import strip_html_to_text
from .display_names import DisplayNameCache
from .queue_manager import ZulipQueueManager
from .dedupe_store import ZulipDedupeStore
from .reactions import ReactionConfig
from .reaction_triggers import (
    ReactionTriggerConfig,
    build_reaction_trigger_message,
    find_unsubscribed_streams,
    is_eligible_target_message,
    match_reaction_trigger,
    reaction_dedupe_key,
)
from .session_queue import (
    DEFAULT_MAX_ACTIVE_SECONDS,
    DEFAULT_QUEUE_CAP,
    SessionQueue,
    SessionQueueConfig,
)
from .commands import is_command
from .policy import PolicyEngine
from . import runtime_scope
from .probe import (
    INSECURE_HTTP_ENV,
    base_url_error,
    _normalize_base_url,
)
from .rate_limiter import RateLimiter
from .audit_logger import AuditLogger
from .activity_trace import ActivityTrace
from . import tracing
from .engagement import (
    EngagementConfig,
    EngagementEntry,
    TopicEngagementStore,
    format_expiry_notice_text,
)

# Config resolution and module-level defaults live in ``zulip.settings`` so the
# adapter consumes typed values instead of reading the environment directly.
# They are imported under their historical private names both for the adapter's
# own call sites and so ``from zulip.adapter import <name>`` keeps working.
from .settings import (
    LONGPOLL_GRACE_SECONDS,
    LONGPOLL_MAX_SECONDS,
    LONGPOLL_MIN_SECONDS,
    POLL_BACKOFF_MAX,
    POLL_BACKOFF_START,
    POLL_FAST_RETURN_SECONDS,
    clamp_longpoll_budget as _clamp_longpoll_budget,
    next_poll_backoff as _next_poll_backoff,
    resolve_chatmode as _resolve_chatmode,
    resolve_chunk_config as _resolve_chunk_config,
    resolve_history_mode as _resolve_history_mode,
    resolve_int_setting as _resolve_int_setting,
    resolve_max_message_length as _resolve_max_message_length,
    resolve_observe_group as _resolve_observe_group,
    resolve_response_prefix as _resolve_response_prefix,
    resolve_session_queue as _resolve_session_queue,
    resolve_soft_gate as _resolve_soft_gate,
    resolve_stream_overrides as _resolve_stream_overrides,
    resolve_streams_filter as _resolve_streams_filter,
    resolve_timeouts as _resolve_timeouts,
    topic_sessions_enabled as _topic_sessions_enabled,
)

# Live adapters, so the plugin-level ``post_tool_call`` callback — registered in
# ``register(ctx)``, before any adapter exists — can find the one whose trace
# owns the current work item.
_LIVE_ADAPTERS: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _on_post_tool_call(**kwargs: Any) -> None:
    """Observer for finished tool calls (epic #139 / #159).

    Deliberately **synchronous**: the host dispatches this hook through the sync
    ``invoke_hook``, so an ``async def`` here would return a coroutine nobody
    awaits and silently do nothing. It only records state — the trace's own
    coalesced flush performs the I/O.
    """
    for adapter in list(_LIVE_ADAPTERS):
        try:
            adapter.record_tool_step(**kwargs)
        except Exception:
            # A trace must never interfere with the agent's tool call.
            continue


def _zulip_progress_handler(args: Any) -> str:
    """``zulip_progress`` tool handler — agent-authored trace steps (#160).

    Synchronous: the tool registry calls the handler directly and this only
    records state (the trace's own coalesced flush does the I/O). Returns a
    short acknowledgement so the model sees a result either way — a run without
    an active trace must not look like a failure.
    """
    note = ""
    if isinstance(args, dict):
        for field in ("note", "step", "message"):
            candidate = args.get(field)
            if isinstance(candidate, str) and candidate.strip():
                note = candidate.strip()
                break
    if not note:
        return "error: zulip_progress requires a non-empty 'note'"

    shown = False
    for adapter in list(_LIVE_ADAPTERS):
        try:
            if adapter.record_progress_step(note):
                shown = True
        except Exception:
            continue
    if shown:
        return "noted"
    return "no activity trace is active for this conversation; note not shown"
from .secret_guard import KnownSecret

logger = logging.getLogger(__name__)


# SDK transport, the connection caches and the address parsers live in
# ``zulip.zulip_client``. They are imported under their historical private
# names both for the adapter's own call sites and so
# ``from zulip.adapter import <name>`` keeps working. The mutable SDK state
# (``ZULIP_AVAILABLE`` and the ``zulip`` handle) is deliberately NOT re-exported
# here: ``zulip_client`` owns it and reads it through its own module globals, so
# a patch aimed at ``zulip.adapter`` would silently stop taking effect.
#
# ``get_cached_client`` is NOT re-exported: it has live patch sites, and a
# second binding here would leave a patch aimed at this module silently
# no-oping on the paths that read the owner. Production reads
# ``zulip_client.get_cached_client`` (see ``Wave 8``), just as the tests patch
# it — the same treatment ``render_refs`` / ``upload_file_to_zulip`` got.
from . import zulip_client
from .zulip_client import (
    _MAX_CLIENT_CACHE,
    _MAX_TARGET_CACHE,
    _client_cache,
    _set_cached_target,
    _target_cache,
    clear_caches as _clear_caches,
    parse_target as _parse_target,
    private_chat_id as _private_chat_id,
    user_lookup_call as _user_lookup_call,
)


# Bounded topic-history harvesting and non-addressed observation live in
# ``zulip.history`` (issues #148, #153). The names are re-exported here so
# ``from zulip.adapter import <name>`` keeps working, but production code reads
# them through the owning module (``history.<name>``) at call time — a local
# ``from .history import ...`` binding would silently stop honouring a patch of
# ``zulip.history``.
from . import history
from .history import (
    FETCHED_HISTORY_LABEL,
    OBSERVED_HISTORY_LABEL,
    ObservedContextBuffer,
    history_intent_matches as _history_intent_matches,
    render_history_block as _render_history_block,
)

# Native exec-approval rendering (the zform widget, its plain-text fallback and
# the two-message send path) lives in ``zulip.approvals``. The adapter keeps
# thin delegates because the gateway enables native-button mode exactly when
# ``ZulipAdapter`` defines ``_send_exec_approval_prompt`` in its own
# ``__dict__`` (the base class ships a no-op), and so the historical
# ``adapter._<name>`` call sites keep working. Production code reads the
# concern through the owning module at call time.
from . import approvals

# Outbound content egress (send/chunk/media/refs/secret guard/typing) lives in
# ``zulip.outbound``. Every ``BasePlatformAdapter`` send/typing override stays on
# the adapter as a thin delegate — the host calls them by name — and the rest of
# the adapter's own send-path helpers keep their historical ``adapter._<name>``
# names for the same reason. ``refs`` and ``media`` are reached through their
# owning modules from inside ``outbound`` (the module that calls them), so the
# adapter no longer imports them.
#
# ``_metadata_topic`` and ``_safe_delete_temp_file`` are re-exported under their
# historical names because the adapter's own routing/trace code calls the first
# and ``from zulip.adapter import _safe_delete_temp_file`` is a published
# import. Both are pure helpers with no patch site, so the re-export is an
# import-compatibility shim, not a live patch target.
from . import outbound
from .outbound import (
    metadata_topic as _metadata_topic,
    safe_delete_temp_file as _safe_delete_temp_file,
)

# Connection / event-loop lifecycle lives in ``zulip.connection`` and the
# per-session queue orchestration in ``zulip.inbound_queue``. Both own their
# module-level helpers too, so those names are imported here (not re-defined) to
# keep ``from zulip.adapter import <name>`` working and to give the adapter's own
# audit helpers ``event_route``. Production code reaches the concerns through
# ``self._connection`` / ``self._queue_controller`` at call time.
from . import inbound
from .connection import ZulipConnection, message_with_flags as _message_with_flags
from .inbound_queue import SessionQueueController, event_route as _event_route

# Stable conversation identity — the session/topic state, the session-key
# derivation and the rename/delete maintenance — lives in ``zulip.routing``.
# ``RoutingState`` OWNS that state; the adapter keeps thin delegates so
# ``adapter._<name>`` call sites (the poll loop's ``_pre_resolve_conversation``
# and ``_handle_topic_update`` included) keep working, plus read-only views so
# the historical ``adapter._<state>`` attribute surface still resolves to the
# same live object. ``_split_session_key_tail`` and ``_CONVERSATION_ID_RE`` are
# re-exported from their owner; ``adapter`` is never imported from ``routing``.
from . import routing
from .routing import (
    _CONVERSATION_ID_RE,
    split_session_key_tail as _split_session_key_tail,
)

# The inbound gate chain — the ~14-stage decide-then-dispatch path a raw
# event-queue message walks — lives in ``zulip.inbound``. ``_handle_message``
# stays a method because the connection event loop and the recovery path call
# ``adapter._handle_message`` by name; it is a one-line delegate, and
# ``inbound.handle_message`` reads adapter state at call time, so an instance
# patch of that state still takes effect.

# Zulip's ``BasePlatformAdapter`` data surface lives in ``zulip.platform_api``.
# Every method it implements stays an override on ``ZulipAdapter`` with an
# unchanged signature — the host calls them by name — and the override is a
# one-line delegate to the function that owns the behaviour. ``platform_api``
# is imported as a module rather than by name because the adapter's methods
# read it at call time, and it is never imported the other way round (no cycle).
from . import platform_api

# The standalone / CLI concern — requirements probe, config validation and env
# seeding, the interactive setup flow, and out-of-process delivery — lives in
# ``zulip.cli``. ``check_requirements``, ``validate_config`` and
# ``interactive_setup`` keep their public names; the two internal helpers are
# aliased to their historical private spellings so
# ``from zulip.adapter import _standalone_send`` / ``_env_enablement`` keeps
# working and so ``register(ctx)`` hands the host the same callables it always
# did.
from .cli import (
    STANDALONE_DEFAULT_TOPIC,
    check_requirements,
    env_enablement as _env_enablement,
    interactive_setup,
    standalone_send as _standalone_send,
    validate_config,
)


# Gateway session ids: <YYYYMMDD>_<HHMMSS>_<hex> (e.g. 20260928_180207_789321ca).
_GATEWAY_SESSION_ID_RE = re.compile(r"^\d{8}_\d{6}_[0-9a-f]+$")


class ZulipAdapter(BasePlatformAdapter):
    """Zulip platform adapter for Hermes Gateway."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("zulip"))
        extra = config.extra or {}

        self.api_key = runtime_scope.get_setting("ZULIP_API_KEY") or extra.get("api_key", "")
        self.email = runtime_scope.get_setting("ZULIP_EMAIL") or extra.get("email", "")
        self.site = runtime_scope.get_setting("ZULIP_SITE") or extra.get("site", "")

        # Outbound secret guard (Issue #136): the platform extra is walked for
        # credential-shaped keys, and the adapter's own api_key is registered
        # explicitly — those are exactly the values a prompt-injected agent
        # would paste into the room.
        self._platform_extra = extra
        self._known_secrets_cache: Optional[list[KnownSecret]] = None
        # Populated on connect. Zulip renders mentions from the display name,
        # not the email local-part, so mention matching needs it.
        self.bot_full_name = ""
        # The bot's own Zulip user id, learned on connect. Together with the
        # email it is how the bot's own messages are recognised on every
        # inbound path (issue #152), and how reaction-trigger targets are
        # recognised (epic #149).
        self._bot_user_id = ""

        # Validate site URL before creating client: https-only unless the
        # operator opted in, so the API key cannot leave in cleartext by
        # accident. (Issue #137)
        if self.site:
            validated = _normalize_base_url(self.site)
            if not validated:
                raise ValueError(base_url_error(self.site))
            self.site = validated
            if self.site.startswith("http://"):
                logger.warning(
                    "zulip: %s is set — the bot API key is sent unencrypted as "
                    "HTTP Basic on every request [site=%s]",
                    INSECURE_HTTP_ENV,
                    mask_pii(self.site),
                )

        _zulip = zulip_client.import_zulip_sdk()
        if not _zulip:
            logger.error(
                "zulip package not installed. Run: pip install zulip"
            )
            raise ImportError(
                "zulip package not installed. Run: pip install zulip"
            )

        # Use cached client if available (avoids repeated base64 encoding + object creation).
        # Read through the owning module: ``zulip_client`` defines it and that is
        # the module the tests patch, so there is one live read path.
        self.client = zulip_client.get_cached_client(
            self.site, self.email, self.api_key, _zulip_mod=_zulip
        )

        # The per-stream reply-topic cache and the F1 dispatch stash are
        # conversation-identity state, owned by ``self._routing`` (a
        # ``RoutingState``) and built below once ``_data_dir`` is known.
        # ``_last_topic_cache`` is context-mitigation state, not identity: it
        # stays here.
        self._last_topic_cache: dict[str, str] = {}      # stream_id → previous topic
        self._message_counts: dict[str, int] = {}        # chat_id → message count
        self._last_message_time: dict[str, float] = {}   # chat_id → last message epoch
        # DM session rotation: prevents context bloat in long conversations
        self._dm_session_turn_limit = int(
            runtime_scope.get_setting("ZULIP_DM_SESSION_TURN_LIMIT", "20").strip()
        )
        self._dm_base_message_counts: dict[str, int] = {}  # base_session_key → turn count

        # Block streaming config (Issue #49 — requires gateway-level streaming support)
        self._block_streaming = (
            runtime_scope.get_setting("ZULIP_BLOCK_STREAMING", "").strip().lower() in ("true", "1", "yes", "on")
        )

        self._data_dir = runtime_scope.get_profile_data_dir()

        # Persisted sender display names (issue #152). Zulip events do not
        # always carry a usable name; the cache resolves one without an API
        # call on every message. A missing or corrupt file is non-fatal.
        self._display_names = DisplayNameCache(self._data_dir)

        # Per-work-item delivery state for the audit trail (issue #145): the
        # key is the same chat+topic route the activity trace uses, and the
        # value records whether a send was attempted. Popped on completion, so
        # it cannot grow for a run the gateway never finalizes.
        self._audit_turns: dict[str, bool] = {}

        # Timeout configuration (Issue #62)
        self._connect_timeout, self._read_timeout, self._send_timeout = _resolve_timeouts()

        # Abort budget for /events, raised once we learn the server's own
        # long-poll budget from /register. Our 60s default is shorter than
        # Zulip's 90s default, so without this we abort every idle long-poll
        # client-side. (Issue #146)
        self._events_timeout = self._read_timeout

        # Stream filtering (Issue #65) — None means all streams
        self._streams_filter = _resolve_streams_filter()

        # Response prefix (Issue #65) — prepended to every outbound message
        self._response_prefix = _resolve_response_prefix()

        # Soft gate + observe group (issue #153). Both off by default: with
        # neither set, this adapter behaves exactly as it did before the flags
        # existed. The observed buffer is in-adapter (not host session) state.
        self._soft_gate = _resolve_soft_gate()
        self._observe_group = _resolve_observe_group()
        if self._soft_gate and self._observe_group:
            # Documented as mutually exclusive, but until now only in the README:
            # the soft gate dispatches every message, so nothing ever reaches the
            # drop path where observation happens and ZULIP_OBSERVE_GROUP silently
            # does nothing. Make the conflict loud instead of letting an admin
            # believe observation is running when it is not.
            logger.warning(
                "zulip: ZULIP_SOFT_GATE and ZULIP_OBSERVE_GROUP are both enabled "
                "— the soft gate dispatches every message, so nothing reaches "
                "the drop path where observation happens and "
                "ZULIP_OBSERVE_GROUP is a no-op. Turn the soft gate off to "
                "observe stream traffic without replying."
            )
        self._observed_context = history.ObservedContextBuffer()

        # Bounded history-aware context (issue #148). "off" adds no round-trip.
        self._history_mode = _resolve_history_mode()
        self._history_max_messages = _resolve_int_setting("ZULIP_HISTORY_MAX_MESSAGES", 8)
        self._history_window_hours = _resolve_int_setting("ZULIP_HISTORY_WINDOW_HOURS", 72)
        self._history_max_chars = _resolve_int_setting("ZULIP_HISTORY_MAX_CHARS", 4000)

        # Rate limiter (per-sender, sliding window)
        self._rate_limiter = RateLimiter(
            max_per_minute=int(
                runtime_scope.get_setting("ZULIP_MAX_MESSAGES_PER_MINUTE", "60").strip()
            ),
        )

        # Audit logger for security events
        self._audit_logger = AuditLogger(
            data_dir=self._data_dir,
            account_id=self.email or "default",
        )

        # Persistent queue and dedupe
        self._queue_mgr = ZulipQueueManager(
            account_id=self.email or "default",
            data_dir=self._data_dir,
            register_fn=self._register_queue,
            needed_event_types_fn=self._needed_event_types,
        )
        self._dedupe = ZulipDedupeStore(
            account_id=self.email or "default",
            data_dir=self._data_dir,
            ttl_ms=300_000,
            max_size=2000,
        )
        self._dedupe.load()

        # Stable conversation identity — the conversation registry, the F1
        # dispatch stash and the per-stream reply cache — is state, and it has
        # exactly one owner: ``self._routing``. The `_conversations`,
        # `_pending_conversations` and `_topic_cache` properties below are
        # read-only views onto that one object (never a second storage), left
        # for the historical ``adapter._<name>`` attribute surface.
        self._routing = routing.RoutingState(self)

        # Reaction config
        self._reaction_cfg = ReactionConfig.from_env()

        # In-channel action triggers (epic #149): a configured reaction on the
        # bot's own message dispatches a turn for that topic. Off by default —
        # when off, the "reaction" event type is not requested at all.
        self._reaction_trigger_cfg = ReactionTriggerConfig.from_env()

        # Extra Zulip event types beyond "message" that this install needs.
        # Features opt in here (reaction triggers need "reaction"; stable
        # topic sessions need "update_message"/"delete_message"); it is
        # consulted when registering the event queue and when deciding
        # whether a persisted queue can be reused. (Issues #162, #163)
        self._extra_event_types: list = (
            ["reaction"] if self._reaction_trigger_cfg.enabled else []
        )
        if _topic_sessions_enabled():
            # Stable topic sessions need rename and topic-deletion events;
            # opt in so a persisted queue whose subscription predates this
            # set is re-registered (issue #162 machinery).
            self._extra_event_types.extend(["update_message", "delete_message"])

        # Sticky topic engagement (issues #165/#166): after a mention in a
        # stream topic, follow-ups there are answered without a fresh mention
        # until the idle TTL lapses or the caller stops. Off by default.
        self._engagement_cfg = EngagementConfig.from_env()
        self._engagement_store = TopicEngagementStore(self._engagement_cfg)
        self._engagement_task: Optional[asyncio.Task] = None

        # DM policy engine (Issue #48 — controls who can DM the bot)
        self._policy = PolicyEngine(data_dir=self._data_dir)

        self._listening = False
        self._event_task: Optional[asyncio.Task] = None
        self._presence_task: Optional[asyncio.Task] = None

        # Connection / event-loop lifecycle and per-session queue orchestration
        # live in their own modules (``zulip.connection``,
        # ``zulip.inbound_queue``). These controllers hold the adapter and read
        # its state at call time, so instance patches still take effect.
        self._connection = ZulipConnection(self)
        self._queue_controller = SessionQueueController(self)

        # Activity trace (epic #139 / #158): one bot-owned status message per
        # work item, edited in place. Disabled unless ZULIP_ACTIVITY_TRACE is set.
        self._trace_cfg = tracing.trace_config()
        self._traces: dict[str, ActivityTrace] = {}
        self._trace_start_tasks: dict[str, asyncio.Task] = {}
        self._trace_started: dict[str, float] = {}
        self._trace_replied: set[str] = set()
        # Aliases (host session key, chat+topic route) -> trace key, so a tool
        # callback running in another thread/task can still find its trace.
        self._trace_sessions: dict[str, str] = {}
        _LIVE_ADAPTERS.add(self)
        if self._trace_cfg.enabled and ProcessingOutcome is None:
            logger.warning(
                "ZULIP_ACTIVITY_TRACE is enabled but this gateway does not expose "
                "the processing lifecycle hooks; the trace will not run"
            )

        # Per-session message queue (issue #151). Off by default. The queue
        # learns that a run ended from on_processing_complete, so on a gateway
        # without the lifecycle hooks it could only ever queue — stalling every
        # message behind the first. There it does nothing at all: behaviour is
        # then exactly what it was before the flag existed.
        self._queue_cfg = SessionQueueConfig(
            enabled=_resolve_session_queue(),
            cap=_resolve_int_setting("ZULIP_QUEUE_CAP", DEFAULT_QUEUE_CAP),
            max_active_seconds=float(
                _resolve_int_setting(
                    "ZULIP_QUEUE_MAX_ACTIVE_SECONDS",
                    int(DEFAULT_MAX_ACTIVE_SECONDS),
                    0,
                )
            ),
        )
        self._session_queue = (
            SessionQueue(self._queue_cfg)
            if self._queue_cfg.enabled and ProcessingOutcome is not None
            else None
        )
        if self._queue_cfg.enabled and ProcessingOutcome is None:
            logger.warning(
                "ZULIP_SESSION_QUEUE is enabled but this gateway does not expose "
                "the processing lifecycle hooks; messages will not be queued"
            )

    # -- views onto RoutingState-owned conversation identity state ----------
    #
    # Single storage location: each property returns the object
    # ``RoutingState`` created, so a caller reading ``adapter._conversations``
    # sees exactly what the router wrote — never a stale copy that would make
    # an assertion silently vacuous.

    @property
    def _conversations(self):
        return self._routing._conversations

    @property
    def _pending_conversations(self):
        return self._routing._pending_conversations

    @property
    def _topic_cache(self):
        return self._routing._topic_cache

    async def _sdk_call(self, fn, *args, timeout: float, **kwargs):
        """Wrap a synchronous SDK call in asyncio.to_thread + asyncio.wait_for.

        Provides outer-timeout protection so the gateway event loop never
        blocks indefinitely on a hung Zulip API request.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args, **kwargs),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "zulip SDK call timed out after %.1fs [fn=%s]",
                timeout,
                getattr(fn, "__name__", repr(fn)),
            )
            raise

    @staticmethod
    def _validate_message_id(message_id: Any) -> int:
        """Validate and convert a message ID to int.

        Raises ValueError if the message ID is not a valid positive integer
        or exceeds the maximum safe value.
        Prevents path traversal, injection, and overflow via malformed IDs.
        """
        if message_id is None:
            raise ValueError("message_id is required")
        try:
            mid = int(str(message_id).strip())
        except (ValueError, TypeError):
            raise ValueError(f"Invalid message_id: {message_id}")
        if mid <= 0:
            raise ValueError(f"message_id must be positive: {message_id}")
        if mid > 2**63 - 1:
            raise ValueError(f"message_id exceeds maximum safe value: {message_id}")
        return mid

    async def _stop_typing(self, typing_params: Optional[dict]) -> None:
        """Stop typing indicator if it was started. Safe to call multiple times."""
        return await outbound.stop_typing_with_params(self, typing_params)

    def _typing_params_for_chat(
        self, chat_id: str, op: str, topic: Optional[str] = None
    ) -> Optional[dict]:
        """Map a gateway chat_id to Zulip set_typing_status params.

        DM session rotation can suffix chat ids ("dm:<id>:session:<n>"), and a
        group DM carries every recipient ("dm:<id>,<id>"); both are stripped to
        the recipient list. Streams are numeric stream ids;
        ``topic`` (the routing metadata's thread_id) wins over the per-stream
        reply cache, so typing shows in the session's own topic.
        """
        return outbound.typing_params_for_chat(self, chat_id, op, topic)

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        """Core typing hook: the gateway calls this every ~2s while the agent
        runs (platform typing state expires after ~5s). Best-effort."""
        await outbound.send_typing(self, chat_id, metadata)

    async def stop_typing(self, chat_id: str, metadata: Any = None) -> None:
        """Core typing hook: called when the agent run finishes. Best-effort.

        Accepts ``metadata`` so the gateway base's introspecting
        ``_stop_typing_with_metadata`` forwards the run's routing metadata
        (a thread-scoped clear must not depend on the last-seen topic).
        """
        await outbound.stop_typing(self, chat_id, metadata)

    # --- activity trace lifecycle (epic #139 / #158) ---
    #
    # The lifecycle, its step recording and its durable records live in
    # ``zulip.tracing``. These are thin delegates so ``adapter._<name>`` — and
    # the tool hooks, which reach the adapter by name — keep working; the trace
    # state itself stays on the adapter and is read at call time.

    def _note_trace_reply(self, chat_id: str, metadata: Any) -> None:
        """Record that this work item actually produced a reply."""
        tracing.note_trace_reply(self, chat_id, metadata)

    def _session_key_for_event(self, event: Any) -> Optional[str]:
        """The host's own session key for an event, when it exposes one.

        Reused rather than reimplemented: a second key derivation would drift
        from the host's, which is the #143 lesson. Returns None when the host
        offers no such method, so callers fall back to the route alias.
        Owned by ``zulip.routing``; this delegate keeps the historical
        ``adapter._session_key_for_event`` call sites working.
        """
        return routing.session_key_for_event(self, event)

    def record_progress_step(self, note: str) -> bool:
        """Add an agent-authored step (mode B). True when it was shown."""
        return tracing.record_progress_step(self, note)

    def record_tool_step(
        self,
        *,
        tool_name: str = "",
        status: Optional[str] = None,
        duration_ms: int = 0,
        error_type: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Attach a finished tool call to its work item's trace.

        A step whose run cannot be identified is **dropped**, never guessed into
        some other topic: a trace that narrates another conversation's work is
        worse than a trace with a gap.
        """
        tracing.record_tool_step(
            self,
            tool_name=tool_name,
            status=status,
            duration_ms=duration_ms,
            error_type=error_type,
            **kwargs,
        )

    # --- durable trace records (issue #161) ---
    #
    # Persistence and restart recovery also live in ``zulip.tracing``. The
    # delegates below keep ``adapter._<name>`` (and ``_listen_for_events``)
    # working.

    def _trace_records_path(self) -> Path:
        return tracing.trace_records_path(self)

    def _load_trace_records(self) -> dict:
        return tracing.load_trace_records(self)

    def _persist_trace_record(
        self, key: str, chat_id: str, metadata: Any, message_id: int
    ) -> None:
        tracing.persist_trace_record(self, key, chat_id, metadata, message_id)

    async def _recover_interrupted_traces(self) -> int:
        """Close out traces whose run died with the previous process."""
        return await tracing.recover_interrupted_traces(self)

    def _start_trace(self, event: Any) -> None:
        """Begin a trace for a work item. Never raises, never blocks the run."""
        tracing.start_trace(self, event)

    async def _finish_trace(self, event: Any, outcome: Any) -> None:
        """Finalize a work item's trace. Never raises into the gateway loop."""
        await tracing.finish_trace(self, event, outcome)

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Gateway lifecycle hook: a work item started. (epic #139 / #158)"""
        await self._audit_dispatch_turn(event)
        self._start_trace(event)
        if self._session_queue is not None:
            self._session_queue.mark_active(self._queue_session_key(event))

    async def _audit_dispatch_turn(self, event: MessageEvent) -> None:
        """Record a dispatched turn and arm this work item for its outcome.

        Pairs with the ``deliver_*`` events so "the run produced nothing" is
        distinguishable from "the run never started" (issue #145).
        """
        chat_id, metadata = _event_route(event)
        if not chat_id:
            return
        self._audit_turns[tracing.trace_key(chat_id, metadata)] = False
        await self._audit_logger.log_dispatch_turn(
            chat_id=chat_id,
            topic=_metadata_topic(metadata),
            message_id=getattr(event, "message_id", None),
        )

    def _note_delivery_attempt(self, chat_id: str, metadata: Any) -> None:
        """Mark this work item as having produced something to deliver."""
        key = tracing.trace_key(chat_id, metadata)
        if key in self._audit_turns:
            self._audit_turns[key] = True

    async def on_processing_complete(
        self, event: MessageEvent, outcome: Any = None
    ) -> None:
        """Gateway lifecycle hook: a work item finished, with its outcome.

        The gateway supplies SUCCESS / FAILURE / CANCELLED, which is what makes
        error and abort distinguishable at all — and therefore what lets a run
        that produced nothing say so instead of staying silent.
        """
        await self._audit_run_end(event)
        await self._finish_trace(event, outcome)
        # Last, so the finished run's status is closed out before the next
        # queued turn for the same session starts (issue #151).
        await self._drain_session_queue(event)

    async def _audit_run_end(self, event: MessageEvent) -> None:
        """Record ``deliver_empty`` for a dispatched run that never sent.

        Only reached when ``on_processing_start`` armed this work item: a run
        the gateway never dispatched has no entry and writes nothing.
        """
        chat_id, metadata = _event_route(event)
        if not chat_id:
            return
        key = tracing.trace_key(chat_id, metadata)
        attempted = self._audit_turns.pop(key, None)
        if attempted is False:
            await self._audit_logger.log_deliver_empty(
                chat_id=chat_id, topic=_metadata_topic(metadata)
            )

    # --- per-session message queue (issue #151) ---
    #
    # The orchestration lives in ``zulip.inbound_queue``. These are thin
    # delegates so ``adapter._<name>`` keeps working; the queue state
    # (``_session_queue``) stays on the adapter and is read at call time.

    def _queue_session_key(self, event: Any) -> str:
        """Session identity for the queue."""
        return self._queue_controller.queue_session_key(event)

    async def _queue_turn(
        self,
        event: Any,
        reactions: Any,
        typing_params: Any,
        message_id: Any,
        *,
        is_slash: bool = False,
    ) -> bool:
        """Queue a fully cleared turn when its session is mid-run."""
        return await self._queue_controller._queue_turn(
            event, reactions, typing_params, message_id, is_slash=is_slash
        )

    async def _drain_session_queue(self, event: Any) -> None:
        """Dispatch the oldest turn waiting behind the session that just ended."""
        return await self._queue_controller._drain_session_queue(event)

    async def _dispatch_turn(
        self, event: Any, reactions: Any, typing_params: Any, message_id: Any
    ) -> None:
        """Hand one cleared turn to the gateway and finish its reaction state."""
        return await self._queue_controller._dispatch_turn(
            event, reactions, typing_params, message_id
        )

    async def _mark_read(self, message_id: Any) -> None:
        """Mark a message as read. Best-effort."""
        try:
            await self._sdk_call(
                self.client.update_message_flags,
                {"messages": [message_id], "op": "add", "flag": "read"},
                timeout=self._send_timeout,
            )
        except Exception:
            pass

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Initialize connection and start listening."""
        return await self._connection.connect(is_reconnect=is_reconnect)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Get information about a chat/channel."""
        return await platform_api.get_chat_info(self, chat_id)

    async def disconnect(self) -> None:
        """Stop listening and close connection."""
        return await self._connection.disconnect()

    def _needed_event_types(self) -> list:
        """Event types this adapter needs from its Zulip queue."""
        return self._connection.needed_event_types()

    def _register_queue(self) -> dict:
        """Register the event queue, learning the server's long-poll budget."""
        return self._connection.register_queue()

    def _engagement_notice_payload(
        self, stream_id: object, topic: str, content: str
    ) -> Optional[dict]:
        """In-topic send payload for an engagement notice.

        Resolved through ``_parse_target`` — the same routing the reply uses —
        so a notice can never land in a different stream than the one it
        describes.
        """
        try:
            target = _parse_target(str(stream_id))
        except (TypeError, ValueError):
            return None
        if target.get("type") != "stream":
            return None
        return {
            "type": "stream",
            "to": target["stream_id"],
            "topic": topic,
            "content": content,
        }

    async def _ack_engagement_stop(self, message: dict, *, cleared: bool) -> None:
        """Acknowledge an explicit stop of sticky listening, in-topic."""
        if cleared:
            text_out = (
                "Okay — I'll stop listening on this topic. "
                "@mention me again when you want to continue."
            )
        else:
            text_out = (
                "I wasn't actively listening on this topic. "
                "@mention me to start a conversation."
            )
        payload = self._engagement_notice_payload(
            message.get("stream_id"), message.get("subject", ""), text_out
        )
        if payload is None:
            return
        try:
            await self._sdk_call(
                self.client.send_message, payload, timeout=self._send_timeout
            )
        except Exception as e:
            logger.warning("zulip engagement stop ack failed: %s", mask_pii(str(e)))

    async def _engagement_expiry_loop(self) -> None:
        """Expire idle engagements and post an in-topic notice (#166).

        Only natural idle TTL expiry notifies; an explicit stop or session end
        clears silently. Runs only while the adapter is listening and only when
        engagement is enabled — the task is simply never started for mode=off.
        """
        interval = self._engagement_cfg.expiry_scan_seconds
        ttl_min = max(1, int(round(self._engagement_cfg.ttl_seconds / 60.0)))
        logger.info(
            "zulip engagement expiry scanner started [interval=%.0fs ttl=%sm]",
            interval,
            ttl_min,
        )
        while self._listening:
            try:
                await asyncio.sleep(interval)
                if not self._listening:
                    break
                await self._queue_controller.sweep_stale_sessions()
                expired = self._engagement_store.pop_expired()
                if not expired:
                    continue

                # Coalesce: one notice per (stream_id, topic).
                by_topic: dict[tuple[str, str], list[EngagementEntry]] = {}
                for entry in expired:
                    by_topic.setdefault((entry.stream_id, entry.topic), []).append(
                        entry
                    )

                if not self._engagement_cfg.expiry_notice:
                    continue
                for (stream_id, topic), entries in by_topic.items():
                    payload = self._engagement_notice_payload(
                        stream_id,
                        topic,
                        format_expiry_notice_text(ttl_minutes=ttl_min),
                    )
                    if payload is None:
                        continue
                    try:
                        await self._sdk_call(
                            self.client.send_message,
                            payload,
                            timeout=self._send_timeout,
                        )
                        logger.info(
                            "zulip engagement expired notice [stream=%s topic=%s users=%d]",
                            stream_id,
                            topic,
                            len(entries),
                        )
                    except Exception as e:
                        logger.warning(
                            "zulip engagement expired notice failed [stream=%s topic=%s]: %s",
                            stream_id,
                            topic,
                            mask_pii(str(e)),
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("zulip engagement expiry loop error: %s", str(e))

    async def _listen_for_events(self):
        """Listen for incoming Zulip messages via persistent event queue."""
        return await self._connection.listen_for_events()

    def _pre_resolve_conversation(self, msg: dict, msg_id: str) -> None:
        """Resolve (mint) a stream topic's conversation at event dispatch.

        The poll loop calls this inline, in event-id order; the deferred
        handler then drops the stash entry. Owned by ``zulip.routing`` — see
        ``RoutingState.pre_resolve_conversation`` for the F1 rationale. This
        delegate keeps the poll loop's ``adapter._<name>`` call site working.
        """
        return self._routing.pre_resolve_conversation(msg, msg_id)

    def _handle_topic_update(self, event: dict) -> None:
        """Registry maintenance for topic renames/moves (stable topic sessions).

        Implements R2/R5 (full rename re-points), R3 (partial moves are
        splits) and R8 (FULL cross-channel moves free the mapping). Owned by
        ``zulip.routing``; this delegate keeps the poll loop's
        ``adapter._<name>`` call site working.
        """
        return self._routing.handle_topic_update(event)

    def _routed_topic(self, stream_id: int, metadata: Any) -> Optional[str]:
        """Routing topic from send metadata, resolving conversation ids (R6).

        Owned by ``zulip.routing``; this delegate keeps the reply path's
        ``adapter._<name>`` call site working.
        """
        return self._routing.routed_topic(stream_id, metadata)

    def _routed_topic_for_chat(self, chat_id: str, metadata: Any) -> Optional[str]:
        """``_routed_topic`` for chat-id strings (typing hooks)."""
        return self._routing.routed_topic_for_chat(chat_id, metadata)

    # -- upgrade migration --------------------------------------------------

    def set_session_store(self, session_store: Any) -> None:
        super().set_session_store(session_store)
        if self._routing._conversations is not None:
            try:
                self._migrate_legacy_topic_sessions()
            except Exception:
                # Fail open: a failed migration must not block adapter
                # startup; affected topics simply start fresh sessions.
                logger.exception(
                    "zulip legacy-session migration failed; continuing with fresh keys"
                )
            try:
                self._backfill_session_starts()
            except Exception:
                # Non-critical: labels fall back to the lineage origin.
                logger.debug(
                    "zulip session-start backfill failed", exc_info=True
                )

    def _migrate_legacy_topic_sessions(self) -> int:
        """One-time continuity migration to conversation-id session keys.

        Re-keys name-keyed zulip stream sessions (and seeds the registry) so a
        user keeps their current session when the feature turns on. Owned by
        ``zulip.routing``; the gateway wires ``set_session_store`` during
        adapter setup, and tests call this delegate by name.
        """
        return self._routing.migrate_legacy_topic_sessions()

    def _route_entries(self) -> dict:
        """Live gateway routing entries keyed by ``session_key`` (public
        ``SessionStore.list_sessions()``; empty on any failure)."""
        store = getattr(self, "_session_store", None)
        if store is None:
            return {}
        try:
            entries = store.list_sessions() or []
        except Exception as exc:
            logger.debug("zulip store.list_sessions unavailable: %s", exc)
            return {}
        return {e.session_key: e for e in entries}

    def _entry_for_conversation(self, entries: dict, conversation_id: str):
        """The routing entry whose key tail is this conversation id."""
        for key, entry in entries.items():
            if key.rsplit(":", 1)[-1] == conversation_id:
                return key, entry
        return None, None

    def _observe_session_start(
        self, stream_id: int, conversation_id: str
    ) -> None:
        """First sight of a conversation's live session records its start.

        Labels are per session (the topic name where the session was
        created — the current name for a freshly minted generation), not
        per lineage, so they stay truthful across renames between
        generations. First record wins; derivation (see
        ``derive_session_start``) covers sessions minted before the
        labels existed. Failures never disturb message flow.
        """
        if self._routing._conversations is None:
            return
        try:
            _key, entry = self._entry_for_conversation(
                self._route_entries(), conversation_id
            )
            if entry is None or not entry.session_id:
                return
            if (
                self._routing._conversations.session_start(stream_id, entry.session_id)
                is None
            ):
                self._routing._conversations.record_session_start(
                    stream_id, conversation_id, entry.session_id
                )
        except Exception:
            logger.debug(
                "zulip session-start observation failed [conv=%s]",
                conversation_id, exc_info=True,
            )

    def _backfill_session_starts(self) -> int:
        """Record start labels for every known session (idempotent).

        Runs at store wiring after the legacy migration. Owned by
        ``zulip.routing``; this delegate keeps ``set_session_store`` and the
        tests' ``adapter._<name>`` call site working.
        """
        return self._routing.backfill_session_starts()

    def _session_db(self):
        """Gateway seam: ``SessionStore._db`` (the state-db handle, v0.21.5).

        Owned by ``zulip.routing``; this delegate keeps the adapter's own
        session-list/switch helpers working.
        """
        return self._routing._session_db()

    def _topic_sessions_command_reply(self, stream_id: int, topic: str) -> str:
        """``/topic-sessions`` (topic sessions): list this topic's session set.

        Read-only for bindings: the current session plus every former
        session, each labeled with the topic where it was created. The
        only write is the first-sight start-label record (v6): ``/new``
        is core-handled and never reaches this adapter, so a fresh
        generation checked before any message traffic must be observed
        here — otherwise its line falls back to the lineage origin
        (Topic261002-2 case). Bindings are not changed;
        ``/continue <session-id>`` (R7) switches.
        """

        if self._routing._conversations is None:
            return (
                "Topic sessions are disabled"
                " (set ZULIP_TOPIC_SESSIONS=true to enable them)."
            )
        current_id, current_origin, members = self._routing._conversations.sessions_for_topic(
            stream_id, topic
        )
        # First sight counts here too (see docstring): observe the live
        # session so its line below shows the name it was minted under.
        if current_id is not None:
            self._observe_session_start(stream_id, current_id)

        entries = self._route_entries()
        db = self._session_db()
        store_missing = getattr(self, "_session_store", None) is None

        def _lineage_sessions(conversation_id: str):
            """All known gateway sessions of one lineage, newest first,
            plus the currently-live id (None when the lineage has no live
            routing entry). Users never see the lineage id itself — only
            its gateway session ids, labeled with the lineage's origin."""
            key, entry = self._entry_for_conversation(entries, conversation_id)
            live = entry.session_id if entry is not None else None
            ids = []
            if db is not None and key is not None:
                try:
                    rows = db.list_sessions_rich(
                        session_key=key, limit=50,
                        order_by_last_active=True, include_hidden=True,
                    )
                except Exception as exc:
                    logger.debug(
                        "zulip session enumeration unavailable [conv=%s]: %s",
                        conversation_id, exc,
                    )
                    rows = []
                for row in rows:
                    sid = (row or {}).get("id") or (row or {}).get(
                        "session_id")
                    if sid and sid not in ids:
                        ids.append(sid)
            if live is not None and live not in ids:
                ids.insert(0, live)
            return live, ids

        # Degraded mode (gateway store not wired): the gateway session ids
        # cannot be known, so lines render without ids rather than falling
        # back to lineage ids (which are plumbing, never user-facing).
        lines = []
        if store_missing:
            # Degraded (gateway store not wired): the gateway session ids
            # cannot be known, so lines render without ids rather than
            # falling back to lineage ids (plumbing, never user-facing).
            number = 0
            if current_id is not None:
                number += 1
                lines.append(
                    f"{number}) **(current)**"
                    f' — started in "{current_origin}"'
                )
            for _member_id, member_origin in members:
                number += 1
                lines.append(f'{number}) — started in "{member_origin}"')
            body = [f"📋 Sessions in this topic: {number}"]
            if lines:
                body += ["", *lines]
            if number > 1:
                body += [""]
                body.append(
                    "Use `/continue <session-id>` to switch to another"
                    " session."
                )
            logger.debug(
                "zulip /topic-sessions listing (degraded)"
                " [channel=%s topic=%r count=%d]",
                stream_id, mask_pii(topic), number,
            )
            return "\n".join(body)

        lines = []
        number = 0
        for is_current, origin, conv in (
            [(True, current_origin, current_id)] if current_id is not None else []
        ) + [(False, origin, member_id) for member_id, origin in members]:
            live, ids = _lineage_sessions(conv)
            for position, sid in enumerate(ids):
                number += 1
                if is_current and sid == live:
                    marker = " **(current)**"
                elif not is_current and (
                    sid == live or (position == 0 and live is None)
                ):
                    marker = " **(last)**"
                else:
                    marker = ""
                label = (
                    self._routing._conversations.session_start(stream_id, sid)
                    or origin
                )
                lines.append(
                    f'{number}) `{sid}`{marker} — started in "{label}"'
                )
        body = [f"📋 Sessions in this topic: {number}"]
        if lines:
            body += ["", *lines]
        if number > 1:
            body += [""]
            body.append(
                "Use `/continue <session-id>` to switch to another session."
            )
        logger.debug(
            "zulip /topic-sessions listing [channel=%s topic=%r count=%d]",
            stream_id, mask_pii(topic), number,
        )
        return "\n".join(body)

    def _continue_command_reply(
        self, stream_id: int, topic: str, arg: str = ""
    ) -> str:
        """``/continue <gateway-session-id>`` (R7, inheritance-scoped).

        Switches this topic to a gateway session from ITS OWN session
        set — the lineages it created plus those handed to it by full
        renames (merges) — as listed by ``/topic-sessions``. Only a
        gateway session id is accepted (user ruling): lineage
        (conversation) ids are grouping labels, not switch tokens.
        Bare ``/continue`` does nothing: there is no implicit pick, and
        a session held by another topic (or orphaned) can never be
        named here, because its lineage is not part of this topic's set.
        """
        if self._routing._conversations is None:
            return (
                "Topic sessions are disabled"
                " (set ZULIP_TOPIC_SESSIONS=true to enable them)."
            )
        arg = (arg or "").strip()
        if not arg:
            return (
                "Usage: `/continue <session-id>` — switch this topic to one"
                " of its own gateway sessions (`YYYYMMDD_HHMMSS_hex`, as"
                " listed by `/topic-sessions`). `/continue` alone does"
                " nothing."
            )
        token = arg.split()[0]
        if _GATEWAY_SESSION_ID_RE.fullmatch(token):
            return self._continue_to_gateway_session(stream_id, topic, token)
        return (
            "That is not a session id shown by `/topic-sessions`."
            " Nothing was changed."
        )

    def _continue_to_gateway_session(
        self, stream_id: int, topic: str, session_id: str
    ) -> str:
        """/continue <gateway-session-id>: one-command switch.

        Resolves the gateway session (live via the routing index, past
        generations via the state-db seam), verifies its conversation
        belongs to THIS topic's own set (R7 inheritance law — a session
        held by another topic or orphaned is never reachable), re-binds
        the topic when needed, and switches the gateway session — all in
        one command.
        """
        store = getattr(self, "_session_store", None)
        if store is None:
            return (
                "Session switching is unavailable (no session store wired)."
                " Nothing was changed."
            )

        # 1. Resolve the session to its routing key / conversation.
        key = None
        entry = None
        try:
            entry = store.lookup_by_session_id(session_id)
        except Exception as exc:
            logger.debug("zulip lookup_by_session_id failed: %s", exc)
        if entry is not None:
            key = entry.session_key
        else:
            db = self._session_db()
            if db is not None:
                try:
                    row = db.get_session(session_id)
                except Exception as exc:
                    logger.debug("zulip get_session failed: %s", exc)
                    row = None
                key = (row or {}).get("session_key")
        if not key:
            return (
                f"No session `{session_id}` was found — use a session id"
                " shown by `/topic-sessions`. Nothing was changed."
            )
        conversation_id = key.rsplit(":", 1)[-1]
        if not _CONVERSATION_ID_RE.fullmatch(conversation_id):
            return (
                "That session is not part of this topic's sessions — use a"
                " session id shown by `/topic-sessions`. Nothing was changed."
            )

        # 2. Inheritance verification (R7): the conversation must be part
        # of this topic's own set.
        current = self._routing._conversations.lookup(stream_id, topic)
        if current is None:
            return "This topic has no session yet — nothing to continue."
        if conversation_id == current and entry is not None:
            return f"Already talking in session `{session_id}` here."
        if conversation_id != current:
            member_ids = {
                member_id
                for member_id, _origin in self._routing._conversations.sessions_for_topic(
                    stream_id, topic
                )[2]
            }
            if conversation_id not in member_ids:
                return (
                    "That session is not one of this topic's sessions —"
                    " use a session id shown by `/topic-sessions`."
                    " Nothing was changed."
                )
            self._routing._conversations.rebind(stream_id, topic, conversation_id)

        # 3. Switch the gateway session under the lineage's key.
        entries = self._route_entries()
        expected = entries[key].session_id if key in entries else None
        try:
            switched = store.switch_session(
                key, session_id, expected_session_id=expected
            )
        except Exception as exc:
            logger.warning(
                "zulip switch_session failed [key=%s target=%s]: %s",
                mask_pii(key), session_id, exc,
            )
            switched = None
        origin = self._routing._conversations.sessions_for_topic(stream_id, topic)[1]
        if switched is None:
            if key not in self._route_entries():
                return (
                    "The topic binding moved, but the gateway has no live"
                    " route for that lineage yet — send a message and it"
                    " will start there."
                )
            return (
                "That lineage moved while switching — please try"
                " `/continue` again."
            )
        self._routing._conversations.record_session_start(
            stream_id, conversation_id, session_id
        )
        label = (
            self._routing._conversations.session_start(stream_id, session_id) or origin
        )
        logger.debug(
            "zulip gateway session switched via /continue"
            " [channel=%s conv=%s session=%s topic=%r]",
            stream_id, conversation_id, session_id, mask_pii(topic),
        )
        return (
            f"🔗 This topic now talks in session `{session_id}`"
            f', started in **{label}**.'
        )

    async def _handle_message_delete_event(self, event: dict) -> None:
        """R10 trigger: a ``delete_message`` event on a mapped topic.

        ANY delete event on a mapped stream topic triggers a verification
        — single-message deletes included, so a topic emptied message-by-
        message (whatever the delete order, anchor first or last) is
        detected by its final delete. The event itself cannot tell a
        topic deletion from a partial one (partial bulk deletes exist —
        deleting 2 of 9 messages is bulk but not a topic deletion), so
        the channel's topic list (``get_stream_topics``) is the
        authority: a topic exists while it has messages. Fail-open: on
        any verification problem the mapping stays.
        """
        if self._routing._conversations is None:
            return
        if event.get("message_type") != "stream":
            return
        stream_id = event.get("stream_id")
        topic = event.get("topic", "")
        if not isinstance(stream_id, int) or not topic:
            return
        if self._routing._conversations.lookup(stream_id, topic) is None:
            return
        # Awaited inline so the spawned task (see the poll loop) covers the
        # whole chain: trigger check -> topic-list verification -> orphaning.
        await self._apply_topic_deletion(stream_id, topic)

    async def _apply_topic_deletion(self, stream_id: int, topic: str) -> None:
        """Verify a deletion trigger against the channel's topic list and,
        only if the topic is really gone, orphan its session set (R10).

        Owned by ``zulip.routing``; this delegate keeps the delete-event path's
        ``adapter._<name>`` call site working.
        """
        return await self._routing.apply_topic_deletion(stream_id, topic)

    def _is_self_message(self, message: dict) -> bool:
        """Whether ``message`` was authored by this bot.

        The event queue subscribes to all public streams, so the bot receives
        its own sends back. Treating those as user input would let the bot
        answer itself and would feed its own output back in as history, so the
        check lives here and is applied on every inbound path (issue #152).
        """
        sender_email = str(message.get("sender_email") or "").strip()
        if sender_email and self.email and sender_email.lower() == self.email.lower():
            return True
        sender_id = message.get("sender_id")
        if self._bot_user_id and sender_id is not None:
            return str(sender_id) == str(self._bot_user_id)
        return False

    async def _fetch_display_name(self, user_id: Any) -> Optional[str]:
        """One bounded lookup for a display name, used on a cache miss (#152)."""
        fn, args = _user_lookup_call(self.client, user_id)
        if fn is None:
            logger.warning(
                "zulip display-name lookup unavailable: %s exposes no supported "
                "user lookup (issue #196)",
                type(self.client).__name__,
            )
            return None
        try:
            result = await self._sdk_call(fn, *args, timeout=self._send_timeout)
        except Exception:
            return None
        if not isinstance(result, dict) or result.get("result") != "success":
            return None
        user = result.get("user") or {}
        if isinstance(user, list):
            # The SDK docstring shows a list shape for /users/{id}; tolerate it.
            user = user[0] if user else {}
        name = user.get("full_name") or ""
        return name.strip() or None

    async def _resolve_display_name(self, message: dict) -> str:
        """Sender display name: the event payload, the cache, then one fetch.

        Zulip's payload name is authoritative and refreshes the cache. When it
        is missing, the persisted cache is consulted and, on a miss, refreshed
        with a single bounded lookup rather than one call per message (#152).
        """
        provided = str(message.get("sender_full_name") or "").strip()
        user_id = message.get("sender_id")
        if provided:
            if user_id is not None:
                self._display_names.put(user_id, provided)
            return provided
        if user_id is None:
            return "Unknown"
        name = await self._display_names.get_or_fetch(
            user_id, self._fetch_display_name
        )
        return name or "Unknown"

    # ------------------------------------------------------------------
    # Observed stream context and history harvest (#153, #148)
    # ------------------------------------------------------------------

    def _stream_monitored(self, stream_name: str) -> bool:
        """Whether ``stream_name`` is inside ``ZULIP_STREAMS`` (all when unset)."""
        if self._streams_filter is None:
            return True
        return stream_name.lower() in self._streams_filter

    def _observe_stream_message(self, message: dict, content: str) -> None:
        """Record a non-addressed stream message as topic context (#153).

        No reply, no dispatch, no API call: the message joins a bounded
        per-topic buffer that is prepended to the agent-facing text the next
        time the bot is addressed in that topic. Self/bot messages are never
        observed, and streams the bot does not monitor contribute nothing.
        """
        if not self._observe_group:
            return
        if message.get("type") != "stream":
            return
        if self._is_self_message(message):
            return
        stream_name = str(message.get("display_recipient") or "")
        if not self._stream_monitored(stream_name):
            return
        text = strip_html_to_text(content or "").strip()
        if not text:
            return
        sender = (
            str(message.get("sender_full_name") or "").strip()
            or str(message.get("sender_email") or "").strip()
            or "Unknown"
        )
        topic = str(message.get("subject") or "")
        self._observed_context.add(
            message.get("stream_id"), topic, f"[{sender}] {text}"
        )
        logger.debug(
            "zulip observed stream msg [stream=%s topic=%s]",
            mask_pii(stream_name),
            mask_pii(topic),
        )

    async def _quoted_topic_history(
        self,
        stream_name: str,
        stream_id: Any,
        topic: str,
        *,
        content: str,
        addressed: bool,
    ) -> str:
        """Quoted context prepended to the agent-facing body for one turn.

        Two independent sources: the observed-topic buffer (#153), which costs
        nothing and only exists for a turn the bot was addressed in, and an
        optional harvested slice of real topic history (#148). Both are
        best-effort — a failed harvest is logged and dropped — and neither ever
        reaches a slash command or a policy/rate-limit gate, which have already
        been decided by the time this runs.
        """
        # Slash commands belong to the gateway — never spend a harvest on them
        # and never modify their body (#148, #190).
        if is_command(content):
            return ""

        blocks: list[str] = []

        if self._observe_group and addressed:
            observed = self._observed_context.render(stream_id, topic)
            if observed:
                blocks.append(f"{history.OBSERVED_HISTORY_LABEL}\n{observed}")

        if self._history_mode != "off" and (
            self._history_mode == "always" or history.history_intent_matches(content)
        ):
            harvested = await self._harvest_history_block(stream_name, topic)
            if harvested:
                blocks.append(f"{history.FETCHED_HISTORY_LABEL}\n{harvested}")

        if not blocks:
            return ""
        return "\n\n".join(blocks) + "\n\n"

    async def _fetch_stream_history(self, stream_name: str, topic: str) -> list[dict]:
        """Narrow ``[stream, topic]`` fetch of the newest topic messages (#148).

        The synchronous SDK call runs through ``_sdk_call`` so it is bounded
        and cannot block the event loop. A non-success response yields an empty
        list rather than an error.
        """
        narrow = [{"operator": "stream", "operand": stream_name}]
        if topic:
            narrow.append({"operator": "topic", "operand": topic})
        result = await self._sdk_call(
            self.client.get_messages,
            {
                "anchor": "newest",
                "num_before": self._history_max_messages,
                "num_after": 0,
                "narrow": narrow,
            },
            timeout=history.HISTORY_FETCH_TIMEOUT,
        )
        if not isinstance(result, dict) or result.get("result") != "success":
            return []
        messages = result.get("messages")
        return messages if isinstance(messages, list) else []

    async def _harvest_history_block(self, stream_name: str, topic: str) -> str:
        """Best-effort quoted slice of the real topic history (#148).

        Streams/topics only — DMs have their own per-user sessions and are
        never harvested across. Any error response or timeout is logged and
        dropped, so a slow or failing Zulip API can never fail or unboundedly
        delay a dispatch.
        """
        try:
            messages = await asyncio.wait_for(
                self._fetch_stream_history(stream_name, topic),
                timeout=history.HISTORY_FETCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "zulip history harvest timed out (dropped) [stream=%s topic=%s]",
                mask_pii(stream_name),
                mask_pii(topic),
            )
            return ""
        except Exception as e:
            logger.warning(
                "zulip history harvest failed (dropped): %s", mask_pii(str(e))
            )
            return ""
        if not messages:
            return ""
        return history.render_history_block(
            messages,
            max_messages=self._history_max_messages,
            window_hours=self._history_window_hours,
            max_chars=self._history_max_chars,
        )

    async def _handle_message(self, message: dict):
        """Process an incoming Zulip message.

        Thin delegate: the inbound gate chain lives in ``zulip.inbound`` so
        it is testable without a live adapter. This method stays — and stays
        callable — because the connection event loop and the recovery path
        call ``adapter._handle_message`` by name. ``self`` is passed through
        as the collaborator, so every gate reads adapter state at call time.
        """
        await inbound.handle_message(self, message)

    async def _resolve_reaction_user(self, user_id: str):
        """Resolve the reacting user's email (a reaction event carries no user
        object). Returns ``(email, full_name)``; email is ``None`` on failure."""
        user = await self.get_user_info(user_id)
        if user and user.get("email"):
            return user["email"], (user.get("full_name") or "")
        return None, None

    async def _fetch_message(self, message_id: str) -> Optional[dict]:
        """Fetch a single Zulip message by id, bounded and best-effort."""
        try:
            mid = self._validate_message_id(message_id)
        except ValueError:
            return None
        try:
            result = await self._sdk_call(
                self.client.get_raw_message, mid, timeout=self._read_timeout
            )
        except Exception as e:
            logger.warning(
                "zulip reaction trigger: message fetch failed [id=%s error=%s]",
                mask_pii(message_id),
                mask_pii(str(e)),
            )
            return None
        if isinstance(result, dict) and result.get("result") == "success":
            message = result.get("message")
            if isinstance(message, dict):
                return message
        return None

    async def _handle_reaction_event(self, event: dict) -> None:
        """Turn a configured reaction on the bot's own message into a turn.

        The synthetic turn carries the *reacting human* as its sender, so
        every authorisation and rate-limit decision is made about them. A
        reaction is never an authorisation bypass. (Epic #149 / #163)
        """
        matched = match_reaction_trigger(event, self._reaction_trigger_cfg)
        if not matched:
            return
        try:
            dedupe_key = reaction_dedupe_key(
                matched.message_id, matched.emoji, matched.user_id
            )
            if self._dedupe.check(dedupe_key):
                logger.debug(
                    "zulip reaction trigger dedupe hit [msg=%s emoji=%s]",
                    mask_pii(matched.message_id),
                    matched.emoji,
                )
                return

            user_email, user_name = await self._resolve_reaction_user(
                matched.user_id
            )
            if not user_email:
                # Fail closed *and say so*: dispatching without an authorizable
                # sender would only produce a silent policy drop. (#164)
                logger.warning(
                    "zulip reaction trigger dropped: could not resolve the "
                    "reacting user's email [user_id=%s msg=%s emoji=%s]",
                    mask_pii(matched.user_id),
                    mask_pii(matched.message_id),
                    matched.emoji,
                )
                await self._audit_logger.log_event(
                    "reaction_trigger_dropped",
                    {
                        "message_id": matched.message_id,
                        "emoji": matched.emoji,
                        "user_id": matched.user_id,
                        "reason": "unresolved_sender",
                    },
                )
                return

            target = await self._fetch_message(matched.message_id)
            if not is_eligible_target_message(
                target,
                any_message=self._reaction_trigger_cfg.any_message,
                bot_user_id=getattr(self, "_bot_user_id", ""),
                bot_email=self.email or "",
            ):
                logger.info(
                    "zulip reaction trigger ignored: ineligible target "
                    "[msg=%s emoji=%s]",
                    mask_pii(matched.message_id),
                    matched.emoji,
                )
                return

            stream_name = str(target.get("display_recipient") or "").strip()
            if not stream_name:
                logger.info(
                    "zulip reaction trigger ignored: no stream name [msg=%s]",
                    mask_pii(matched.message_id),
                )
                return
            if (
                self._streams_filter is not None
                and stream_name.lower() not in self._streams_filter
            ):
                logger.info(
                    "zulip reaction trigger ignored: stream not monitored "
                    "[stream=%s]",
                    mask_pii(stream_name),
                )
                return
            topic = str(target.get("subject") or "").strip() or "general"

            logger.info(
                "zulip reaction trigger fired [msg=%s emoji=%s sender=%s "
                "stream=%s topic=%s]",
                mask_pii(matched.message_id),
                matched.emoji,
                mask_pii(user_email),
                mask_pii(stream_name),
                mask_pii(topic),
            )
            await self._audit_logger.log_event(
                "reaction_trigger",
                {
                    "message_id": matched.message_id,
                    "emoji": matched.emoji,
                    "sender_id": user_email,
                    "stream": stream_name,
                    "topic": topic,
                },
            )

            synthetic = build_reaction_trigger_message(
                target,
                matched,
                stream_name,
                topic,
                user_email=user_email,
                user_name=user_name,
            )
            await self._handle_message(synthetic)
        except Exception as e:
            logger.error(
                "zulip reaction trigger failed [msg=%s error=%s]",
                mask_pii(matched.message_id),
                mask_pii(str(e)),
            )

    async def _check_reaction_trigger_subscriptions(self) -> None:
        """Warn about monitored streams the bot is not subscribed to (#164).

        Zulip only delivers ``reaction`` events for streams the user is
        subscribed to, while ``message`` events arrive anyway, so an
        unsubscribed monitored stream looks healthy while the trigger silently
        never fires.
        """
        if not self._reaction_trigger_cfg.enabled:
            return
        try:
            result = await self._sdk_call(
                self.client.get_subscriptions, {}, timeout=self._read_timeout
            )
        except Exception as e:
            logger.warning(
                "zulip reaction trigger subscription check failed: %s",
                mask_pii(str(e)),
            )
            return
        subscriptions = (
            result.get("subscriptions", []) if isinstance(result, dict) else []
        )
        subscribed = [
            str(s.get("name") or "")
            for s in subscriptions
            if isinstance(s, dict)
        ]
        if self._streams_filter is None:
            # "*" cannot be enumerated, so report what we are subscribed to
            # instead of pretending we verified the monitor set.
            logger.info(
                "zulip reaction triggers enabled; monitoring all streams; "
                "subscribed to: %s",
                ", ".join(sorted(n for n in subscribed if n)) or "(none)",
            )
            return
        missing = find_unsubscribed_streams(
            sorted(self._streams_filter), subscribed
        )
        if missing:
            logger.warning(
                "zulip reaction triggers will not fire in monitored streams "
                "the bot is not subscribed to: %s",
                ", ".join(missing),
            )
            await self._audit_logger.log_event(
                "reaction_trigger_subscription_gap",
                {"streams": missing},
            )
        else:
            logger.info(
                "zulip reaction triggers: bot is subscribed to every "
                "monitored stream"
            )

    # ── BasePlatformAdapter data surface (zulip.platform_api) ───────────────
    # The read/query half of the host contract lives in ``zulip.platform_api``.
    # These overrides stay — the host calls them by name on the adapter and the
    # signatures are part of the contract — and each one is a one-line delegate,
    # so the behaviour has exactly one home.
    async def resolve_topic(self, stream_id: int, topic: str) -> dict[str, Any]:
        """Mark a topic as resolved by prepending ✔.

        Returns the API response dict. If the topic is already resolved,
        returns early without calling the API.
        """
        return await platform_api.resolve_topic(self, stream_id, topic)

    async def fetch_messages(
        self,
        stream: str,
        topic: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch recent messages from a stream, optionally filtered by topic."""
        return await platform_api.fetch_messages(self, stream, topic, limit)

    async def search_messages(
        self,
        query: str,
        stream: Optional[str] = None,
        topic: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Search messages by query, optionally scoped to stream/topic."""
        return await platform_api.search_messages(self, query, stream, topic, limit)

    async def list_streams(self) -> list[dict]:
        """List all streams the bot can see."""
        return await platform_api.list_streams(self)

    async def subscribe_stream(self, stream_name: str) -> bool:
        """Subscribe the bot to a stream."""
        return await platform_api.subscribe_stream(self, stream_name)

    async def delete_message(self, message_id: int) -> bool:
        """Delete a message by ID."""
        return await platform_api.delete_message(self, message_id)

    async def get_user_presence(self, user_id_or_email: str) -> Optional[dict]:
        """Get presence status for a user."""
        return await platform_api.get_user_presence(self, user_id_or_email)

    async def star_message(self, message_id: int, starred: bool = True) -> bool:
        """Star or unstar a message."""
        return await platform_api.star_message(self, message_id, starred)

    async def get_user_info(self, user_id_or_email: str) -> Optional[dict]:
        """Get information about a user, by numeric id or email address."""
        return await platform_api.get_user_info(self, user_id_or_email)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image as a native Zulip attachment (overrides the
        base class's "unavailable" stub — MEDIA:<path> screenshots were
        silently failing to deliver on Zulip; see #123).

        Zulip has no separate "photo" primitive: an uploaded file becomes an
        inline image automatically when its URL is embedded in message
        markdown (``![name](url)``), so this just uploads via
        ``media.upload_file_to_zulip()`` and sends that markdown as the body.
        """
        return await outbound.send_image_file(
            self,
            chat_id,
            image_path,
            caption,
            reply_to,
            metadata,
            **kwargs,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local file as a native Zulip attachment (overrides the
        base class's "unavailable" stub)."""
        return await outbound.send_document(
            self,
            chat_id,
            file_path,
            caption,
            file_name,
            reply_to,
            metadata,
            **kwargs,
        )

    # ── Native exec-approval buttons (zform widget) ─────────────────────────
    # The widget payload, the plain-text fallback and the two-message send path
    # live in ``zulip.approvals``. These delegates stay because the gateway
    # enables native-button mode exactly when ``ZulipAdapter`` defines
    # ``_send_exec_approval_prompt`` in its own ``__dict__`` (the base class
    # ships a no-op), and to keep the historical ``adapter._<name>`` call sites
    # working. The reply/instruction tables are re-exported for the drift guard
    # in tests/test_exec_approval_zform.py.
    _EA_ZFORM_REPLY: dict[str, str] = approvals._EA_ZFORM_REPLY
    _EA_ZFORM_INSTRUCTIONS: dict[str, str] = approvals._EA_ZFORM_INSTRUCTIONS

    def _zform_widget_for_approval(self, prompt: ExecApprovalPrompt) -> Optional[str]:
        return approvals.zform_widget_for_approval(prompt)

    def _approval_fallback_instructions(self, prompt: ExecApprovalPrompt) -> str:
        return approvals.approval_fallback_instructions(prompt)

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        return await approvals.send_exec_approval_prompt(self, prompt)

    # ── Outbound content egress (zulip.outbound) ────────────────────────────
    # The send path, chunking, media egress, the outbound secret guard, the
    # ref rendering and the typing indicators all live in ``zulip.outbound``.
    # These delegates keep the historical ``adapter._<name>`` call sites and the
    # ``BasePlatformAdapter`` contract methods (``send``, ``send_typing``,
    # ``stop_typing``, ``send_image_file``, ``send_document``) callable on the
    # adapter with identical signatures.
    def _known_secrets(self) -> list[KnownSecret]:
        """Credential values that must never be transmitted (Issue #136).

        Memoised on the adapter: the credential set does not change at runtime,
        and re-walking the config and environment on every send would be
        wasted work.
        """
        return outbound.known_secrets(self)

    async def _refuse_secret_leak(
        self, hits: list[KnownSecret], chat_id: str
    ) -> str:
        """Audit a refused send, naming only *where* the credential came from.

        The value itself is never logged or audited: a message describing a
        leak must not become one. (Issue #136)
        """
        return await outbound.refuse_secret_leak(self, hits, chat_id)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to=None,
        metadata=None,
        media_files=None,
    ) -> SendResult:
        """Send message to a Zulip stream or DM, with chunking, topic directives, and files."""
        return await outbound.send(
            self,
            chat_id,
            content,
            reply_to=reply_to,
            metadata=metadata,
            media_files=media_files,
        )

    async def _send_single(
        self,
        chat_id: str,
        content: str,
        metadata: dict,
        topic_override: Optional[str],
    ) -> SendResult:
        """Send a single (unchunked) message."""
        return await outbound.send_single(
            self, chat_id, content, metadata, topic_override
        )


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    # Activity-trace tool checkpoints (epic #139 / #159).
    #
    # Registered ONLY when the trace is enabled: any registration flips
    # has_hook("post_tool_call") true and switches on host-side dispatch, so
    # gating by *not registering* is what keeps a disabled trace free.
    #
    # ``pre_tool_call`` is deliberately never registered — it is fail-closed
    # (it returns (block_message, modified_args)), so a status observer could
    # block the agent's own tool call.
    if tracing.trace_config().enabled:
        try:
            ctx.register_hook("post_tool_call", _on_post_tool_call)
        except Exception as e:
            logger.warning("zulip: could not register post_tool_call hook: %s", e)

    # Mode B (epic #139 / #160): let the agent narrate intent that hooks cannot
    # infer. Registered only when the trace is enabled — registration is what
    # exposes the tool to the model, so a disabled trace must not advertise one.
    if tracing.trace_config().enabled:
        try:
            ctx.register_tool(
                name="zulip_progress",
                toolset="zulip",
                schema={
                    "name": "zulip_progress",
                    "description": (
                        "Show a short progress note on this conversation's activity "
                        "board while you work. Use it for intent that a tool call does "
                        "not reveal, e.g. 'about to ask a clarifying question' or "
                        "'switching approach'."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "note": {
                                "type": "string",
                                "description": "Short status line, e.g. 'checking the logs'.",
                            }
                        },
                        "required": ["note"],
                    },
                },
                handler=_zulip_progress_handler,
                description="Show a short progress note on the activity trace",
                emoji="\U0001f4dd",
            )
        except Exception as e:
            logger.warning("zulip: could not register zulip_progress tool: %s", e)

    ctx.register_platform(
        name="zulip",
        label="Zulip",
        adapter_factory=lambda cfg: ZulipAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"],
        install_hint="pip install zulip",
        env_enablement_fn=_env_enablement,
        allowed_users_env="ZULIP_ALLOWED_USERS",
        allow_all_env="ZULIP_ALLOW_ALL_USERS",
        # Lets Hermes cron accept ``deliver: zulip[:<stream_id>]`` targets.
        # Without this, cron preflight rejects the job as "not a known cron
        # delivery target" and never runs it. The env var supplies the default
        # stream when no explicit id is given.
        cron_deliver_env_var="ZULIP_HOME_CHANNEL",
        # Out-of-process delivery (``hermes cron run``, cron in its own
        # process): without this, Hermes has no way to send to Zulip when no
        # gateway adapter is live and reports "No live adapter for platform".
        standalone_sender_fn=_standalone_send,
        max_message_length=10000,
        platform_hint=(
            "You are chatting via Zulip. Messages are organized into streams and topics. "
            "When replying to a stream message, preserve the original topic unless asked to change it."
        ),
        emoji="📬",
        setup_fn=interactive_setup,
    )
