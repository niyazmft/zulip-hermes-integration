"""Conversation-scoped observation under the recommended profile (#217).

The recommended profile's memory is *conversation-scoped*: a topic is remembered
only once the bot was addressed in it. Before this, ``ZULIP_OBSERVE_GROUP``
recorded every non-addressed stream message regardless of whether the bot had
ever been spoken to in that topic — room-scoped memory, which accumulates rooms
nobody asked it to join.

The three acceptance criteria from the issue are the three tests named
``test_..._acceptance`` below. The rest pin the surrounding contract: the
tracker is bounded, the gate is profile-scoped, and nothing else about
observation changed.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from zulip import history
from zulip.history import AddressedTopicTracker

A_TOPIC = ("engineering", 42, "deploy")
B_TOPIC = ("engineering", 42, "lunch")


class _FakeClient:
    """Minimal Zulip SDK stand-in for adapter construction.

    No raise-on-unknown-attribute guard: the adapter *probes* the client while
    wiring itself up (``hasattr(client, "ensure_session")``), so raising there
    breaks construction rather than catching a stray observation call.
    """

    def __init__(self, **kwargs):
        self.calls: list[str] = []
        self.get_messages_calls: list[dict] = []

    def send_message(self, request):
        self.calls.append("send_message")
        return {"result": "success", "id": 1}

    def get_messages(self, request):
        self.calls.append("get_messages")
        self.get_messages_calls.append(request)
        return {"result": "success", "messages": []}

    def update_message_flags(self, request):
        self.calls.append("update_message_flags")
        return {"result": "success"}

    def add_reaction(self, request):
        self.calls.append("add_reaction")
        return {"result": "success"}

    def remove_reaction(self, request):
        self.calls.append("remove_reaction")
        return {"result": "success"}

    def set_typing_status(self, request):
        self.calls.append("set_typing_status")
        return {"result": "success"}


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
        # Reset first: monkeypatch accumulates within a test, so a second call in
        # the same test would otherwise inherit the first call's environment and
        # every "fresh adapter" assumption here would be wrong.
        for key in ("ZULIP_OBSERVE_GROUP", "ZULIP_PROFILE", "ZULIP_STREAMS"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)
        adapter.email = "bot@test.zulipchat.com"
        adapter.bot_full_name = "Test Bot"
        adapter.handle_message = AsyncMock()
        return adapter

    return _make


def _msg(content, *, stream="engineering", stream_id=42, topic="deploy", sender="Alice"):
    return {
        "id": 7,
        "type": "stream",
        "stream_id": stream_id,
        "subject": topic,
        "display_recipient": stream,
        "content": content,
        "sender_email": "alice@zulip.com",
        "sender_full_name": sender,
        "sender_id": 9,
        "flags": [],
    }


async def _address(adapter, stream="engineering", stream_id=42, topic="deploy"):
    """One turn the bot was addressed in, which is what marks a topic."""
    return await adapter._quoted_topic_history(
        stream, stream_id, topic, content="what do you think?", addressed=True
    )


# ---------------------------------------------------------------- the tracker


def test_tracker_records_and_reports():
    tracker = AddressedTopicTracker()
    assert tracker.was_addressed(42, "deploy") is False
    tracker.mark(42, "deploy")
    assert tracker.was_addressed(42, "deploy") is True
    # Keyed exactly like the observed buffer, so the two cannot disagree.
    assert tracker.was_addressed("42", "deploy") is True
    assert tracker.was_addressed(42, "") is False


def test_tracker_is_bounded_and_evicts_least_recently_used():
    tracker = AddressedTopicTracker(max_topics=2)
    tracker.mark(1, "a")
    tracker.mark(1, "b")
    tracker.mark(1, "a")  # refresh "a"
    tracker.mark(1, "c")  # evicts "b"

    assert len(tracker) == 2
    assert tracker.was_addressed(1, "a") is True
    assert tracker.was_addressed(1, "c") is True
    assert tracker.was_addressed(1, "b") is False


def test_tracker_keeps_streams_apart():
    tracker = AddressedTopicTracker()
    tracker.mark(1, "general")
    assert tracker.was_addressed(2, "general") is False


# ------------------------------------------------------------ acceptance (1)


@pytest.mark.asyncio
async def test_untouched_topic_yields_no_memory_acceptance(make_adapter):
    """Addressing the bot in A must not make it remember untouched topic B."""
    adapter = make_adapter(ZULIP_OBSERVE_GROUP="true", ZULIP_PROFILE="recommended")
    assert adapter._conversation_scoped_observe is True

    await _address(adapter, topic="deploy")

    # Coworkers talk in B, a topic the bot was never addressed in.
    adapter._observe_stream_message(_msg("anyone up for lunch?", topic="lunch"), "anyone up for lunch?")

    assert adapter._observed_context.render(42, "lunch") == ""
    # ...and asking the bot in B quotes nothing from B.
    quoted = await _address(adapter, topic="lunch")
    assert quoted == ""


# ------------------------------------------------------------ acceptance (2)


@pytest.mark.asyncio
async def test_addressed_topic_keeps_its_chatter_acceptance(make_adapter):
    """Chatter in a topic the bot *was* addressed in is quoted back."""
    adapter = make_adapter(ZULIP_OBSERVE_GROUP="true", ZULIP_PROFILE="recommended")

    await _address(adapter, topic="deploy")

    adapter._observe_stream_message(_msg("the build is green", topic="deploy"), "the build is green")
    adapter._observe_stream_message(_msg("shipping now", topic="deploy"), "shipping now")

    quoted = await _address(adapter, topic="deploy")
    assert history.OBSERVED_HISTORY_LABEL in quoted
    assert "the build is green" in quoted
    assert "shipping now" in quoted


# ------------------------------------------------------------ acceptance (3)


@pytest.mark.asyncio
async def test_without_the_profile_observation_is_unchanged_acceptance(make_adapter):
    """No marker: today's room-scoped behaviour, untouched."""
    adapter = make_adapter(ZULIP_OBSERVE_GROUP="true")
    assert adapter._conversation_scoped_observe is False

    adapter._observe_stream_message(_msg("anyone up for lunch?", topic="lunch"), "anyone up for lunch?")

    assert adapter._observed_context.render(42, "lunch") != ""
    quoted = await _address(adapter, topic="lunch")
    assert "anyone up for lunch?" in quoted


# ----------------------------------------------------------- the gate itself


def test_gate_needs_both_the_profile_and_observe(make_adapter):
    # No profile: today's room-scoped behaviour, whatever observe says.
    assert make_adapter()._conversation_scoped_observe is False
    assert (
        make_adapter(ZULIP_OBSERVE_GROUP="true")._conversation_scoped_observe is False
    )
    # Profile on: the preset itself turns observe on, so scoping is active.
    assert (
        make_adapter(ZULIP_PROFILE="recommended")._conversation_scoped_observe is True
    )
    # An explicit override beats the preset, and then there is nothing to scope.
    assert (
        make_adapter(
            ZULIP_PROFILE="recommended", ZULIP_OBSERVE_GROUP="false"
        )._conversation_scoped_observe
        is False
    )
    assert (
        make_adapter(
            ZULIP_OBSERVE_GROUP="true", ZULIP_PROFILE="recommended"
        )._conversation_scoped_observe
        is True
    )


def test_observation_is_off_entirely_when_not_enabled(make_adapter):
    # The preset enables observe, so turn it off explicitly to test the off state.
    adapter = make_adapter(ZULIP_PROFILE="recommended", ZULIP_OBSERVE_GROUP="false")
    adapter._observe_stream_message(_msg("hello", topic="deploy"), "hello")
    assert adapter._observed_context.render(42, "deploy") == ""


def test_self_messages_are_still_never_observed(make_adapter):
    adapter = make_adapter(ZULIP_OBSERVE_GROUP="true", ZULIP_PROFILE="recommended")
    adapter._addressed_topics.mark(42, "deploy")
    self_msg = _msg("beep", topic="deploy", sender="Test Bot")
    self_msg["sender_email"] = "bot@test.zulipchat.com"

    adapter._observe_stream_message(self_msg, "beep")

    assert adapter._observed_context.render(42, "deploy") == ""


def test_unmonitored_streams_are_still_never_observed(make_adapter):
    adapter = make_adapter(
        ZULIP_OBSERVE_GROUP="true",
        ZULIP_PROFILE="recommended",
        ZULIP_STREAMS="engineering",
    )
    adapter._addressed_topics.mark(42, "deploy")

    adapter._observe_stream_message(
        _msg("hello", stream="other", stream_id=99, topic="deploy"), "hello"
    )

    assert adapter._observed_context.render(99, "deploy") == ""


@pytest.mark.asyncio
async def test_addressing_in_one_stream_does_not_engage_another(make_adapter):
    adapter = make_adapter(ZULIP_OBSERVE_GROUP="true", ZULIP_PROFILE="recommended")

    await _address(adapter, stream="engineering", stream_id=42, topic="general")
    adapter._observe_stream_message(
        _msg("hi", stream="random", stream_id=77, topic="general"), "hi"
    )

    assert adapter._observed_context.render(77, "general") == ""
    assert adapter._addressed_topics.was_addressed(77, "general") is False
    # ...while the stream that *was* addressed is tracked, for contrast.
    assert adapter._addressed_topics.was_addressed(42, "general") is True
