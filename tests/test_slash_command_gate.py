"""A native slash command is never mention-gated in a stream (issue #259).

The trigger gate (chatmode / mention / onchar / sticky engagement) decides
whether a *conversational* message is for the bot. A slash command is not
conversational: the plugin answers its own and forwards the rest to the gateway
that owns them — and both paths sit after the gate, so a gated stream used to
drop every gateway-native command on the floor. The exec-approval buttons are
the sharp edge of that: a click sends an ordinary ``/approve`` / ``/deny``
message from the clicker, so under the recommended posture (``oncall``,
mention-gated, sticky engagement off) the buttons were dead and the approval
lapsed into a refusal.

These tests pin the rule *and* the gates that must survive it: the trigger gate
stops applying to commands, and the rate limit, stream filter, group policy and
stream policy keep their order and their effect.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from tests.test_engagement import _build, _dm, _stream_msg


def _gated(mock_platform_config, monkeypatch, **env):
    """A mention-gated stream install with sticky engagement off (the posture
    the recommended profile ships)."""
    return _build(
        mock_platform_config,
        monkeypatch,
        ZULIP_CHATMODE="oncall",
        ZULIP_REQUIRE_MENTION="true",
        **env,
    )


class TestCommandsBypassTheTriggerGate:
    @pytest.mark.asyncio
    async def test_deny_in_gated_stream_reaches_the_gateway(
        self, mock_platform_config, monkeypatch
    ):
        a = _gated(mock_platform_config, monkeypatch)
        await a._handle_message(_stream_msg("/deny", sender="coworker@zulip.com"))
        assert a.handle_message.call_count == 1, (
            "an approval click is a /deny message; dropping it leaves the "
            "prompt to time out as if nobody answered"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content", ["/approve", "/approve always", "/deny all", "/help", "/model", "/stop"]
    )
    async def test_every_gateway_native_command_reaches_the_gateway(
        self, mock_platform_config, monkeypatch, content
    ):
        a = _gated(mock_platform_config, monkeypatch)
        await a._handle_message(_stream_msg(content))
        assert a.handle_message.call_count == 1, content

    @pytest.mark.asyncio
    async def test_command_is_dispatched_but_not_treated_as_addressed(
        self, mock_platform_config, monkeypatch
    ):
        """The bypass is the *trigger* gate only: nothing addresses the bot."""
        a = _gated(mock_platform_config, monkeypatch)
        await a._handle_message(_stream_msg("/deny"))
        event = a.handle_message.call_args[0][0]
        assert event.metadata.get("addressed") in (None, False)
        assert a._engagement_store.active_count() == 0

    @pytest.mark.asyncio
    async def test_plugin_command_still_intercepted_after_the_gate(
        self, mock_platform_config, monkeypatch
    ):
        """`/unlisten` is the plugin's, not the gateway's: it is consumed and
        answered in-topic rather than dispatched to the agent."""
        a = _gated(
            mock_platform_config,
            monkeypatch,
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="topic",
        )
        await a._handle_message(_stream_msg("/unlisten"))
        assert a.handle_message.call_count == 0
        assert a.client.send_message.call_count >= 1, "the stop is acknowledged"

    @pytest.mark.asyncio
    async def test_plain_message_in_the_same_stream_is_still_dropped(
        self, mock_platform_config, monkeypatch
    ):
        """The gate itself is unchanged — this is the regression that matters."""
        a = _gated(mock_platform_config, monkeypatch)
        await a._handle_message(_stream_msg("did anyone fix the deploy?"))
        assert a.handle_message.call_count == 0


class TestTheOtherGatesSurvive:
    @pytest.mark.asyncio
    async def test_group_policy_still_blocks_a_command(
        self, mock_platform_config, monkeypatch
    ):
        a = _gated(
            mock_platform_config,
            monkeypatch,
            ZULIP_GROUP_POLICY="allowlist",
            ZULIP_GROUP_ALLOW_FROM="friend@zulip.com",
        )
        await a._handle_message(_stream_msg("/deny", sender="stranger@zulip.com"))
        assert a.handle_message.call_count == 0
        sent = [c.args[0] for c in a.client.send_message.call_args_list]
        assert any("not authorized" in s.get("content", "") for s in sent)

    @pytest.mark.asyncio
    async def test_stream_filter_still_blocks_a_command(
        self, mock_platform_config, monkeypatch
    ):
        a = _gated(mock_platform_config, monkeypatch, ZULIP_STREAMS="other-stream")
        await a._handle_message(_stream_msg("/deny"))
        assert a.handle_message.call_count == 0

    @pytest.mark.asyncio
    async def test_rate_limit_still_applies_to_commands(
        self, mock_platform_config, monkeypatch
    ):
        a = _gated(mock_platform_config, monkeypatch, ZULIP_MAX_MESSAGES_PER_MINUTE="2")
        for _ in range(2):
            await a._handle_message(_stream_msg("/deny"))
        assert a.handle_message.call_count == 2
        await a._handle_message(_stream_msg("/deny"))
        assert a.handle_message.call_count == 2, "the rate limit is not bypassed"

    @pytest.mark.asyncio
    async def test_command_in_a_dm_is_unchanged(self, mock_platform_config, monkeypatch):
        a = _gated(mock_platform_config, monkeypatch)
        await a._handle_message(_dm("/deny"))
        assert a.handle_message.call_count == 1

    @pytest.mark.asyncio
    async def test_command_under_soft_gate_is_still_observed_not_addressed(
        self, mock_platform_config, monkeypatch
    ):
        a = _gated(mock_platform_config, monkeypatch, ZULIP_SOFT_GATE="true")
        await a._handle_message(_stream_msg("/deny"))
        assert a.handle_message.call_count == 1
        event = a.handle_message.call_args[0][0]
        assert event.metadata.get("addressed") is False
