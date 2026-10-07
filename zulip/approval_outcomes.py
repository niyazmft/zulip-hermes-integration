"""Exec-approval outcomes: what silence means, who decided, and the audit (#222).

The gateway owns the approval *decision*. It posts the prompt, resolves it from
the buttons or the plain-text ``/approve`` / ``/deny`` commands, and refuses an
unanswered request once ``approvals.timeout`` elapses. A plugin cannot
pre-answer, veto or extend that wait — the host's approval observers explicitly
cannot, and this module does not try to.

What a plugin *can* own is the record and the saying-so:

* **the audit entry** for every resolved approval — the choice, the decider and
  the request id — so "who let this run, and did anyone?" is a lookup rather
  than a guess;
* **one refusal line in the prompt's own route** when the policy is ``deny`` and
  the host cannot post one itself. Hosts below 0.21.4 have no timeout notice at
  all (``gateway.run_turn_runner_approval_settle`` arrived with 0.21.4), so an
  unanswered approval there was refused *silently*; where the host does post
  one, the plugin deliberately stays quiet rather than repeating it.

The gateway fails closed on timeout on every host this plugin supports
(0.18.2 through the current release), so ``ZULIP_APPROVAL_ON_TIMEOUT`` cannot
and does not make silence run a command. It selects what this install *promises*
about silence — and therefore what the bot says and records — with ``allow``
keeping today's behaviour byte-for-byte (the migration contract), and ``deny``
(the recommended profile's value) turning the promise into a visible, audited
refusal. ``docs/PARITY.md`` and the README carry the same statement.

Two things the host does not hand a plugin, recorded here rather than guessed:

* **No request id.** The hook payload carries the command, the pattern and the
  session key — not the host's internal request id — so the plugin mints its own
  at ``pre_approval_request`` and carries it into the audit. It identifies the
  prompt, which is what the audit needs.
* **No decider on the gateway surface.** ``decided_by`` is set only by the
  smart/guardian path (``aux_llm``). A human's identity comes from the message
  the click produced: the plugin sees ``/approve`` / ``/deny`` as ordinary
  inbound traffic *before* the gateway resolves it, so the sender is recorded
  against the session key (and the route, when the host exposes no session key
  for an event). A miss degrades to ``unknown`` — never to a guess.

The hooks fire on three surfaces. Only ``gateway`` concerns a Zulip room, and
the ``smart`` surface fires a pre-hook for verdicts that never produce a
post-hook (``escalate``), so pairing them per session key requires filtering on
the surface; a ledger keyed on ``pre`` alone would desynchronise.

Layering: L2 — ``logger`` (L0) and ``settings`` (L1), which resolves the policy
key through the preset gate. Pure: no I/O and no awaits. The adapter performs the
audit write and the notice on its own loop, and the notice text is decided here
so the wording lives with the policy that selected it.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from .logger import mask_pii
from .settings import (
    APPROVAL_AUTHORITY_ANYONE,
    APPROVAL_AUTHORITY_OWNER,
    APPROVAL_ON_TIMEOUT_ALLOW,
    APPROVAL_ON_TIMEOUT_DENY,
    resolve_approval_authority,
    resolve_approval_on_timeout,
)

logger = logging.getLogger(__name__)

#: The only approval surface that concerns a Zulip room.
GATEWAY_SURFACE = "gateway"

#: Choices that mean "nobody approved". ``timeout`` is the host's own token for
#: an elapsed window; ``cancelled`` is a withdrawn prompt (the turn ended or was
#: interrupted under it); ``notify_failed`` means the prompt never reached the
#: room at all. All three are refusals, and none of them is a person.
TIMEOUT_CHOICE = "timeout"
CANCELLED_CHOICE = "cancelled"
NOTIFY_FAILED_CHOICE = "notify_failed"
NO_ANSWER_CHOICES = frozenset({TIMEOUT_CHOICE, CANCELLED_CHOICE, NOTIFY_FAILED_CHOICE})

#: Decider tokens. A human decision is recorded as the (masked) sender address;
#: these stand in when no human decided.
DECIDER_TIMEOUT = "timeout"
DECIDER_CANCELLED = "cancelled"
DECIDER_UNDELIVERED = "undelivered"
DECIDER_POLICY = "policy"
DECIDER_UNKNOWN = "unknown"

#: How long a recorded decider stays eligible. The host's own approval window is
#: ``approvals.timeout`` (300s by default), so a record older than this cannot
#: belong to a prompt that is still waiting — and a stale decider attributed to
#: a fresh decision would be exactly the guess this module refuses to make.
DECIDER_TTL_SECONDS = 600.0

#: Bound on pending requests kept per session key. A pre-hook whose post-hook
#: never fires (an escalated smart verdict, a killed process) would otherwise
#: accumulate for the process's lifetime.
MAX_PENDING_PER_SESSION = 8

#: The one line posted when ``ZULIP_APPROVAL_ON_TIMEOUT=deny`` and nobody
#: answered. Deliberately short: the prompt above it already names the command,
#: and this message exists to say that the silence was a refusal, not to repeat
#: the request.
TIMEOUT_REFUSAL_TEXT = (
    "⌛ Refused by timeout — nobody approved, so the command did not run. "
    "Ask me to try again if you still want it."
)

#: Why a decision was refused (issue #228). Machine-readable so the audit entry
#: says which rule fired, and so the wording has one source.
REJECT_NOT_OWNER = "not_owner"
REJECT_NO_OWNER = "no_owner"

#: One short line for each refusal. Posted in the prompt's own route, so the
#: person who clicked learns why nothing happened and, for the second case, what
#: the operator must set. Deliberately states that the request is still open —
#: the owner can still answer it, and the window has not changed.
_REJECTION_TEXT: dict[str, str] = {
    REJECT_NOT_OWNER: (
        "🚫 Only the bot owner may decide this exec approval, so that decision "
        "was not counted — the request is still waiting for them."
    ),
    REJECT_NO_OWNER: (
        "🚫 This install limits exec approvals to the bot owner, but no owner "
        "could be resolved, so no decision can be accepted. Set "
        "`ZULIP_OWNER_EMAIL` to the owner's Zulip address (or "
        "`ZULIP_APPROVAL_AUTHORITY=anyone`) and ask again."
    ),
}


# Host capability: does the gateway post its own timed-out notice? It arrived
# with host 0.21.4 (``gateway/run_turn_runner_approval_settle.py``) and is
# absent on the declared floor (0.18.2) and at 0.21.3, where an unanswered
# approval is refused silently. Guarded like every other host symbol (#132): a
# host without it must lose this feature, not fail to load the plugin.
try:
    from gateway.run_turn_runner_approval_settle import (
        register_timeout_notice as _host_register_timeout_notice,
    )
except ImportError:  # pragma: no cover - hosts < 0.21.4
    _host_register_timeout_notice = None  # type: ignore[assignment]

# The gateway publishes the current turn's routing context per task. This is the
# same channel ``zulip.tracing`` uses to attribute a hook payload to a
# conversation, and the only way to locate a prompt's route on a host that never
# handed the plugin the prompt. Guarded: a host without it simply cannot locate
# the route, and the outcome is audited without one.
try:
    from gateway.session_context import get_session_env as _host_get_session_env
except ImportError:  # pragma: no cover - gateways without session context
    _host_get_session_env = None  # type: ignore[assignment]


def host_notices_timeouts() -> bool:
    """Whether the gateway posts its own notice for an elapsed approval window.

    Read through the module attribute so a test can pin either side of the
    capability boundary without installing a host.
    """
    return _host_register_timeout_notice is not None


def route_from_session_env() -> Optional[tuple[str, str]]:
    """``(chat_id, topic)`` for the turn this hook fired inside, or None.

    The host binds the session context to the task the approval wait runs on, so
    a hook callback reads the route its turn belongs to even when the plugin
    never rendered the prompt (the plain-text path on hosts below 0.21.3).
    """
    if _host_get_session_env is None:
        return None
    try:
        chat_id = str(_host_get_session_env("HERMES_SESSION_CHAT_ID") or "").strip()
        thread = str(_host_get_session_env("HERMES_SESSION_THREAD_ID") or "").strip()
    except Exception:
        return None
    if not chat_id:
        return None
    return chat_id, thread


def route_key(chat_id: str, topic: str = "") -> str:
    """Join key for a route, matching ``zulip.routing``'s alias shape."""
    return f"route:{chat_id}\x00{topic or ''}"


def session_key_alias(session_key: str) -> str:
    """Join key for a host session key, matching ``zulip.routing``'s shape."""
    return f"key:{session_key or ''}"


def audit_decider(decider: str) -> str:
    """The decider as the audit log should carry it.

    A human decider is an address and is masked; a token is passed through
    untouched, because masking ``timeout`` into ``ti***ut`` would destroy the
    one machine-readable value in the entry.
    """
    text = (decider or "").strip()
    if not text:
        return DECIDER_UNKNOWN
    if "@" in text:
        return mask_pii(text)
    return text


@dataclass
class ApprovalRequest:
    """One approval the gateway is about to deliver, with the plugin's own id."""

    request_id: str
    session_key: str
    command: str = ""
    pattern_key: str = ""
    created_at: float = 0.0

    def expired(self, now: float, ttl: float = DECIDER_TTL_SECONDS) -> bool:
        return (now - self.created_at) > ttl


@dataclass
class ApprovalOutcome:
    """One resolved approval, as the audit and the notice need it."""

    choice: str
    decider: str
    request_id: str
    session_key: str
    command: str = ""
    pattern_key: str = ""
    chat_id: Optional[str] = None
    topic: Optional[str] = None
    cancelled: str = ""
    owned: bool = False
    notice: Optional[str] = None

    @property
    def refused(self) -> bool:
        """True when the request did not run."""
        return self.choice in NO_ANSWER_CHOICES or self.choice == "deny"

    @property
    def route(self) -> Optional[tuple[str, str]]:
        if not self.chat_id:
            return None
        return self.chat_id, self.topic or ""


class ApprovalLedger:
    """Pending approvals and their deciders, for one adapter (thread-safe).

    Two threads touch this: the plugin's event loop records a decision when the
    click arrives, and the agent thread resolves it when the gateway reports the
    outcome. Every mutation and read is under one lock.

    Keyed by the host's session key, which both the approval hooks and
    ``zulip.routing.session_key_for_event`` produce, so the join never guesses.
    Routes are recorded too, as the fallback for hosts where the click's event
    yields no session key.
    """

    def __init__(self, *, ttl: float = DECIDER_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, list[ApprovalRequest]] = {}
        self._routes: dict[str, tuple[str, str]] = {}
        self._deciders: dict[str, tuple[str, float]] = {}
        self._ttl = ttl

    # --- writers ---------------------------------------------------------

    def note_request(
        self,
        *,
        session_key: str = "",
        surface: str = "",
        command: str = "",
        pattern_key: str = "",
        now: Optional[float] = None,
    ) -> Optional[ApprovalRequest]:
        """Mint a request id for an approval the gateway is about to deliver.

        Only the ``gateway`` surface is tracked: the ``smart`` surface fires a
        pre-hook for verdicts that never post, and pairing on its behalf would
        shift every later outcome onto the wrong request.
        """
        if surface != GATEWAY_SURFACE or not session_key:
            return None
        now = time.monotonic() if now is None else now
        request = ApprovalRequest(
            request_id=uuid.uuid4().hex,
            session_key=session_key,
            command=command or "",
            pattern_key=pattern_key or "",
            created_at=now,
        )
        with self._lock:
            pending = [
                item
                for item in self._pending.get(session_key, [])
                if not item.expired(now, self._ttl)
            ]
            pending.append(request)
            self._pending[session_key] = pending[-MAX_PENDING_PER_SESSION:]
        return request

    def note_route(self, *, session_key: str = "", chat_id: str = "", topic: str = "") -> None:
        """Remember where this adapter rendered the prompt for ``session_key``.

        This is also the ownership record: the adapter that rendered a prompt is
        the one that may speak for it, which is what keeps a multiplexed
        gateway from reporting one profile's approval into another's room.
        """
        if not session_key or not chat_id:
            return
        with self._lock:
            self._routes[session_key] = (str(chat_id), topic or "")

    def note_decider(
        self,
        *,
        session_key: str = "",
        chat_id: str = "",
        topic: str = "",
        email: str = "",
        now: Optional[float] = None,
    ) -> bool:
        """Record who produced a decision for a pending approval.

        Recorded under the session key *and* the route when both are known, so a
        host that exposes one and not the other still joins. Returns whether
        anything was recorded.
        """
        address = (email or "").strip()
        if not address:
            return False
        now = time.monotonic() if now is None else now
        aliases = [session_key_alias(session_key)] if session_key else []
        if chat_id:
            aliases.append(route_key(str(chat_id), topic))
        if not aliases:
            return False
        with self._lock:
            for alias in aliases:
                self._deciders[alias] = (address, now)
        return True

    # --- readers ---------------------------------------------------------

    def owns(self, session_key: str = "") -> bool:
        """Whether this adapter rendered the prompt for ``session_key``."""
        if not session_key:
            return False
        with self._lock:
            return session_key in self._routes

    def resolve(
        self,
        *,
        session_key: str = "",
        choice: str = "",
        decided_by: str = "",
        cancelled: str = "",
        route: Optional[tuple[str, str]] = None,
        now: Optional[float] = None,
    ) -> Optional[ApprovalOutcome]:
        """Pop the oldest pending request for ``session_key`` and label its outcome.

        Returns None when this adapter has no request for that session key — the
        process-wide hook fires for every adapter, and the resolve is what makes
        exactly one of them act.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            pending = self._pending.get(session_key, [])
            request = pending.pop(0) if pending else None
            if not pending:
                self._pending.pop(session_key, None)
            owned_route = self._routes.pop(session_key, None)
            decider = self._take_decider(session_key, owned_route or route, now)
        if request is None:
            return None
        chat_id, topic = owned_route or route or (None, None)
        return ApprovalOutcome(
            choice=str(choice or ""),
            decider=_decider_for(choice, decided_by, decider),
            request_id=request.request_id,
            session_key=session_key,
            command=request.command,
            pattern_key=request.pattern_key,
            chat_id=chat_id,
            topic=topic,
            cancelled=cancelled or "",
            owned=owned_route is not None,
        )

    def _take_decider(
        self,
        session_key: str,
        route: Optional[tuple[str, str]],
        now: float,
    ) -> Optional[str]:
        """Consume the freshest recorded decider for this session or route."""
        aliases: list[str] = []
        if session_key:
            aliases.append(session_key_alias(session_key))
        if route and route[0]:
            aliases.append(route_key(route[0], route[1]))
        for alias in aliases:
            entry = self._deciders.pop(alias, None)
            if entry is None:
                continue
            address, recorded_at = entry
            if (now - recorded_at) <= self._ttl:
                return address
        return None

    def pending_count(self, session_key: str = "") -> int:
        with self._lock:
            if session_key:
                return len(self._pending.get(session_key, []))
            return sum(len(items) for items in self._pending.values())

    def pending_request_id(
        self, session_key: str = "", *, chat_id: str = "", topic: str = ""
    ) -> str:
        """The oldest pending request id for a prompt, without consuming it.

        Read-only on purpose: a refused decision (#228) must not consume the
        request, because a refused decision is not a decision — the prompt stays
        open and a later answer (or the timeout) still has to be audited against
        the same request.

        Joined by session key, then by route: a host that exposes no session key
        for an event (``zulip.routing.session_key_for_event`` returns None) still
        lets the refusal name the prompt it refused to decide.
        """
        with self._lock:
            if session_key and self._pending.get(session_key):
                return self._pending[session_key][0].request_id
            if chat_id:
                wanted = (str(chat_id), topic or "")
                for key, route in self._routes.items():
                    if route == wanted and self._pending.get(key):
                        return self._pending[key][0].request_id
        return ""


def _decider_for(choice: str, decided_by: str, recorded: Optional[str]) -> str:
    """Label who decided, preferring the host's own answer over the plugin's.

    ``decided_by`` (the guardian LLM) outranks a recorded sender, because a
    policy verdict belongs to no person even when one is standing by.
    """
    if choice == TIMEOUT_CHOICE:
        return DECIDER_TIMEOUT
    if choice == CANCELLED_CHOICE:
        return DECIDER_CANCELLED
    if choice == NOTIFY_FAILED_CHOICE:
        return DECIDER_UNDELIVERED
    if decided_by:
        return DECIDER_POLICY
    return recorded or DECIDER_UNKNOWN


def notice_for(
    outcome: ApprovalOutcome,
    *,
    policy: str,
    host_notices: bool,
    multiplexed: bool = False,
) -> Optional[str]:
    """The refusal line to post for this outcome, or None to stay quiet.

    Three conditions, each with a reason:

    * a **timeout** only. A person's ``/deny`` is already confirmed in-route by
      the gateway's own command reply on every supported host, and duplicating
      it would put two lines about one decision in the topic.
    * the policy is ``deny``. With the key unset (``allow``) the install gets
      today's behaviour exactly, which is what makes the key migration-safe.
    * the **host cannot post its own**. From 0.21.4 on, the gateway edits the
      card or posts its own notice; a second line from the plugin is noise.
    """
    if outcome.choice != TIMEOUT_CHOICE:
        return None
    if policy != APPROVAL_ON_TIMEOUT_DENY:
        return None
    if host_notices:
        return None
    if multiplexed and not outcome.owned:
        # No route we can trust: several profiles are live and this adapter
        # never rendered the prompt, so posting could speak into another
        # profile's realm. The audit entry still lands.
        return None
    return TIMEOUT_REFUSAL_TEXT


def current_policy() -> str:
    """The configured unanswered-approval policy, read at call time."""
    return resolve_approval_on_timeout()


def current_authority() -> str:
    """Who may decide an exec approval, read at call time (#228)."""
    return resolve_approval_authority()


def is_owner_decision(sender_email: str, owner_address: str) -> bool:
    """Whether this decision came from the bot owner.

    Address comparison, case-insensitive, against the one owner identity #214
    resolves — the same value the DM allowlist is seeded from, so an install
    cannot end up with two different notions of "the owner". A missing owner on
    either side answers False, which is what makes an unresolvable owner fail
    closed rather than open.
    """
    sender = (sender_email or "").strip().lower()
    owner = (owner_address or "").strip().lower()
    return bool(sender) and bool(owner) and sender == owner


def rejection_reason(sender_email: str, owner_address: str) -> Optional[str]:
    """Why this decision must be refused, or None when it may proceed (#228)."""
    if current_authority() != APPROVAL_AUTHORITY_OWNER:
        return None
    if is_owner_decision(sender_email, owner_address):
        return None
    return REJECT_NOT_OWNER if (owner_address or "").strip() else REJECT_NO_OWNER


def authority_rejection_notice(reason: str) -> str:
    """The one line posted for a refused decision (#228)."""
    return _REJECTION_TEXT.get(reason, _REJECTION_TEXT[REJECT_NOT_OWNER])


def unresolved_authority_warning() -> str:
    """The actionable startup warning for an owner-restricted install with no owner.

    Mirrors #214's DM warning: name the cause and the single setting that fixes
    it, and point at nothing else — a fix that does not exist is not a fix.
    """
    return (
        "zulip: ZULIP_APPROVAL_AUTHORITY=owner but the bot owner could not be "
        "resolved -- no exec approval can be decided until ZULIP_OWNER_EMAIL is "
        "set to the owner's Zulip email (or the authority is set back to 'anyone')"
    )


__all__ = [
    "APPROVAL_AUTHORITY_ANYONE",
    "APPROVAL_AUTHORITY_OWNER",
    "APPROVAL_ON_TIMEOUT_ALLOW",
    "ApprovalLedger",
    "ApprovalOutcome",
    "ApprovalRequest",
    "REJECT_NOT_OWNER",
    "REJECT_NO_OWNER",
    "audit_decider",
    "authority_rejection_notice",
    "current_authority",
    "current_policy",
    "host_notices_timeouts",
    "is_owner_decision",
    "notice_for",
    "rejection_reason",
    "route_from_session_env",
    "route_key",
    "session_key_alias",
    "unresolved_authority_warning",
]
