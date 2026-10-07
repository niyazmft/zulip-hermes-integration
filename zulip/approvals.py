"""Native exec-approval buttons for the Zulip adapter (zform widget).

Zulip has no Discord-style component API, but bots can attach a generic button
widget to any message via the ``widget_content`` send-message parameter
(zform/choices; the same mechanism the official ``trivia_bot`` uses). On
web/desktop each choice renders as a button, and a click makes the CLIENT send
an ordinary message from the clicker whose content is the choice's ``reply``.
Mapping each reply to the gateway's plain-text approval command reuses the
existing resolution path unchanged: the press IS a typed ``/approve``-family
message from the clicker, so authorization is identical. It reaches the
gateway because a native slash command is never mention-gated in a stream
(``inbound.handle_message``, issue #259) — without that rule, a prompt in a
gated stream renders buttons whose clicks are dropped before the host sees
them, and the approval lapses into a timeout refusal. Clients without widget
support (mobile, terminals) show only the message text — the same prompt the
gateway's text fallback renders.

This module owns the whole concern: the widget payload
(:func:`zform_widget_for_approval`), the plain-text reply instructions
(:func:`approval_fallback_instructions`) and the two-message send path
(:func:`send_exec_approval_prompt`) behind the adapter's
``_send_exec_approval_prompt`` hook.

Layering: L3 — depends on ``zulip.logger`` (L0) and ``zulip.zulip_client`` (L1)
plus the host's ``gateway.platforms.base``. The send path takes the adapter as
an opaque collaborator and reads its ``client`` / ``_sdk_call`` /
``_send_timeout`` / ``_audit_logger`` / ``_routed_topic`` / ``_topic_cache`` at
call time, so an instance patch of any of those still takes effect.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from gateway.platforms.base import SendResult

# Native exec-approval buttons (ExecApprovalPrompt + _send_exec_approval_prompt)
# first shipped in Hermes 0.21.3. Import the type defensively so the plugin
# still loads on older gateways: there the gateway never calls the hook and
# keeps using its plain-text approval prompt, instead of the whole plugin
# failing to import.
try:
    from gateway.platforms.base import ExecApprovalPrompt
except ImportError:  # pragma: no cover - gateways < 0.21.3
    ExecApprovalPrompt = Any  # type: ignore[assignment,misc]

from .logger import format_zulip_log, mask_pii
from .zulip_client import parse_target

logger = logging.getLogger(__name__)

# Gateway approval choice -> the exact command a button's click replays. The
# keys mirror the gateway's ``_exec_approval_actions`` vocabulary; ``adapter``
# re-exports both tables only for the drift guard in
# tests/test_exec_approval_zform.py.
_EA_ZFORM_REPLY: dict[str, str] = {
    "once": "/approve",
    "session": "/approve session",
    "always": "/approve always",
    "deny": "/deny",
}
_EA_ZFORM_INSTRUCTIONS: dict[str, str] = {
    "once": "`/approve` to execute this one operation",
    "session": "`/approve session` to approve this pattern for the session",
    "always": "`/approve always` to approve permanently",
    "deny": "`/deny` to cancel",
}


def zform_widget_for_approval(prompt: ExecApprovalPrompt) -> str | None:
    """JSON ``widget_content`` for an exec-approval prompt, or None when the
    prompt carries no renderable actions.

    Schema per web/src/zform_data.ts (client zod): every choice needs
    ``type``/``short_name``/``long_name``/``reply`` strings and extra_data a
    ``heading``. The server validator (check_widget_content) does NOT check
    the per-choice ``type``, but the web renderer rejects its absence — a
    payload missing it sends fine and silently renders nothing.
    """
    choices: list[dict[str, str]] = []
    for label, choice, _style in prompt.actions:
        reply = _EA_ZFORM_REPLY.get(choice)
        if not reply:
            # Unknown choice vocabulary — bail out so the gateway's text
            # fallback renders instead of a widget that resolves nothing.
            return None
        choices.append(
            {
                "type": "multiple_choice",
                "short_name": chr(ord("A") + len(choices)) if len(choices) < 26 else "?",
                "long_name": str(label),
                "reply": reply,
            }
        )
    if not choices:
        return None
    return json.dumps(
        {
            "widget_type": "zform",
            "extra_data": {
                "type": "choices",
                "heading": "Choose an action:",
                "choices": choices,
            },
        }
    )


def approval_fallback_instructions(prompt: ExecApprovalPrompt) -> str:
    """Plain-text reply instructions mirroring the gateway's text fallback,
    built from the same action set as the buttons (widget-less clients)."""
    instructions = [
        _EA_ZFORM_INSTRUCTIONS[choice]
        for _label, choice, _style in prompt.actions
        if choice in _EA_ZFORM_INSTRUCTIONS
    ]
    if not instructions:
        return ""
    if len(instructions) == 1:
        return f"Reply {instructions[0]}."
    return "Reply " + ", ".join(instructions[:-1]) + f", or {instructions[-1]}."


async def send_plain_to_route(
    adapter: Any, chat_id: str, topic: Optional[str], content: str
) -> bool:
    """Send one plain message to an already-resolved approval route.

    The outcome-notice path (#222, reused by the owner-only rejection in #228):
    the route was resolved when the prompt was sent or when the outcome fired,
    so nothing here re-derives a topic. Best-effort — the approval already
    resolved and nothing may raise into the thread waiting on it — and the
    caller decides what a failure means.
    """
    try:
        target = parse_target(chat_id)
    except Exception as e:
        logger.error(
            format_zulip_log(
                "zulip approval notice send error",
                chat_id=mask_pii(chat_id),
                error=mask_pii(str(e)),
            )
        )
        return False

    if target["type"] == "dm":
        request: dict[str, Any] = {"type": "private", "to": target["user_ids"]}
    else:
        request = {
            "type": "stream",
            "to": target["stream_id"],
            "topic": topic or "general",
        }
    request["content"] = content
    try:
        result = await adapter._sdk_call(
            adapter.client.send_message, request, timeout=adapter._send_timeout
        )
    except Exception as e:
        logger.warning("zulip approval notice send failed: %s", mask_pii(str(e)))
        return False
    if isinstance(result, dict) and result.get("result") == "success":
        return True
    logger.warning("zulip approval notice rejected: %s", mask_pii(str(result)))
    return False


async def send_exec_approval_prompt(
    adapter: Any, prompt: ExecApprovalPrompt
) -> SendResult:
    """Send the exec-approval prompt as context text + a zform-button message.

    Two messages, because a rendered zform widget REPLACES its own message
    body on web/desktop (``$outer_elem.empty().append($choices)`` in
    web/src/zform.ts): context sent as the widget message's text would be
    invisible exactly where the buttons render. Message 1 = the shared
    approval text plus reply instructions (visible on every client);
    message 2 = the button widget. Non-widget clients show both as plain
    text. Delivered if either message sends — the text alone is the full
    text-fallback experience — so the runner's plain-text re-send only
    happens when both fail.
    """
    widget_content = zform_widget_for_approval(prompt)
    instructions = approval_fallback_instructions(prompt)
    content = prompt.text if not instructions else f"{prompt.text}\n\n{instructions}"
    try:
        target = parse_target(prompt.chat_id)
    except Exception as e:
        logger.error(
            format_zulip_log(
                "zulip approval prompt send error",
                chat_id=mask_pii(prompt.chat_id),
                error=mask_pii(str(e)),
            )
        )
        await adapter._audit_logger.log_deliver_failed(
            "invalid_target", chat_id=prompt.chat_id
        )
        return SendResult(success=False, message_id="")

    if target["type"] == "dm":
        base: dict[str, Any] = {"type": "private", "to": target["user_ids"]}
        audit_topic: str | None = None
        topic: str | None = None
    else:
        # prompt.metadata carries the turn's routing metadata (thread_id =
        # a conversation id with stable topic sessions, else the topic
        # name); the cache is only a fallback.
        topic = adapter._routed_topic(target["stream_id"], prompt.metadata) or (
            adapter._routing._topic_cache.get(prompt.chat_id, "general")
        )
        base = {"type": "stream", "to": target["stream_id"], "topic": topic}
        audit_topic = topic

    # Remember where this adapter rendered the prompt (#222): the outcome hook
    # fires for the whole process and carries only the session key, so the
    # adapter that owns the prompt is the one that may speak for it — and the
    # recorded route is the topic the prompt actually landed in, not one
    # re-derived later. ``prompt.session_key`` is the host's own key, the same
    # one both approval hooks fire with.
    try:
        adapter.remember_approval_prompt(prompt.session_key, prompt.chat_id, topic)
    except Exception:  # bookkeeping must never stop a prompt being delivered
        logger.debug("zulip approval route bookkeeping failed", exc_info=True)

    async def _send(request: dict[str, Any]) -> SendResult | None:
        try:
            result = await adapter._sdk_call(
                adapter.client.send_message, request, timeout=adapter._send_timeout
            )
        except Exception as e:
            logger.error(
                format_zulip_log(
                    "zulip approval prompt send error",
                    chat_id=mask_pii(prompt.chat_id),
                    error=mask_pii(str(e)),
                )
            )
            await adapter._audit_logger.log_deliver_failed(
                "send_exception", chat_id=prompt.chat_id, topic=audit_topic
            )
            return None
        if result.get("result") == "success":
            logger.debug("zulip approval message sent to %s", mask_pii(prompt.chat_id))
            message_id = str(result.get("id", ""))
            await adapter._audit_logger.log_deliver_payload(
                chat_id=prompt.chat_id, topic=audit_topic, message_id=message_id
            )
            return SendResult(success=True, message_id=message_id)
        logger.error(
            format_zulip_log(
                "zulip approval prompt send failed",
                chat_id=mask_pii(prompt.chat_id),
                error=mask_pii(str(result)),
            )
        )
        await adapter._audit_logger.log_deliver_failed(
            "api_error", chat_id=prompt.chat_id, topic=audit_topic
        )
        return None

    context_result = await _send({**base, "content": content})
    if widget_content is None:
        return context_result or SendResult(success=False, message_id="")

    widget_result = await _send(
        {
            **base,
            "content": "Choose an action:",
            "widget_content": widget_content,
        }
    )
    if widget_result is not None:
        # Last-sent id: the interactive message is the one later tooling
        # would target (matches SendResult's newest-chunk convention).
        return widget_result
    return context_result or SendResult(success=False, message_id="")
