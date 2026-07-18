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
class MessageSource:
    chat_id: str = ""
    chat_name: str = ""
    chat_type: str = ""
    user_id: str = ""
    user_name: str = ""


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
        self._connected = False

    def build_source(self, **kwargs):
        return MessageSource(**kwargs)

    async def handle_message(self, event: MessageEvent):
        pass

    def _mark_connected(self):
        self._connected = True

    def _mark_disconnected(self):
        self._connected = False
