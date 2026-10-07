"""Inbound gate chain: decide whether an arriving Zulip message is dispatched.

This module owns the whole inbound chain — the ~14 sequential gate stages a raw
event-queue message walks before it becomes an agent turn: pre-resolved
conversation pop, self-message filter, per-sender rate limit, HTML strip,
reaction lifecycle, stream trigger gating (chatmode / mention / onchar /
``ZULIP_SOFT_GATE`` / sticky-engagement follow-up / observe buffer), stream
filter, group policy, engagement stop, engagement open/refresh, mention
normalization, acknowledgement, display-name resolution, command interception,
DM policy, routing + context metadata, quoted topic history, and the
per-session queue.

``ZulipAdapter._handle_message`` is a one-line delegate into
:func:`handle_message`. The adapter is passed in as an opaque collaborator and
every piece of its state — ``_rate_limiter``,
``_policy``, ``_engagement_store``, ``_streams_filter``, ``_observe_group``,
``_message_counts``, … — is read at call time, so an instance patch of any of
it still takes effect. Conversation-identity state (the dispatch stash, the
conversation registry, the reply-topic cache) is owned by ``adapter._routing``
(``zulip.routing``) and is read through it here. ``zulip.adapter``
is deliberately never imported here: that would be a cycle, and the adapter's
own ``_handle_message`` is a thin delegate into this module.

Layering: L3 — depends on L0-L2 (``settings``, ``text_utils``, ``reactions``,
``engagement``, ``commands``, ``zulip_client``, ``logger``, ``version``).

WHY THIS IS ONE FUNCTION AND NOT A DECISION OBJECT
--------------------------------------------------
The Stage-1 blueprint sketched ``InboundGate.evaluate(message) ->
InboundDecision`` (an ``InboundKind`` plus a normalized ``InboundEnvelope``),
with the adapter acting on the returned decision. That decision/effect split was
evaluated and REJECTED, because seven of the gates perform a side effect as an
inseparable part of the decision, so returning a decision object would defer
every one of those effects past the boundary (or force the adapter to
re-implement seven branches byte-for-byte — a larger divergence surface than
the extraction itself). The effectful gates, in chain order:

1. per-sender rate limit   — ``_rate_limiter.check`` CONSUMES a token, then the
   failure path audits and returns;
2. stream trigger gating   — the not-addressed drop inserts into the observe
   buffer (``_observe_stream_message``) as part of the drop decision;
3. group policy            — audit + rejection reply send + mark-read;
4. engagement stop         — ``clear`` + ack send + mark-read;
5. engagement open/refresh — the ``mark_engaged`` mutation on a proceed decision;
6. command interception    — reply send + stop-typing + mark-read;
7. DM policy               — ``refresh_if_changed`` + audit + rejection reply +
   stop-typing + mark-read.

Do not "improve" this back into the risky shape. The gate ORDER and each
effect's count and position are the security contract — allowlist, DM policy,
group policy, rate limit, mention/trigger gating, sticky engagement, soft gate
and self-message filter all live here. Move a side effect across a decision
boundary and you have a security regression, not a style bug.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from gateway.platforms.base import MessageEvent, MessageType

from . import commands
from .commands import CommandResult, is_approval_decision, is_command
from .engagement import (
    MODE_OFF as ENGAGEMENT_MODE_OFF,
    is_bot_sender,
    is_end_session_message,
    is_stop_listening_message,
)
from .logger import mask_pii
from .reactions import ReactionLifecycle
from .settings import resolve_chatmode as _resolve_chatmode
from .text_utils import (
    create_mention_regex,
    normalize_mention,
    strip_html_to_text,
    strip_onchar_prefix,
)
from .version import __version__
from .zulip_client import (
    private_chat_id as _private_chat_id,
    private_recipient_ids as _private_recipient_ids,
)

logger = logging.getLogger(__name__)


async def handle_message(adapter: Any, message: dict) -> None:
    """Process one incoming Zulip message through the full inbound gate chain.

    ``adapter`` is the live ``ZulipAdapter``; its state is read at call time so
    instance patches still take effect. Returns ``None`` — the host observes the
    result through the dispatched turn, not through a return value. See the
    module docstring for why this is not split into a decision object.
    """
    message_id = message.get("id")
    # Pop the conversation resolved at dispatch (if any) up front —
    # before ANY early return, the self-message filter below included —
    # so a pre-resolved self-message cannot leak a stash entry.
    pre_resolved_conversation = (
        adapter._routing._pending_conversations.pop(str(message_id), None)
        if message_id is not None
        else None
    )

    # Filter self-messages to prevent loops
    if adapter._is_self_message(message):
        logger.debug("zulip drop [self-message] msg=%s", mask_pii(str(message_id)))
        return

    msg_type = message.get("type")  # "stream" or "private"
    content = message.get("content", "")
    sender_email = message.get("sender_email", "")
    # Cheap payload name for the early gating/engagement paths; the
    # authoritative name is resolved (and cached/refreshed) after the drop
    # paths below, so a discarded message never costs a lookup (#152).
    sender_full_name = (
        str(message.get("sender_full_name") or "").strip() or "Unknown"
    )

    # --- Rate limiting (per-sender) ---
    sender_key = sender_email or str(message.get("sender_id", ""))
    if not adapter._rate_limiter.check(sender_key):
        logger.warning(
            "zulip rate limit hit [sender=%s msg=%s]",
            mask_pii(sender_key),
            mask_pii(str(message_id)),
        )
        await adapter._audit_logger.log_rate_limit_exceeded(
            sender_id=sender_key,
            limit=adapter._rate_limiter.config["max_per_minute"],
        )
        return

    # Strip Zulip @-mention syntax and HTML
    content = strip_html_to_text(content)
    # Captured before any gating or prompt-prefixing, so it still identifies a
    # native slash command once a history block has been prepended below. The
    # per-session queue must never serialize these: the gateway answers them
    # itself, so no agent run (and no completion hook) ever follows.
    is_slash_command = content.lstrip().startswith("/")

    # Whether the bot was actually named (or an onchar prefix fired). Only
    # meaningful for stream messages; carried into event metadata under the
    # soft gate (issue #153).
    addressed = False

    # --- Reactions ---
    # Constructed here so the error path below can reach it, but not
    # started until the message has cleared every drop path. See the
    # acknowledgement block after stream gating.
    reactions = ReactionLifecycle(
        adapter.client, str(message_id), adapter._reaction_cfg,
        timeout=adapter._send_timeout,
    )

    # --- Stream trigger gating ---
    if msg_type == "stream":
        # str() guard: Zulip sends a string here for stream messages, but a
        # malformed event would otherwise raise AttributeError inside the
        # handler rather than being skipped.
        stream_name = str(message.get("display_recipient", ""))
        # Engagement keys on the stream + topic pair, read here so the gate
        # below never depends on locals bound later in the handler.
        gate_stream_id = message.get("stream_id")
        gate_topic = message.get("subject", "")
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
        bot_username = adapter.email.split("@")[0] if adapter.email else ""
        mention_regex = (
            create_mention_regex(bot_username, adapter.bot_full_name)
            if bot_username
            else None
        )
        was_mentioned = "mentioned" in (message.get("flags") or [])
        if not was_mentioned and mention_regex:
            was_mentioned = bool(mention_regex.search(content))

        # Apply gating
        #
        # A synthetic reaction-trigger turn (epic #149) already carries an
        # explicit human instruction, so it does not need a fresh @mention.
        # It still flows through the per-sender rate limit, stream filter
        # and group policy below — the reaction is a trigger, never an
        # authorisation bypass.
        is_reaction_trigger = bool(message.get("_reaction_trigger"))
        # ``addressed`` is the honest mention signal — the bot was named or
        # an onchar prefix fired — independent of which mode decided to
        # dispatch. ``ZULIP_SOFT_GATE`` dispatches every monitored stream
        # message like onmessage but keeps that distinction in metadata so
        # the agent can watch a stream it was not spoken to (issue #153).
        should_process = False
        addressed = was_mentioned or onchar_triggered
        if is_reaction_trigger:
            should_process = True
        elif adapter._soft_gate:
            should_process = True
        elif chatmode == "onmessage":
            should_process = True
        elif chatmode == "oncall":
            should_process = was_mentioned
        elif chatmode == "onchar":
            should_process = onchar_triggered or was_mentioned

        # requireMention acts as additional gate (ignored in onmessage mode
        # and under the soft gate, which exists to dispatch everything).
        if (
            not is_reaction_trigger
            and not adapter._soft_gate
            and chatmode != "onmessage"
            and require_mention
            and not was_mentioned
            and not onchar_triggered
        ):
            should_process = False

        # A native slash command belongs to the gateway, never to the trigger
        # gate (issue #259). The plugin answers its own commands and forwards
        # the rest, but both paths sit *after* this gate — so a mention-gated
        # stream used to drop "/deny", "/approve", "/model", "/stop" and
        # every other gateway-native command on the floor, with the message
        # never reaching the host that owns it. That made the exec-approval
        # buttons dead on the recommended posture (chatmode ``oncall``,
        # mention-gated, sticky engagement off): a click sends an ordinary
        # "/approve"/"/deny" message from the clicker, so the window silently
        # lapsed into a refusal — see ``zulip.approvals``.
        #
        # Only the *trigger* gate is bypassed. The per-sender rate limit above
        # and the stream filter, group policy and stream policy below keep
        # their order and their effect, and slash-command authorization stays
        # the host's (an admin-only command is still its decision).
        # ``addressed`` is deliberately left alone: a command that passes only
        # because it is a command was not addressed, so it opens no engagement
        # and harvests no history.
        if is_slash_command:
            should_process = True

        # Sticky topic engagement (#165/#166): once a user (or the whole
        # topic, per scope) has engaged the bot with a real mention/onchar,
        # later messages in that same topic are accepted without a fresh
        # mention until the idle TTL lapses. DMs never reach this block and
        # onmessage streams already answer everything, so engagement is
        # only consulted for mention-gated modes. An engaged follow-up
        # still passes stream filtering, group policy and the per-sender
        # rate limit below exactly like any other message.
        #
        # Loop prevention (#221): a bot-authored message may never be
        # accepted *because* a topic is engaged, nor refresh that
        # engagement — otherwise two bots in one topic keep each other's
        # window open forever. Decided from the sender alone (our own
        # identity, or the realm's `<name>-bot@…` convention), which is why
        # it needs no bot registry and introduces no state.
        sender_is_bot = is_bot_sender(
            sender_email,
            sender_id=message.get("sender_id"),
            bot_email=adapter.email,
            bot_user_id=getattr(adapter, "_bot_user_id", ""),
        )
        engaged_followup = False
        if (
            adapter._engagement_cfg.mode != ENGAGEMENT_MODE_OFF
            and chatmode != "onmessage"
            and not should_process
            and adapter._engagement_store.is_engaged(
                gate_stream_id, gate_topic, sender_email
            )
        ):
            if sender_is_bot:
                logger.debug(
                    "zulip engagement skip [bot sender=%s stream=%s topic=%s]",
                    mask_pii(sender_email),
                    mask_pii(str(message.get("display_recipient", ""))),
                    mask_pii(str(gate_topic)),
                )
            else:
                engaged_followup = True
                should_process = True

        if not should_process:
            # Not addressed and not dispatched: keep it as topic context
            # without ever replying (issue #153).
            if msg_type == "stream" and adapter._observe_group and not addressed:
                adapter._observe_stream_message(message, content)
            # INFO, not DEBUG: this is the single most common reason a user
            # sees no reply, and at DEBUG it was invisible in the gateway log at
            # its default level -- "the bot ignored me" was undiagnosable.
            logger.info(
                "zulip drop [no mention/trigger, mode=%s] stream=%s topic=%s "
                "msg=%s",
                chatmode,
                mask_pii(str(message.get("display_recipient", ""))),
                mask_pii(str(message.get("subject", ""))),
                mask_pii(str(message_id)),
            )
            return

        # --- Stream filtering (Issue #65) ---
        if adapter._streams_filter is not None:
            stream_name = message.get("display_recipient", "").lower()
            if stream_name not in adapter._streams_filter:
                logger.info(
                    "zulip drop [stream=%s not in ZULIP_STREAMS filter] msg=%s",
                    mask_pii(stream_name),
                    mask_pii(str(message_id)),
                )
                return

        # --- Group policy check (Issue #66) ---
        if not adapter._policy.can_group_message(sender_email):
            await adapter._audit_logger.log_policy_block(
                sender_id=sender_email,
                reason=f"group_policy={adapter._policy.group_mode}",
                kind="stream",
            )
            if adapter._policy.group_mode == "disabled":
                reply = "🚫 Stream messages to this bot are currently disabled."
            else:
                reply = "🚫 You are not authorized to send stream messages to this bot."
            try:
                await adapter._sdk_call(
                    adapter.client.send_message,
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
                    timeout=adapter._send_timeout,
                )
            except Exception as e:
                logger.warning("group policy rejection reply failed: %s", mask_pii(str(e)))
            # Mark message as read and stop processing
            try:
                await adapter._sdk_call(
                    adapter.client.update_message_flags,
                    {"messages": [message_id], "op": "add", "flag": "read"},
                    timeout=adapter._send_timeout,
                )
            except Exception:
                pass
            logger.info(
                "zulip group message blocked [policy=%s sender=%s stream=%s]",
                adapter._policy.group_mode,
                mask_pii(sender_email),
                mask_pii(str(message.get("display_recipient", ""))),
            )
            return

        # --- Sticky engagement: explicit stop / session end (#166) ---
        # This sits inside the stream gate — after filtering and group
        # policy, so an unauthorized or off-topic sender can never use it —
        # and before the ``is_command`` interception below. "/unlisten" and
        # "/reset" start with "/" yet are not registered admin commands,
        # so without this they would fall through to an agent turn. A stop
        # is acknowledged in-topic and consumes the message.
        engagement_blocked = False
        if (
            adapter._engagement_cfg.mode != ENGAGEMENT_MODE_OFF
            and chatmode != "onmessage"
        ):
            if is_stop_listening_message(content):
                cleared = adapter._engagement_store.clear(
                    gate_stream_id, gate_topic, sender_email
                )
                await adapter._ack_engagement_stop(message, cleared=cleared)
                await adapter._mark_read(message_id)
                return
            if is_end_session_message(content):
                # A session ender clears the topic and must not re-open it
                # via the engaged_followup path below.
                adapter._engagement_store.clear(
                    gate_stream_id, gate_topic, sender_email
                )
                engagement_blocked = True

        # --- Sticky engagement: open / refresh (#165/#166) ---
        # Only a message that actually reaches the agent — a real
        # mention/onchar, or a follow-up on an already-engaged topic —
        # (re)starts the idle TTL. Keeping onmessage streams out avoids
        # expiry notices for topics that never needed a mention. A bot
        # sender is excluded (#221) even when it names the bot explicitly:
        # a mention by a bot must not open a window that later admits the
        # bot's unmentioned traffic.
        if (
            adapter._engagement_cfg.mode != ENGAGEMENT_MODE_OFF
            and chatmode != "onmessage"
            and not engagement_blocked
            and not sender_is_bot
            and (was_mentioned or onchar_triggered or engaged_followup)
        ):
            adapter._engagement_store.mark_engaged(
                gate_stream_id,
                gate_topic,
                sender_email,
                user_name=sender_full_name,
            )

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

    # Typing is owned by the gateway core's keep-typing loop, which calls
    # the send_typing/stop_typing hooks above every ~2s for the whole
    # agent run. The manual start this block used to do only ever lasted
    # ZULIP_TYPING_DELAY_SECONDS, because handle_message() returns as
    # soon as the background agent task is spawned — a ~2s flash instead
    # of a real thinking indicator. Left as None so the legacy
    # _stop_typing(typing_params) calls in the command/policy paths stay
    # safe no-ops.
    typing_params = None

    # Resolve the sender's display name once, after the drop paths above so
    # a discarded message never costs a lookup. The event payload wins; a
    # miss refreshes the persisted cache with one bounded fetch (#152).
    sender_full_name = await adapter._resolve_display_name(message)

    # --- Command interception (before AI dispatch) ---
    if is_command(content):
        sender_email = message.get("sender_email", "")
        # Determine chat_id early for command replies
        if msg_type == "stream":
            cmd_chat_id = str(message.get("stream_id", ""))
            cmd_topic = message.get("subject", "")
        else:
            cmd_chat_id = _private_chat_id(message)
            cmd_topic = None

        # /continue and /topic-sessions (stable topic sessions): manual
        # re-bind, and the read-only listing of this topic's sessions.
        topic_cmd = (
            content.strip().lower()
            if adapter._routing._conversations is not None and msg_type == "stream"
            else ""
        )
        if topic_cmd == "/continue" or topic_cmd.startswith("/continue "):
            cmd_result = CommandResult(
                handled=True,
                reply=adapter._continue_command_reply(
                    int(message.get("stream_id") or 0),
                    cmd_topic or "",
                    topic_cmd[len("/continue"):].strip(),
                ),
            )
        elif topic_cmd == "/topic-sessions":
            cmd_result = CommandResult(
                handled=True,
                reply=adapter._topic_sessions_command_reply(
                    int(message.get("stream_id") or 0), cmd_topic or ""
                ),
            )
        else:
            cmd_result = commands.handle_command(
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
                    await adapter._sdk_call(
                        adapter.client.send_message,
                        {
                            "type": "stream",
                            "to": message.get("stream_id"),
                            "topic": cmd_topic,
                            "content": cmd_result.reply,
                        },
                        timeout=adapter._send_timeout,
                    )
                else:
                    await adapter._sdk_call(
                        adapter.client.send_message,
                        {
                            "type": "private",
                            "to": _private_recipient_ids(message),
                            "content": cmd_result.reply,
                        },
                        timeout=adapter._send_timeout,
                    )
            except Exception as e:
                logger.warning("command reply failed: %s", mask_pii(str(e)))
            # Clean up: stop typing, mark as read
            await adapter._stop_typing(typing_params)
            await adapter._mark_read(message_id)
            return

    # --- DM policy check (Issue #48) ---
    if msg_type == "private":
        sender_email = message.get("sender_email", "")
        # Approval happens out of process (``python3 -m zulip.pairing``), so
        # pick up a rewritten allowlist before deciding (issue #198). This
        # is one stat() on the data file, and it also makes a revocation
        # take effect without a restart.
        adapter._policy.refresh_if_changed()
        allowed, pairing_code = adapter._policy.check_dm(sender_email)
        if not allowed:
            await adapter._audit_logger.log_policy_block(
                sender_id=sender_email,
                reason=f"dm_policy={adapter._policy.mode}",
                kind="dm",
            )
            reply = ""
            if pairing_code:
                reply = (
                    f"👋 Hi! You need to be approved before messaging this bot.\n\n"
                    f"Your pairing code: **PAIR-{pairing_code}**\n\n"
                    f"Share this code with your admin to get access. They can\n"
                    f"approve it with:\n"
                    f"`python3 -m zulip.pairing approve PAIR-{pairing_code}`"
                )
            elif adapter._policy.mode == "disabled":
                reply = "🚫 DMs to this bot are currently disabled."
            else:
                reply = "🚫 You are not authorized to message this bot."

            try:
                await adapter._sdk_call(
                    adapter.client.send_message,
                    {
                        "type": "private",
                        "to": _private_recipient_ids(message),
                        "content": reply,
                    },
                    timeout=adapter._send_timeout,
                )
            except Exception as e:
                logger.warning("DM policy rejection failed: %s", mask_pii(str(e)))

            # Clean up: stop typing, mark as read
            await adapter._stop_typing(typing_params)
            await adapter._mark_read(message_id)
            logger.info("zulip DM blocked [policy=%s sender=%s]", adapter._policy.mode, mask_pii(sender_email))
            return

    if msg_type == "stream":
        stream_id = message.get("stream_id")
        topic = message.get("subject", "")
        stream_name = message.get("display_recipient", str(stream_id))

        # Cache topic for reply threading
        chat_id = str(stream_id)
        adapter._routing._topic_cache[chat_id] = topic

        source_kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "chat_name": stream_name,
            "chat_type": "stream",
            "user_id": sender_email,
            "user_name": sender_full_name,
        }
        if topic and adapter._routing._conversations is not None:
            # Sessions key on a rename-proof conversation id — never the
            # topic name. Renames must not strand or fabricate sessions;
            # /new is the only way to start a fresh session in a topic.
            if isinstance(stream_id, int):
                if pre_resolved_conversation is not None:
                    # Resolved at dispatch, in event-id order (F1) — a
                    # rename processed since then must not fork this
                    # message onto a fresh conversation.
                    source_kwargs["thread_id"] = pre_resolved_conversation
                else:
                    source_kwargs["thread_id"] = adapter._routing._conversations.resolve(
                        stream_id,
                        topic,
                        anchor_message_id=int(message_id) if message_id else None,
                    )
                # Per-session start labels (v6): first sight of the
                # conversation's live session records where it started.
                adapter._observe_session_start(
                    stream_id, source_kwargs["thread_id"]
                )
            # Malformed stream_id: no thread_id → degrades to the
            # per-stream session (unreachable for well-formed events).
        source = adapter.build_source(**source_kwargs)
        extra_meta = {"topic": topic, "stream_id": stream_id}
        if message.get("_reaction_trigger"):
            # Label the triggered run legibly instead of echoing the
            # internal envelope into the room. (#164)
            extra_meta["reaction_trigger"] = True
            extra_meta["trace_title"] = (
                f"reaction :{message.get('_reaction_emoji', '?')}: — "
                f"{message.get('_reaction_instruction', 'triggered')}"
            )
        if adapter._soft_gate:
            # Only added under the soft gate, so with the flag off the event
            # metadata is byte-identical to before (issue #153).
            extra_meta["addressed"] = addressed
    else:
        sender_id = message.get("sender_id")
        chat_id = _private_chat_id(message)

        # DM session rotation: prevent context bloat by rotating
        # the session key every N turns (default 20, 0 to disable).
        if adapter._dm_session_turn_limit > 0:
            base_key = chat_id
            turn_count = adapter._dm_base_message_counts.get(base_key, 0) + 1
            adapter._dm_base_message_counts[base_key] = turn_count
            epoch = (turn_count - 1) // adapter._dm_session_turn_limit
            if epoch > 0:
                chat_id = f"{base_key}:session:{epoch}"

        source = adapter.build_source(
            chat_id=chat_id,
            chat_name=sender_full_name,
            chat_type="dm",
            user_id=sender_email,
            user_name=sender_full_name,
        )
        extra_meta = {
            "user_id": sender_id,
            "user_email": sender_email,
            "recipient_ids": _private_recipient_ids(message),
        }

    # --- Context-mitigation metadata ---
    now = time.time()
    msg_count = adapter._message_counts.get(chat_id, 0) + 1
    adapter._message_counts[chat_id] = msg_count

    last_time = adapter._last_message_time.get(chat_id)
    session_gap = (now - last_time) if last_time else 0
    adapter._last_message_time[chat_id] = now

    # Detect topic change in streams
    topic_changed = False
    if msg_type == "stream":
        prev_topic = adapter._last_topic_cache.get(chat_id)
        if prev_topic and prev_topic != topic:
            topic_changed = True
        adapter._last_topic_cache[chat_id] = topic

    extra_meta.update({
        "conversation_turn": msg_count,
        "session_gap_seconds": round(session_gap, 1),
        "topic_changed": topic_changed,
    })

    # --- Quoted topic history (issues #153, #148) ---
    #
    # Appended to the agent-facing body only. Slash commands were handled
    # and returned above, and every policy/rate-limit gate has already
    # decided this turn, so quoted context can never change what the bot is
    # allowed to answer or cost a round-trip on a message that is dropped.
    # DMs are never harvested: their sessions are per-user and isolated.
    if msg_type == "stream":
        quoted = await adapter._quoted_topic_history(
            stream_name,
            stream_id,
            topic,
            content=content,
            addressed=addressed,
        )
        if quoted:
            content = quoted + content

    event = MessageEvent(
        text=content,
        message_type=MessageType.TEXT,
        source=source,
        message_id=str(message_id),
        metadata=extra_meta,
    )

    # --- Approval decisions (issue #222) ---
    #
    # An exec-approval click is an ordinary ``/approve`` or ``/deny`` message
    # from the decider, and the gateway's ``post_approval_response`` hook carries
    # no human identity on the gateway surface — so the sender is recorded here,
    # on the way to the host that will resolve the approval. Nothing is consumed,
    # answered or authorised: the message still reaches the gateway, which owns
    # slash authorization and the decision itself. Recorded after every policy
    # gate (an unauthorized sender never gets this far) and before dispatch, so
    # the record exists by the time the outcome fires.
    if is_command(content) and is_approval_decision(content):
        adapter.note_approval_decider(
            session_key=adapter._session_key_for_event(event) or "",
            chat_id=source.chat_id,
            topic=message.get("subject", "") if msg_type == "stream" else None,
            sender_email=message.get("sender_email", ""),
        )

    # --- Per-session queue (issue #151) ---
    #
    # Deliberately last: the turn has already been rate limited, cleared
    # stream gating, stream filtering, group and DM policy, and had any
    # command intercepted, so waiting for its session cannot change what
    # the bot is allowed to answer.
    if adapter._session_queue is not None and await adapter._queue_turn(
        event, reactions, typing_params, message_id, is_slash=is_slash_command
    ):
        return

    await adapter._dispatch_turn(event, reactions, typing_params, message_id)
