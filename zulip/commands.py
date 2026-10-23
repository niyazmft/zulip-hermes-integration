"""Admin command framework for Zulip Hermes integration.

Commands are parsed BEFORE messages reach the AI agent.
Unknown commands fall through to the AI.

Gateway-native slash commands (``/help``, ``/status``, ``/model``, ``/stop``,
``/new``, ...) are deliberately **not** registered here: the Hermes gateway
implements them natively, so registering them in the plugin would shadow the
gateway and swallow the command (issue #190). The set is recorded once, as
``GATEWAY_NATIVE_COMMANDS`` -- complete, with its host source -- and
``scripts/check_compat.py`` compares it against the host's own registry (#251).
Only plugin-specific commands are registered below; everything else falls
through to the gateway.
"""

from __future__ import annotations

import re
from typing import Callable
from dataclasses import dataclass

from .logger import mask_pii

logger = __import__("logging").getLogger(__name__)


# The host's own exec-approval decision commands. Recognised (not consumed) so
# the plugin can record *who* decided an approval (issue #222): the gateway
# resolves the approval from this message and its ``post_approval_response``
# hook carries no human identity on the gateway surface. ``/approvals`` — the
# command that shows or sets the approval *mode* — must not match, so the
# pattern requires a word boundary after the verb.
_APPROVAL_DECISION_RE = re.compile(r"^/(?:approve|deny)\b", re.IGNORECASE)


def is_approval_decision(content: str) -> bool:
    """Whether ``content`` is the host's ``/approve`` / ``/deny`` command.

    Covers the modifiers the host accepts (``/approve session``, ``/approve
    always``, ``/approve all``, ``/deny all``). The message is never consumed or
    answered here: it still reaches the gateway, which owns slash authorization
    and the decision itself.
    """
    text = (content or "").strip()
    if not text:
        return False
    return bool(_APPROVAL_DECISION_RE.match(text))

CommandHandler = Callable[[str, str, str, str], str]

_COMMANDS: dict[str, CommandHandler] = {}


@dataclass
class CommandResult:
    """Result of command parsing."""

    handled: bool
    reply: str = ""


# ------------------------------------------------------------------
# The gateway-owned command namespace
#
# A plugin command with one of these names SHADOWS the gateway's own command:
# the plugin's router answers it and the host never sees it (#43 ``/version``,
# #190/#191 ``/help``/``/status``/``/model``). The set used to live here as three
# prose copies, one ending in a literal "...", so it was hand-maintained,
# duplicated and incomplete by admission (#251) -- the ``PLUGIN_FILES`` drift
# mechanism (CONTRIBUTING row 1) applied to command names.
#
# It is recorded here so a host that does not ship its registry can still be
# checked, and ``scripts/check_compat.py`` prefers the host's own
# ``hermes_cli.commands.GATEWAY_KNOWN_COMMANDS`` whenever it is importable --
# derivation first, this constant as the floor.
_GATEWAY_NATIVE_SOURCE = (
    "hermes_cli.commands.GATEWAY_KNOWN_COMMANDS at host v2026.9.24; the same "
    "127 names are present at v2026.9.14 (the declared __min_hermes__ floor). "
    "Names and aliases read from COMMAND_REGISTRY, so this is a superset of the "
    "names the gateway dispatches (host-cli-only commands included): a superset "
    "can only produce a needless refusal, never a missed collision."
)

GATEWAY_NATIVE_COMMANDS: frozenset[str] = frozenset({
    "agents", "approvals", "approve", "battery", "bg", "blueprint", "bp", "branch",
    "browser", "btw", "bundles", "busy", "clear", "codex-runtime", "codex_runtime",
    "commands", "compact", "compose", "compress", "config", "context", "copy", "cron",
    "ctx", "curator", "debug", "deny", "diff", "egress", "exit", "export", "fast",
    "focus", "footer", "fork", "gateway", "generate-pet", "goal", "handoff", "hatch",
    "hb", "heartbeat", "help", "history", "image", "import", "indicator", "init",
    "insights", "journey", "kanban", "learn", "learning", "login", "loop", "memory",
    "memory-graph", "moa", "model", "new", "palette", "paste", "pause", "personality",
    "pet", "plan", "platform", "platforms", "plugins", "proactive", "profile", "prompt",
    "q", "queue", "quit", "reasoning", "redraw", "refine", "reload", "reload-mcp",
    "reload-skills", "reload_mcp", "reload_skills", "reset", "restart", "resume",
    "retry", "review", "rollback", "save", "sb", "sessions", "set-home", "sethome",
    "skills", "skin", "snap", "snapshot", "start", "status", "statusbar", "steer",
    "stop", "subgoal", "subscription", "suggest", "suggestions", "tasks", "timestamps",
    "title", "tools", "toolsets", "topic", "topup", "ts", "undo", "update", "upgrade",
    "usage", "v", "verbose", "version", "voice", "wake", "whoami", "worktree", "yolo",
})

# The provenance above, exposed so a test can assert the set says where it came
# from instead of asking the reader to trust it.
GATEWAY_NATIVE_SOURCE = _GATEWAY_NATIVE_SOURCE


def command_collisions(plugin_names: Any, gateway_names: Any) -> list[str]:
    def _norm(name) -> str:
        return str(name).strip().lower().lstrip("/")

    plugin = {_norm(n) for n in plugin_names or ()} - {""}
    gateway = {_norm(n) for n in gateway_names or ()} - {""}
    return sorted(plugin & gateway)


def plugin_command_names() -> frozenset[str]:
    """The names this plugin answers itself.

    The comparison side of :data:`GATEWAY_NATIVE_COMMANDS`; ``check_compat``
    reads it so a future registration colliding with a host command fails the
    gate rather than a user's slash command.
    """
    return frozenset(_COMMANDS)


def register_command(name: str, handler: CommandHandler | None = None):
    """Register a command handler.

    Can be used as a decorator:
        @register_command("ping")
        def _cmd_ping(args, chat_id, sender_email, sender_name) -> str:
            return "🏓 Pong!"

    Do not register a name in ``GATEWAY_NATIVE_COMMANDS``; that shadows the
    gateway command (issue #190), and ``check_compat`` fails the build when the
    installed host owns it (#251).
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
        logger.warning("command error [cmd=%s sender=%s]: %s", cmd, mask_pii(sender_email), e)
        return CommandResult(handled=True, reply=f"❌ Error processing /{cmd}. Please try again later.")


def is_command(content: str) -> bool:
    """Check if content looks like a command (starts with /)."""
    return content.strip().startswith("/")


# ------------------------------------------------------------------
# Plugin-specific commands
#
# Nothing here may share a name with a gateway-native command. The complete
# set -- derived from the host, not remembered -- is GATEWAY_NATIVE_COMMANDS;
# those must reach the gateway, not be absorbed by this router (issue #190).
# ------------------------------------------------------------------

@register_command("streams")
def _cmd_streams(
    args: str, chat_id: str, sender_email: str, sender_name: str
) -> str:
    """List streams the bot can see.

    Usage: /streams [--all]
    """
    return (
        "Stream management is available through the AI agent.\n"
        "Ask the bot to list, create, or manage streams "
        "using natural language."
    )


@register_command("user")
def _cmd_user(
    args: str, chat_id: str, sender_email: str, sender_name: str
) -> str:
    """Get user information.

    Usage: /user <email_or_id>
    """
    if not args:
        return "Usage: `/user <email_or_id>`"
    return (
        "User information is available through the AI agent.\n"
        f"Ask the bot about user `{args.strip()}`."
    )


@register_command("pin")
def _cmd_pin(
    args: str, chat_id: str, sender_email: str, sender_name: str
) -> str:
    """Star/pin a message.

    Usage: /pin <message_id>
    """
    if not args:
        return "Usage: `/pin <message_id>`"
    return (
        "Message pinning is available through the AI agent.\n"
        f"Ask the bot to pin message `{args.strip()}`."
    )


@register_command("unpin")
def _cmd_unpin(
    args: str, chat_id: str, sender_email: str, sender_name: str
) -> str:
    """Unstar/unpin a message.

    Usage: /unpin <message_id>
    """
    if not args:
        return "Usage: `/unpin <message_id>`"
    return (
        "Message unpinning is available through the AI agent.\n"
        f"Ask the bot to unpin message `{args.strip()}`."
    )
