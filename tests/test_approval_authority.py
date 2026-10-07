"""Only the bot owner may decide an exec approval (issue #228).

The prompt is posted into a **topic**, so on a shared work Zulip "anyone who can
read it" is a privilege escalation through a side channel: a coworker approves
the owner's bot running a command that mutates state, spends the owner's
credentials or touches the owner's host — and the audit names the coworker as the
decider of the owner's bot.

Enforcement is possible because a decision *is* a message: a zform button replies
with ``/approve``-family text, and a client without widgets (or a gateway below
0.21.3, where the prompt is plain text) types the same command. The plugin sees
that message before the gateway resolves it, so consuming it under the
owner-only rule leaves the prompt open instead of approving it.

Pinned here: the owner may decide; a coworker may not, on the button path and on
the text path alike; a refused decision is not counted (the prompt stays open and
the timeout default still applies); the refusal posts one line and one audit
entry naming the decider; an unresolvable owner fails closed with an actionable
warning; and an install with no marker behaves exactly as it did before.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from zulip import approval_outcomes as outcomes
from zulip import settings
from zulip.approval_outcomes import ApprovalLedger


OWNER = "owner@org.zulipchat.com"
COWORKER = "coworker@org.zulipchat.com"

REJECTION_LINE = outcomes.authority_rejection_notice(outcomes.REJECT_NOT_OWNER)
NO_OWNER_LINE = outcomes.authority_rejection_notice(outcomes.REJECT_NO_OWNER)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "ZULIP_APPROVAL_AUTHORITY",
        "ZULIP_APPROVAL_ON_TIMEOUT",
        "ZULIP_PROFILE",
        "ZULIP_OWNER_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)


def _adapter(mock_platform_config, monkeypatch, *, owner: str = OWNER):
    from tests.test_engagement import _build

    adapter = _build(mock_platform_config, monkeypatch)
    adapter.handle_message = AsyncMock()
    adapter._resolved_bot_owner_email = owner
    return adapter


def _stream_msg(*args, **kwargs):
    from tests.test_engagement import _stream_msg as builder

    return builder(*args, **kwargs)


def _pre(adapter, session_key: str = "k1", command: str = "rm -rf /tmp/x") -> None:
    """The gateway is about to deliver a prompt: mint its request id."""
    adapter.note_approval_request(
        session_key=session_key, surface="gateway", command=command
    )


def _sent(adapter) -> list[dict]:
    return [call.args[0] for call in adapter.client.send_message.call_args_list]


async def _audit_events(adapter) -> list[dict]:
    audit_dir = Path(adapter._data_dir) / "audit"
    events: list[dict] = []
    for path in sorted(audit_dir.glob("*.audit.log")):
        events += [
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        ]
    return events


async def _events_of(adapter, event: str) -> list[dict]:
    return [e for e in await _audit_events(adapter) if e["event"] == event]


# --------------------------------------------------------------------------
# The policy key
# --------------------------------------------------------------------------


class TestAuthorityPolicy:
    def test_unset_is_anyone(self):
        assert settings.resolve_approval_authority() == "anyone"
        assert outcomes.current_authority() == "anyone"

    def test_explicit_owner_wins(self, monkeypatch):
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        assert settings.resolve_approval_authority() == "owner"

    def test_invalid_value_falls_back_to_anyone_with_a_warning(self, monkeypatch, caplog):
        """A typo must not lock an install out of its own approvals."""
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "just-me")
        with caplog.at_level("WARNING"):
            assert settings.resolve_approval_authority() == "anyone"
        assert "ZULIP_APPROVAL_AUTHORITY" in caplog.text

    def test_recommended_profile_restricts_to_the_owner(self, monkeypatch):
        monkeypatch.setenv("ZULIP_PROFILE", "recommended")
        assert settings.resolve_approval_authority() == "owner"

    def test_an_explicit_value_wins_over_the_profile(self, monkeypatch):
        monkeypatch.setenv("ZULIP_PROFILE", "recommended")
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "anyone")
        assert settings.resolve_approval_authority() == "anyone"

    def test_the_manifest_declares_the_key(self):
        manifest = (Path(__file__).resolve().parents[1] / "zulip" / "plugin.yaml")
        assert "ZULIP_APPROVAL_AUTHORITY" in manifest.read_text(encoding="utf-8")


class TestOwnerComparison:
    def test_the_owner_is_recognised_case_insensitively(self):
        assert outcomes.is_owner_decision("Owner@Org.Zulipchat.com", OWNER) is True

    @pytest.mark.parametrize("sender", ["coworker@org.zulipchat.com", "", "   "])
    def test_anyone_else_is_not(self, sender):
        assert outcomes.is_owner_decision(sender, OWNER) is False

    def test_an_unresolved_owner_matches_nobody(self):
        assert outcomes.is_owner_decision(OWNER, "") is False

    def test_the_rule_only_applies_under_owner_authority(self):
        assert outcomes.rejection_reason(COWORKER, OWNER) is None
        assert outcomes.rejection_reason(OWNER, OWNER) is None

    def test_the_reason_distinguishes_no_owner_from_not_the_owner(self, monkeypatch):
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        assert outcomes.rejection_reason(COWORKER, OWNER) == outcomes.REJECT_NOT_OWNER
        assert outcomes.rejection_reason(OWNER, OWNER) is None
        assert outcomes.rejection_reason(OWNER, "") == outcomes.REJECT_NO_OWNER
        # Even the owner cannot decide when nobody could be resolved: fail closed.
        assert outcomes.rejection_reason(COWORKER, "") == outcomes.REJECT_NO_OWNER


# --------------------------------------------------------------------------
# Enforcement, through the inbound path a decision actually takes
# --------------------------------------------------------------------------


class TestOwnerMayDecide:
    @pytest.mark.asyncio
    async def test_the_owner_is_forwarded_and_recorded_as_the_decider(
        self, mock_platform_config, monkeypatch
    ):
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")

        await adapter._handle_message(
            _stream_msg(
                "/approve", stream_id=573423, topic="api-review", sender=OWNER
            )
        )
        assert adapter.handle_message.call_count == 1, "the owner's decision proceeds"

        outcome = adapter.claim_approval_outcome(
            route=None, multiplexed=False, session_key="k1", choice="once"
        )
        assert outcome is not None
        assert outcome.decider == OWNER

    @pytest.mark.asyncio
    async def test_the_owner_override_works_without_a_profile(
        self, mock_platform_config, monkeypatch
    ):
        """``ZULIP_OWNER_EMAIL`` is #214's explicit override, so it needs no
        preset marker and no connect-time lookup."""
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        monkeypatch.setenv("ZULIP_OWNER_EMAIL", OWNER)
        adapter = _adapter(mock_platform_config, monkeypatch, owner="")
        assert adapter.bot_owner_address() == OWNER

        adapter.handle_message.reset_mock()
        await adapter._handle_message(_stream_msg("/approve", sender=OWNER))
        assert adapter.handle_message.call_count == 1
        assert not [s for s in _sent(adapter) if REJECTION_LINE in s.get("content", "")]


class TestCoworkerMayNotDecide:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("content", ["/approve", "/approve always", "/deny all"])
    async def test_the_button_path_cannot_be_clicked_by_anyone_else(
        self, mock_platform_config, monkeypatch, content
    ):
        """The exact replies the zform choices send (hosts >= 0.21.3)."""
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")

        await adapter._handle_message(
            _stream_msg(
                content, stream_id=573423, topic="api-review", sender=COWORKER
            )
        )
        assert adapter.handle_message.call_count == 0, "the gateway must never see it"
        # The prompt is still pending, so the owner can still answer it.
        assert adapter._approval_ledger.pending_count("k1") == 1

    @pytest.mark.asyncio
    async def test_the_text_fallback_path_enforces_the_same_rule(
        self, mock_platform_config, monkeypatch
    ):
        """A plain typed ``/approve`` — what a widget-less client sends, and what
        every decision is on hosts below 0.21.3, where the gateway renders the
        prompt itself and the plugin never touches a widget."""
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        # No prompt rendered by this plugin: the host posted the plain text.
        assert adapter._approval_ledger.owns("k1") is False

        await adapter._handle_message(
            _stream_msg("/approve", stream_id=573423, topic="api-review", sender=COWORKER)
        )
        assert adapter.handle_message.call_count == 0
        assert adapter._approval_ledger.pending_count("k1") == 1

    @pytest.mark.asyncio
    async def test_a_refusal_posts_one_line_in_the_prompts_route(
        self, mock_platform_config, monkeypatch
    ):
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")

        await adapter._handle_message(
            _stream_msg("/approve", stream_id=573423, topic="api-review", sender=COWORKER)
        )
        lines = [s for s in _sent(adapter) if REJECTION_LINE in s.get("content", "")]
        assert len(lines) == 1, "one line, not a second mechanism"
        assert lines[0]["type"] == "stream"
        assert str(lines[0]["to"]) == "573423"
        assert lines[0]["topic"] == "api-review"

    @pytest.mark.asyncio
    async def test_a_refusal_is_audited_once_with_the_decider(
        self, mock_platform_config, monkeypatch
    ):
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")

        await adapter._handle_message(
            _stream_msg("/deny", stream_id=573423, topic="api-review", sender=COWORKER)
        )
        entries = await _events_of(adapter, "approval_rejected")
        assert len(entries) == 1
        details = entries[0]["details"]
        assert details["reason"] == outcomes.REJECT_NOT_OWNER
        assert details["decider"] == "c***@org.zulipchat.com", "masked, per the discipline"
        assert details["request_id"], "tied to the prompt it refused to decide"
        assert details["topic"] == "api-review"

    @pytest.mark.asyncio
    async def test_a_refused_decision_is_not_a_decision(
        self, mock_platform_config, monkeypatch
    ):
        """The refusal must not consume the request, so #222's default still
        applies: the window lapses and the run fail-closes."""
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")

        await adapter._handle_message(
            _stream_msg("/deny", stream_id=573423, topic="api-review", sender=COWORKER)
        )
        # No decider was recorded from the refused attempt.
        outcome = adapter.claim_approval_outcome(
            route=None, multiplexed=False, session_key="k1", choice="timeout"
        )
        assert outcome is not None
        assert outcome.decider == outcomes.DECIDER_TIMEOUT
        assert outcome.refused is True

    @pytest.mark.asyncio
    async def test_the_owner_can_still_decide_after_a_refused_attempt(
        self, mock_platform_config, monkeypatch
    ):
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch)
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")

        for sender in (COWORKER, COWORKER):
            await adapter._handle_message(
                _stream_msg("/deny", stream_id=573423, topic="api-review", sender=sender)
            )
        assert adapter.handle_message.call_count == 0

        await adapter._handle_message(
            _stream_msg("/approve", stream_id=573423, topic="api-review", sender=OWNER)
        )
        assert adapter.handle_message.call_count == 1
        outcome = adapter.claim_approval_outcome(
            route=None, multiplexed=False, session_key="k1", choice="once"
        )
        assert outcome is not None and outcome.decider == OWNER


class TestUnresolvedOwnerFailsClosed:
    @pytest.mark.asyncio
    async def test_nobody_can_decide_and_the_log_names_the_setting(
        self, mock_platform_config, monkeypatch, caplog
    ):
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch, owner="")
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")

        with caplog.at_level("WARNING"):
            await adapter._handle_message(
                _stream_msg(
                    "/approve", stream_id=573423, topic="api-review", sender=OWNER
                )
            )
        assert adapter.handle_message.call_count == 0, "not even the owner, until configured"
        assert "ZULIP_OWNER_EMAIL" in caplog.text

        lines = [s for s in _sent(adapter) if NO_OWNER_LINE in s.get("content", "")]
        assert len(lines) == 1
        assert "ZULIP_OWNER_EMAIL" in lines[0]["content"]

        entries = await _events_of(adapter, "approval_rejected")
        assert entries[0]["details"]["reason"] == outcomes.REJECT_NO_OWNER
        # Failing closed means the prompt still lapses into a refusal, not open.
        assert adapter._approval_ledger.pending_count("k1") == 1


class TestMigrationSafety:
    @pytest.mark.asyncio
    async def test_no_marker_is_todays_behaviour(self, mock_platform_config, monkeypatch):
        adapter = _adapter(mock_platform_config, monkeypatch, owner="")
        _pre(adapter)
        adapter.remember_approval_prompt("k1", "573423", "api-review")

        await adapter._handle_message(
            _stream_msg("/approve", stream_id=573423, topic="api-review", sender=COWORKER)
        )
        assert adapter.handle_message.call_count == 1, "anyone in the topic may decide"
        assert not [s for s in _sent(adapter) if REJECTION_LINE in s.get("content", "")]
        assert not await _events_of(adapter, "approval_rejected")

    @pytest.mark.asyncio
    async def test_an_unrelated_command_is_never_refused(
        self, mock_platform_config, monkeypatch
    ):
        """Only decision commands are gated; ``/help`` is the gateway's."""
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch)
        await adapter._handle_message(_stream_msg("/help", sender=COWORKER))
        assert adapter.handle_message.call_count == 1
        assert not [s for s in _sent(adapter) if REJECTION_LINE in s.get("content", "")]


class TestStartupReport:
    def test_owner_authority_with_an_owner_is_reported_at_info(
        self, mock_platform_config, monkeypatch, caplog
    ):
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch)
        with caplog.at_level("INFO"):
            adapter.report_approval_authority()
        assert "owner-only" in caplog.text

    def test_owner_authority_without_an_owner_warns_actionably(
        self, mock_platform_config, monkeypatch, caplog
    ):
        """Fail-closed must not be discovered at the first approval."""
        monkeypatch.setenv("ZULIP_APPROVAL_AUTHORITY", "owner")
        adapter = _adapter(mock_platform_config, monkeypatch, owner="")
        with caplog.at_level("WARNING"):
            adapter.report_approval_authority()
        assert "ZULIP_OWNER_EMAIL" in caplog.text

    def test_anyone_authority_says_nothing(
        self, mock_platform_config, monkeypatch, caplog
    ):
        adapter = _adapter(mock_platform_config, monkeypatch, owner="")
        with caplog.at_level("INFO"):
            adapter.report_approval_authority()
        assert "owner-only" not in caplog.text
        assert "ZULIP_OWNER_EMAIL" not in caplog.text


class TestLedgerReadOnly:
    def test_pending_request_id_does_not_consume(self):
        ledger = ApprovalLedger()
        request = ledger.note_request(session_key="k1", surface="gateway")
        assert ledger.pending_request_id("k1") == request.request_id
        assert ledger.pending_request_id("k1") == request.request_id
        assert ledger.pending_count("k1") == 1
        assert ledger.pending_request_id("other") == ""

    def test_pending_request_id_falls_back_to_the_route(self):
        """A host that exposes no session key for an event still lets a refusal
        name the prompt it refused to decide."""
        ledger = ApprovalLedger()
        request = ledger.note_request(session_key="k1", surface="gateway")
        ledger.note_route(session_key="k1", chat_id="573423", topic="api-review")
        assert ledger.pending_request_id(
            "", chat_id="573423", topic="api-review"
        ) == request.request_id
        assert ledger.pending_request_id("", chat_id="573423", topic="other") == ""
