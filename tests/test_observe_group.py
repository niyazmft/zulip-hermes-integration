"""Tests for the soft gate and observe group (issue #153).

``ZULIP_SOFT_GATE`` dispatches every monitored stream message like onmessage
while keeping the honest mention signal in ``metadata["addressed"]``.
``ZULIP_OBSERVE_GROUP`` records non-addressed stream messages as bounded
per-topic context *without* replying, and quotes that buffer into the prompt the
next time the bot is addressed in the same topic. Both are off by default.
"""

import time
from unittest.mock import AsyncMock

import pytest

from zulip.adapter import (
    OBSERVED_HISTORY_LABEL,
    ObservedContextBuffer,
    _resolve_observe_group,
    _resolve_soft_gate,
)


class _FakeClient:
    """Minimal Zulip SDK stand-in for the inbound path."""

    def __init__(self, **kwargs):
        self.sent: list[dict] = []
        self.get_messages_calls: list[dict] = []
        self.get_messages_result = {"result": "success", "messages": []}

    def send_message(self, request):
        self.sent.append(request)
        return {"result": "success", "id": len(self.sent) + 1000}

    def update_message_flags(self, request):
        return {"result": "success"}

    def add_reaction(self, request):
        return {"result": "success"}

    def remove_reaction(self, request):
        return {"result": "success"}

    def set_typing_status(self, request):
        return {"result": "success"}

    def get_messages(self, request):
        self.get_messages_calls.append(request)
        return self.get_messages_result


@pytest.fixture
def make_adapter(mock_platform_config, monkeypatch):
    """Build an adapter with a fake client, applying env before construction."""

    def _make(**env):
        import zulip.zulip_client as zulip_client_module

        monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = _FakeClient(**kwargs)

                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)
        adapter.email = "bot@test.zulipchat.com"
        adapter.bot_full_name = "Test Bot"
        adapter.handle_message = AsyncMock()
        return adapter

    return _make


def _stream_msg(
    content,
    *,
    stream_id=1,
    topic="general",
    stream="test",
    sender_email="user@zulip.com",
    sender_id=42,
    flags=None,
    msg_id=1,
):
    return {
        "id": msg_id,
        "type": "stream",
        "stream_id": stream_id,
        "subject": topic,
        "display_recipient": stream,
        "content": content,
        "sender_email": sender_email,
        "sender_full_name": "User",
        "sender_id": sender_id,
        "flags": flags or [],
    }


def _dm_msg(content, *, sender_email="user@zulip.com", sender_id=42, msg_id=1):
    return {
        "id": msg_id,
        "type": "private",
        "content": content,
        "sender_email": sender_email,
        "sender_full_name": "User",
        "sender_id": sender_id,
    }


class TestFlagResolution:
    def test_soft_gate_off_by_default(self, monkeypatch):
        monkeypatch.delenv("ZULIP_SOFT_GATE", raising=False)
        assert _resolve_soft_gate() is False

    def test_observe_group_off_by_default(self, monkeypatch):
        monkeypatch.delenv("ZULIP_OBSERVE_GROUP", raising=False)
        assert _resolve_observe_group() is False

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
    def test_flags_accept_truthy(self, monkeypatch, value):
        monkeypatch.setenv("ZULIP_SOFT_GATE", value)
        monkeypatch.setenv("ZULIP_OBSERVE_GROUP", value)
        assert _resolve_soft_gate() is True
        assert _resolve_observe_group() is True


class TestObservedContextBuffer:
    def test_caps_message_count_and_drops_oldest(self):
        buffer = ObservedContextBuffer(max_messages=2, max_chars=1000)
        buffer.add(1, "general", "[A] one")
        buffer.add(1, "general", "[B] two")
        buffer.add(1, "general", "[C] three")
        assert buffer.render(1, "general") == "[B] two\n[C] three"

    def test_caps_characters_and_keeps_newest(self):
        buffer = ObservedContextBuffer(max_messages=10, max_chars=20)
        buffer.add(1, "general", "[A] " + "x" * 20)
        buffer.add(1, "general", "[B] newest")
        rendered = buffer.render(1, "general")
        assert rendered == "[B] newest"

    def test_truncates_a_single_oversized_line(self):
        buffer = ObservedContextBuffer(max_messages=10, max_chars=10)
        buffer.add(1, "general", "y" * 100)
        assert len(buffer.render(1, "general")) <= 10

    def test_topics_are_isolated(self):
        buffer = ObservedContextBuffer()
        buffer.add(1, "general", "[A] in general")
        buffer.add(1, "design", "[B] in design")
        assert buffer.render(1, "general") == "[A] in general"
        assert buffer.render(1, "design") == "[B] in design"
        assert buffer.render(2, "general") == ""

    def test_streams_with_same_topic_are_isolated(self):
        buffer = ObservedContextBuffer()
        buffer.add(1, "general", "[A] stream one")
        assert buffer.render(2, "general") == ""

    def test_empty_line_is_ignored(self):
        buffer = ObservedContextBuffer()
        buffer.add(1, "general", "   ")
        assert buffer.render(1, "general") == ""


class TestSoftGate:
    @pytest.mark.asyncio
    async def test_off_drops_unmentioned_stream_message(self, make_adapter):
        adapter = make_adapter(ZULIP_CHATMODE="oncall")
        await adapter._handle_message(_stream_msg("just chatting"))
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_dispatches_unmentioned_stream_message(self, make_adapter):
        adapter = make_adapter(ZULIP_CHATMODE="oncall", ZULIP_SOFT_GATE="true")
        await adapter._handle_message(_stream_msg("just chatting"))
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.metadata["addressed"] is False
        assert event.text == "just chatting"

    @pytest.mark.asyncio
    async def test_on_marks_mentioned_message_addressed(self, make_adapter):
        adapter = make_adapter(ZULIP_CHATMODE="oncall", ZULIP_SOFT_GATE="true")
        await adapter._handle_message(
            _stream_msg("hello there", flags=["mentioned"])
        )
        event = adapter.handle_message.call_args[0][0]
        assert event.metadata["addressed"] is True

    @pytest.mark.asyncio
    async def test_off_omits_addressed_metadata(self, make_adapter):
        adapter = make_adapter(ZULIP_CHATMODE="onmessage")
        await adapter._handle_message(_stream_msg("hello"))
        event = adapter.handle_message.call_args[0][0]
        assert "addressed" not in event.metadata

    @pytest.mark.asyncio
    async def test_dm_dispatch_is_unchanged_by_soft_gate(self, make_adapter):
        adapter = make_adapter(ZULIP_CHATMODE="oncall", ZULIP_SOFT_GATE="true")
        await adapter._handle_message(_dm_msg("hi in a dm"))
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.source.chat_type == "dm"
        assert "addressed" not in event.metadata


class TestObserveGroup:
    @pytest.mark.asyncio
    async def test_observed_message_gets_no_reply(self, make_adapter):
        adapter = make_adapter(
            ZULIP_CHATMODE="oncall", ZULIP_OBSERVE_GROUP="true"
        )
        await adapter._handle_message(_stream_msg("ambient chatter"))
        adapter.handle_message.assert_not_called()
        assert "ambient chatter" in adapter._observed_context.render(1, "general")

    @pytest.mark.asyncio
    async def test_observed_context_injected_on_next_addressed_turn(self, make_adapter):
        adapter = make_adapter(
            ZULIP_CHATMODE="oncall", ZULIP_OBSERVE_GROUP="true"
        )
        await adapter._handle_message(_stream_msg("we picked blue", msg_id=1))
        adapter.handle_message.assert_not_called()

        await adapter._handle_message(
            _stream_msg("what colour?", flags=["mentioned"], msg_id=2)
        )
        event = adapter.handle_message.call_args[0][0]
        assert event.text.startswith(OBSERVED_HISTORY_LABEL)
        assert "we picked blue" in event.text
        assert event.text.endswith("what colour?")

    @pytest.mark.asyncio
    async def test_observation_is_isolated_per_topic(self, make_adapter):
        adapter = make_adapter(
            ZULIP_CHATMODE="oncall", ZULIP_OBSERVE_GROUP="true"
        )
        await adapter._handle_message(_stream_msg("topic one chatter", topic="one"))
        await adapter._handle_message(
            _stream_msg("hello", topic="two", flags=["mentioned"], msg_id=2)
        )
        event = adapter.handle_message.call_args[0][0]
        assert "topic one chatter" not in event.text
        assert not event.text.startswith(OBSERVED_HISTORY_LABEL)

    @pytest.mark.asyncio
    async def test_unaddressed_message_is_not_observed_when_soft_gate_on(
        self, make_adapter
    ):
        adapter = make_adapter(
            ZULIP_CHATMODE="oncall",
            ZULIP_SOFT_GATE="true",
            ZULIP_OBSERVE_GROUP="true",
        )
        await adapter._handle_message(_stream_msg("dispatched anyway"))
        adapter.handle_message.assert_called_once()
        assert adapter._observed_context.render(1, "general") == ""

    @pytest.mark.asyncio
    async def test_self_messages_are_not_observed(self, make_adapter):
        adapter = make_adapter(
            ZULIP_CHATMODE="oncall", ZULIP_OBSERVE_GROUP="true"
        )
        own = _stream_msg("my own words", sender_email=adapter.email)
        await adapter._handle_message(own)
        assert adapter._observed_context.render(1, "general") == ""

    def test_observe_direct_call_rejects_self_messages(self, make_adapter):
        adapter = make_adapter(ZULIP_OBSERVE_GROUP="true")
        own = _stream_msg("my own words", sender_email=adapter.email)
        adapter._observe_stream_message(own, "my own words")
        assert adapter._observed_context.render(1, "general") == ""

    @pytest.mark.asyncio
    async def test_dms_are_never_observed(self, make_adapter):
        adapter = make_adapter(
            ZULIP_CHATMODE="oncall", ZULIP_OBSERVE_GROUP="true"
        )
        await adapter._handle_message(_dm_msg("private words"))
        adapter.handle_message.assert_called_once()
        assert adapter._observed_context.render(1, "general") == ""

    @pytest.mark.asyncio
    async def test_unmonitored_stream_is_not_observed(self, make_adapter):
        adapter = make_adapter(
            ZULIP_CHATMODE="oncall",
            ZULIP_OBSERVE_GROUP="true",
            ZULIP_STREAMS="other",
        )
        await adapter._handle_message(_stream_msg("not for us", stream="test"))
        assert adapter._observed_context.render(1, "general") == ""

    @pytest.mark.asyncio
    async def test_observed_block_is_not_injected_into_dm(self, make_adapter):
        adapter = make_adapter(
            ZULIP_CHATMODE="oncall", ZULIP_OBSERVE_GROUP="true"
        )
        await adapter._handle_message(_stream_msg("ambient chatter"))
        await adapter._handle_message(_dm_msg("hi in a dm", msg_id=2))
        event = adapter.handle_message.call_args[0][0]
        assert "ambient chatter" not in event.text
