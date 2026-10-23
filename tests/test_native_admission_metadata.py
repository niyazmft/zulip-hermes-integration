"""Gate 0 of the inbound chain: the host's native admission metadata contract.

The host admits native input through a **closed** metadata contract
(``gateway.session_envelope._native_metadata``): only the WhatsApp adapter's
keys may cross, and any other adapter-stamped ``MessageEvent.metadata`` refuses
the whole message with ``invalid_params`` — before it is stored or dispatched.
Every Zulip message was refused once the host narrowed that contract.

So the adapter must keep its routing context *off* ``MessageEvent.metadata`` and
put the topic on the session source, which the host serializes across admission.
These tests pin the shape the host accepts, and that the per-topic route
identity survives without the metadata field:

* the dispatched event carries no adapter metadata (a stamp is an instant
  refusal in production, and no stub would notice)
* the adapter's own context is on ``_zulip_meta``, and the topic is on
  ``source.chat_topic``
* ``outbound.route_metadata`` rebuilds ``thread_id``/``topic`` from the source
  for an event restored after admission, so trace, audit and queue keys keep
  their per-topic identity instead of collapsing onto the chat
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from zulip import inbound_queue, outbound

# Streams only dispatch unmentioned traffic with the mention requirement off.
_DISPATCH_ALL = {"ZULIP_CHATMODE": "onmessage", "ZULIP_REQUIRE_MENTION": "false"}


class _FakeClient:
    """Minimal Zulip SDK stand-in for the inbound path."""

    def __init__(self, **kwargs):
        self.sent: list[dict] = []

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
        return {"result": "success", "messages": []}


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


def _stream_msg(content, *, stream_id=1, topic="general", msg_id=1, flags=()):
    return {
        "id": msg_id,
        "type": "stream",
        "stream_id": stream_id,
        "subject": topic,
        "display_recipient": "test",
        "content": content,
        "sender_email": "user@zulip.com",
        "sender_full_name": "User",
        "sender_id": 42,
        "flags": list(flags),
    }


def _dm_msg(content, *, msg_id=1):
    return {
        "id": msg_id,
        "type": "private",
        "content": content,
        "sender_email": "user@zulip.com",
        "sender_full_name": "User",
        "sender_id": 42,
    }


def _dispatched(adapter):
    return adapter.handle_message.call_args[0][0]


class TestDispatchedEventShape:
    @pytest.mark.asyncio
    async def test_stream_event_carries_no_adapter_metadata(self, make_adapter):
        adapter = make_adapter(**_DISPATCH_ALL)
        await adapter._handle_message(_stream_msg("hello", topic="release", stream_id=7))
        event = _dispatched(adapter)

        # The refusal clause is exactly this field: a non-empty dict that is not
        # WhatsApp metadata makes _native_metadata return None.
        assert not event.metadata

        # The same identity is kept, but off the host's wire contract.
        assert event._zulip_meta["topic"] == "release"
        assert event._zulip_meta["stream_id"] == 7
        assert event._zulip_meta["conversation_turn"] == 1
        # The topic also rides the source, which is serialized across admission.
        assert event.source.chat_topic == "release"

    @pytest.mark.asyncio
    async def test_dm_event_carries_no_adapter_metadata(self, make_adapter):
        adapter = make_adapter(**_DISPATCH_ALL)
        await adapter._handle_message(_dm_msg("hello"))
        event = _dispatched(adapter)

        assert not event.metadata
        assert event._zulip_meta["user_id"] == 42
        assert "recipient_ids" in event._zulip_meta


class TestSoftGateObservedContext:
    """The soft gate's "watching, not asked" tag must survive admission.

    ``ZULIP_SOFT_GATE`` dispatches every monitored stream message and used to
    tag the ones the bot was not addressed in via ``metadata["addressed"]``.
    That field is refused by native admission, so the tag rides
    ``channel_context`` — which the gateway prepends to the turn.
    """

    @pytest.mark.asyncio
    async def test_unaddressed_soft_gate_message_is_labelled(self, make_adapter):
        from zulip.inbound import OBSERVED_STREAM_CONTEXT

        adapter = make_adapter(ZULIP_SOFT_GATE="true", ZULIP_CHATMODE="oncall")
        await adapter._handle_message(_stream_msg("chatter", topic="release"))
        event = _dispatched(adapter)

        assert event.channel_context == OBSERVED_STREAM_CONTEXT
        # Not on the refused field, but the adapter still records it in-process.
        assert not event.metadata
        assert event._zulip_meta["addressed"] is False

    @pytest.mark.asyncio
    async def test_addressed_soft_gate_message_carries_no_label(self, make_adapter):
        adapter = make_adapter(ZULIP_SOFT_GATE="true", ZULIP_CHATMODE="oncall")
        await adapter._handle_message(_stream_msg("hello", flags=["mentioned"]))
        event = _dispatched(adapter)

        assert event.channel_context == ""
        assert event._zulip_meta["addressed"] is True

    @pytest.mark.asyncio
    async def test_no_label_with_the_soft_gate_off(self, make_adapter):
        adapter = make_adapter(**_DISPATCH_ALL)
        await adapter._handle_message(_stream_msg("hello"))
        event = _dispatched(adapter)

        assert event.channel_context == ""
        assert "addressed" not in event._zulip_meta

    @pytest.mark.asyncio
    async def test_dm_never_carries_the_label(self, make_adapter):
        adapter = make_adapter(ZULIP_SOFT_GATE="true")
        await adapter._handle_message(_dm_msg("hello"))
        event = _dispatched(adapter)

        assert event.channel_context == ""


class TestRouteMetadataAfterAdmission:
    def test_in_process_stamp_wins(self):
        from gateway.platforms.base import MessageEvent, MessageSource

        stamp = {"topic": "release", "stream_id": 7, "conversation_turn": 2}
        event = MessageEvent(
            text="hello",
            source=MessageSource(chat_id="7", chat_topic="release", thread_id="conv-1"),
        )
        event._zulip_meta = stamp

        assert outbound.route_metadata(event) == stamp

    def test_rebuilt_from_the_source_when_the_stamp_is_gone(self):
        """A restored event has no stamp and no metadata — the source carries it."""
        from gateway.platforms.base import MessageEvent, MessageSource

        event = MessageEvent(
            text="hello",
            source=MessageSource(
                chat_id="7", chat_topic="release", thread_id="conv-1"
            ),
        )

        assert outbound.route_metadata(event) == {
            "thread_id": "conv-1",
            "topic": "release",
        }

    def test_none_when_the_source_has_no_identity(self):
        from gateway.platforms.base import MessageEvent, MessageSource

        event = MessageEvent(text="hello", source=MessageSource(chat_id="7"))

        assert outbound.route_metadata(event) is None


class TestRouteKeySurvivesAdmission:
    def test_event_route_uses_the_rebuilt_metadata(self):
        from gateway.platforms.base import MessageEvent, MessageSource

        event = MessageEvent(
            text="hello",
            source=MessageSource(
                chat_id="7", chat_topic="release", thread_id="conv-1"
            ),
        )

        chat_id, metadata = inbound_queue.event_route(event)

        assert chat_id == "7"
        assert metadata == {"thread_id": "conv-1", "topic": "release"}
        assert inbound_queue.route_key(chat_id, metadata) == "7\x00conv-1"

    def test_two_topics_do_not_collapse_onto_one_key(self):
        from gateway.platforms.base import MessageEvent, MessageSource

        def key(topic, thread_id):
            event = MessageEvent(
                text="hello",
                source=MessageSource(
                    chat_id="7", chat_topic=topic, thread_id=thread_id
                ),
            )
            chat_id, metadata = inbound_queue.event_route(event)
            return inbound_queue.route_key(chat_id, metadata)

        assert key("release", "conv-1") != key("incident", "conv-2")
