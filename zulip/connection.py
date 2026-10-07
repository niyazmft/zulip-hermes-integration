"""Connection and event-loop lifecycle for the Zulip adapter.

Everything between "the plugin is loaded" and "an inbound message reaches the
adapter" lives here: the pre-flight health probe and credential check
(:meth:`ZulipConnection.connect`), the long-poll event loop that owns /events
pacing and the persisted queue (:meth:`ZulipConnection.listen_for_events`), the
queue registration that learns the server's own long-poll budget
(:meth:`ZulipConnection.register_queue`), the presence heartbeat, and the
teardown (:meth:`ZulipConnection.disconnect`).

Layering: L3 — depends on L0-L2 (``probe``, ``settings``, ``updater``,
``recovery``, ``version``, ``logger``) and the host's ``gateway`` base.

The adapter is passed in as an opaque collaborator and its state (``site``,
``email``, ``api_key``, ``client``, ``_sdk_call``, ``_send_timeout``,
``_queue_mgr``, ``_listening``, ``_event_task``, ``_presence_task``, ...) is read
at call time, so an instance patch of any of those still takes effect.
``adapter`` is deliberately never imported here — that would be a cycle, and the
adapter's own ``connect``/``disconnect`` ``BasePlatformAdapter`` overrides stay
on the adapter as thin delegates.

``probe.probe_zulip`` and ``settings.clamp_longpoll_budget`` /
``settings.next_poll_backoff`` are reached through their owning modules rather
than imported as names, so patching ``zulip.probe`` / ``zulip.settings`` keeps
working — the ownership rule that makes a stale patch impossible to hide.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import probe
from . import settings
from . import updater
from .engagement import MODE_OFF as ENGAGEMENT_MODE_OFF
from .logger import format_zulip_log, mask_pii
from .recovery import recover_interrupted_messages
from .version import __version__, __repo__

logger = logging.getLogger(__name__)


def message_with_flags(event: dict) -> dict:
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


class ZulipConnection:
    """Owns connect/disconnect and the /events long-poll loop for one adapter."""

    def __init__(self, adapter: Any):
        self._adapter = adapter

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Initialize connection and start listening."""
        adapter = self._adapter
        logger.info("Zulip adapter connecting...")

        # 0. Pre-flight health probe (side-effect free)
        probe_result = await probe.probe_zulip(
            adapter.site, adapter.email, adapter.api_key, timeout=10
        )
        if not probe_result.get("ok"):
            error = probe_result.get("error", "unknown")
            logger.error(
                format_zulip_log(
                    "zulip probe failed",
                    site=mask_pii(adapter.site),
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
            server_settings = await adapter._sdk_call(
                adapter.client.get_server_settings,
                timeout=adapter._connect_timeout,
            )
            if server_settings.get("result") != "success":
                raise ConnectionError(
                    f"Cannot reach Zulip server: {adapter.site}"
                )
        except Exception as e:
            logger.error(
                format_zulip_log(
                    "zulip server unreachable",
                    site=mask_pii(adapter.site),
                    error=mask_pii(str(e)),
                )
            )
            raise ConnectionError(f"Cannot reach Zulip server: {adapter.site}") from e

        # 2. Validate credentials with lightweight profile call
        try:
            result = await adapter._sdk_call(
                adapter.client.get_profile,
                timeout=adapter._connect_timeout,
            )
            if result.get("result") != "success":
                raise ConnectionError(f"Zulip authentication failed: {result}")
            bot_name = result.get("full_name", "Unknown")
            adapter.bot_full_name = result.get("full_name") or ""
            logger.info(
                format_zulip_log(
                    "zulip bot authenticated",
                    bot=mask_pii(bot_name),
                )
            )

            # 2b. Seed the DM allowlist from the bot's Zulip owner (#214). This
            # is the payload from the call above, so no extra round-trip is
            # needed to learn who the owner is. Best-effort: it logs and returns
            # rather than raising, because streams work regardless and a DM-only
            # misconfiguration must not take the bot offline.
            await adapter.seed_bot_owner_dm_allowlist(result)
            # 2c. The same owner identity decides exec approvals (#228) under
            # ``ZULIP_APPROVAL_AUTHORITY=owner``, and failing closed is right but
            # silent — so say now whether there is an owner to accept one.
            adapter.report_approval_authority()
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
            subs = await adapter._sdk_call(
                adapter.client.get_subscriptions,
                timeout=adapter._connect_timeout,
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
                site=mask_pii(adapter.site),
            )
        )

        # Structured health status for monitoring tools
        logger.info(
            "health_status=connected platform=zulip site=%s account=%s",
            mask_pii(adapter.site),
            mask_pii(adapter.email),
        )

        # Start presence heartbeat so bot appears online
        adapter._presence_task = asyncio.create_task(self.presence_heartbeat())

        # Bind the loop the approval hooks must cross back to (issue #222): they
        # fire on the agent thread, and the audit write and refusal line both
        # await SDK calls that belong on this loop. Captured here because this
        # is the adapter's own async entry point; reconnect runs on the same one.
        adapter._zulip_loop = asyncio.get_running_loop()

        # Sticky engagement expiry scanner (#166). Started only when engagement
        # is enabled, so mode=off runs no background task at all.
        if adapter._engagement_cfg.mode != ENGAGEMENT_MODE_OFF:
            adapter._engagement_task = asyncio.create_task(
                adapter._engagement_expiry_loop()
            )

        # Check for plugin updates on startup
        updater.startup_version_check(__version__, __repo__)

        # Ensure queue is registered before starting listener
        await adapter._queue_mgr.ensure_queue()

        # Recover interrupted messages from previous gateway instance
        bot_user_id = str(probe_result.get("bot", {}).get("id", ""))
        bot_user_id = str(probe_result.get("bot", {}).get("id", ""))
        if bot_user_id:
            adapter._bot_user_id = bot_user_id
        # Warn about reaction-trigger blind spots: Zulip only delivers
        # ``reaction`` events for streams the bot is subscribed to. (#164)
        await adapter._check_reaction_trigger_subscriptions()
        asyncio.create_task(
            recover_interrupted_messages(
                client=adapter.client,
                bot_email=adapter.email,
                bot_user_id=bot_user_id,
                reaction_start=adapter._reaction_cfg.on_start,
                reaction_success=adapter._reaction_cfg.on_success,
                reaction_error=adapter._reaction_cfg.on_error,
                handle_message=adapter._handle_message,
                sdk_call=adapter._sdk_call,
                send_timeout=adapter._send_timeout,
            )
        )

        adapter._listening = True
        adapter._event_task = asyncio.create_task(self.listen_for_events())
        adapter._mark_connected()
        return True

    async def disconnect(self) -> None:
        """Stop listening and close connection."""
        adapter = self._adapter
        adapter._listening = False
        if adapter._event_task:
            adapter._event_task.cancel()
            try:
                await adapter._event_task
            except asyncio.CancelledError:
                pass
        if adapter._presence_task:
            adapter._presence_task.cancel()
            try:
                await adapter._presence_task
            except asyncio.CancelledError:
                pass
        if adapter._engagement_task:
            adapter._engagement_task.cancel()
            try:
                await adapter._engagement_task
            except asyncio.CancelledError:
                pass
        adapter._mark_disconnected()
        logger.info("Zulip adapter disconnected")
        logger.info(
            "health_status=disconnected platform=zulip site=%s account=%s",
            mask_pii(adapter.site),
            mask_pii(adapter.email),
        )

    def needed_event_types(self) -> list:
        """Event types this adapter needs from its Zulip queue.

        ``"message"`` is always required. Features that need more subscribe to
        them through ``self._extra_event_types`` (for example reaction triggers
        add ``"reaction"``), so installs that do not use those features request
        nothing extra. A persisted queue is re-registered when this set changes,
        because ``/register`` fixes ``event_types`` for the queue's whole
        lifetime. (Issues #162, #149)
        """
        return ["message", *self._adapter._extra_event_types]

    def register_queue(self) -> dict:
        """Register the event queue, learning the server's long-poll budget.

        ``fetch_event_types: ["realm"]`` is what makes Zulip include
        ``event_queue_longpoll_timeout_seconds`` in the response; without it
        the field is omitted and there is no way to know how long the server
        intends to hold a /events long-poll. Knowing it lets the poll loop
        abort *after* the server's own budget instead of pre-empting a healthy
        idle poll with our shorter generic read timeout. (Issue #146)
        """
        adapter = self._adapter
        result = adapter.client.register(
            event_types=self.needed_event_types(),
            fetch_event_id=0,
            fetch_event_types=["realm"],
        )

        raw_budget = None
        if isinstance(result, dict):
            raw_budget = result.get("event_queue_longpoll_timeout_seconds")
        budget = settings.clamp_longpoll_budget(raw_budget)
        if budget is not None:
            adapter._events_timeout = max(
                adapter._read_timeout, budget + settings.LONGPOLL_GRACE_SECONDS
            )
            logger.info(
                "zulip long-poll budget learned [server=%.0fs client_abort=%.0fs]",
                budget,
                adapter._events_timeout,
            )
        else:
            logger.debug(
                "zulip long-poll budget absent from /register; "
                "keeping client abort budget at %.0fs",
                adapter._events_timeout,
            )

        return result

    async def presence_heartbeat(self):
        """Keep bot presence active while connected."""
        adapter = self._adapter
        while adapter._listening:
            try:
                await adapter._sdk_call(
                    adapter.client.update_presence,
                    {"status": "active", "ping_only": False},
                    timeout=adapter._send_timeout,
                )
            except Exception:
                pass  # presence is best-effort
            await asyncio.sleep(60)

    async def listen_for_events(self):
        """Listen for incoming Zulip messages via persistent event queue."""
        adapter = self._adapter
        # Close out traces orphaned by an abrupt restart before taking new work
        # (issue #161). Best-effort: it must never block listening.
        try:
            await adapter._recover_interrupted_traces()
        except Exception as e:
            logger.warning("zulip: interrupted-trace recovery failed: %s", e)

        logger.info("zulip adapter listening [account=%s]", mask_pii(adapter.email))

        # Latency-gated backoff state (Issue #146). Stays 0.0 while the server
        # holds the long-poll — the healthy case, which must not slow down.
        backoff = 0.0

        while adapter._listening:
            # Bound before the try so the error path can always tell how far this
            # poll got, even when the failure happened mid-batch.
            queue = None
            batch_max_event_id = None
            try:
                queue = await adapter._queue_mgr.ensure_queue()

                poll_started = time.monotonic()
                events = await adapter._sdk_call(
                    adapter.client.get_events,
                    queue_id=queue.queue_id,
                    last_event_id=queue.last_event_id,
                    timeout=adapter._events_timeout,
                )
                poll_elapsed = time.monotonic() - poll_started

                if events.get("result") == "error":
                    msg = events.get("msg", "")
                    code = events.get("code")
                    is_bad_queue = (
                        code == "BAD_EVENT_QUEUE_ID"
                        or (code == "BAD_REQUEST" and "event newer than" in msg.lower() and "pruned" in msg.lower())
                        or "bad event queue" in msg.lower()
                    )
                    if is_bad_queue:
                        logger.warning("zulip queue expired, re-registering")
                        adapter._queue_mgr.mark_queue_expired()
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
                had_message = False
                for event in events.get("events", []):
                    event_id = event["id"]
                    if event_id > batch_max_event_id:
                        batch_max_event_id = event_id
                    if event.get("type") == "message":
                        # Any delivered message means this poll had work to do,
                        # so it is a real return even when dedupe drops it.
                        had_message = True
                        msg = message_with_flags(event)
                        msg_id = str(msg.get("id", ""))
                        # Dedupe check
                        if adapter._dedupe.check(msg_id):
                            logger.debug("zulip dedupe hit [msg=%s]", mask_pii(msg_id))
                            continue
                        # Process messages concurrently so a slow model call
                        # does not block the poll loop for unrelated messages.
                        # Per-session serialization is handled by the gateway.
                        # F1 fix: resolve the topic conversation NOW, inline,
                        # in event-id order — a rename later in this batch (or
                        # processed while the handler awaits) must not fork
                        # this message into a fresh conversation under the
                        # freed name. The id is stashed; the handler reuses it.
                        adapter._pre_resolve_conversation(msg, msg_id)
                        task = asyncio.create_task(adapter._handle_message(msg))
                        processing_tasks.append(task)
                    elif event.get("type") == "update_message":
                        # Topic renames/moves: registry maintenance for stable
                        # topic sessions. Fast + ordered, handled inline.
                        adapter._handle_topic_update(event)
                    elif event.get("type") == "delete_message":
                        # Topic deletion (R10): the event is only a trigger;
                        # verified against the channel's topic list before
                        # anything is freed. Spawned like message handling
                        # (one API call), rare.
                        asyncio.create_task(
                            adapter._handle_message_delete_event(event)
                        )
                    elif event.get("type") == "reaction":
                        # In-channel action triggers (epic #149). Fire-and-forget
                        # like messages so a slow resolution/fetch cannot stall
                        # the poll loop.
                        had_message = True
                        task = asyncio.create_task(
                            adapter._handle_reaction_event(event)
                        )
                        processing_tasks.append(task)

                # Fire-and-forget: don't await processing tasks here so the
                # poll loop keeps fetching events. Errors are logged inside
                # _handle_message.

                # Batch update event ID
                if batch_max_event_id > queue.last_event_id:
                    adapter._queue_mgr.update_last_event_id(batch_max_event_id)

                # Pace only a poll the server did not hold (Issue #146).
                backoff = settings.next_poll_backoff(
                    poll_elapsed, had_message, backoff
                )
                if backoff:
                    logger.debug(
                        "zulip poll backoff [delay=%.1fs last_poll=%.2fs]",
                        backoff,
                        poll_elapsed,
                    )
                    await asyncio.sleep(backoff)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                # The exception CLASS and the raising frame identify the failure
                # without exposing a message body: the masked message alone left a
                # live outage undiagnosable (every occurrence masked to the same
                # shape). Type and frame names carry no user data.
                frame = "?"
                try:
                    tb = e.__traceback__
                    while tb is not None:
                        frame = tb.tb_frame.f_code.co_name
                        tb = tb.tb_next
                except Exception:
                    pass
                logger.error(
                    format_zulip_log(
                        "zulip event polling error",
                        error=mask_pii(str(e)),
                        exc=f"{type(e).__name__}@{frame}",
                    )
                )
                # Never let a poison batch pin ``last_event_id``. Refetching the
                # same events forever leaves the bot permanently deaf, which is
                # far worse than skipping the tail of a single batch. Advancing
                # here is what guarantees forward progress on every iteration.
                if (
                    queue is not None
                    and batch_max_event_id is not None
                    and batch_max_event_id > queue.last_event_id
                ):
                    adapter._queue_mgr.update_last_event_id(batch_max_event_id)
                await asyncio.sleep(5)
