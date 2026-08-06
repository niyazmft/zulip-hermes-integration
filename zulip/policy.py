"""DM policy engine for Zulip Hermes integration.

Controls who can send DMs to the bot and manages pairing codes
for secure onboarding.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = __import__("logging").getLogger(__name__)

# Policy modes
POLICY_OPEN = "open"
POLICY_ALLOWLIST = "allowlist"
POLICY_PAIRING = "pairing"
POLICY_DISABLED = "disabled"

_VALID_POLICIES = frozenset({POLICY_OPEN, POLICY_ALLOWLIST, POLICY_PAIRING, POLICY_DISABLED})
_PAIRING_CODE_TTL_SECONDS = 86_400  # 24 hours


@dataclass
class PairingCode:
    code: str
    email: str
    created_at: float
    used: bool = False


class PolicyEngine:
    """Manages DM and group/stream policies with disk persistence."""

    def __init__(self, *, pairing_ttl: int = _PAIRING_CODE_TTL_SECONDS, data_dir: Optional[str] = None):
        # DM policy
        self.mode = self._resolve_dm_mode()
        self.allowlist = self._parse_allowlist()
        self._pairing_codes: dict[str, PairingCode] = {}  # code → PairingCode
        self._email_to_code: dict[str, str] = {}            # email → code
        self._pairing_ttl = pairing_ttl

        # Group (stream) policy
        self.group_mode = self._resolve_group_mode()
        self.group_allowlist = self._parse_group_allowlist()

        # Disk persistence for allowlist
        self._data_dir = Path(data_dir).expanduser() if data_dir else None
        self._load_from_disk()

    def _persistence_path(self) -> Optional[Path]:
        """Return path to allowlist persistence file."""
        if not self._data_dir:
            return None
        return self._data_dir / "zulip_allowlist.json"

    def _load_from_disk(self) -> None:
        """Load persisted allowlist from disk."""
        path = self._persistence_path()
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            disk_allowlist = set(data.get("allowlist", []))
            disk_group_allowlist = set(data.get("group_allowlist", []))
            # Merge with env-based allowlist (env takes precedence)
            self.allowlist = self._parse_allowlist() | disk_allowlist
            self.group_allowlist = self._parse_group_allowlist() | disk_group_allowlist
            logger.info(
                "policy allowlist loaded from disk [allowlist=%d group_allowlist=%d]",
                len(self.allowlist),
                len(self.group_allowlist),
            )
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_to_disk(self) -> None:
        """Persist allowlist to disk atomically."""
        path = self._persistence_path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "allowlist": sorted(self.allowlist),
                "group_allowlist": sorted(self.group_allowlist),
            }
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                suffix=".tmp",
                delete=False,
            ) as f:
                json.dump(data, f)
                temp_path = f.name
            os.replace(temp_path, path)
            # Restrict file permissions to owner-only (0600)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except OSError as e:
            logger.warning("policy allowlist save failed: %s", e)

    @staticmethod
    def _resolve_dm_mode() -> str:
        raw = os.getenv("ZULIP_DM_POLICY", "open").strip().lower()
        return raw if raw in _VALID_POLICIES else POLICY_OPEN

    @staticmethod
    def _resolve_group_mode() -> str:
        """Group policy defaults to 'open' for backward compatibility."""
        raw = os.getenv("ZULIP_GROUP_POLICY", "open").strip().lower()
        # Group policy does not support 'pairing'
        valid = frozenset({POLICY_OPEN, POLICY_ALLOWLIST, POLICY_DISABLED})
        return raw if raw in valid else POLICY_OPEN

    @staticmethod
    def _parse_allowlist() -> set[str]:
        raw = os.getenv("ZULIP_ALLOWED_USERS", "").strip()
        if not raw:
            return set()
        return {e.strip().lower() for e in raw.split(",") if e.strip()}

    @staticmethod
    def _parse_group_allowlist() -> set[str]:
        raw = os.getenv("ZULIP_GROUP_ALLOW_FROM", "").strip()
        if not raw:
            return set()
        return {e.strip().lower() for e in raw.split(",") if e.strip()}

    def can_dm(self, email: str) -> bool:
        """Return True if this email is allowed to DM the bot."""
        email = email.strip().lower()

        if self.mode == POLICY_OPEN:
            return True

        if self.mode == POLICY_DISABLED:
            return False

        if self.mode == POLICY_ALLOWLIST:
            return email in self.allowlist

        if self.mode == POLICY_PAIRING:
            # Paired emails are stored in allowlist dynamically
            if email in self.allowlist:
                return True
            # Check if they have a valid pairing code
            code = self._email_to_code.get(email)
            if code:
                pc = self._pairing_codes.get(code)
                if pc and not pc.used and (time.time() - pc.created_at) < self._pairing_ttl:
                    return False  # Has code but not yet approved
            return False

        return True  # Default fallback

    def can_group_message(self, email: str) -> bool:
        """Return True if this email is allowed to send stream messages.

        Group policy controls who can interact with the bot in public
        streams. It does not support 'pairing' mode.
        """
        email = email.strip().lower()

        if self.group_mode == POLICY_OPEN:
            return True

        if self.group_mode == POLICY_DISABLED:
            return False

        if self.group_mode == POLICY_ALLOWLIST:
            return email in self.group_allowlist

        return True  # Default fallback

    def check_dm(self, email: str) -> tuple[bool, Optional[str]]:
        """Check if DM is allowed. Returns (allowed, pairing_code_or_none)."""
        email = email.strip().lower()

        if self.mode == POLICY_OPEN:
            return True, None

        if self.mode == POLICY_DISABLED:
            return False, None

        if self.mode == POLICY_ALLOWLIST:
            return email in self.allowlist, None

        if self.mode == POLICY_PAIRING:
            if email in self.allowlist:
                return True, None
            # Generate pairing code if they don't have one
            code = self._email_to_code.get(email)
            if code:
                pc = self._pairing_codes.get(code)
                if pc and (time.time() - pc.created_at) < self._pairing_ttl:
                    return False, code
            # Create new pairing code
            new_code = self._generate_code()
            self._pairing_codes[new_code] = PairingCode(
                code=new_code, email=email, created_at=time.time()
            )
            self._email_to_code[email] = new_code
            return False, new_code

        return True, None

    def approve_email(self, email: str) -> bool:
        """Approve an email (admin action). Returns True if newly approved."""
        email = email.strip().lower()
        if email not in self.allowlist:
            self.allowlist.add(email)
            # Mark any pairing code as used
            code = self._email_to_code.get(email)
            if code:
                pc = self._pairing_codes.get(code)
                if pc:
                    pc.used = True
            self._save_to_disk()
            return True
        return False

    def revoke_email(self, email: str) -> bool:
        """Revoke an email from the allowlist."""
        email = email.strip().lower()
        if email in self.allowlist:
            self.allowlist.discard(email)
            self._save_to_disk()
            return True
        return False

    @staticmethod
    def _generate_code(length: int = 6) -> str:
        """Generate a random alphanumeric pairing code."""
        alphabet = string.ascii_uppercase + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def get_status(self, email: str) -> str:
        """Get human-readable status for an email."""
        email = email.strip().lower()
        if self.mode == POLICY_OPEN:
            return "open"
        if self.mode == POLICY_DISABLED:
            return "disabled"
        if email in self.allowlist:
            return "approved"
        code = self._email_to_code.get(email)
        if code:
            pc = self._pairing_codes.get(code)
            if pc and (time.time() - pc.created_at) < self._pairing_ttl:
                return f"pending ({code})"
        return "unauthorized"
