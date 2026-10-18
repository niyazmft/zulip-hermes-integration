"""Recovery of interrupted messages after gateway restart.

Scans recent DMs for messages with stale 👀 reactions (no ✅/⚠️, no response)
and re-dispatches them with fresh session keys.
"""

import asyncio
import hashlib
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# How many recent DMs to scan for stale 👀 reactions on startup.
RECOVERY_SCAN_LIMIT = 100

# How many messages one recovery pass may dispatch.
#
# A bound is not a nicety: recovery runs as a task started beside the poll loop,
# so an unbounded catch-up over a busy crash window would answer an arbitrary
# amount of traffic at once, in whatever order the server returned it.
RECOVERY_MAX_DISPATCH = 5

# ``is:dm`` narrows to direct messages (Zulip server >= 7.0; older servers
# accept the legacy ``is:private`` spelling, and modern ones still honour it).
RECOVERY_NARROW = [{"operator": "is", "operand": "dm"}]

# Addressed stream traffic. The DM-only narrow was correct when recovery was
# written -- DMs were the only path that placed an 👀 the gateway could lose --
# but with stream triggers and sticky topic engagement a missed @mention is now
# the *more* likely loss in a shared realm (issue #220). Scoping to mentions
# keeps the scan proportional to work addressed to the bot rather than to total
# realm traffic, and a server that rejects the operator costs only this half of
# the pass: the DM scan runs first and is unaffected.
RECOVERY_MENTION_NARROW = [{"operator": "is", "operand": "mentioned"}]


async def _scan(
    client: Any,
    sdk_call: Callable,
    narrow: list[dict],
    send_timeout: float,
    label: str,
) -> list[dict]:
    """One recovery scan. A failure is logged and dropped, never raised.

    Recovery runs as a task beside startup; a server that rejects a narrow (an
    older Zulip without ``is:mentioned``) must cost that half of the pass and
    nothing else -- it can never delay or break the gateway coming up.
    """
    try:
        result = await sdk_call(
            client.get_messages,
            {
                "anchor": "newest",
                "num_before": RECOVERY_SCAN_LIMIT,
                "num_after": 0,
                "narrow": narrow,
            },
            timeout=send_timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "zulip recovery: %s scan failed [error=%s]; skipping it", label, e
        )
        return []
    if not isinstance(result, dict) or result.get("result") != "success":
        logger.warning(
            "zulip recovery: %s scan unavailable on this server; skipping it", label
        )
        return []
    messages = result.get("messages") or []
    logger.info("zulip recovery: %s scan found %d message(s)", label, len(messages))
    return messages


async def recover_interrupted_messages(
    client: Any,
    bot_email: str,
    bot_user_id: str,
    reaction_start: str,
    reaction_success: str,
    reaction_error: str,
    handle_message: Callable[[dict], Any],
    sdk_call: Callable,
    send_timeout: float,
    dedupe: Any = None,
    max_dispatch: int = RECOVERY_MAX_DISPATCH,
    include_streams: bool = True,
) -> int:
    """Scan recent DMs for interrupted messages and re-dispatch them.

    Looks for messages where the bot placed a 👀 (start) reaction but
    never placed a ✅ (success) or ⚠️ (error) reaction, and where no
    response from the bot exists after the message.

    ``dedupe`` is the poll loop's own store (``ZulipDedupeStore``), whose
    ``check`` claims a key on first call and returns True afterwards. Consulting
    it here is what stops a message in the crash window from being answered
    twice -- once by this pass and once by the new queue (#227). It is optional
    so the function stays testable without an adapter.

    Returns the number of messages recovered.
    """
    recovered = 0

    try:
        # Fetch recent direct messages, oldest first, via the SDK's generic
        # ``get_messages`` (GET /messages). The zulip SDK has no
        # ``get_private_messages`` method, so the former call raised
        # AttributeError on every gateway start and recovery never ran.
        messages = await _scan(
            client, sdk_call, RECOVERY_NARROW, send_timeout, "dm"
        )

        if include_streams:
            # Addressed stream traffic, appended after the DM scan so the DM
            # result -- and everything derived from its order -- is unchanged
            # (#220). Ids are deduplicated: a message can match both narrows.
            known = {str(m.get("id", "")) for m in messages}
            for extra in await _scan(
                client, sdk_call, RECOVERY_MENTION_NARROW, send_timeout, "mention"
            ):
                key = str(extra.get("id", ""))
                if key and key not in known:
                    known.add(key)
                    messages.append(extra)

        if not messages:
            return 0

        logger.info(
            "zulip recovery: scanning %d messages for stale reactions",
            len(messages),
        )

        # Build set of message IDs that have responses from the bot
        responded_ids: set[str] = set()
        for msg in messages:
            sender_email = msg.get("sender_email", "") or ""
            sender_id = str(msg.get("sender_id", "") or "")
            if sender_email == bot_email or sender_id == bot_user_id:
                responded_ids.add(str(msg.get("id", "")))

        for msg in messages:
            message_id = str(msg.get("id", ""))
            if not message_id:
                continue

            sender_email = msg.get("sender_email", "") or ""
            sender_id = str(msg.get("sender_id", "") or "")

            # Skip messages from the bot itself
            if sender_email == bot_email or sender_id == bot_user_id:
                continue

            # Check if the bot has a start reaction on this message
            reactions = msg.get("reactions") or []
            has_start = any(
                r.get("emoji_name") == reaction_start
                and (
                    r.get("user", {}).get("email") == bot_email
                    or str(r.get("user_id", "") or r.get("user", {}).get("id", "")) == bot_user_id
                )
                for r in reactions
            )

            if not has_start:
                logger.info(
                    "zulip recovery: skip [id=%s reason=no start reaction]", message_id
                )
                continue

            # Check if the bot has an end reaction (success or error)
            has_end = any(
                r.get("emoji_name") in (reaction_success, reaction_error)
                and (
                    r.get("user", {}).get("email") == bot_email
                    or str(r.get("user_id", "") or r.get("user", {}).get("id", "")) == bot_user_id
                )
                for r in reactions
            )

            if has_end:
                logger.info(
                    "zulip recovery: skip [id=%s reason=already end-reacted]", message_id
                )
                continue

            # Check if there's a response from the bot after this message
            msg_index = messages.index(msg)
            has_response = any(
                m.get("sender_email") == bot_email or str(m.get("sender_id", "")) == bot_user_id
                for m in messages[msg_index + 1:]
            )

            if has_response:
                logger.info(
                    "zulip recovery: skip [id=%s reason=already answered]", message_id
                )
                continue

            # Claim the id in the poll loop's own store before dispatching.
            # ``check`` records on first call, so whichever of recovery and the
            # poll loop gets here first wins and the other drops it (#227).
            if dedupe is not None and dedupe.check(message_id):
                logger.info(
                    "zulip recovery: skip [id=%s reason=duplicate]", message_id
                )
                continue

            if recovered >= max_dispatch:
                logger.info(
                    "zulip recovery: skip [id=%s reason=cap reached] cap=%d",
                    message_id,
                    max_dispatch,
                )
                break

            # This message was interrupted — re-dispatch with fresh session key
            logger.info(
                "zulip recovery: re-dispatching interrupted message [id=%s sender=%s]",
                message_id,
                sender_email,
            )

            # A DM needs a fresh session key to avoid the dead session from the
            # previous gateway instance. A STREAM message must not get one: its
            # session comes from the topic conversation registry, and a
            # DM-shaped key would be a second, drifting derivation of routing.
            # Preserving ``stream_id``/``subject`` (untouched on the dict) is
            # what makes the reply land in the topic the user wrote in (#220).
            if msg.get("type") != "stream":
                # Hash the sender email to avoid leaking PII in session keys.
                email_hash = hashlib.sha256(sender_email.encode()).hexdigest()[:16]
                recovery_key = (
                    f"agent:main:zulip:direct:{email_hash}:recovery:"
                    f"{int(asyncio.get_event_loop().time() * 1000)}"
                )
                msg["_recovery_session_key"] = recovery_key
            # Recovery only selects messages that ALREADY carry the start
            # reaction, so the inbound path must not add it again: the server
            # deduplicates it silently and the log cannot tell that apart from a
            # failure (#227).
            msg["_recovery_skip_start"] = True

            await handle_message(msg)
            recovered += 1

        logger.info("zulip recovery: complete [recovered=%d]", recovered)

    except Exception as e:
        logger.warning("zulip recovery: failed [error=%s]", e)

    return recovered
