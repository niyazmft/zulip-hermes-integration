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

from . import runtime_scope

logger = __import__("logging").getLogger(__name__)

# Policy modes
POLICY_OPEN = "open"
POLICY_ALLOWLIST = "allowlist"
POLICY_PAIRING = "pairing"
POLICY_DISABLED = "disabled"

_VALID_POLICIES = frozenset({POLICY_OPEN, POLICY_ALLOWLIST, POLICY_PAIRING, POLICY_DISABLED})
_PAIRING_CODE_TTL_SECONDS = 86_400  # 24 hours

#: The bot owner's DM address, stated explicitly (#214).  Normally unnecessary:
#: under ``ZULIP_PROFILE=recommended`` the address is discovered from Zulip.  It
#: exists for the cases discovery cannot cover -- an org-owned bot with no
#: personal owner, an older SDK, an API error -- and is what ``zulip config``
#: writes when auto-detection is wrong or impossible.
OWNER_EMAIL_ENV = "ZULIP_OWNER_EMAIL"


def owner_email_override() -> str:
    """The explicit ``ZULIP_OWNER_EMAIL``, normalised, or ``''`` when unset.

    Honoured whatever the profile: a variable nobody had set before cannot be a
    silent change on upgrade, and naming an address is an explicit instruction.
    """
    return (runtime_scope.effective_value(OWNER_EMAIL_ENV, "") or "").strip().lower()


def bot_owner_address(profile: object) -> str:
    """The bot owner, as an id or an address, from a ``GET /users/me`` payload.

    Zulip reports ``bot_owner_id``; some builds inline the owner's address too.
    An address is preferred when present because it saves a second round-trip,
    but the id is the documented field, and ``user_lookup_call`` already accepts
    either form.
    """
    if not isinstance(profile, dict):
        return ""
    for key in ("bot_owner", "bot_owner_email"):
        value = profile.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip()
    owner_id = profile.get("bot_owner_id")
    if owner_id is None or owner_id == "":
        return ""
    return str(owner_id).strip()


def owner_email_from_user(result: object) -> str:
    """The address out of a single-user lookup result, or ``''``.

    The SDK's ``get_user_by_id`` and the raw ``users/{id}`` endpoint both answer
    ``{"result": "success", "user": {...}}``; a members-style payload nests the
    same object under ``members``.  Anything else yields ``''`` so the caller can
    fall back to the loud warning rather than seeding a bogus address.
    """
    if not isinstance(result, dict):
        return ""
    if result.get("result") not in (None, "success"):
        return ""
    candidates: list[object] = [result.get("user")]
    members = result.get("members")
    if isinstance(members, list):
        candidates.extend(members)
    candidates.append(result)
    for candidate in candidates:
        if isinstance(candidate, list):
            candidate = candidate[0] if candidate else None
        if isinstance(candidate, dict):
            email = candidate.get("email")
            if isinstance(email, str) and "@" in email:
                return email.strip().lower()
    return ""


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
        # (mtime, size, inode) of the data file as last read/written —
        # a change signal robust to coarse timestamp granularity (two
        # rapid saves can share an mtime tick; os.replace still swaps
        # the inode).
        self._loaded_stamp: Optional[tuple[float, int, int]] = None
        self._load_from_disk()

        # The DM address the operator named explicitly, if any (#214). Seeded
        # after the disk merge so a reload cannot drop it, and before the
        # emptiness check below so the seed is visible in the startup line.
        self.owner_email = owner_email_override()
        if self.owner_email:
            self.seed_dm_allowlist(self.owner_email)

        # Make the effective authorization posture visible at startup. Two
        # silent misconfigurations are otherwise indistinguishable from a
        # working bot: (a) `open` group policy, where the stream allowlist is
        # ignored entirely, so any realm member can trigger the bot; and
        # (b) `allowlist` mode with an EMPTY allowlist, which blocks everyone
        # -- including the operator. Both are only discoverable by reading the
        # resolver, so they are logged here instead.
        logger.info(
            "zulip policy resolved [dm_mode=%s dm_allowlist=%d "
            "group_mode=%s stream_allowlist=%d]",
            self.mode,
            len(self.allowlist),
            self.group_mode,
            len(self.group_allowlist),
        )
        if self.mode == POLICY_ALLOWLIST and not self.allowlist:
            logger.warning(
                "zulip DM policy is 'allowlist' but ZULIP_ALLOWED_USERS is "
                "empty -- NO ONE can DM the bot"
            )
        if self.group_mode == POLICY_ALLOWLIST and not self.group_allowlist:
            logger.warning(
                "zulip group policy is 'allowlist' but "
                "ZULIP_GROUP_ALLOW_FROM is empty -- NO ONE can trigger the "
                "bot in streams"
            )

    def _persistence_path(self) -> Optional[Path]:
        """Return path to allowlist persistence file."""
        if not self._data_dir:
            return None
        return self._data_dir / "zulip_allowlist.json"

    def _load_from_disk(self) -> None:
        """Load persisted allowlist and pending pairing requests from disk."""
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
            self._restore_pairing(data)
            try:
                st = path.stat()
                self._loaded_stamp = (st.st_mtime, st.st_size, st.st_ino)
            except OSError:
                pass
            logger.info(
                "policy allowlist loaded from disk [allowlist=%d group_allowlist=%d pending=%d]",
                len(self.allowlist),
                len(self.group_allowlist),
                len(self._pairing_codes),
            )
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _restore_pairing(self, data: dict) -> None:
        """Rebuild pending pairing requests from a persisted snapshot.

        Codes are persisted for two reasons (issue #198): a gateway restart
        must not invalidate a code the user has already been given, and
        ``python -m zulip.pairing`` runs in a separate process, so the data
        file is the only way it can see a pending request at all.
        """
        self._pairing_codes.clear()
        self._email_to_code.clear()
        for entry in data.get("pairing", []):
            try:
                pc = PairingCode(
                    code=str(entry["code"]),
                    email=str(entry["email"]).strip().lower(),
                    created_at=float(entry["created_at"]),
                    used=bool(entry.get("used", False)),
                )
            except (KeyError, TypeError, ValueError):
                # A hand-edited or half-written entry must not poison startup.
                continue
            self._pairing_codes[pc.code] = pc
            self._email_to_code[pc.email] = pc.code

    def refresh_if_changed(self) -> None:
        """Reload when another process rewrote the data file.

        Approval happens out of process (``python -m zulip.pairing``), so the
        running gateway has to notice the new allowlist. Cheap: one stat().

        mtime alone is not a reliable change signal: file writes are stamped
        with a coarse clock, so two rapid saves can share an mtime tick.
        Size and inode ride along in the same stat() — every save goes
        through ``os.replace``, so the inode changes even when mtime and
        size collide. A revocation also lands here, because _load_from_disk
        rebuilds the set from env + disk rather than merging into what is
        already held.
        """
        path = self._persistence_path()
        if not path:
            return
        try:
            st = path.stat()
        except OSError:
            return
        if (st.st_mtime, st.st_size, st.st_ino) != self._loaded_stamp:
            self._load_from_disk()

    def _save_to_disk(self) -> None:
        """Persist allowlist and pending pairing requests to disk atomically."""
        path = self._persistence_path()
        if not path:
            return
        self._prune_pairing()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "allowlist": sorted(self.allowlist),
                "group_allowlist": sorted(self.group_allowlist),
                "pairing": [
                    {
                        "code": pc.code,
                        "email": pc.email,
                        "created_at": pc.created_at,
                    }
                    for pc in self._pending_codes()
                ],
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
            try:
                st = path.stat()
                self._loaded_stamp = (st.st_mtime, st.st_size, st.st_ino)
            except OSError:
                pass
            # Restrict file permissions to owner-only (0600)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except OSError as e:
            logger.warning("policy allowlist save failed: %s", e)

    @staticmethod
    def _resolve_dm_mode() -> str:
        # Through the preset gate (#213): ZULIP_PROFILE=recommended sets the DM
        # policy to ``allowlist`` so a fresh install's DMs belong to the owner
        # without the operator editing anything.
        raw = (runtime_scope.effective_value("ZULIP_DM_POLICY", "open") or "open").strip().lower()
        return raw if raw in _VALID_POLICIES else POLICY_OPEN

    @staticmethod
    def _resolve_group_mode() -> str:
        """Group policy defaults to 'open' for backward compatibility."""
        # The recommended profile pins ``open`` explicitly rather than leaving it
        # to the built-in default: the preset states the whole posture, so a later
        # default change cannot silently move what the profile means. Explicit env
        # still wins.
        raw = (runtime_scope.effective_value("ZULIP_GROUP_POLICY", "open") or "open").strip().lower()
        # Group policy does not support 'pairing'
        valid = frozenset({POLICY_OPEN, POLICY_ALLOWLIST, POLICY_DISABLED})
        return raw if raw in valid else POLICY_OPEN

    @staticmethod
    def _parse_allowlist() -> set[str]:
        raw = runtime_scope.get_setting("ZULIP_ALLOWED_USERS", "").strip()
        if not raw:
            return set()
        return {e.strip().lower() for e in raw.split(",") if e.strip()}

    @staticmethod
    def _parse_group_allowlist() -> set[str]:
        raw = runtime_scope.get_setting("ZULIP_GROUP_ALLOW_FROM", "").strip()
        if not raw:
            return set()
        return {e.strip().lower() for e in raw.split(",") if e.strip()}

    def seed_dm_allowlist(self, email: str) -> bool:
        """Allow one address to DM, persist it, and say whether it was new (#214).

        The allowlist is the one place a Zulip user becomes authorised to DM, so
        this is deliberately the only way the owner is added: the caller decides
        *whose* address is trustworthy (an explicit setting, or a lookup that
        answered), and this method only records it.  A value that is not
        address-shaped is refused rather than stored, because an allowlist entry
        that can never match a sender is worse than an absent one -- it looks
        like it works.
        """
        address = (email or "").strip().lower()
        if "@" not in address or address.startswith("@") or address.endswith("@"):
            return False
        if address in self.allowlist:
            return False
        self.allowlist.add(address)
        self._save_to_disk()
        return True

    def needs_bot_owner_lookup(self) -> bool:
        """Whether the DM allowlist still needs the owner discovered from Zulip.

        Scoped to the recommended profile on purpose.  Auto-detection is a
        preset behaviour; doing it for every ``allowlist`` install would widen
        an existing user's allowlist on upgrade, which is a silent authorization
        change and exactly what epic #211's migration contract forbids.  Nothing
        to do either when the policy does not consult the allowlist, or when the
        operator has already named the owner.
        """
        return (
            self.mode == POLICY_ALLOWLIST
            and not self.owner_email
            and runtime_scope.active_profile() == runtime_scope.PROFILE_RECOMMENDED
        )

    def report_unresolved_bot_owner(self) -> None:
        """Say *why* the DM allowlist is empty, and what to do about it (#214).

        The startup warning for an empty allowlist states the symptom. Under the
        recommended profile the operator never chose to run an allowlist at all,
        so the symptom alone is unexplainable: this names the cause and the one
        setting that fixes it. It deliberately points at no other command -- a
        fix that does not exist yet is not a fix.
        """
        logger.warning(
            "zulip: DM policy is 'allowlist' under ZULIP_PROFILE=%s but the bot "
            "owner could not be resolved -- set %s to the owner's Zulip email "
            "or no one can DM the bot",
            runtime_scope.PROFILE_RECOMMENDED,
            OWNER_EMAIL_ENV,
        )

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
            # Persist immediately: the request has to outlive this process for
            # an admin to act on it at all (issue #198).
            self._save_to_disk()
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

    def _pending_codes(self) -> list[PairingCode]:
        """Pairing requests that are neither used nor expired, oldest first."""
        now = time.time()
        return sorted(
            (
                pc
                for pc in self._pairing_codes.values()
                if not pc.used and (now - pc.created_at) < self._pairing_ttl
            ),
            key=lambda pc: pc.created_at,
        )

    def _prune_pairing(self) -> None:
        """Drop used and expired codes, so neither memory nor the file grows.

        Also the replay guard: an approved code is removed here, so a second
        attempt with the same code finds nothing to approve.
        """
        keep = {pc.code for pc in self._pending_codes()}
        for code in list(self._pairing_codes):
            if code not in keep:
                self._pairing_codes.pop(code, None)
        for email, code in list(self._email_to_code.items()):
            if code not in keep:
                self._email_to_code.pop(email, None)

    def list_pending(self) -> list[tuple[str, str, float]]:
        """Pending requests as ``(code, email, age_seconds)``, oldest first.

        This is what makes the queue discoverable without reading the data
        file or restarting the gateway (issue #198).
        """
        now = time.time()
        return [(pc.code, pc.email, now - pc.created_at) for pc in self._pending_codes()]

    def approve_code(self, code: str) -> Optional[str]:
        """Approve the request behind a pairing *code*.

        Accepts the code with or without its ``PAIR-`` prefix, in any case.
        Returns the approved email, or None when the code is unknown, already
        used, or expired.
        """
        normalised = code.strip().upper()
        for prefix in ("PAIR-", "PAIR"):
            if normalised.startswith(prefix):
                normalised = normalised[len(prefix):]
                break
        normalised = normalised.lstrip("- ")
        pc = self._pairing_codes.get(normalised)
        if not pc or pc.used or (time.time() - pc.created_at) >= self._pairing_ttl:
            return None
        self.approve_email(pc.email)
        return pc.email

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
