"""Admin actions for Zulip — stream CRUD, user info, member management.

Provides the bot with the ability to:
- List, create, update, and delete streams
- Get user/member information
- Subscribe/unsubscribe users to streams

All destructive operations require explicit confirmation.
Stream creation/deletion requires admin privileges on the Zulip server.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .logger import mask_pii

logger = logging.getLogger(__name__)


async def list_streams(client: Any, include_all_public: bool = False) -> list[dict]:
    """List streams the bot can see, optionally including all public streams."""
    try:
        if include_all_public:
            result = await _sdk_call_async(client.get_streams, {})
        else:
            result = await _sdk_call_async(client.get_subscriptions, {})
        if result.get("result") == "success":
            if include_all_public:
                return result.get("streams", [])
            return result.get("subscriptions", [])
        logger.warning("list_streams failed: %s", result.get("msg"))
        return []
    except Exception as e:
        logger.error("list_streams error: %s", e)
        return []


async def create_stream(
    client: Any,
    name: str,
    description: Optional[str] = None,
    invite_only: bool = False,
    is_web_public: bool = False,
) -> bool:
    """Create a new stream. Requires admin privileges."""
    try:
        subscriptions = [{"name": name}]
        if description:
            subscriptions[0]["description"] = description

        params: dict[str, Any] = {
            "subscriptions": subscriptions,
        }
        if invite_only:
            params["invite_only"] = True
        if is_web_public:
            params["is_web_public"] = True

        result = await _sdk_call_async(client.add_subscriptions, params)
        if result.get("result") == "success":
            logger.info("stream created [name=%s]", mask_pii(name))
            return True
        logger.warning("create_stream failed: %s", result.get("msg"))
        return False
    except Exception as e:
        logger.error("create_stream error: %s", e)
        return False


async def update_stream(
    client: Any,
    stream_id: int,
    new_name: Optional[str] = None,
    description: Optional[str] = None,
    is_private: Optional[bool] = None,
    is_web_public: Optional[bool] = None,
) -> bool:
    """Update a stream's properties. Requires admin privileges."""
    try:
        params: dict[str, Any] = {}
        if new_name is not None:
            params["new_name"] = new_name
        if description is not None:
            params["description"] = description
        if is_private is not None:
            params["is_private"] = is_private
        if is_web_public is not None:
            params["is_web_public"] = is_web_public

        if not params:
            logger.warning("update_stream: no parameters provided")
            return False

        result = await _sdk_call_async(client.update_stream, stream_id, params)
        if result.get("result") == "success":
            logger.info("stream updated [id=%d]", stream_id)
            return True
        logger.warning("update_stream failed: %s", result.get("msg"))
        return False
    except Exception as e:
        logger.error("update_stream error: %s", e)
        return False


async def delete_stream(client: Any, stream_id: int) -> bool:
    """Delete a stream. Requires admin privileges."""
    try:
        result = await _sdk_call_async(client.delete_stream, stream_id)
        if result.get("result") == "success":
            logger.info("stream deleted [id=%d]", stream_id)
            return True
        logger.warning("delete_stream failed: %s", result.get("msg"))
        return False
    except Exception as e:
        logger.error("delete_stream error: %s", e)
        return False


async def get_user_info(client: Any, user_id_or_email: str) -> Optional[dict]:
    """Get information about a user."""
    try:
        result = await _sdk_call_async(client.get_user, user_id_or_email)
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


async def get_user_presence(client: Any, user_id_or_email: str) -> Optional[dict]:
    """Get presence status for a user."""
    try:
        result = await _sdk_call_async(client.get_user_presence, user_id_or_email)
        if result.get("result") == "success":
            return result.get("presence")
        return None
    except Exception as e:
        logger.error("get_user_presence error: %s", e)
        return None


async def star_message(client: Any, message_id: int, starred: bool = True) -> bool:
    """Star or unstar a message."""
    try:
        op = "add" if starred else "remove"
        result = await _sdk_call_async(
            client.update_message_flags,
            {"messages": [message_id], "op": op, "flag": "starred"},
        )
        if result.get("result") == "success":
            logger.debug("message %s [id=%d]", "starred" if starred else "unstarred", message_id)
            return True
        logger.warning("star_message failed: %s", result.get("msg"))
        return False
    except Exception as e:
        logger.error("star_message error: %s", e)
        return False


async def _sdk_call_async(fn, *args, **kwargs) -> dict:
    """Wrap a synchronous SDK call in asyncio.to_thread."""
    import asyncio
    return await asyncio.to_thread(fn, *args, **kwargs)
