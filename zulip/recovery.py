"""Recovery of interrupted messages after gateway restart.

Scans recent DMs for messages with stale 👀 reactions (no ✅/⚠️, no response)
and re-dispatches them with fresh session keys.
"""

import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


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
) -> int:
    """Scan recent DMs for interrupted messages and re-dispatch them.

    Looks for messages where the bot placed a 👀 (start) reaction but
    never placed a ✅ (success) or ⚠️ (error) reaction, and where no
    response from the bot exists after the message.

    Returns the number of messages recovered.
    """
    recovered = 0

    try:
        # Fetch recent private messages
        result = await sdk_call(
            client.get_private_messages,
            timeout=send_timeout,
        )

        if result.get("result") != "success":
            logger.warning("zulip recovery: failed to fetch messages for recovery scan")
            return 0

        messages = result.get("messages", [])
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
                continue

            # Check if there's a response from the bot after this message
            msg_index = messages.index(msg)
            has_response = any(
                m.get("sender_email") == bot_email or str(m.get("sender_id", "")) == bot_user_id
                for m in messages[msg_index + 1:]
            )

            if has_response:
                continue

            # This message was interrupted — re-dispatch with fresh session key
            logger.info(
                "zulip recovery: re-dispatching interrupted message [id=%s sender=%s]",
                message_id,
                sender_email,
            )

            # Create a fresh session key to avoid the dead session from the
            # previous gateway instance
            recovery_key = (
                f"agent:main:zulip:direct:{sender_email}:recovery:{int(asyncio.get_event_loop().time() * 1000)}"
            )
            msg["_recovery_session_key"] = recovery_key

            await handle_message(msg)
            recovered += 1

        logger.info("zulip recovery: complete [recovered=%d]", recovered)

    except Exception as e:
        logger.warning("zulip recovery: failed [error=%s]", e)

    return recovered
