"""Resilient Zulip API client wrapper with retry, backoff, and typing indicators.

Wraps the official zulip.Client and adds:
- Exponential backoff retry (max 3, 1s/2s/4s)
- Rate-limit (429) respect via Retry-After header
- Retry on: 429, 502, 503, 504, network errors
- All sync calls wrapped in asyncio.to_thread()
"""

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 502, 503, 504}
MAX_RETRIES = 3
BASE_DELAY = 1.0
RATE_LIMIT_DELAY = 10.0
MAX_DELAY = 30.0


class ZulipApiClient:
    """Thin async wrapper around zulip.Client with retry logic."""

    def __init__(self, client: Any):
        self._client = client

    # ------------------------------------------------------------------
    # Internal retry wrapper
    # ------------------------------------------------------------------
    async def _call(self, method_name: str, *args, **kwargs) -> Any:
        """Call a zulip client method with exponential backoff retry."""
        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                fn = getattr(self._client, method_name)
                return await asyncio.to_thread(fn, *args, **kwargs)
            except Exception as e:
                last_error = e
                status = getattr(e, "status", None)
                retry_after = getattr(e, "retry_after", None)

                if attempt >= MAX_RETRIES:
                    break

                # Network errors (no status) are retryable
                if status is not None and status not in RETRY_STATUSES:
                    break

                if status == 429 and retry_after:
                    delay = min(float(retry_after), MAX_DELAY)
                else:
                    delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)

                logger.warning(
                    "zulip %s failed [attempt=%d/%d status=%s delay=%.1fs error=%s]",
                    method_name,
                    attempt + 1,
                    MAX_RETRIES + 1,
                    status,
                    delay,
                    e,
                )
                await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Queue / Events
    # ------------------------------------------------------------------
    async def register(self, event_types: list[str]) -> dict:
        """Register event queue."""
        return await self._call("register", event_types=event_types, fetch_event_id=0)

    async def get_events(self, queue_id: str, last_event_id: int) -> dict:
        """Poll events from queue."""
        return await self._call("get_events", queue_id=queue_id, last_event_id=last_event_id)

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------
    async def send_message(self, request: dict) -> dict:
        """Send a message (stream or private)."""
        return await self._call("send_message", request)

    async def send_typing(self, request: dict) -> dict:
        """Send typing start/stop notification."""
        return await self._call("set_typing_status", request)

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------
    async def add_reaction(self, request: dict) -> dict:
        """Add an emoji reaction to a message."""
        return await self._call("add_reaction", request)

    async def remove_reaction(self, request: dict) -> dict:
        """Remove an emoji reaction from a message."""
        return await self._call("remove_reaction", request)

    # ------------------------------------------------------------------
    # Uploads
    # ------------------------------------------------------------------
    async def upload_file(self, file: Any) -> dict:
        """Upload a file to Zulip server."""
        return await self._call("upload_file", file)

    # ------------------------------------------------------------------
    # Admin / Search
    # ------------------------------------------------------------------
    async def get_members(self) -> dict:
        return await self._call("get_members")

    async def get_messages(self, request: dict) -> dict:
        return await self._call("get_messages", request)

    async def add_subscriptions(self, streams: list[dict], **kwargs) -> dict:
        return await self._call("add_subscriptions", streams, **kwargs)

    async def update_stream(self, stream_id: int, **kwargs) -> dict:
        return await self._call("update_stream", stream_id, **kwargs)

    async def delete_stream(self, stream_id: int) -> dict:
        return await self._call("delete_stream", stream_id)
