"""Multi-account config resolver for Zulip Hermes integration.

Supports backward-compatible single-account config and multi-account
configs. Future: full multi-account support with isolated event queues.
"""

from __future__ import annotations

import os
from typing import Any
from dataclasses import dataclass


@dataclass
class ZulipAccount:
    """Represents a single Zulip account configuration."""

    name: str
    email: str
    api_key: str
    site: str
    streams: list[str] | None = None
    dm_policy: str = "open"
    allow_from: list[str] | None = None


class AccountResolver:
    """Resolve account configuration from env vars or Hermes config."""

    def __init__(self, extra: dict[str, Any] | None = None):
        self.extra = extra or {}

    def resolve(self) -> list[ZulipAccount]:
        """Return list of configured accounts.

        Backward-compatible: if no 'accounts' section, returns single account
        from env vars or top-level config.
        """
        accounts_raw = self.extra.get("accounts")
        if isinstance(accounts_raw, dict):
            return self._parse_multi(accounts_raw)
        return [self._single_account()]

    def _single_account(self) -> ZulipAccount:
        """Build single account from env vars or top-level config."""
        return ZulipAccount(
            name="default",
            email=os.getenv("ZULIP_EMAIL") or self.extra.get("email", ""),
            api_key=os.getenv("ZULIP_API_KEY") or self.extra.get("api_key", ""),
            site=os.getenv("ZULIP_SITE") or self.extra.get("site", ""),
            streams=self._parse_streams(self.extra.get("streams")),
            dm_policy=os.getenv("ZULIP_DM_POLICY", self.extra.get("dm_policy", "open")),
            allow_from=self._parse_allowlist(self.extra.get("allow_from")),
        )

    def _parse_multi(self, accounts_raw: dict) -> list[ZulipAccount]:
        """Parse multi-account config."""
        accounts = []
        for name, cfg in accounts_raw.items():
            if not isinstance(cfg, dict):
                continue
            account = ZulipAccount(
                name=name,
                email=cfg.get("email", ""),
                api_key=cfg.get("api_key", ""),
                site=cfg.get("site", ""),
                streams=self._parse_streams(cfg.get("streams")),
                dm_policy=cfg.get("dm_policy", "open"),
                allow_from=self._parse_allowlist(cfg.get("allow_from")),
            )
            accounts.append(account)
        return accounts if accounts else [self._single_account()]

    @staticmethod
    def _parse_streams(raw: Any) -> list[str] | None:
        if isinstance(raw, list):
            return [str(s) for s in raw]
        if isinstance(raw, str):
            return [s.strip() for s in raw.split(",") if s.strip()]
        return None

    @staticmethod
    def _parse_allowlist(raw: Any) -> list[str] | None:
        if isinstance(raw, list):
            return [str(s).strip().lower() for s in raw]
        if isinstance(raw, str):
            return [s.strip().lower() for s in raw.split(",") if s.strip()]
        return None
