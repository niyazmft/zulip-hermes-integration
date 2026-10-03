"""Zulip's ``BasePlatformAdapter`` data surface.

The read/query half of the host contract — chat info, topic resolution, message
fetch and search, stream listing and subscription, message deletion and
starring, and user presence and lookup — lives here. Each function is a thin
delegate over the adapter's SDK connection: it validates its inputs, calls the
SDK method through the adapter's ``_sdk_call`` timeout wrapper, and normalises
the API result into the shape the host expects.

Layering: L3 — depends on L0/L1 (``settings``, ``zulip_client``, ``logger``).

Every one of the ten methods stays an override on ``ZulipAdapter`` with an
unchanged name and signature, because the host calls it by name on the adapter;
each override is a one-line delegate into this module. The adapter is passed in
as an opaque collaborator and its state (``client``, ``_sdk_call``,
``_send_timeout``, ``_validate_message_id``) is read at call time, so an
instance patch of any of those still takes effect. ``adapter`` is deliberately
never imported here — that would be a cycle.

``zulip_client.user_lookup_call`` is reached through its owning module rather
than imported as a name, so the lookup keeps honouring a patch of
``zulip.zulip_client``.
"""

from __future__ import annotations

import logging
from typing import Any

from . import zulip_client
from .logger import mask_pii
from .settings import MAX_INPUT_LENGTH

logger = logging.getLogger(__name__)


def _validate_string_length(
    value: Any, name: str, max_length: int = MAX_INPUT_LENGTH
) -> str:
    """Validate and truncate a string input to prevent DoS.

    Raises ValueError if the value is not a string or exceeds max_length.
    """
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds maximum length ({len(value)} > {max_length})")
    return value


async def get_chat_info(adapter: Any, chat_id: str) -> dict[str, Any]:
    """Get information about a chat/channel."""
    if chat_id.startswith("dm:"):
        return {"name": chat_id, "type": "dm"}
    return {"name": chat_id, "type": "stream"}


async def resolve_topic(adapter: Any, stream_id: int, topic: str) -> dict[str, Any]:
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
        result = await adapter._sdk_call(
            adapter.client.update_message,
            {
                "message_id": 0,  # Not used for topic updates with propagate_mode
                "topic": resolved_topic,
                "propagate_mode": "change_all",
            },
            timeout=adapter._send_timeout,
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


async def fetch_messages(
    adapter: Any,
    stream: str,
    topic: str | None = None,
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
        result = await adapter._sdk_call(
            adapter.client.get_messages,
            {
                "anchor": "newest",
                "num_before": min(max(1, limit), 1000),
                "num_after": 0,
                "narrow": narrow,
            },
            timeout=adapter._send_timeout,
        )
        if result.get("result") == "success":
            return result.get("messages", [])
        logger.warning("fetch_messages failed: %s", result.get("msg"))
        return []
    except Exception as e:
        logger.error("fetch_messages error: %s", e)
        return []


async def search_messages(
    adapter: Any,
    query: str,
    stream: str | None = None,
    topic: str | None = None,
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
        result = await adapter._sdk_call(
            adapter.client.get_messages,
            {
                "anchor": "newest",
                "num_before": min(max(1, limit), 1000),
                "num_after": 0,
                "narrow": narrow,
            },
            timeout=adapter._send_timeout,
        )
        if result.get("result") == "success":
            return result.get("messages", [])
        logger.warning("search_messages failed: %s", result.get("msg"))
        return []
    except Exception as e:
        logger.error("search_messages error: %s", e)
        return []


async def list_streams(adapter: Any) -> list[dict]:
    """List all streams the bot can see."""
    try:
        result = await adapter._sdk_call(
            adapter.client.get_streams,
            timeout=adapter._send_timeout,
        )
        if result.get("result") == "success":
            return result.get("streams", [])
        return []
    except Exception as e:
        logger.error("list_streams error: %s", e)
        return []


async def subscribe_stream(adapter: Any, stream_name: str) -> bool:
    """Subscribe the bot to a stream."""
    stream_name = _validate_string_length(stream_name, "stream_name")
    try:
        result = await adapter._sdk_call(
            adapter.client.add_subscriptions,
            {stream_name},
            timeout=adapter._send_timeout,
        )
        return result.get("result") == "success"
    except Exception as e:
        logger.error("subscribe_stream error: %s", e)
        return False


async def delete_message(adapter: Any, message_id: int) -> bool:
    """Delete a message by ID."""
    try:
        validated_id = adapter._validate_message_id(message_id)
        result = await adapter._sdk_call(
            adapter.client.delete_message,
            validated_id,
            timeout=adapter._send_timeout,
        )
        return result.get("result") == "success"
    except Exception as e:
        logger.error("delete_message error: %s", e)
        return False


async def get_user_presence(adapter: Any, user_id_or_email: str) -> dict | None:
    """Get presence status for a user."""
    try:
        result = await adapter._sdk_call(
            adapter.client.get_user_presence,
            user_id_or_email,
            timeout=adapter._send_timeout,
        )
        if result.get("result") == "success":
            return result.get("presence")
        return None
    except Exception as e:
        logger.error("get_user_presence error: %s", e)
        return None


async def star_message(adapter: Any, message_id: int, starred: bool = True) -> bool:
    """Star or unstar a message."""
    try:
        validated_id = adapter._validate_message_id(message_id)
        op = "add" if starred else "remove"
        result = await adapter._sdk_call(
            adapter.client.update_message_flags,
            {"messages": [validated_id], "op": op, "flag": "starred"},
            timeout=adapter._send_timeout,
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


async def get_user_info(adapter: Any, user_id_or_email: str) -> dict | None:
    """Get information about a user, by numeric id or email address.

    ``zulip`` 0.9.1 has no ``get_user`` method (issue #196), so the lookup
    goes through whatever the installed SDK actually exposes.
    """
    fn, args = zulip_client.user_lookup_call(adapter.client, user_id_or_email)
    if fn is None:
        logger.error(
            "get_user_info unavailable: %s exposes no supported user lookup "
            "(issue #196)",
            type(adapter.client).__name__,
        )
        return None
    try:
        result = await adapter._sdk_call(fn, *args, timeout=adapter._send_timeout)
        if isinstance(result, dict) and result.get("result") == "success":
            user = result.get("user") or {}
            if isinstance(user, list):
                user = user[0] if user else {}
            return {
                "user_id": user.get("user_id"),
                "email": user.get("email"),
                "full_name": user.get("full_name"),
                "is_admin": user.get("is_admin", False),
                "is_bot": user.get("is_bot", False),
            }
        logger.warning(
            "get_user_info failed: %s",
            result.get("msg") if isinstance(result, dict) else result,
        )
        return None
    except Exception as e:
        logger.error("get_user_info error: %s", mask_pii(str(e)))
        return None
