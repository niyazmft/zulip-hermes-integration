"""
Zulip Platform Adapter for Hermes Gateway (Plugin)

Bi-directional integration with Zulip chat platform.
Supports stream messages (with topics) and private messages.
"""

import asyncio
import logging
import os
import re
import tempfile
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Optional, Any

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
from .version import __version__, __repo__, PLUGIN_FILES
from .commands import handle_command, is_command
from . import updater
from .probe import probe_zulip, _normalize_base_url

logger = logging.getLogger(__name__)

# Module-level SDK handle — updated by _import_zulip_sdk()
zulip = None  # type: ignore

# ------------------------------------------------------------------
# Performance: client + target caching (Issue #49)
# ------------------------------------------------------------------
_MAX_CLIENT_CACHE = 50
_MAX_TARGET_CACHE = 500

_client_cache: OrderedDict[str, Any] = OrderedDict()
_target_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()


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


def _resolve_chunk_config() -> tuple[int, str]:
    """Read chunking config from environment."""
    limit_raw = os.getenv("ZULIP_TEXT_CHUNK_LIMIT", "").strip()
    limit = int(limit_raw) if limit_raw.isdigit() else DEFAULT_CHUNK_LIMIT
    mode = os.getenv("ZULIP_CHUNK_MODE", DEFAULT_CHUNK_MODE).strip()
    if mode not in ("length", "newline"):
        mode = DEFAULT_CHUNK_MODE
    return limit, mode


def _resolve_chatmode() -> tuple[str, list[str], bool]:
    """Read stream trigger mode config from environment."""
    mode = os.getenv("ZULIP_CHATMODE", "onmessage").strip().lower()
    if mode not in ("onmessage", "oncall", "onchar"):
        mode = "onmessage"
    prefixes = resolve_onchar_prefixes(os.getenv("ZULIP_ONCHAR_PREFIXES", ""))
    require_mention = os.getenv("ZULIP_REQUIRE_MENTION", "true").strip().lower() not in ("false", "0", "no", "off")
    return mode, prefixes, require_mention


def _safe_delete_temp_file(file_path: str) -> None:
    """Delete a local file only if it resides under /tmp or a bot workspace.

    Prevents accidental deletion of user-owned files outside temp dirs.
    Errors are logged, not raised.
    """
    try:
        p = Path(file_path).resolve()
        tmp = Path(tempfile.gettempdir()).resolve()
        ws = tmp / "hermes_bot_workspace"
        if str(p).startswith(str(tmp)) or str(p).startswith(str(ws)):
            p.unlink()
            logger.debug("cleaned up temp file [path=%s]", file_path)
    except OSError as e:
        logger.warning("temp file cleanup failed [path=%s]: %s", file_path, e)


class ZulipAdapter(BasePlatformAdapter):
    """Zulip platform adapter for Hermes Gateway."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("zulip"))
        extra = config.extra or {}

        self.api_key = os.getenv("ZULIP_API_KEY") or extra.get("api_key", "")
        self.email = os.getenv("ZULIP_EMAIL") or extra.get("email", "")
        self.site = os.getenv("ZULIP_SITE") or extra.get("site", "")

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
        # Pending placeholder message IDs for editing (chat_id → deque of msg_ids)
        self._pending_placeholders: dict[str, deque[int]] = {}
        # Context-mitigation state
        self._last_topic_cache: dict[str, str] = {}      # stream_id → previous topic
        self._message_counts: dict[str, int] = {}        # chat_id → message count
        self._last_message_time: dict[str, float] = {}   # chat_id → last message epoch

        # Placeholder editing config (default: true, set false to disable)
        self._edit_placeholder_enabled = (
            os.getenv("ZULIP_EDIT_PLACEHOLDER", "").strip().lower() not in ("false", "0", "no", "off")
        )

        # Block streaming config (Issue #49 — requires gateway-level streaming support)
        self._block_streaming = (
            os.getenv("ZULIP_BLOCK_STREAMING", "").strip().lower() in ("true", "1", "yes", "on")
        )

        self._data_dir = os.environ.get("HERMES_DATA_DIR", os.path.expanduser("~/.hermes"))

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

        self._listening = False
        self._event_task: Optional[asyncio.Task] = None
        self._presence_task: Optional[asyncio.Task] = None

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
            settings = await asyncio.to_thread(self.client.get_server_settings)
            if settings.get("result") != "success":
                raise ConnectionError(
                    f"Cannot reach Zulip server: {self.site}"
                )
        except Exception as e:
            logger.error("Zulip server unreachable: %s", e)
            raise ConnectionError(f"Cannot reach Zulip server: {self.site}") from e

        # 2. Validate credentials with lightweight profile call
        try:
            result = await asyncio.to_thread(self.client.get_profile)
            if result.get("result") != "success":
                raise ConnectionError(f"Zulip authentication failed: {result}")
            bot_name = result.get("full_name", "Unknown")
            logger.info(
                format_zulip_log(
                    "zulip bot authenticated",
                    bot=mask_pii(bot_name),
                )
            )
        except Exception as e:
            logger.error("Zulip authentication error: %s", e)
            raise

        # 3. Log subscriptions so admins know what streams the bot sees
        try:
            subs = await asyncio.to_thread(self.client.get_subscriptions)
            if subs.get("result") == "success":
                stream_names = [s["name"] for s in subs.get("subscriptions", [])]
                if stream_names:
                    logger.info(
                        "zulip bot subscribed to %d stream(s): %s",
                        len(stream_names),
                        ", ".join(stream_names),
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
                await asyncio.to_thread(
                    self.client.update_presence,
                    {"status": "active", "ping_only": False},
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

                events = await asyncio.to_thread(
                    self.client.get_events,
                    queue_id=queue.queue_id,
                    last_event_id=queue.last_event_id,
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
                for event in events.get("events", []):
                    event_id = event["id"]
                    if event_id > batch_max_event_id:
                        batch_max_event_id = event_id
                    if event.get("type") == "message":
                        msg = event["message"]
                        msg_id = str(msg.get("id", ""))
                        # Dedupe check
                        if self._dedupe.check(msg_id):
                            logger.debug("zulip dedupe hit [msg=%s]", msg_id)
                            continue
                        await self._handle_message(msg)

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

        # Strip Zulip @-mention syntax and HTML
        content = strip_html_to_text(content)

        # --- Reactions ---
        reactions = ReactionLifecycle(
            self.client, str(message_id), self._reaction_cfg
        )
        await reactions.start()

        # --- Typing indicator ---
        typing_params = None
        if msg_type == "private":
            typing_params = {
                "op": "start",
                "type": "direct",
                "to": [message.get("sender_id")],
            }
        elif msg_type == "stream":
            stream_id = message.get("stream_id")
            topic = message.get("subject", "")
            if stream_id:
                typing_params = {
                    "op": "start",
                    "type": "stream",
                    "stream_id": stream_id,
                    "topic": topic,
                }

        if typing_params:
            try:
                await asyncio.to_thread(
                    self.client.set_typing_status, typing_params
                )
            except Exception:
                pass  # typing is best-effort

        # --- Stream trigger gating ---
        if msg_type == "stream":
            chatmode, onchar_prefixes, require_mention = _resolve_chatmode()

            # Check onchar trigger
            onchar_triggered, stripped = strip_onchar_prefix(content, onchar_prefixes)
            if onchar_triggered:
                content = stripped

            # Check mention (simple substring; bot username from email prefix)
            bot_username = self.email.split("@")[0] if self.email else ""
            mention_regex = create_mention_regex(bot_username) if bot_username else None
            was_mentioned = bool(mention_regex and mention_regex.search(content))

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
                logger.debug("zulip drop [mode=%s, no trigger] msg=%s", chatmode, message_id)
                return

            # Normalize mention from content
            if was_mentioned and mention_regex:
                content = normalize_mention(content, mention_regex)

        # --- Command interception (before placeholders / AI dispatch) ---
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
                        await asyncio.to_thread(
                            self.client.send_message,
                            {
                                "type": "stream",
                                "to": message.get("stream_id"),
                                "topic": cmd_topic,
                                "content": cmd_result.reply,
                            },
                        )
                    else:
                        await asyncio.to_thread(
                            self.client.send_message,
                            {
                                "type": "private",
                                "to": [message.get("sender_id")],
                                "content": cmd_result.reply,
                            },
                        )
                except Exception as e:
                    logger.warning("command reply failed: %s", e)
                # Mark message as read and stop processing
                try:
                    await asyncio.to_thread(
                        self.client.update_message_flags,
                        {"messages": [message_id], "op": "add", "flag": "read"},
                    )
                except Exception:
                    pass
                return

        if msg_type == "stream":
            stream_id = message.get("stream_id")
            topic = message.get("subject", "")
            stream_name = message.get("display_recipient", str(stream_id))

            # Cache topic for reply threading
            chat_id = str(stream_id)
            self._topic_cache[chat_id] = topic

            # Send placeholder if editing is enabled
            if self._edit_placeholder_enabled:
                try:
                    ph_result = await asyncio.to_thread(
                        self.client.send_message,
                        {
                            "type": "stream",
                            "to": stream_id,
                            "topic": topic,
                            "content": "🤔 Thinking...",
                        },
                    )
                    if ph_result.get("result") == "success":
                        self._pending_placeholders.setdefault(chat_id, deque()).append(
                            ph_result["id"]
                        )
                except Exception:
                    pass  # placeholder is best-effort

            source = self.build_source(
                chat_id=chat_id,
                chat_name=stream_name,
                chat_type="stream",
                user_id=sender_email,
                user_name=sender_full_name,
            )
            extra_meta = {"topic": topic, "stream_id": stream_id}
        else:
            sender_id = message.get("sender_id")
            chat_id = f"dm:{sender_id}"

            # Send placeholder if editing is enabled (DMs too)
            if self._edit_placeholder_enabled:
                try:
                    ph_result = await asyncio.to_thread(
                        self.client.send_message,
                        {
                            "type": "private",
                            "to": [sender_id],
                            "content": "🤔 Thinking...",
                        },
                    )
                    if ph_result.get("result") == "success":
                        self._pending_placeholders.setdefault(chat_id, deque()).append(
                            ph_result["id"]
                        )
                except Exception:
                    pass  # placeholder is best-effort

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
            await reactions.success()
        except Exception:
            await reactions.error()
            # Clean up orphaned placeholder if present
            orphaned_queue = self._pending_placeholders.get(chat_id)
            if orphaned_queue:
                try:
                    orphaned_id = orphaned_queue.popleft()
                    await asyncio.to_thread(
                        self.client.update_message,
                        {"message_id": orphaned_id, "content": "❌ Error — could not generate response"},
                    )
                except Exception:
                    pass  # best-effort cleanup
            raise
        finally:
            if typing_params:
                typing_params["op"] = "stop"
                try:
                    await asyncio.to_thread(
                        self.client.set_typing_status, typing_params
                    )
                except Exception:
                    pass
            # Mark message as read (best-effort)
            try:
                await asyncio.to_thread(
                    self.client.update_message_flags,
                    {"messages": [message_id], "op": "add", "flag": "read"},
                )
            except Exception:
                pass

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
                try:
                    url = await upload_file_to_zulip(
                        self.client, file_path, data_dir
                    )
                    uploaded_urls.append(url)
                    uploaded_local_paths.append(file_path)
                except Exception as e:
                    logger.error("zulip upload failed [file=%s]: %s", file_path, e)

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
                    chat_id,
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
        # Check for pending placeholder to edit instead of sending new
        placeholder_id: Optional[int] = None
        orphaned_queue = self._pending_placeholders.get(chat_id)
        if orphaned_queue:
            try:
                placeholder_id = orphaned_queue.popleft()
            except IndexError:
                placeholder_id = None
        if placeholder_id is not None:
            try:
                result = await asyncio.to_thread(
                    self.client.update_message,
                    {"message_id": placeholder_id, "content": content},
                )
                if result.get("result") == "success":
                    logger.debug("zulip placeholder edited for %s", chat_id)
                    return SendResult(
                        success=True, message_id=str(placeholder_id)
                    )
                # If edit fails, fall through to normal send
            except Exception:
                pass  # fall through to normal send

        try:
            target = _parse_target(chat_id)
            if target["type"] == "dm":
                result = await asyncio.to_thread(
                    self.client.send_message,
                    {
                        "type": "private",
                        "to": [target["user_id"]],
                        "content": content,
                    },
                )
            else:
                stream_id = target["stream_id"]
                topic = topic_override or metadata.get("topic")
                if not topic:
                    topic = self._topic_cache.get(chat_id, "general")

                result = await asyncio.to_thread(
                    self.client.send_message,
                    {
                        "type": "stream",
                        "to": stream_id,
                        "topic": topic,
                        "content": content,
                    },
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
