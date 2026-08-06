"""
Zulip Platform Adapter for Hermes Gateway (Plugin)

Bi-directional integration with Zulip chat platform.
Supports stream messages (with topics) and private messages.
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Any, overload

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform, PlatformConfig

# Use relative imports for internal modules so the plugin works
# regardless of how Hermes loads it (bundled, user path, etc.)
from .logger import format_zulip_log, mask_pii
from .text_utils import (
    chunk_text,
    extract_topic_directive,
    strip_onchar_prefix,
    resolve_onchar_prefixes,
    create_mention_regex,
    normalize_mention,
    strip_html_to_text,
)
from .media import upload_file_to_zulip
from .queue_manager import ZulipQueueManager
from .dedupe_store import ZulipDedupeStore
from .reactions import ReactionConfig, ReactionLifecycle
from .version import __version__, __repo__
from .commands import handle_command, is_command
from .policy import PolicyEngine
from . import updater
from .probe import probe_zulip, _normalize_base_url
from .recovery import recover_interrupted_messages
from .rate_limiter import RateLimiter
from .audit_logger import AuditLogger

logger = logging.getLogger(__name__)

# Max input string length to prevent DoS via huge query strings
_MAX_INPUT_LENGTH = 10000
_MAX_JSON_OVERRIDES_BYTES = 10240  # 10KB max for ZULIP_STREAM_OVERRIDES


def _validate_string_length(value: Any, name: str, max_length: int = _MAX_INPUT_LENGTH) -> str:
    """Validate and truncate a string input to prevent DoS.

    Raises ValueError if the value is not a string or exceeds max_length.
    """
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds maximum length ({len(value)} > {max_length})")
    return value

# Module-level SDK handle — updated by _import_zulip_sdk()
zulip = None  # type: ignore

# ------------------------------------------------------------------
# Performance: client + target caching (Issue #49)
# ------------------------------------------------------------------
_MAX_CLIENT_CACHE = 50
_MAX_TARGET_CACHE = 500

_client_cache: OrderedDict[str, Any] = OrderedDict()
_target_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()

# Parsed ZULIP_STREAM_OVERRIDES, keyed on the raw environment string so the
# value stays live-reloadable while a busy stream does not re-parse JSON on
# every inbound message.
_stream_overrides_cache: tuple[str, dict[str, dict[str, Any]]] = ("", {})


def _get_cached_client(site: str, email: str, api_key: str, *, _zulip_mod: Any = None) -> Any:
    """Return a cached Zulip client or create a new one.

    LRU eviction keeps the most-recently-used clients.
    """
    key = f"{site}\x00{email}\x00{api_key}"
    client = _client_cache.pop(key, None)
    if client is not None:
        _client_cache[key] = client
        return client

    _zulip = _zulip_mod or _import_zulip_sdk()
    if _zulip is None:
        raise ImportError("zulip package not installed")

    client = _zulip.Client(email=email, api_key=api_key, site=site)

    if len(_client_cache) >= _MAX_CLIENT_CACHE:
        oldest = next(iter(_client_cache))
        del _client_cache[oldest]

    _client_cache[key] = client
    return client


def _get_cached_target(chat_id: str) -> dict[str, Any] | None:
    """Return cached target info or None.

    Target info: {"type": "dm", "user_id": int} | {"type": "stream", "stream_id": int}
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


def _parse_target(chat_id: str) -> dict[str, Any]:
    """Parse chat_id into target info, using cache if available."""
    cached = _get_cached_target(chat_id)
    if cached is not None:
        return cached

    if chat_id.startswith("dm:"):
        info = {"type": "dm", "user_id": int(chat_id[3:])}
    else:
        info = {"type": "stream", "stream_id": int(chat_id)}

    _set_cached_target(chat_id, info)
    return info


def _clear_caches() -> None:
    """Clear all caches. Used by tests and for resource cleanup."""
    _client_cache.clear()
    _target_cache.clear()
ZULIP_AVAILABLE = False


def _import_zulip_sdk():
    """Lazy-import the zulip SDK, bypassing plugin shadow if needed.

    Hermes adds ~/.hermes/plugins/ to sys.path, so a directory named
    'zulip' shadows the pip-installed zulip package. We temporarily
    remove the shadowed entry from sys.modules to force Python to
    re-resolve to the real SDK.
    """
    import sys

    global ZULIP_AVAILABLE, zulip
    if ZULIP_AVAILABLE and zulip is not None:
        return zulip

    # Remove any shadowed plugin entry so Python resolves the real SDK
    _shadow = sys.modules.pop("zulip", None)
    try:
        import zulip as _sdk

        zulip = _sdk
        ZULIP_AVAILABLE = True
        return _sdk
    except ImportError:
        zulip = None
        ZULIP_AVAILABLE = False
        return None
    finally:
        # Restore the shadowed plugin entry so Hermes/other imports
        # that expect the zulip package continue to work
        if _shadow is not None:
            sys.modules["zulip"] = _shadow


# Chunking defaults (overridable via env)
DEFAULT_CHUNK_LIMIT = 10000  # Hermes registry max_message_length
DEFAULT_CHUNK_MODE = "length"

# Timeout defaults (seconds) — Issue #62
DEFAULT_CONNECT_TIMEOUT = 30.0
DEFAULT_READ_TIMEOUT = 60.0
DEFAULT_SEND_TIMEOUT = 90.0

# Typing indicator delay (seconds) — how long to keep typing visible after
# the API confirms the message was sent, so the response is visible in the UI
# before the typing indicator stops and the success reaction appears.
DEFAULT_TYPING_DELAY = 2.0


def _resolve_chunk_config() -> tuple[int, str]:
    """Read chunking config from environment."""
    limit_raw = os.getenv("ZULIP_TEXT_CHUNK_LIMIT", "").strip()
    limit = int(limit_raw) if limit_raw.isdigit() else DEFAULT_CHUNK_LIMIT
    mode = os.getenv("ZULIP_CHUNK_MODE", DEFAULT_CHUNK_MODE).strip()
    if mode not in ("length", "newline"):
        mode = DEFAULT_CHUNK_MODE
    return limit, mode


def _resolve_timeouts() -> tuple[float, float, float]:
    """Read timeout config from environment.

    Returns (connect_timeout, read_timeout, send_timeout) in seconds.
    """
    def _parse(val: str, default: float) -> float:
        try:
            return float(val.strip())
        except (ValueError, AttributeError):
            return default

    connect = _parse(os.getenv("ZULIP_CONNECT_TIMEOUT", ""), DEFAULT_CONNECT_TIMEOUT)
    read = _parse(os.getenv("ZULIP_READ_TIMEOUT", ""), DEFAULT_READ_TIMEOUT)
    send = _parse(os.getenv("ZULIP_SEND_TIMEOUT", ""), DEFAULT_SEND_TIMEOUT)
    return connect, read, send


def _resolve_typing_delay() -> float:
    """Read typing indicator delay from environment.

    After the message is accepted by the Zulip API, the typing indicator
    stays active for this many seconds so the response has time to propagate
    to all clients before the indicator stops and the success reaction fires.
    """
    try:
        val = float(os.getenv("ZULIP_TYPING_DELAY_SECONDS", "").strip())
        return max(0.0, val)
    except (ValueError, AttributeError):
        return DEFAULT_TYPING_DELAY


def _resolve_streams_filter() -> set[str] | None:
    """Read stream filtering config from environment.

    Returns None if all streams are allowed (default), or a set of
    lowercase stream names to monitor.
    """
    raw = os.getenv("ZULIP_STREAMS", "").strip()
    if not raw or raw == "*":
        return None
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def _resolve_response_prefix() -> str:
    """Read outbound response prefix from environment."""
    return os.getenv("ZULIP_RESPONSE_PREFIX", "")


def _resolve_stream_overrides() -> dict[str, dict[str, Any]]:
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
    raw = os.getenv("ZULIP_STREAM_OVERRIDES", "").strip()
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
def _resolve_chatmode() -> tuple[str, list[str], bool]:
    ...


@overload
def _resolve_chatmode(stream_name: str) -> tuple[str, list[str], bool]:
    ...


def _resolve_chatmode(stream_name: Optional[str] = None) -> tuple[str, list[str], bool]:
    """Read stream trigger mode config from environment.

    When ``stream_name`` is supplied, a matching entry in
    ``ZULIP_STREAM_OVERRIDES`` takes precedence over the global
    ``ZULIP_CHATMODE`` for that stream only.
    """
    mode = os.getenv("ZULIP_CHATMODE", "onmessage").strip().lower()
    if mode not in ("onmessage", "oncall", "onchar"):
        mode = "onmessage"
    prefixes = resolve_onchar_prefixes(os.getenv("ZULIP_ONCHAR_PREFIXES", ""))
    require_mention = os.getenv("ZULIP_REQUIRE_MENTION", "true").strip().lower() not in ("false", "0", "no", "off")

    if stream_name:
        override = _resolve_stream_overrides().get(stream_name.strip().lower())
        if override:
            mode = override.get("chatmode", mode)

    return mode, prefixes, require_mention


def _message_with_flags(event: dict) -> dict:
    """Return the event's message with Zulip's per-user flags attached.

    Zulip delivers flags on the *event*, as a sibling of ``message``, while its
    REST API returns them on the message itself. Downstream code should only
    have one place to look, and losing them here means losing the authoritative
    ``mentioned`` signal.

    An existing ``flags`` key on the message is left alone.
    """
    message = event.get("message") or {}
    if "flags" not in message:
        message["flags"] = event.get("flags") or []
    return message


def _topic_sessions_enabled() -> bool:
    """Whether each Zulip topic should get its own conversation session.

    Off by default. When enabled, the topic is passed to ``build_source`` as
    ``thread_id``, which is what Hermes scopes session state by — so each topic
    in a stream becomes an independent conversation instead of all topics
    sharing one.

    This is opt-in because turning it on splits an existing stream's history
    into per-topic sessions, which changes what an agent remembers.
    """
    return os.getenv("ZULIP_TOPIC_SESSIONS", "").strip().lower() in ("true", "1", "yes", "on")


def _safe_delete_temp_file(file_path: str) -> None:
    """Delete a local file only if it resides under /tmp or a bot workspace.

    Prevents accidental deletion of user-owned files outside temp dirs.
    Uses stat with follow_symlinks=False to prevent TOCTOU symlink swaps.
    Errors are logged, not raised.
    """
    try:
        p = Path(file_path).resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
        ws = tmp / "hermes_bot_workspace"
        if not (str(p).startswith(str(tmp)) or str(p).startswith(str(ws))):
            return
        # Atomic stat + unlink to prevent TOCTOU symlink race
        st = p.stat(follow_symlinks=False)
        if st.st_ino != p.resolve().stat().st_ino:
            logger.warning(
                "temp file cleanup skipped: symlink detected [path=%s]",
                mask_pii(file_path),
            )
            return
        p.unlink()
        logger.debug("cleaned up temp file [path=%s]", mask_pii(file_path))
    except OSError as e:
        logger.warning("temp file cleanup failed [path=%s]: %s", mask_pii(file_path), e)


class ZulipAdapter(BasePlatformAdapter):
    """Zulip platform adapter for Hermes Gateway."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("zulip"))
        extra = config.extra or {}

        self.api_key = os.getenv("ZULIP_API_KEY") or extra.get("api_key", "")
        self.email = os.getenv("ZULIP_EMAIL") or extra.get("email", "")
        self.site = os.getenv("ZULIP_SITE") or extra.get("site", "")
        # Populated on connect. Zulip renders mentions from the display name,
        # not the email local-part, so mention matching needs it.
        self.bot_full_name = ""

        # Validate site URL to prevent SSRF before creating client
        if self.site:
            validated = _normalize_base_url(self.site)
            if not validated:
                raise ValueError(f"Invalid or unsafe ZULIP_SITE: {self.site}")
            self.site = validated

        _zulip = _import_zulip_sdk()
        if not _zulip:
            logger.error(
                "zulip package not installed. Run: pip install zulip"
            )
            raise ImportError(
                "zulip package not installed. Run: pip install zulip"
            )

        # Use cached client if available (avoids repeated base64 encoding + object creation)
        self.client = _get_cached_client(self.site, self.email, self.api_key, _zulip_mod=_zulip)

        # Track latest topic per stream so replies stay threaded
        self._topic_cache: dict[str, str] = {}
        # Context-mitigation state
        self._last_topic_cache: dict[str, str] = {}      # stream_id → previous topic
        self._message_counts: dict[str, int] = {}        # chat_id → message count
        self._last_message_time: dict[str, float] = {}   # chat_id → last message epoch
        # DM session rotation: prevents context bloat in long conversations
        self._dm_session_turn_limit = int(
            os.getenv("ZULIP_DM_SESSION_TURN_LIMIT", "20").strip()
        )
        self._dm_base_message_counts: dict[str, int] = {}  # base_session_key → turn count

        # Block streaming config (Issue #49 — requires gateway-level streaming support)
        self._block_streaming = (
            os.getenv("ZULIP_BLOCK_STREAMING", "").strip().lower() in ("true", "1", "yes", "on")
        )

        self._data_dir = os.environ.get("HERMES_DATA_DIR", os.path.expanduser("~/.hermes"))

        # Timeout configuration (Issue #62)
        self._connect_timeout, self._read_timeout, self._send_timeout = _resolve_timeouts()

        # Typing indicator delay (Issue #96)
        self._typing_delay = _resolve_typing_delay()

        # Stream filtering (Issue #65) — None means all streams
        self._streams_filter = _resolve_streams_filter()

        # Response prefix (Issue #65) — prepended to every outbound message
        self._response_prefix = _resolve_response_prefix()

        # Rate limiter (per-sender, sliding window)
        self._rate_limiter = RateLimiter(
            max_per_minute=int(
                os.getenv("ZULIP_MAX_MESSAGES_PER_MINUTE", "60").strip()
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
            register_fn=lambda: self.client.register(
                event_types=["message"], fetch_event_id=0
            ),
        )
        self._dedupe = ZulipDedupeStore(
            account_id=self.email or "default",
            data_dir=self._data_dir,
            ttl_ms=300_000,
            max_size=2000,
        )
        self._dedupe.load()

        # Reaction config
        self._reaction_cfg = ReactionConfig.from_env()

        # DM policy engine (Issue #48 — controls who can DM the bot)
        self._policy = PolicyEngine(data_dir=self._data_dir)

        self._listening = False
        self._event_task: Optional[asyncio.Task] = None
        self._presence_task: Optional[asyncio.Task] = None

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
        if typing_params is None:
            return
        params = dict(typing_params)
        params["op"] = "stop"
        try:
            await self._sdk_call(
                self.client.set_typing_status,
                params,
                timeout=self._send_timeout,
            )
        except Exception:
            pass

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
        logger.info("Zulip adapter connecting...")

        # 0. Pre-flight health probe (side-effect free)
        probe_result = await probe_zulip(self.site, self.email, self.api_key, timeout=10)
        if not probe_result.get("ok"):
            error = probe_result.get("error", "unknown")
            logger.error(
                format_zulip_log(
                    "zulip probe failed",
                    site=mask_pii(self.site),
                    error=error,
                )
            )
            raise ConnectionError(f"Zulip probe failed: {error}")

        bot = probe_result.get("bot", {})
        logger.info(
            format_zulip_log(
                "zulip probe ok",
                bot=mask_pii(bot.get("full_name", "Unknown")),
                id=bot.get("id"),
            )
        )

        # 1. Verify server is reachable (no auth required)
        try:
            settings = await self._sdk_call(
                self.client.get_server_settings,
                timeout=self._connect_timeout,
            )
            if settings.get("result") != "success":
                raise ConnectionError(
                    f"Cannot reach Zulip server: {self.site}"
                )
        except Exception as e:
            logger.error(
                format_zulip_log(
                    "zulip server unreachable",
                    site=mask_pii(self.site),
                    error=mask_pii(str(e)),
                )
            )
            raise ConnectionError(f"Cannot reach Zulip server: {self.site}") from e

        # 2. Validate credentials with lightweight profile call
        try:
            result = await self._sdk_call(
                self.client.get_profile,
                timeout=self._connect_timeout,
            )
            if result.get("result") != "success":
                raise ConnectionError(f"Zulip authentication failed: {result}")
            bot_name = result.get("full_name", "Unknown")
            self.bot_full_name = result.get("full_name") or ""
            logger.info(
                format_zulip_log(
                    "zulip bot authenticated",
                    bot=mask_pii(bot_name),
                )
            )
        except Exception as e:
            logger.error(
                format_zulip_log(
                    "zulip authentication error",
                    error=mask_pii(str(e)),
                )
            )
            raise

        # 3. Log subscriptions so admins know what streams the bot sees
        try:
            subs = await self._sdk_call(
                self.client.get_subscriptions,
                timeout=self._connect_timeout,
            )
            if subs.get("result") == "success":
                stream_names = [s["name"] for s in subs.get("subscriptions", [])]
                if stream_names:
                    logger.info(
                        "zulip bot subscribed to %d stream(s)",
                        len(stream_names),
                    )
                else:
                    logger.warning(
                        "zulip bot not subscribed to any streams — "
                        "stream messages will be invisible"
                    )
        except Exception:
            # Non-fatal: subscription info is advisory
            pass

        logger.info(
            format_zulip_log(
                "zulip connection established",
                site=mask_pii(self.site),
            )
        )

        # Structured health status for monitoring tools
        logger.info(
            "health_status=connected platform=zulip site=%s account=%s",
            mask_pii(self.site),
            mask_pii(self.email),
        )

        # Start presence heartbeat so bot appears online
        self._presence_task = asyncio.create_task(self._presence_heartbeat())

        # Check for plugin updates on startup
        updater.startup_version_check(__version__, __repo__)

        # Ensure queue is registered before starting listener
        await self._queue_mgr.ensure_queue()

        # Recover interrupted messages from previous gateway instance
        bot_user_id = str(probe_result.get("bot", {}).get("id", ""))
        asyncio.create_task(
            recover_interrupted_messages(
                client=self.client,
                bot_email=self.email,
                bot_user_id=bot_user_id,
                reaction_start=self._reaction_cfg.on_start,
                reaction_success=self._reaction_cfg.on_success,
                reaction_error=self._reaction_cfg.on_error,
                handle_message=self._handle_message,
                sdk_call=self._sdk_call,
                send_timeout=self._send_timeout,
            )
        )

        self._listening = True
        self._event_task = asyncio.create_task(self._listen_for_events())
        self._mark_connected()
        return True

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Get information about a chat/channel."""
        if chat_id.startswith("dm:"):
            return {"name": chat_id, "type": "dm"}
        return {"name": chat_id, "type": "stream"}

    async def disconnect(self) -> None:
        """Stop listening and close connection."""
        self._listening = False
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        if self._presence_task:
            self._presence_task.cancel()
            try:
                await self._presence_task
            except asyncio.CancelledError:
                pass
        self._mark_disconnected()
        logger.info("Zulip adapter disconnected")
        logger.info(
            "health_status=disconnected platform=zulip site=%s account=%s",
            mask_pii(self.site),
            mask_pii(self.email),
        )

    async def _presence_heartbeat(self):
        """Keep bot presence active while connected."""
        while self._listening:
            try:
                await self._sdk_call(
                    self.client.update_presence,
                    {"status": "active", "ping_only": False},
                    timeout=self._send_timeout,
                )
            except Exception:
                pass  # presence is best-effort
            await asyncio.sleep(60)

    async def _listen_for_events(self):
        """Listen for incoming Zulip messages via persistent event queue."""
        logger.info("zulip adapter listening [account=%s]", mask_pii(self.email))

        while self._listening:
            try:
                queue = await self._queue_mgr.ensure_queue()

                events = await self._sdk_call(
                    self.client.get_events,
                    queue_id=queue.queue_id,
                    last_event_id=queue.last_event_id,
                    timeout=self._read_timeout,
                )

                if events.get("result") == "error":
                    msg = events.get("msg", "")
                    is_bad_queue = (
                        events.get("code") == "BAD_EVENT_QUEUE_ID"
                        or "bad event queue" in msg.lower()
                    )
                    if is_bad_queue:
                        logger.warning("zulip queue expired, re-registering")
                        self._queue_mgr.mark_queue_expired()
                        continue
                    logger.warning(
                        format_zulip_log(
                            "zulip event queue error",
                            error=mask_pii(msg),
                        )
                    )
                    await asyncio.sleep(1)
                    continue

                batch_max_event_id = queue.last_event_id
                processing_tasks = []
                for event in events.get("events", []):
                    event_id = event["id"]
                    if event_id > batch_max_event_id:
                        batch_max_event_id = event_id
                    if event.get("type") == "message":
                        msg = _message_with_flags(event)
                        msg_id = str(msg.get("id", ""))
                        # Dedupe check
                        if self._dedupe.check(msg_id):
                            logger.debug("zulip dedupe hit [msg=%s]", mask_pii(msg_id))
                            continue
                        # Process messages concurrently so a slow model call
                        # does not block the poll loop for unrelated messages.
                        # Per-session serialization is handled by the gateway.
                        task = asyncio.create_task(self._handle_message(msg))
                        processing_tasks.append(task)

                # Fire-and-forget: don't await processing tasks here so the
                # poll loop keeps fetching events. Errors are logged inside
                # _handle_message.

                # Batch update event ID
                if batch_max_event_id > queue.last_event_id:
                    self._queue_mgr.update_last_event_id(batch_max_event_id)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    format_zulip_log(
                        "zulip event polling error",
                        error=mask_pii(str(e)),
                    )
                )
                await asyncio.sleep(5)

    async def _handle_message(self, message: dict):
        """Process incoming Zulip message."""
        # Filter self-messages to prevent loops
        if message.get("sender_email") == self.email:
            return

        msg_type = message.get("type")  # "stream" or "private"
        content = message.get("content", "")
        message_id = message.get("id")
        sender_email = message.get("sender_email", "")
        sender_full_name = message.get("sender_full_name", "Unknown")

        # --- Rate limiting (per-sender) ---
        sender_key = sender_email or str(message.get("sender_id", ""))
        if not self._rate_limiter.check(sender_key):
            logger.warning(
                "zulip rate limit hit [sender=%s msg=%s]",
                mask_pii(sender_key),
                mask_pii(str(message_id)),
            )
            await self._audit_logger.log_rate_limit_exceeded(
                sender_id=sender_key,
                limit=self._rate_limiter.config["max_per_minute"],
            )
            return

        # Strip Zulip @-mention syntax and HTML
        content = strip_html_to_text(content)

        # --- Reactions ---
        # Constructed here so the error path below can reach it, but not
        # started until the message has cleared every drop path. See the
        # acknowledgement block after stream gating.
        reactions = ReactionLifecycle(
            self.client, str(message_id), self._reaction_cfg,
            timeout=self._send_timeout,
        )

        # --- Stream trigger gating ---
        if msg_type == "stream":
            # str() guard: Zulip sends a string here for stream messages, but a
            # malformed event would otherwise raise AttributeError inside the
            # handler rather than being skipped.
            stream_name = str(message.get("display_recipient", ""))
            chatmode, onchar_prefixes, require_mention = _resolve_chatmode(stream_name)

            # Check onchar trigger
            onchar_triggered, stripped = strip_onchar_prefix(content, onchar_prefixes)
            if onchar_triggered:
                content = stripped

            # Check mention (simple substring; bot username from email prefix)
            # Mention detection.
            #
            # Zulip's own "mentioned" flag is authoritative: the server sets it
            # for personal mentions regardless of which markup form was used,
            # so it covers @**Name**, @_**Name** and @**Name|id** without this
            # adapter having to parse any of them. Text matching is only a
            # fallback for events that arrive without flags.
            bot_username = self.email.split("@")[0] if self.email else ""
            mention_regex = (
                create_mention_regex(bot_username, self.bot_full_name)
                if bot_username
                else None
            )
            was_mentioned = "mentioned" in (message.get("flags") or [])
            if not was_mentioned and mention_regex:
                was_mentioned = bool(mention_regex.search(content))

            # Apply gating
            should_process = False
            if chatmode == "onmessage":
                should_process = True
            elif chatmode == "oncall":
                should_process = was_mentioned
            elif chatmode == "onchar":
                should_process = onchar_triggered or was_mentioned

            # requireMention acts as additional gate (ignored in onmessage mode)
            if chatmode != "onmessage" and require_mention and not was_mentioned and not onchar_triggered:
                should_process = False

            if not should_process:
                logger.debug("zulip drop [mode=%s, no trigger] msg=%s", chatmode, mask_pii(str(message_id)))
                return

            # --- Stream filtering (Issue #65) ---
            if self._streams_filter is not None:
                stream_name = message.get("display_recipient", "").lower()
                if stream_name not in self._streams_filter:
                    logger.debug(
                        "zulip drop [stream=%s not in filter] msg=%s",
                        mask_pii(stream_name),
                        mask_pii(str(message_id)),
                    )
                    return

            # --- Group policy check (Issue #66) ---
            if not self._policy.can_group_message(sender_email):
                await self._audit_logger.log_policy_block(
                    sender_id=sender_email,
                    reason=f"group_policy={self._policy.group_mode}",
                    kind="stream",
                )
                if self._policy.group_mode == "disabled":
                    reply = "🚫 Stream messages to this bot are currently disabled."
                else:
                    reply = "🚫 You are not authorized to send stream messages to this bot."
                try:
                    await self._sdk_call(
                        self.client.send_message,
                        {
                            "type": "stream",
                            # Read from the message rather than relying on
                            # locals set elsewhere: these were previously bound
                            # as a side effect of the typing-indicator block,
                            # which now runs after this point.
                            "to": message.get("stream_id"),
                            "topic": message.get("subject", ""),
                            "content": reply,
                        },
                        timeout=self._send_timeout,
                    )
                except Exception as e:
                    logger.warning("group policy rejection reply failed: %s", mask_pii(str(e)))
                # Mark message as read and stop processing
                try:
                    await self._sdk_call(
                        self.client.update_message_flags,
                        {"messages": [message_id], "op": "add", "flag": "read"},
                        timeout=self._send_timeout,
                    )
                except Exception:
                    pass
                logger.info(
                    "zulip group message blocked [policy=%s sender=%s stream=%s]",
                    self._policy.group_mode,
                    mask_pii(sender_email),
                    mask_pii(str(message.get("display_recipient", ""))),
                )
                return

            # Normalize mention from content
            if was_mentioned and mention_regex:
                if mention_regex.search(content):
                    content = normalize_mention(content, mention_regex)
                else:
                    logger.debug(
                        "zulip mention flagged by server but not found in text "
                        "[msg=%s]; passing content through unmodified",
                        message_id,
                    )

        # --- Acknowledge the message ---
        #
        # Deliberately after all stream gating, stream filtering, and group
        # policy checks. These signals used to fire before them, so a message
        # the bot then dropped was left with a permanent "typing..." indicator
        # and an uncleared start reaction — the bot appeared to be thinking
        # about a message it had already discarded, forever.
        #
        # Direct messages skip the stream block above and reach here normally.
        await reactions.start()

        typing_params = None
        if msg_type == "private":
            typing_params = {
                "op": "start",
                "type": "direct",
                "to": [message.get("sender_id")],
            }
        elif msg_type == "stream":
            typing_stream_id = message.get("stream_id")
            if typing_stream_id:
                typing_params = {
                    "op": "start",
                    "type": "stream",
                    "stream_id": typing_stream_id,
                    "topic": message.get("subject", ""),
                }

        if typing_params:
            try:
                await self._sdk_call(
                    self.client.set_typing_status,
                    typing_params,
                    timeout=self._send_timeout,
                )
            except Exception:
                pass  # typing is best-effort

        # --- Command interception (before AI dispatch) ---
        if is_command(content):
            sender_email = message.get("sender_email", "")
            sender_full_name = message.get("sender_full_name", "")
            # Determine chat_id early for command replies
            if msg_type == "stream":
                cmd_chat_id = str(message.get("stream_id", ""))
                cmd_topic = message.get("subject", "")
            else:
                cmd_chat_id = f"dm:{message.get('sender_id', '')}"
                cmd_topic = None

            cmd_result = handle_command(
                content=content,
                chat_id=cmd_chat_id,
                sender_email=sender_email,
                sender_name=sender_full_name,
                version=__version__,
            )
            if cmd_result.handled:
                # Send command reply directly
                try:
                    if msg_type == "stream":
                        await self._sdk_call(
                            self.client.send_message,
                            {
                                "type": "stream",
                                "to": message.get("stream_id"),
                                "topic": cmd_topic,
                                "content": cmd_result.reply,
                            },
                            timeout=self._send_timeout,
                        )
                    else:
                        await self._sdk_call(
                            self.client.send_message,
                            {
                                "type": "private",
                                "to": [message.get("sender_id")],
                                "content": cmd_result.reply,
                            },
                            timeout=self._send_timeout,
                        )
                except Exception as e:
                    logger.warning("command reply failed: %s", mask_pii(str(e)))
                # Clean up: stop typing, mark as read
                await self._stop_typing(typing_params)
                await self._mark_read(message_id)
                return

        # --- DM policy check (Issue #48) ---
        if msg_type == "private":
            sender_email = message.get("sender_email", "")
            allowed, pairing_code = self._policy.check_dm(sender_email)
            if not allowed:
                await self._audit_logger.log_policy_block(
                    sender_id=sender_email,
                    reason=f"dm_policy={self._policy.mode}",
                    kind="dm",
                )
                reply = ""
                if pairing_code:
                    reply = (
                        f"👋 Hi! You need to be approved before messaging this bot.\n\n"
                        f"Your pairing code: **PAIR-{pairing_code}**\n\n"
                        f"Share this code with your admin to get access."
                    )
                elif self._policy.mode == "disabled":
                    reply = "🚫 DMs to this bot are currently disabled."
                else:
                    reply = "🚫 You are not authorized to message this bot."

                try:
                    await self._sdk_call(
                        self.client.send_message,
                        {
                            "type": "private",
                            "to": [message.get("sender_id")],
                            "content": reply,
                        },
                        timeout=self._send_timeout,
                    )
                except Exception as e:
                    logger.warning("DM policy rejection failed: %s", mask_pii(str(e)))

                # Clean up: stop typing, mark as read
                await self._stop_typing(typing_params)
                await self._mark_read(message_id)
                logger.info("zulip DM blocked [policy=%s sender=%s]", self._policy.mode, mask_pii(sender_email))
                return

        if msg_type == "stream":
            stream_id = message.get("stream_id")
            topic = message.get("subject", "")
            stream_name = message.get("display_recipient", str(stream_id))

            # Cache topic for reply threading
            chat_id = str(stream_id)
            self._topic_cache[chat_id] = topic

            source_kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "chat_name": stream_name,
                "chat_type": "stream",
                "user_id": sender_email,
                "user_name": sender_full_name,
            }
            if topic and _topic_sessions_enabled():
                source_kwargs["thread_id"] = topic
            source = self.build_source(**source_kwargs)
            extra_meta = {"topic": topic, "stream_id": stream_id}
        else:
            sender_id = message.get("sender_id")
            chat_id = f"dm:{sender_id}"

            # DM session rotation: prevent context bloat by rotating
            # the session key every N turns (default 20, 0 to disable).
            if self._dm_session_turn_limit > 0:
                base_key = chat_id
                turn_count = self._dm_base_message_counts.get(base_key, 0) + 1
                self._dm_base_message_counts[base_key] = turn_count
                epoch = (turn_count - 1) // self._dm_session_turn_limit
                if epoch > 0:
                    chat_id = f"{base_key}:session:{epoch}"

            source = self.build_source(
                chat_id=chat_id,
                chat_name=sender_full_name,
                chat_type="dm",
                user_id=sender_email,
                user_name=sender_full_name,
            )
            extra_meta = {"user_id": sender_id, "user_email": sender_email}

        # --- Context-mitigation metadata ---
        now = time.time()
        msg_count = self._message_counts.get(chat_id, 0) + 1
        self._message_counts[chat_id] = msg_count

        last_time = self._last_message_time.get(chat_id)
        session_gap = (now - last_time) if last_time else 0
        self._last_message_time[chat_id] = now

        # Detect topic change in streams
        topic_changed = False
        if msg_type == "stream":
            prev_topic = self._last_topic_cache.get(chat_id)
            if prev_topic and prev_topic != topic:
                topic_changed = True
            self._last_topic_cache[chat_id] = topic

        extra_meta.update({
            "conversation_turn": msg_count,
            "session_gap_seconds": round(session_gap, 1),
            "topic_changed": topic_changed,
        })

        event = MessageEvent(
            text=content,
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(message_id),
            metadata=extra_meta,
        )

        try:
            await self.handle_message(event)
        except Exception:
            await reactions.error()
            await self._stop_typing(typing_params)
            raise
        finally:
            await self._mark_read(message_id)

        # Only reached on success.
        # Wait for the configured delay so the response is visible in the UI
        # before the typing indicator stops and the success reaction appears.
        if self._typing_delay > 0:
            await asyncio.sleep(self._typing_delay)
        await self._stop_typing(typing_params)
        await reactions.success()

    async def resolve_topic(self, stream_id: int, topic: str) -> dict[str, Any]:
        """Mark a topic as resolved by prepending ✔.

        Returns the API response dict. If the topic is already resolved,
        returns early without calling the API.
        """
        trimmed = topic.strip()
        if not trimmed:
            return {"skipped": True, "reason": "empty topic"}

        resolved_prefix = "✔ "
        if trimmed.startswith(resolved_prefix):
            return {"skipped": True, "reason": "already resolved", "topic": trimmed}

        resolved_topic = resolved_prefix + trimmed
        try:
            result = await self._sdk_call(
                self.client.update_message,
                {
                    "message_id": 0,  # Not used for topic updates with propagate_mode
                    "topic": resolved_topic,
                    "propagate_mode": "change_all",
                },
                timeout=self._send_timeout,
            )
            if result.get("result") == "success":
                logger.info(
                    "zulip topic resolved [stream_id=%d old=%s new=%s]",
                    stream_id,
                    trimmed,
                    resolved_topic,
                )
                return {"ok": True, "stream_id": stream_id, "topic": resolved_topic}
            else:
                logger.warning(
                    "zulip topic resolution failed [stream_id=%d topic=%s error=%s]",
                    stream_id,
                    trimmed,
                    result.get("msg"),
                )
                return {"ok": False, "error": result.get("msg")}
        except Exception as e:
            logger.error(
                "zulip topic resolution error [stream_id=%d topic=%s]: %s",
                stream_id,
                trimmed,
                e,
            )
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Zulip API Features: Search, Stream CRUD, User Management, Deletion
    # ------------------------------------------------------------------

    async def fetch_messages(
        self,
        stream: str,
        topic: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Fetch recent messages from a stream, optionally filtered by topic.

        Uses Zulip's /messages endpoint with narrow filters.
        Returns a list of message dicts.
        """
        stream = _validate_string_length(stream, "stream")
        if topic:
            topic = _validate_string_length(topic, "topic")

        narrow = [{"operator": "stream", "operand": stream}]
        if topic:
            narrow.append({"operator": "topic", "operand": topic})

        try:
            result = await self._sdk_call(
                self.client.get_messages,
                {
                    "anchor": "newest",
                    "num_before": min(max(1, limit), 1000),
                    "num_after": 0,
                    "narrow": narrow,
                },
                timeout=self._send_timeout,
            )
            if result.get("result") == "success":
                return result.get("messages", [])
            logger.warning("fetch_messages failed: %s", result.get("msg"))
            return []
        except Exception as e:
            logger.error("fetch_messages error: %s", e)
            return []

    async def search_messages(
        self,
        query: str,
        stream: Optional[str] = None,
        topic: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        """Search messages by query, optionally scoped to stream/topic."""
        query = _validate_string_length(query, "query")
        if stream:
            stream = _validate_string_length(stream, "stream")
        if topic:
            topic = _validate_string_length(topic, "topic")
        narrow = [{"operator": "search", "operand": query}]
        if stream:
            narrow.append({"operator": "stream", "operand": stream})
        if topic:
            narrow.append({"operator": "topic", "operand": topic})

        try:
            result = await self._sdk_call(
                self.client.get_messages,
                {
                    "anchor": "newest",
                    "num_before": min(max(1, limit), 1000),
                    "num_after": 0,
                    "narrow": narrow,
                },
                timeout=self._send_timeout,
            )
            if result.get("result") == "success":
                return result.get("messages", [])
            logger.warning("search_messages failed: %s", result.get("msg"))
            return []
        except Exception as e:
            logger.error("search_messages error: %s", e)
            return []

    async def list_streams(self) -> list[dict]:
        """List all streams the bot can see."""
        try:
            result = await self._sdk_call(
                self.client.get_streams,
                timeout=self._send_timeout,
            )
            if result.get("result") == "success":
                return result.get("streams", [])
            return []
        except Exception as e:
            logger.error("list_streams error: %s", e)
            return []

    async def subscribe_stream(self, stream_name: str) -> bool:
        """Subscribe the bot to a stream."""
        stream_name = _validate_string_length(stream_name, "stream_name")
        try:
            result = await self._sdk_call(
                self.client.add_subscriptions,
                {stream_name},
                timeout=self._send_timeout,
            )
            return result.get("result") == "success"
        except Exception as e:
            logger.error("subscribe_stream error: %s", e)
            return False

    async def delete_message(self, message_id: int) -> bool:
        """Delete a message by ID."""
        try:
            validated_id = self._validate_message_id(message_id)
            result = await self._sdk_call(
                self.client.delete_message,
                validated_id,
                timeout=self._send_timeout,
            )
            return result.get("result") == "success"
        except Exception as e:
            logger.error("delete_message error: %s", e)
            return False

    async def get_user_presence(self, user_id_or_email: str) -> Optional[dict]:
        """Get presence status for a user."""
        try:
            result = await self._sdk_call(
                self.client.get_user_presence,
                user_id_or_email,
                timeout=self._send_timeout,
            )
            if result.get("result") == "success":
                return result.get("presence")
            return None
        except Exception as e:
            logger.error("get_user_presence error: %s", e)
            return None

    async def star_message(self, message_id: int, starred: bool = True) -> bool:
        """Star or unstar a message."""
        try:
            validated_id = self._validate_message_id(message_id)
            op = "add" if starred else "remove"
            result = await self._sdk_call(
                self.client.update_message_flags,
                {"messages": [validated_id], "op": op, "flag": "starred"},
                timeout=self._send_timeout,
            )
            if result.get("result") == "success":
                logger.debug(
                    "message %s [id=%d]",
                    "starred" if starred else "unstarred",
                    validated_id,
                )
                return True
            logger.warning("star_message failed: %s", result.get("msg"))
            return False
        except Exception as e:
            logger.error("star_message error: %s", e)
            return False

    async def get_user_info(self, user_id_or_email: str) -> Optional[dict]:
        """Get information about a user."""
        try:
            result = await self._sdk_call(
                self.client.get_user,
                user_id_or_email,
                timeout=self._send_timeout,
            )
            if result.get("result") == "success":
                user = result.get("user", {})
                return {
                    "user_id": user.get("user_id"),
                    "email": user.get("email"),
                    "full_name": user.get("full_name"),
                    "is_admin": user.get("is_admin", False),
                    "is_bot": user.get("is_bot", False),
                }
            logger.warning("get_user_info failed: %s", result.get("msg"))
            return None
        except Exception as e:
            logger.error("get_user_info error: %s", e)
            return None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to=None,
        metadata=None,
        media_files=None,
    ) -> SendResult:
        """Send message to a Zulip stream or DM, with chunking, topic directives, and files."""
        metadata = metadata or {}
        media_files = media_files or []

        # Upload files first
        uploaded_urls = []
        uploaded_local_paths = []
        if media_files:
            data_dir = os.environ.get("HERMES_DATA_DIR", os.path.expanduser("~/.hermes"))
            for file_path in media_files:
                # Security: reject URL-like values in media_files (must be local paths)
                if isinstance(file_path, str) and (file_path.startswith("http://") or file_path.startswith("https://")):
                    logger.warning(
                        "zulip send rejected URL in media_files [url=%s]",
                        mask_pii(file_path),
                    )
                    continue
                try:
                    url = await upload_file_to_zulip(
                        self.client, file_path, data_dir
                    )
                    uploaded_urls.append(url)
                    uploaded_local_paths.append(file_path)
                except Exception as e:
                    logger.error("zulip upload failed [file=%s]: %s", mask_pii(file_path), e)

        # Clean up local temp files after upload (best-effort)
        for local_path in uploaded_local_paths:
            _safe_delete_temp_file(local_path)

        # Append uploaded file links to content
        if uploaded_urls:
            file_links = "\n".join(f"[{Path(u).name}]({u})" for u in uploaded_urls)
            if content:
                content = f"{content}\n\n{file_links}"
            else:
                content = file_links

        # Extract inline topic directive if present
        content, topic_override = extract_topic_directive(content)

        limit, mode = _resolve_chunk_config()
        chunks = chunk_text(content, limit=limit, mode=mode)

        if not chunks:
            chunks = [""]

        last_result: Optional[SendResult] = None

        # When block streaming is enabled, send each chunk as a separate message
        # immediately. This requires gateway-level support (not yet implemented).
        for idx, chunk in enumerate(chunks):
            result = await self._send_single(chat_id, chunk, metadata, topic_override)
            last_result = result
            if not result.success:
                logger.error(
                    "zulip send failed on chunk %d/%d [chat=%s]",
                    idx + 1,
                    len(chunks),
                    mask_pii(chat_id),
                )

        return last_result or SendResult(success=False, message_id="")

    async def _send_single(
        self,
        chat_id: str,
        content: str,
        metadata: dict,
        topic_override: Optional[str],
    ) -> SendResult:
        """Send a single (unchunked) message, editing placeholder if present."""
        # Prepend response prefix if configured (Issue #65)
        if self._response_prefix and content:
            content = self._response_prefix + content

        try:
            target = _parse_target(chat_id)
            if target["type"] == "dm":
                result = await self._sdk_call(
                    self.client.send_message,
                    {
                        "type": "private",
                        "to": [target["user_id"]],
                        "content": content,
                    },
                    timeout=self._send_timeout,
                )
            else:
                stream_id = target["stream_id"]
                topic = topic_override or metadata.get("topic")
                if not topic:
                    topic = self._topic_cache.get(chat_id, "general")

                result = await self._sdk_call(
                    self.client.send_message,
                    {
                        "type": "stream",
                        "to": stream_id,
                        "topic": topic,
                        "content": content,
                    },
                    timeout=self._send_timeout,
                )

            if result.get("result") == "success":
                logger.debug("zulip message sent to %s", chat_id)
                return SendResult(
                    success=True, message_id=str(result.get("id", ""))
                )
            else:
                logger.error(
                    format_zulip_log(
                        "zulip send failed",
                        chat_id=mask_pii(chat_id),
                        error=mask_pii(str(result)),
                    )
                )
                return SendResult(success=False, message_id="")

        except Exception as e:
            logger.error(
                format_zulip_log(
                    "zulip send error",
                    chat_id=mask_pii(chat_id),
                    error=mask_pii(str(e)),
                )
            )
            return SendResult(success=False, message_id="")


def check_requirements() -> bool:
    """Return True if the zulip SDK is installed."""
    return _import_zulip_sdk() is not None


def validate_config(config) -> bool:
    """Validate that required credentials are present."""
    extra = getattr(config, "extra", {}) or {}
    return bool(
        (os.getenv("ZULIP_API_KEY") or extra.get("api_key"))
        and (os.getenv("ZULIP_EMAIL") or extra.get("email"))
        and (os.getenv("ZULIP_SITE") or extra.get("site"))
    )


def _env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from environment variables."""
    key = os.getenv("ZULIP_API_KEY", "").strip()
    email = os.getenv("ZULIP_EMAIL", "").strip()
    site = os.getenv("ZULIP_SITE", "").strip()
    if not (key and email and site):
        return None

    return {"api_key": key, "email": email, "site": site}


def interactive_setup() -> None:
    """Interactive `hermes gateway setup` flow for the Zulip platform.

    Lazy-imports ``hermes_cli.setup`` helpers so the plugin stays importable
    in non-CLI contexts (gateway runtime, tests).
    """
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("Zulip")
    existing_email = get_env_value("ZULIP_EMAIL")
    if existing_email:
        print_info(f"Zulip: already configured ({existing_email})")
        if not prompt_yes_no("Reconfigure Zulip?", False):
            return

    print_info("Connect Hermes to Zulip via a bot account.")
    print_info("   Create a bot at: Settings → Bots → Add a new bot (Generic bot)")
    print()

    site = prompt(
        "Zulip site URL (e.g. https://your-org.zulipchat.com)",
        default=get_env_value("ZULIP_SITE") or "",
    )
    if not site:
        print_warning("Site URL is required — skipping Zulip setup")
        return
    save_env_value("ZULIP_SITE", site.rstrip("/").strip())

    email = prompt(
        "Bot email address (e.g. hermes-bot@your-org.zulipchat.com)",
        default=get_env_value("ZULIP_EMAIL") or "",
    )
    if not email:
        print_warning("Bot email is required — skipping Zulip setup")
        return
    save_env_value("ZULIP_EMAIL", email.strip())

    api_key = prompt(
        "Bot API key",
        default=get_env_value("ZULIP_API_KEY") or "",
        password=True,
    )
    if not api_key:
        print_warning("API key is required — skipping Zulip setup")
        return
    save_env_value("ZULIP_API_KEY", api_key.strip())

    # Authorization (optional but recommended)
    allowed = prompt(
        "Allowed user emails (comma-separated, or empty for none yet)",
        default=get_env_value("ZULIP_ALLOWED_USERS") or "",
    )
    if allowed:
        save_env_value("ZULIP_ALLOWED_USERS", allowed.strip())

    print_success("Zulip configured.")
    print_info("Tip: Subscribe your bot to streams via Stream settings → Subscribers")


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
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
        max_message_length=10000,
        platform_hint=(
            "You are chatting via Zulip. Messages are organized into streams and topics. "
            "When replying to a stream message, preserve the original topic unless asked to change it."
        ),
        emoji="📬",
        setup_fn=interactive_setup,
    )
