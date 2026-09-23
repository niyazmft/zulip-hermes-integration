"""Stub for gateway.platforms.base — provides minimal types for adapter imports."""

from dataclasses import dataclass, field
from typing import Optional, Any, Literal
from enum import Enum


class MessageType(Enum):
    TEXT = "text"


@dataclass
class SendResult:
    success: bool
    message_id: str = ""


@dataclass
class ExecApprovalPrompt:
    """Mirror of gateway.platforms.base.ExecApprovalPrompt — input to the
    native-button exec-approval hook (_send_exec_approval_prompt)."""

    chat_id: str
    session_key: str
    text: str
    actions: list = field(default_factory=list)  # rows of (label, choice, style)
    command: str = ""
    description: str = ""
    smart_denied: bool = False
    metadata: Optional[dict] = None

    @property
    def choices(self) -> list:
        return [choice for _, choice, _ in self.actions]


@dataclass
class MessageSource:
    chat_id: str = ""
    chat_name: str = ""
    chat_type: str = ""
    user_id: str = ""
    user_name: str = ""
    # Session-scoping discriminator on the real SessionSource. Only set by the
    # adapter when ZULIP_TOPIC_SESSIONS is enabled.
    thread_id: str = ""


@dataclass
class MessageEvent:
    text: str = ""
    message_type: MessageType = MessageType.TEXT
    source: MessageSource = field(default_factory=MessageSource)
    message_id: str = ""
    metadata: dict = field(default_factory=dict)


class BasePlatformAdapter:
    """Minimal stub of BasePlatformAdapter."""

    def __init__(self, config, platform):
        self._config = config
        self._platform = platform
        self.platform = platform
        self._connected = False

    @property
    def name(self) -> str:
        """Human-readable name for this adapter (mirrors the real
        BasePlatformAdapter.name property in gateway.platforms.base)."""
        value = getattr(self.platform, "value", self.platform)
        return str(value).title()

    def build_source(self, **kwargs):
        return MessageSource(**kwargs)

    async def handle_message(self, event: MessageEvent):
        pass

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No-op typing hook (the real base's default)."""

    async def stop_typing(self, chat_id: str, metadata=None) -> None:
        """No-op typing hook (the real base's default)."""

    def _mark_connected(self):
        self._connected = True

    def _mark_disconnected(self):
        self._connected = False

    def _accepts_kwarg(self, func, name: str, *, var_kw: bool = False, unknown: bool = False) -> bool:
        """Mirror of gateway.platforms.base.BasePlatformAdapter._accepts_kwarg:
        True when ``func`` accepts the keyword ``name`` (explicitly, via
        **kwargs when var_kw, or — when unknown — via an unknown-kwargs
        catch-all). Kept in sync with the real gateway base so introspection
        behavior (e.g. _stop_typing_with_metadata) is testable."""
        import inspect

        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):
            return unknown
        return name in params or (var_kw and any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()))

    async def _stop_typing_with_metadata(self, chat_id: str, metadata=None) -> None:
        """Mirror of the real gateway base hook: stop typing, forwarding
        ``metadata`` only when ``stop_typing`` accepts it. Legacy
        ``stop_typing(chat_id)`` adapters keep working via introspection."""
        if metadata and self._accepts_kwarg(
                self.stop_typing, "metadata", var_kw=True, unknown=False):
            await self.stop_typing(chat_id, metadata=metadata)
            return
        await self.stop_typing(chat_id)
