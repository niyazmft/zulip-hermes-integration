"""Admin command framework for Zulip Hermes integration.

Commands are parsed BEFORE messages reach the AI agent.
Unknown commands fall through to the AI.
"""

from __future__ import annotations

import os
from typing import Callable, Any
from dataclasses import dataclass

logger = __import__("logging").getLogger(__name__)

CommandHandler = Callable[[str, str, str, str], str]

_COMMANDS: dict[str, CommandHandler] = {}


@dataclass
class CommandResult:
    """Result of command parsing."""

    handled: bool
    reply: str = ""


def register_command(name: str, handler: CommandHandler | None = None):
    """Register a command handler.

    Can be used as a decorator:
        @register_command("help")
        def _cmd_help(args, chat_id, sender_email, sender_name) -> str:
            return "Help text..."
    """

    def decorator(func: CommandHandler) -> CommandHandler:
        _COMMANDS[name] = func
        return func

    if handler is not None:
        _COMMANDS[name] = handler
        return handler
    return decorator


def _extract_command(content: str) -> tuple[str, str] | None:
    """Parse \"/command arg1 arg2\" from message text.

    Returns (cmd, args) or None if not a command.
    """
    stripped = content.strip()
    if stripped.startswith("/"):
        parts = stripped[1:].split(maxsplit=1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        return cmd, args
    return None


def handle_command(
    content: str,
    chat_id: str,
    sender_email: str,
    sender_name: str,
    version: str = "unknown",
) -> CommandResult:
    """Try to parse and execute a command.

    Returns CommandResult(handled=True, reply=...) if a known command matched.
    Returns CommandResult(handled=False) to fall through to AI agent.
    """
    parsed = _extract_command(content)
    if not parsed:
        return CommandResult(handled=False)

    cmd, args = parsed
    handler = _COMMANDS.get(cmd)
    if handler is None:
        return CommandResult(handled=False)

    try:
        reply = handler(args, chat_id, sender_email, sender_name)
        return CommandResult(handled=True, reply=reply)
    except Exception as e:
        logger.warning("command error [cmd=%s sender=%s]: %s", cmd, sender_email, e)
        return CommandResult(handled=True, reply=f"❌ Error processing /{cmd}: {e}")


# ------------------------------------------------------------------
# Built-in commands
# ------------------------------------------------------------------

@register_command("help")
def _cmd_help(
    args: str, chat_id: str, sender_email: str, sender_name: str
) -> str:
    """List available commands."""
    cmd_list = sorted(_COMMANDS.keys())
    lines = ["**Bot Commands:**", ""]
    for name in cmd_list:
        lines.append(f"• `/{name}`")
    lines.extend(
        [
            "",
            "Unknown commands are passed to the AI agent.",
        ]
    )
    return "\n".join(lines)


@register_command("status")
def _cmd_status(
    args: str, chat_id: str, sender_email: str, sender_name: str
) -> str:
    """Show bot status."""
    from .version import __version__, __repo__

    lines = [
        "**Bot Status**",
        f"Version: `{__version__}`",
        f"Repo: {__repo__}",
        f"Sender: {sender_email}",
    ]
    return "\n".join(lines)


@register_command("model")
def _cmd_model(
    args: str, chat_id: str, sender_email: str, sender_name: str
) -> str:
    """Show or set model."""
    if not args:
        return "Current model: default\nUsage: `/model <name>`"
    return f"Model switching is managed by the Hermes gateway. Current: `{args.strip()}`"


def is_command(content: str) -> bool:
    """Check if content looks like a command (starts with /)."""
    return content.strip().startswith("/")
