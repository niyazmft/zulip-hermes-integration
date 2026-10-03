"""Gate 17 of the inbound chain: context-mitigation metadata + DM rotation.

After the god-object decomposition this was the only gate stage in
``zulip/inbound.py`` with *zero* test coverage (disclosed in wave 9, never
closed). It is not a security control — nothing here decides whether the bot may
answer — but it decides what the agent is *told* about the conversation, so a
silent regression degrades every turn without failing anything else.

Covered here:

* ``conversation_turn`` counts per chat (not globally) and is on every turn
* ``session_gap_seconds`` is ``0`` for a first message and tracks the real gap
* ``topic_changed`` flips only when a stream's topic actually changes, and is
  never set for DMs
* DM session rotation re-keys ``chat_id`` every N turns
  (``ZULIP_DM_SESSION_TURN_LIMIT``) and is disabled at ``0``

The metadata is read off the dispatched ``MessageEvent`` exactly as the gateway
receives it, rather than from the adapter's internal counters.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

# Streams only dispatch unmentioned traffic with the mention requirement off.
_DISPATCH_ALL = {"ZULIP_CHATMODE": "onmessage", "ZULIP_REQUIRE_MENTION": "false"}


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
        "flags": [],
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


def _dispatched(adapter):
    """The ``MessageEvent`` the adapter actually handed to the gateway."""
    return adapter.handle_message.call_args[0][0]


class TestConversationTurn:
    @pytest.mark.asyncio
    async def test_increments_across_turns_and_is_on_every_turn(self, make_adapter):
        adapter = make_adapter(**_DISPATCH_ALL)
        seen = []
        for index in range(3):
            await adapter._handle_message(_stream_msg(f"m{index}", msg_id=index + 1))
            seen.append(_dispatched(adapter).metadata["conversation_turn"])
        assert seen == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_counted_per_chat_not_globally(self, make_adapter):
        adapter = make_adapter(**_DISPATCH_ALL)
        await adapter._handle_message(_stream_msg("first", stream_id=1, msg_id=1))
        await adapter._handle_message(
            _stream_msg("first", stream_id=2, topic="other", msg_id=2)
        )
        # A different stream is a different chat, so its own count restarts.
        assert _dispatched(adapter).metadata["conversation_turn"] == 1


class TestSessionGapSeconds:
    @pytest.mark.asyncio
    async def test_first_message_reports_zero_gap(self, make_adapter):
        adapter = make_adapter(**_DISPATCH_ALL)
        await adapter._handle_message(_stream_msg("first", msg_id=1))
        assert _dispatched(adapter).metadata["session_gap_seconds"] == 0

    @pytest.mark.asyncio
    async def test_gap_reflects_elapsed_time(self, make_adapter):
        adapter = make_adapter(**_DISPATCH_ALL)
        # Stream chat_id is str(stream_id); seed a known 120s-old message.
        adapter._last_message_time["1"] = time.time() - 120
        await adapter._handle_message(_stream_msg("later", msg_id=2))
        gap = _dispatched(adapter).metadata["session_gap_seconds"]
        assert 119 <= gap <= 130, gap
        # ...and it is rounded to one decimal, as documented.
        assert gap == round(gap, 1)


class TestTopicChanged:
    @pytest.mark.asyncio
    async def test_false_on_first_sight_of_a_topic(self, make_adapter):
        adapter = make_adapter(**_DISPATCH_ALL)
        await adapter._handle_message(_stream_msg("hi", topic="one", msg_id=1))
        assert _dispatched(adapter).metadata["topic_changed"] is False

    @pytest.mark.asyncio
    async def test_true_when_the_topic_differs(self, make_adapter):
        adapter = make_adapter(**_DISPATCH_ALL)
        await adapter._handle_message(_stream_msg("hi", topic="one", msg_id=1))
        await adapter._handle_message(_stream_msg("again", topic="two", msg_id=2))
        assert _dispatched(adapter).metadata["topic_changed"] is True

    @pytest.mark.asyncio
    async def test_false_when_the_topic_is_unchanged(self, make_adapter):
        adapter = make_adapter(**_DISPATCH_ALL)
        await adapter._handle_message(_stream_msg("hi", topic="one", msg_id=1))
        await adapter._handle_message(_stream_msg("again", topic="one", msg_id=2))
        assert _dispatched(adapter).metadata["topic_changed"] is False

    @pytest.mark.asyncio
    async def test_never_set_for_dms(self, make_adapter):
        adapter = make_adapter()
        for index in range(2):
            await adapter._handle_message(_dm_msg(f"m{index}", msg_id=index + 1))
        assert _dispatched(adapter).metadata["topic_changed"] is False


class TestDmSessionRotation:
    @pytest.mark.asyncio
    async def test_rekeys_chat_id_every_n_turns(self, make_adapter):
        adapter = make_adapter(ZULIP_DM_SESSION_TURN_LIMIT="2")
        chat_ids = []
        for index in range(5):
            await adapter._handle_message(_dm_msg(f"m{index}", msg_id=index + 1))
            chat_ids.append(_dispatched(adapter).source.chat_id)

        base = chat_ids[0]
        # epoch = (turn - 1) // limit, so the 3rd turn opens epoch 1 and the
        # 5th opens epoch 2. The base chat is never re-keyed on turn 1 or 2.
        assert chat_ids[1] == base
        assert chat_ids[2] == f"{base}:session:1"
        assert chat_ids[3] == f"{base}:session:1"
        assert chat_ids[4] == f"{base}:session:2"

    @pytest.mark.asyncio
    async def test_rotation_disabled_when_limit_is_zero(self, make_adapter):
        adapter = make_adapter(ZULIP_DM_SESSION_TURN_LIMIT="0")
        for index in range(4):
            await adapter._handle_message(_dm_msg(f"m{index}", msg_id=index + 1))
        assert ":session:" not in _dispatched(adapter).source.chat_id

    @pytest.mark.asyncio
    async def test_rotation_counter_is_keyed_on_the_base_chat(self, make_adapter):
        """The rotation counter keys on the un-rotated chat id, exactly once.

        If a rotated id became its own key, every turn would look like a fresh
        conversation and the rotation would never advance.
        """
        adapter = make_adapter(ZULIP_DM_SESSION_TURN_LIMIT="1")
        await adapter._handle_message(_dm_msg("one", msg_id=1))
        first = _dispatched(adapter).source.chat_id

        # limit=1 -> turn 1 is epoch 0, so this is the base id itself.
        assert list(adapter._dm_base_message_counts) == [first]
        assert adapter._dm_base_message_counts[first] == 1

        # Turn 2 does rotate, and must count on the SAME base key.
        await adapter._handle_message(_dm_msg("two", msg_id=2))
        second = _dispatched(adapter).source.chat_id

        assert second == f"{first}:session:1"
        assert list(adapter._dm_base_message_counts) == [first]
        assert adapter._dm_base_message_counts[first] == 2
