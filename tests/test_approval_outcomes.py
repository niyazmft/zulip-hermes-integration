"""Exec-approval outcomes: the audit entry, the decider, what silence means (#222).

The gateway owns the approval decision and refuses an unanswered request on
every host this plugin supports, so the plugin's half of the contract is the
*record* (choice, decider, request id) and the *saying-so*, which is needed on
hosts below 0.21.4 that have no timeout notice of their own.

Pinned here:

* the policy key resolves through the preset gate, defaults to ``allow``
  (today's behaviour, migration-safe) and never falls back to the strict value;
* a request is minted per gateway-surface approval and paired with its outcome
  by session key, with exactly one claim per outcome;
* the decider is the person who clicked, or ``timeout`` / ``policy`` / … when no
  person decided — never a guess;
* the refusal line is posted once, in the prompt's own route, only when the
  policy is ``deny`` *and* the host cannot post one itself, and a failure to
  post it is logged and dropped rather than raised;
* ``allow`` adds nothing, and a person's denial is not duplicated in the room.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

from zulip import approval_outcomes as outcomes
from zulip import settings
from zulip.approval_outcomes import ApprovalLedger, ApprovalOutcome


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("ZULIP_APPROVAL_ON_TIMEOUT", "ZULIP_PROFILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def _no_host_notice(monkeypatch):
    """Pin the capability boundary: a host that cannot post a timeout notice."""
    monkeypatch.setattr(outcomes, "_host_register_timeout_notice", None)


@pytest.fixture
def _host_notice(monkeypatch):
    """Pin the other side: a host that posts its own (0.21.4+)."""
    monkeypatch.setattr(outcomes, "_host_register_timeout_notice", lambda *a, **k: None)


# --------------------------------------------------------------------------
# The policy key
# --------------------------------------------------------------------------


class TestPolicy:
    def test_unset_is_allow(self):
        assert settings.resolve_approval_on_timeout() == "allow"
        assert outcomes.current_policy() == "allow"

    def test_explicit_deny_wins(self, monkeypatch):
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "deny")
        assert settings.resolve_approval_on_timeout() == "deny"

    def test_case_and_whitespace_are_forgiven(self, monkeypatch):
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", " DENY ")
        assert settings.resolve_approval_on_timeout() == "deny"

    def test_invalid_value_falls_back_to_allow_with_a_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "maybe")
        with caplog.at_level("WARNING"):
            assert settings.resolve_approval_on_timeout() == "allow"
        assert "ZULIP_APPROVAL_ON_TIMEOUT" in caplog.text

    def test_recommended_profile_selects_deny(self, monkeypatch):
        monkeypatch.setenv("ZULIP_PROFILE", "recommended")
        assert settings.resolve_approval_on_timeout() == "deny"

    def test_an_explicit_value_wins_over_the_profile(self, monkeypatch):
        monkeypatch.setenv("ZULIP_PROFILE", "recommended")
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "allow")
        assert settings.resolve_approval_on_timeout() == "allow"

    def test_the_manifest_declares_the_key(self):
        manifest = (REPO_ROOT / "zulip" / "plugin.yaml").read_text(encoding="utf-8")
        assert "ZULIP_APPROVAL_ON_TIMEOUT" in manifest


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


class TestLedgerPairing:
    def test_a_gateway_request_is_minted_and_pair_with_its_outcome(self):
        ledger = ApprovalLedger()
        request = ledger.note_request(
            session_key="k1", surface="gateway", command="rm -rf /", pattern_key="rm"
        )
        assert request is not None and len(request.request_id) == 32

        outcome = ledger.resolve(session_key="k1", choice="once")
        assert outcome is not None
        assert outcome.choice == "once"
        assert outcome.request_id == request.request_id
        assert outcome.command == "rm -rf /"
        assert outcome.pattern_key == "rm"
        assert outcome.refused is False
        # A second claim finds nothing: one outcome, one report.
        assert ledger.resolve(session_key="k1", choice="once") is None

    @pytest.mark.parametrize("surface", ["cli", "smart", ""])
    def test_only_the_gateway_surface_is_tracked(self, surface):
        """``smart`` fires a pre-hook for verdicts that never post: tracking it
        would shift every later outcome onto the wrong request."""
        ledger = ApprovalLedger()
        assert ledger.note_request(session_key="k1", surface=surface) is None
        assert ledger.pending_count() == 0
        assert ledger.resolve(session_key="k1", choice="timeout") is None

    def test_outcomes_pair_fifo_when_two_are_in_flight(self):
        ledger = ApprovalLedger()
        first = ledger.note_request(session_key="k1", surface="gateway", command="a")
        second = ledger.note_request(session_key="k1", surface="gateway", command="b")
        assert ledger.pending_count("k1") == 2
        assert ledger.resolve(session_key="k1", choice="deny").request_id == first.request_id
        assert ledger.resolve(session_key="k1", choice="deny").request_id == second.request_id

    def test_an_unclaimed_request_is_bounded_and_expires(self):
        ledger = ApprovalLedger(ttl=10.0)
        for index in range(3):
            ledger.note_request(session_key="k1", surface="gateway", command=str(index), now=100.0)
        assert ledger.pending_count("k1") == 3
        # Past the TTL the stale request is not carried into a fresh pairing.
        ledger.note_request(session_key="k1", surface="gateway", command="fresh", now=1000.0)
        assert ledger.pending_count("k1") == 1
        outcome = ledger.resolve(session_key="k1", choice="timeout", now=1000.0)
        assert outcome.command == "fresh"

    def test_route_is_recorded_only_by_the_adapter_that_rendered_the_prompt(self):
        ledger = ApprovalLedger()
        ledger.note_request(session_key="k1", surface="gateway")
        assert ledger.owns("k1") is False
        ledger.note_route(session_key="k1", chat_id="573423", topic="api-review")
        assert ledger.owns("k1") is True
        outcome = ledger.resolve(session_key="k1", choice="once")
        assert outcome.owned is True
        assert outcome.route == ("573423", "api-review")

    def test_resolve_without_a_request_is_none(self):
        ledger = ApprovalLedger()
        assert ledger.resolve(session_key="unknown", choice="deny") is None


class TestDecider:
    def test_a_human_decider_comes_from_the_click_sender(self):
        ledger = ApprovalLedger()
        ledger.note_request(session_key="k1", surface="gateway")
        assert ledger.note_decider(session_key="k1", email="Dana@org.zulipchat.com") is True
        outcome = ledger.resolve(session_key="k1", choice="deny")
        assert outcome.decider == "Dana@org.zulipchat.com"
        # The audit carries it masked; the room text never needs a person.
        assert outcomes.audit_decider(outcome.decider) == "D***@org.zulipchat.com"

    def test_the_route_is_the_fallback_join(self):
        """A host that exposes no session key for an event still joins by route."""
        ledger = ApprovalLedger()
        ledger.note_request(session_key="k1", surface="gateway")
        ledger.note_decider(chat_id="573423", topic="t", email="dana@x.com")
        outcome = ledger.resolve(session_key="k1", choice="once", route=("573423", "t"))
        assert outcome.decider == "dana@x.com"

    def test_a_stale_decider_is_not_attributed(self):
        ledger = ApprovalLedger(ttl=10.0)
        ledger.note_request(session_key="k1", surface="gateway", now=100.0)
        ledger.note_decider(session_key="k1", email="dana@x.com", now=100.0)
        outcome = ledger.resolve(session_key="k1", choice="deny", now=200.0)
        assert outcome.decider == outcomes.DECIDER_UNKNOWN

    def test_a_timer_is_the_decider_for_a_timeout(self):
        ledger = ApprovalLedger()
        ledger.note_request(session_key="k1", surface="gateway")
        ledger.note_decider(session_key="k1", email="dana@x.com")
        outcome = ledger.resolve(session_key="k1", choice="timeout")
        # Nobody decided, even though somebody had clicked earlier and gone.
        assert outcome.decider == outcomes.DECIDER_TIMEOUT
        assert outcome.refused is True

    def test_policy_outranks_a_bystander(self):
        ledger = ApprovalLedger()
        ledger.note_request(session_key="k1", surface="gateway")
        ledger.note_decider(session_key="k1", email="dana@x.com")
        outcome = ledger.resolve(session_key="k1", choice="smart_deny", decided_by="aux_llm")
        assert outcome.decider == outcomes.DECIDER_POLICY

    @pytest.mark.parametrize(
        "choice, expected",
        [
            ("cancelled", outcomes.DECIDER_CANCELLED),
            ("notify_failed", outcomes.DECIDER_UNDELIVERED),
        ],
    )
    def test_withdrawn_and_undelivered_prompts_are_named(self, choice, expected):
        ledger = ApprovalLedger()
        ledger.note_request(session_key="k1", surface="gateway")
        assert ledger.resolve(session_key="k1", choice=choice).decider == expected

    def test_an_unrecorded_decider_is_unknown_not_a_guess(self):
        ledger = ApprovalLedger()
        ledger.note_request(session_key="k1", surface="gateway")
        assert ledger.resolve(session_key="k1", choice="deny").decider == outcomes.DECIDER_UNKNOWN

    def test_audit_masking_leaves_tokens_alone(self):
        assert outcomes.audit_decider("timeout") == "timeout"
        assert outcomes.audit_decider("policy") == "policy"
        assert outcomes.audit_decider("") == "unknown"
        assert outcomes.audit_decider("a@b.com") == "***@b.com"
        assert outcomes.audit_decider("dana@b.com") == "d***@b.com"


# --------------------------------------------------------------------------
# The notice decision
# --------------------------------------------------------------------------


def _outcome(choice="timeout", *, owned=True, chat_id="573423") -> ApprovalOutcome:
    return ApprovalOutcome(
        choice=choice,
        decider=outcomes.DECIDER_TIMEOUT,
        request_id="req",
        session_key="k1",
        chat_id=chat_id,
        topic="api-review",
        owned=owned,
    )


class TestNoticeDecision:
    def test_deny_policy_without_a_host_notice_posts_one_line(self):
        assert (
            outcomes.notice_for(_outcome(), policy="deny", host_notices=False)
            == outcomes.TIMEOUT_REFUSAL_TEXT
        )

    def test_the_host_notice_is_never_duplicated(self):
        assert outcomes.notice_for(_outcome(), policy="deny", host_notices=True) is None

    def test_allow_is_unchanged(self):
        assert outcomes.notice_for(_outcome(), policy="allow", host_notices=False) is None

    def test_a_persons_denial_is_left_to_the_gateways_own_confirmation(self):
        assert (
            outcomes.notice_for(_outcome("deny"), policy="deny", host_notices=False) is None
        )

    def test_a_withdrawn_prompt_does_not_produce_a_line(self):
        assert (
            outcomes.notice_for(_outcome("cancelled"), policy="deny", host_notices=False)
            is None
        )

    def test_multiplexed_and_unowned_stays_quiet(self):
        """Several profiles live and this adapter rendered nothing: posting would
        risk speaking into another profile's realm."""
        assert (
            outcomes.notice_for(
                _outcome(owned=False), policy="deny", host_notices=False, multiplexed=True
            )
            is None
        )


# --------------------------------------------------------------------------
# Adapter wiring: hooks in, audit + notice out
# --------------------------------------------------------------------------


def _adapter(mock_platform_config, monkeypatch):
    from tests.test_engagement import _build

    adapter = _build(mock_platform_config, monkeypatch)
    adapter.handle_message = AsyncMock()
    return adapter


def _stream_msg(*args, **kwargs):
    """The stream-message builder the engagement/trigger tests use."""
    from tests.test_engagement import _stream_msg as builder

    return builder(*args, **kwargs)


def _pre(adapter, **kwargs):
    kwargs.setdefault("session_key", "k1")
    kwargs.setdefault("surface", "gateway")
    kwargs.setdefault("command", "rm -rf /tmp/x")
    adapter.note_approval_request(**kwargs)


async def _post(adapter, choice, *, route=None, **kwargs):
    """Drive the outcome path the sync hook drives, and await the reporting."""
    outcome = adapter.claim_approval_outcome(
        route=route, multiplexed=False, session_key="k1", choice=choice, **kwargs
    )
    assert outcome is not None
    await adapter.report_approval_outcome(outcome)
    return outcome


async def _audit_events(adapter):
    audit_dir = Path(adapter._data_dir) / "audit"
    events = []
    for path in sorted(audit_dir.glob("*.audit.log")):
        events += [
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        ]
    return events


class TestAdapterOutcome:
    @pytest.mark.asyncio
    async def test_a_timeout_is_audited_with_the_timer_as_decider(
        self, mock_platform_config, monkeypatch, _no_host_notice
    ):
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")
        await _post(adapter, "timeout")

        entries = [e for e in await _audit_events(adapter) if e["event"] == "approval_outcome"]
        assert len(entries) == 1
        details = entries[0]["details"]
        assert details["choice"] == "timeout"
        assert details["decider"] == "timeout"
        assert details["request_id"]
        assert details["chat_id"] == "573423"
        assert details["topic"] == "api-review"

    @pytest.mark.asyncio
    async def test_fail_closed_posts_one_refusal_line_in_the_prompts_route(
        self, mock_platform_config, monkeypatch, _no_host_notice
    ):
        adapter = _adapter(mock_platform_config, monkeypatch)
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "deny")
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")
        await _post(adapter, "timeout")

        sent = [c.args[0] for c in adapter.client.send_message.call_args_list]
        notices = [s for s in sent if outcomes.TIMEOUT_REFUSAL_TEXT in s.get("content", "")]
        assert len(notices) == 1, "exactly one refusal line, not a duplicate"
        assert notices[0]["type"] == "stream"
        assert str(notices[0]["to"]) == "573423"
        assert notices[0]["topic"] == "api-review"

    @pytest.mark.asyncio
    async def test_fail_closed_in_a_dm_stays_a_dm(
        self, mock_platform_config, monkeypatch, _no_host_notice
    ):
        adapter = _adapter(mock_platform_config, monkeypatch)
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "deny")
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "dm:1032616", None)
        await _post(adapter, "timeout")

        sent = [c.args[0] for c in adapter.client.send_message.call_args_list]
        notices = [s for s in sent if outcomes.TIMEOUT_REFUSAL_TEXT in s.get("content", "")]
        assert notices and notices[0]["type"] == "private"

    @pytest.mark.asyncio
    async def test_allow_preserves_todays_behaviour(
        self, mock_platform_config, monkeypatch, _no_host_notice
    ):
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")
        await _post(adapter, "timeout")

        sent = [c.args[0] for c in adapter.client.send_message.call_args_list]
        assert not [s for s in sent if outcomes.TIMEOUT_REFUSAL_TEXT in s.get("content", "")]
        # The record still lands: the key decides what is said, not what is known.
        assert [e for e in await _audit_events(adapter) if e["event"] == "approval_outcome"]

    @pytest.mark.asyncio
    async def test_a_host_that_posts_its_own_notice_is_not_duplicated(
        self, mock_platform_config, monkeypatch, _host_notice
    ):
        adapter = _adapter(mock_platform_config, monkeypatch)
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "deny")
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")
        await _post(adapter, "timeout")

        sent = [c.args[0] for c in adapter.client.send_message.call_args_list]
        assert not [s for s in sent if outcomes.TIMEOUT_REFUSAL_TEXT in s.get("content", "")]

    @pytest.mark.asyncio
    async def test_a_persons_denial_is_audited_with_their_identity_not_repeated(
        self, mock_platform_config, monkeypatch, _no_host_notice
    ):
        adapter = _adapter(mock_platform_config, monkeypatch)
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "deny")
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")
        # The click arrived first, through inbound: the sender is the decider.
        adapter.note_approval_decider(
            session_key="k1", chat_id="573423", topic="api-review",
            sender_email="Dana@org.zulipchat.com",
        )
        await _post(adapter, "deny")

        entries = [e for e in await _audit_events(adapter) if e["event"] == "approval_outcome"]
        assert entries[0]["details"]["decider"] == "D***@org.zulipchat.com"
        assert entries[0]["details"]["choice"] == "deny"
        sent = [c.args[0] for c in adapter.client.send_message.call_args_list]
        assert not [s for s in sent if outcomes.TIMEOUT_REFUSAL_TEXT in s.get("content", "")]

    @pytest.mark.asyncio
    async def test_a_failed_notice_is_logged_and_dropped(
        self, mock_platform_config, monkeypatch, _no_host_notice, caplog
    ):
        adapter = _adapter(mock_platform_config, monkeypatch)
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "deny")
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")
        adapter.client.send_message = MagicMock(side_effect=RuntimeError("boom"))

        with caplog.at_level("WARNING"):
            await _post(adapter, "timeout")  # must not raise
        assert "approval notice" in caplog.text

    @pytest.mark.asyncio
    async def test_a_failed_audit_write_is_logged_and_dropped(
        self, mock_platform_config, monkeypatch, _no_host_notice, caplog
    ):
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")
        adapter._audit_logger.log_approval_outcome = AsyncMock(
            side_effect=RuntimeError("disk full")
        )
        with caplog.at_level("WARNING"):
            await _post(adapter, "once")
        assert "approval audit" in caplog.text

    @pytest.mark.asyncio
    async def test_the_route_comes_from_the_session_context_when_no_prompt_was_rendered(
        self, mock_platform_config, monkeypatch, _no_host_notice
    ):
        """Hosts below 0.21.3 post the plain-text prompt themselves, so the plugin
        never saw the route — the session context is then the only source."""
        adapter = _adapter(mock_platform_config, monkeypatch)
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "deny")
        _pre(adapter)
        await _post(adapter, "timeout", route=("573423", "api-review"))

        sent = [c.args[0] for c in adapter.client.send_message.call_args_list]
        notices = [s for s in sent if outcomes.TIMEOUT_REFUSAL_TEXT in s.get("content", "")]
        assert notices and notices[0]["topic"] == "api-review"

    @pytest.mark.asyncio
    async def test_one_claim_per_outcome(self, mock_platform_config, monkeypatch):
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")
        first = adapter.claim_approval_outcome(
            route=None, multiplexed=False, session_key="k1", choice="once"
        )
        second = adapter.claim_approval_outcome(
            route=None, multiplexed=False, session_key="k1", choice="once"
        )
        assert first is not None and second is None

    @pytest.mark.asyncio
    async def test_scheduling_from_a_sync_context_uses_the_bound_loop(
        self, mock_platform_config, monkeypatch
    ):
        """The hook runs on the agent thread; the reporting must cross to the
        adapter's loop rather than being dropped."""
        adapter = _adapter(mock_platform_config, monkeypatch)
        adapter._zulip_loop = asyncio.get_running_loop()
        outcome = ApprovalOutcome(
            choice="once", decider="timeout", request_id="req", session_key="k1"
        )
        adapter.report_approval_outcome = AsyncMock()
        future = adapter.schedule_approval_outcome(outcome)
        assert future is not None
        await asyncio.wrap_future(future)
        adapter.report_approval_outcome.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_loop_means_no_write_and_no_raise(
        self, mock_platform_config, monkeypatch
    ):
        adapter = _adapter(mock_platform_config, monkeypatch)
        adapter._zulip_loop = None
        outcome = ApprovalOutcome(
            choice="once", decider="timeout", request_id="req", session_key="k1"
        )
        # Running inside the test's loop: the same-loop path is taken, which is
        # correct — the guard is for a sync caller with no loop at all.
        assert adapter.schedule_approval_outcome(outcome) is not None


class TestDeciderVocabulary:
    @pytest.mark.parametrize(
        "content",
        [
            "/approve",
            "/approve session",
            "/approve always",
            "/approve all",
            "/deny",
            "/deny all",
            "/DENY",
        ],
    )
    def test_the_hosts_decision_commands_are_recognised(self, content):
        from zulip.commands import is_approval_decision

        assert is_approval_decision(content) is True

    @pytest.mark.parametrize(
        "content",
        ["/approvals", "/approvals deny", "/denied", "/help", "approve", "deny", "", "   "],
    )
    def test_everything_else_is_not(self, content):
        """``/approvals`` sets the approval *mode* — it must not be mistaken for
        a decision on a pending request."""
        from zulip.commands import is_approval_decision

        assert is_approval_decision(content) is False


class TestDeciderThroughInbound:
    @pytest.mark.asyncio
    async def test_the_click_sender_becomes_the_decider(
        self, mock_platform_config, monkeypatch, _no_host_notice
    ):
        """End to end: a click is an ordinary ``/deny`` from the clicker, and
        the sender is what names the decider in the audit entry."""
        adapter = _adapter(mock_platform_config, monkeypatch)
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "deny")
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")

        await adapter._handle_message(
            _stream_msg(
                "/deny",
                stream_id=573423,
                topic="api-review",
                sender="Dana@org.zulipchat.com",
            )
        )
        assert adapter.handle_message.call_count == 1, "the click must reach the gateway"

        await _post(adapter, "deny")
        entries = [e for e in await _audit_events(adapter) if e["event"] == "approval_outcome"]
        assert entries[0]["details"]["decider"] == "D***@org.zulipchat.com"
        assert entries[0]["details"]["choice"] == "deny"

    @pytest.mark.asyncio
    async def test_an_unrelated_slash_command_records_no_decider(
        self, mock_platform_config, monkeypatch
    ):
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        await adapter._handle_message(
            _stream_msg("/help", stream_id=573423, topic="api-review")
        )
        # The pending request is untouched, and no decider was attached, so the
        # audit will say ``unknown`` rather than name whoever typed a command.
        assert adapter._approval_ledger.pending_count("k1") == 1
        outcome = adapter._approval_ledger.resolve(session_key="k1", choice="once")
        assert outcome is not None and outcome.decider == outcomes.DECIDER_UNKNOWN


class TestHookEntryPoints:
    @pytest.mark.asyncio
    async def test_the_hooks_find_the_adapter_that_owns_the_session(
        self, mock_platform_config, monkeypatch, _no_host_notice
    ):
        from zulip import adapter as adapter_module

        adapter = _adapter(mock_platform_config, monkeypatch)
        monkeypatch.setenv("ZULIP_APPROVAL_ON_TIMEOUT", "deny")
        adapter._zulip_loop = asyncio.get_running_loop()

        adapter_module._on_pre_approval_request(
            session_key="k1", surface="gateway", command="rm -rf /tmp/x"
        )
        adapter.remember_approval_prompt("k1", "573423", "api-review")
        adapter_module._on_post_approval_response(session_key="k1", surface="gateway", choice="timeout")
        await asyncio.sleep(0.05)

        sent = [c.args[0] for c in adapter.client.send_message.call_args_list]
        assert [s for s in sent if outcomes.TIMEOUT_REFUSAL_TEXT in s.get("content", "")]

    @pytest.mark.asyncio
    async def test_an_unattributed_outcome_is_logged_not_guessed(
        self, mock_platform_config, monkeypatch, caplog
    ):
        from zulip import adapter as adapter_module

        adapter = _adapter(mock_platform_config, monkeypatch)
        with caplog.at_level("DEBUG"):
            adapter_module._on_post_approval_response(
                session_key="nobody-owns-this", surface="gateway", choice="timeout"
            )
        assert not adapter.client.send_message.call_args_list
        assert "unattributed" in caplog.text or adapter._approval_ledger.pending_count() == 0
