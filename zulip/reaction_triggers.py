"""In-channel action triggers via reactions (epic #149 / issues #163, #164).

A configured emoji reaction on the bot's **own** stream message turns into a
normal agent turn for that stream/topic session, so the work and the
discussion stay in the same place.

Safety properties (each is load-bearing):

- **A reaction is a trigger, never an authorisation bypass.** The synthetic
  turn is dispatched through the normal message path carrying the *reacting
  human* as its sender, so DM/group policy, static and persisted allowlists,
  the command gate and the per-sender rate limit all apply to them.
- **Only the bot's own messages are actionable** by default. Reacting on
  someone else's message must not make the agent act on it.
- **Idempotent per reaction** through the existing on-disk dedupe store, so
  repeated taps, toggles, replayed events and restarts cannot re-fire.
- **Off by default**: no configured trigger means the ``reaction`` event type
  is not requested at all.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A trigger instruction is a short directive, not a document.
MAX_INSTRUCTION_LENGTH = 500

# Default topic when the target message has none (mirrors the adapter).
DEFAULT_TOPIC = "general"


def normalize_emoji_name(raw: Optional[str]) -> str:
    """Normalise an emoji name the way reaction config does.

    Zulip uses underscore names (``+1``, ``check_mark``); strip the colons a
    user might type and case-fold so ``:Thumbs_Up:`` and ``thumbs_up`` match.
    """
    if not raw:
        return ""
    return raw.strip().strip(":").strip().lower()


class ReactionTriggerConfig:
    """Emoji-name -> instruction map plus the "any message" escape hatch."""

    def __init__(
        self,
        triggers: Optional[dict[str, str]] = None,
        any_message: bool = False,
    ):
        self.triggers: dict[str, str] = dict(triggers or {})
        self.any_message = bool(any_message)

    @property
    def enabled(self) -> bool:
        return bool(self.triggers)

    @classmethod
    def from_mapping(
        cls,
        raw_triggers: Any,
        any_message: bool = False,
    ) -> "ReactionTriggerConfig":
        """Build a config from a JSON string or a mapping, ignoring junk."""
        parsed = raw_triggers
        if isinstance(raw_triggers, str):
            text = raw_triggers.strip()
            if not text:
                parsed = {}
            else:
                try:
                    parsed = json.loads(text)
                except (ValueError, TypeError):
                    logger.warning(
                        "ZULIP_REACTION_TRIGGERS is not valid JSON; "
                        "reaction triggers are disabled"
                    )
                    parsed = {}

        triggers: dict[str, str] = {}
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if not isinstance(value, str):
                    continue
                emoji = normalize_emoji_name(str(key))
                instruction = " ".join(value.split())[:MAX_INSTRUCTION_LENGTH]
                if emoji and instruction:
                    triggers[emoji] = instruction
        return cls(triggers, any_message)

    @classmethod
    def from_env(cls) -> "ReactionTriggerConfig":
        def truthy(val: str) -> bool:
            return val.strip().lower() not in ("", "false", "0", "no", "off")

        return cls.from_mapping(
            os.getenv("ZULIP_REACTION_TRIGGERS", ""),
            any_message=truthy(
                os.getenv("ZULIP_REACTION_TRIGGER_ANY_MESSAGE", "false")
            ),
        )


class MatchedReactionTrigger:
    """The parts of a reaction event that identify one trigger firing."""

    __slots__ = ("emoji", "instruction", "message_id", "user_id")

    def __init__(self, emoji: str, instruction: str, message_id: str, user_id: str):
        self.emoji = emoji
        self.instruction = instruction
        self.message_id = message_id
        self.user_id = user_id


def match_reaction_trigger(
    event: Any,
    config: ReactionTriggerConfig,
) -> Optional[MatchedReactionTrigger]:
    """Return the matched trigger for a ``reaction`` event, else ``None``.

    Anything that is not an ``add`` of a configured emoji with a usable
    message/user identity is silently not a trigger, not an error.
    """
    if not config.enabled or not isinstance(event, dict):
        return None
    if event.get("type") != "reaction" or event.get("op") != "add":
        return None

    message_id = str(event.get("message_id") or "").strip()
    if not message_id:
        return None

    emoji = normalize_emoji_name(event.get("emoji_name"))
    instruction = config.triggers.get(emoji) if emoji else None
    if not emoji or not instruction:
        return None

    user_id = str(event.get("user_id") or "").strip()
    if not user_id:
        return None

    return MatchedReactionTrigger(emoji, instruction, message_id, user_id)


def is_eligible_target_message(
    message: Any,
    *,
    any_message: bool,
    bot_user_id: str = "",
    bot_email: str = "",
) -> bool:
    """Whether the reacted message may be triggered on.

    ``any_message=False`` (the default) is the safety rule: a reaction is an
    approval of the *agent's* proposal, so reacting on anyone else's message
    is ignored.
    """
    if not isinstance(message, dict):
        return False
    if message.get("type") != "stream":
        return False
    if any_message:
        return True

    sender_id = str(message.get("sender_id") or "").strip()
    if bot_user_id and sender_id and sender_id == str(bot_user_id):
        return True

    sender_email = str(message.get("sender_email") or "").strip().lower()
    if bot_email and sender_email and sender_email == bot_email.strip().lower():
        return True
    return False


def build_reaction_trigger_message(
    target: dict,
    matched: MatchedReactionTrigger,
    stream_name: str,
    topic: str,
    *,
    user_email: str = "",
    user_name: str = "",
) -> dict:
    """Build the synthetic inbound turn for a matched trigger.

    It carries the *reacting human* as the sender so every authorisation and
    rate-limit decision is made about them, and the original message id so
    reactions and session context stay consistent. The ``_reaction_*`` keys
    let the message handler skip the "did a human address the bot?" gate while
    keeping the policy ones.
    """
    where = f"#{stream_name} / {topic}" if topic else f"#{stream_name}"
    who = user_name or user_email or matched.user_id
    content = (
        f"[Zulip reaction] {who} reacted with :{matched.emoji}: to your message "
        f"in {where}.\nInstruction: {matched.instruction}"
    )
    return {
        "id": str(target.get("id") or matched.message_id),
        "sender_id": matched.user_id,
        "sender_email": user_email,
        "sender_full_name": user_name or user_email or matched.user_id,
        "content": content,
        "timestamp": target.get("timestamp") or int(time.time()),
        "type": "stream",
        "display_recipient": stream_name,
        "subject": topic,
        "stream_id": target.get("stream_id"),
        "_reaction_trigger": True,
        "_reaction_emoji": matched.emoji,
        "_reaction_user_id": matched.user_id,
        "_reaction_instruction": matched.instruction,
    }


def reaction_dedupe_key(message_id: str, emoji: str, user_id: str) -> str:
    """Stable key: one dispatch per (message, emoji, user)."""
    return f"reaction:{message_id}:{emoji}:{user_id}"


def find_unsubscribed_streams(
    monitored: list[str],
    subscribed: list[str],
) -> list[str]:
    """Monitored streams the bot is not subscribed to.

    Zulip only delivers ``reaction`` events for streams the user is
    subscribed to, while ``message`` events arrive anyway when the queue was
    registered with ``all_public_streams``. The mismatch makes reaction
    triggers fail silently, so the missing set is surfaced at startup.

    ``"*"`` cannot be enumerated, so it returns ``[]`` and the caller reports
    the subscribed list instead of pretending it checked.
    """
    if "*" in monitored:
        return []
    subscribed_names = {
        str(name).strip().lower() for name in subscribed if str(name).strip()
    }
    return [
        name
        for name in monitored
        if str(name).strip() and str(name).strip().lower() not in subscribed_names
    ]
