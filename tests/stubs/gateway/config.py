"""Stub for gateway.config — provides minimal types for adapter imports."""

from dataclasses import dataclass
from typing import Optional, Any


@dataclass
class PlatformConfig:
    extra: Optional[dict] = None


class Platform:
    def __init__(self, name: str):
        self.name = name
