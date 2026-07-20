"""Reaction status indicators for Zulip messages.

Adds/removes emoji reactions to signal processing state:
- eyes (👀)  → bot is working on the request
- check_mark (✅) → response delivered successfully
- warning (⚠️) → error occurred

Safe wrappers catch and log errors so a reaction failure never breaks the flow.
"""

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default emoji names (Zulip uses underscore names)
DEFAULT_START = "eyes"
DEFAULT_SUCCESS = "check_mark"
DEFAULT_ERROR = "warning"


class ReactionConfig:
    """Holds reaction settings parsed from env/config."""

    def __init__(
        self,
        enabled: bool = True,
        clear_on_finish: bool = True,
        on_start: str = DEFAULT_START,
        on_success: str = DEFAULT_SUCCESS,
        on_error: str = DEFAULT_ERROR,
    ):
        self.enabled = enabled
        self.clear_on_finish = clear_on_finish
        self.on_start = self._normalize(on_start) or DEFAULT_START
        self.on_success = self._normalize(on_success) or DEFAULT_SUCCESS
        self.on_error = self._normalize(on_error) or DEFAULT_ERROR

    @staticmethod
    def _normalize(raw: Optional[str]) -> str:
        if not raw:
            return ""
        return raw.strip().strip(":")

    @classmethod
    def from_env(cls) -> "ReactionConfig":
        """Build config from environment variables."""
        import os

        def truthy(val: str) -> bool:
            return val.lower() not in ("false", "0", "", "no", "off")

        enabled = truthy(os.getenv("ZULIP_REACTIONS_ENABLED", "true"))
        clear = truthy(os.getenv("ZULIP_REACTION_CLEAR_ON_FINISH", "true"))
        return cls(
            enabled=enabled,
            clear_on_finish=clear,
            on_start=os.getenv("ZULIP_REACTION_START", DEFAULT_START),
            on_success=os.getenv("ZULIP_REACTION_SUCCESS", DEFAULT_SUCCESS),
            on_error=os.getenv("ZULIP_REACTION_ERROR", DEFAULT_ERROR),
        )


async def add_reaction(
    client: Any,
    message_id: str,
    emoji_name: str,
    enabled: bool = True,
) -> None:
    """Safely add a Zulip reaction. Errors are logged, not raised."""
    if not enabled or not emoji_name:
        return
    try:
        await asyncio.to_thread(
            client.add_reaction,
            {"message_id": message_id, "emoji_name": emoji_name},
        )
    except Exception as e:
        logger.warning(
            "zulip add reaction failed [message_id=%s emoji=%s error=%s]",
            message_id,
            emoji_name,
            e,
        )


async def remove_reaction(
    client: Any,
    message_id: str,
    emoji_name: str,
    enabled: bool = True,
) -> None:
    """Safely remove a Zulip reaction. Errors are logged, not raised."""
    if not enabled or not emoji_name:
        return
    try:
        await asyncio.to_thread(
            client.remove_reaction,
            {"message_id": message_id, "emoji_name": emoji_name},
        )
    except Exception as e:
        logger.warning(
            "zulip remove reaction failed [message_id=%s emoji=%s error=%s]",
            message_id,
            emoji_name,
            e,
        )


class ReactionLifecycle:
    """High-level helper that manages the full reaction lifecycle for a message."""

    def __init__(self, client: Any, message_id: str, config: ReactionConfig):
        self.client = client
        self.message_id = message_id
        self.config = config

    async def start(self) -> None:
        await add_reaction(
            self.client,
            self.message_id,
            self.config.on_start,
            self.config.enabled,
        )

    async def success(self) -> None:
        cfg = self.config
        if cfg.clear_on_finish:
            await remove_reaction(
                self.client, self.message_id, cfg.on_start, cfg.enabled
            )
        await add_reaction(
            self.client, self.message_id, cfg.on_success, cfg.enabled
        )

    async def error(self) -> None:
        cfg = self.config
        if cfg.clear_on_finish:
            await remove_reaction(
                self.client, self.message_id, cfg.on_start, cfg.enabled
            )
        await add_reaction(
            self.client, self.message_id, cfg.on_error, cfg.enabled
        )
