"""Tests for outbound topic routing with ZULIP_TOPIC_SESSIONS.

With topic sessions on, each topic in a stream is its own session. The
gateway core routes each session's outbound traffic with the session
origin's topic in ``metadata["thread_id"]`` (see
gateway.platforms.base._thread_metadata_for_source). The adapter must
honour that routing metadata — replies, typing indicators, and
exec-approval prompts all belong to their own topic — and only fall back
to the per-stream "last seen topic" cache when a send carries no routing
metadata (e.g. topic sessions off, metadata-less callers).

Regression: the adapter used to resolve every outbound stream topic from
a single per-stream cache slot overwritten by every incoming message, so
a reply generated for TopicA landed in TopicB whenever TopicB saw traffic
while TopicA's turn was still running.
"""

import inspect
from unittest.mock import AsyncMock

import pytest

import zulip.adapter as adapter_module
from tests.conftest import MockZulipClient


class RecordingTypingClient(MockZulipClient):
    """MockZulipClient that records set_typing_status calls."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._typing_calls = []

    def set_typing_status(self, request):
        self._typing_calls.append(request)
        return {"result": "success"}


@pytest.fixture
def adapter(mock_platform_config, monkeypatch):
    """ZulipAdapter with the real SDK swapped for a recording mock client."""
    monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                self._client = RecordingTypingClient(**kwargs)

            def __getattr__(self, name):
                return getattr(self._client, name)

    monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(mock_platform_config)
    a.email = "bot@zulip.com"
    a.handle_message = AsyncMock()
    return a


def _stream_msg(topic: str, msg_id: int = 1) -> dict:
    """Inbound stream message for cache-stomping (mirrors test_topic_sessions)."""
    return {
        "id": msg_id,
        "type": "stream",
        "stream_id": 7,
        "subject": topic,
        "display_recipient": "engineering",
        "content": "hello",
        "sender_email": "user@zulip.com",
        "sender_full_name": "User",
        "sender_id": 42,
    }


class TestReplyTopicRouting:
    """adapter.send() must route stream replies by the session's topic."""

    @pytest.mark.asyncio
    async def test_reply_routes_by_thread_id_not_last_seen_topic(self, adapter, monkeypatch):
        """THE regression: TopicA's reply must land in TopicA even though
        TopicB saw traffic after TopicA's message."""
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", "true")
        # TopicA message → per-topic session + cache slot
        await adapter._handle_message(_stream_msg("TopicA", msg_id=1))
        # TopicB message → cache slot now says "TopicB"
        await adapter._handle_message(_stream_msg("TopicB", msg_id=2))

        # TopicA's turn finishes now; the core routes by the session origin.
        result = await adapter.send("7", "Task done", metadata={"thread_id": "TopicA"})

        assert result.success is True
        call = adapter.client._client._sent_messages[0]
        assert call["type"] == "stream"
        assert call["topic"] == "TopicA"

    @pytest.mark.asyncio
    async def test_reply_falls_back_to_cache_without_metadata(self, adapter, monkeypatch):
        """No routing metadata (topic sessions off / metadata-less caller):
        the last-seen-topic cache keeps threading replies as before."""
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.delenv("ZULIP_TOPIC_SESSIONS", raising=False)
        await adapter._handle_message(_stream_msg("deploys"))

        result = await adapter.send("7", "cache-threaded reply")

        assert result.success is True
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "deploys"

    @pytest.mark.asyncio
    async def test_reply_cache_never_used_over_thread_id(self, adapter):
        """Routing metadata wins even when the cache holds a different topic."""
        adapter._topic_cache["7"] = "Stale Topic"
        await adapter.send("7", "routed reply", metadata={"thread_id": "Fresh Topic"})
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "Fresh Topic"

    @pytest.mark.asyncio
    async def test_topic_directive_beats_metadata(self, adapter):
        """An explicit inline topic directive is user intent and wins."""
        await adapter.send(
            "7", "[[zulip_topic: design-review-v2]] moved", metadata={"thread_id": "TopicA"}
        )
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "design-review-v2"
        assert "design-review-v2" not in call["content"]

    @pytest.mark.asyncio
    async def test_legacy_topic_metadata_key_still_honored(self, adapter):
        """metadata["topic"] (parity key with the adapter's own event
        metadata) still routes when thread_id is absent."""
        await adapter.send("7", "legacy routed", metadata={"topic": "Legacy Topic"})
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "Legacy Topic"

    @pytest.mark.asyncio
    async def test_blank_thread_id_falls_back_to_cache(self, adapter):
        """An empty/whitespace thread_id is no routing info: use the cache."""
        adapter._topic_cache["7"] = "Cached Topic"
        await adapter.send("7", "blank thread id", metadata={"thread_id": "  "})
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "Cached Topic"

    @pytest.mark.asyncio
    async def test_unknown_stream_no_topic_anywhere_gets_general(self, adapter):
        """No metadata, no cache entry: keep the historic 'general' default."""
        await adapter.send("999", "who are you")
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "general"

    @pytest.mark.asyncio
    async def test_chunked_reply_keeps_topic(self, adapter, monkeypatch):
        """Chunked sends all land in the routed topic."""
        monkeypatch.setenv("ZULIP_TEXT_CHUNK_LIMIT", "10")
        await adapter.send("7", "chunk one and chunk two", metadata={"thread_id": "TopicA"})
        calls = adapter.client._client._sent_messages
        assert len(calls) > 1
        assert all(c["topic"] == "TopicA" for c in calls)

    @pytest.mark.asyncio
    async def test_dm_sends_ignore_topic_routing(self, adapter):
        """DMs have no topic; rotated DM chat ids still send as private."""
        await adapter.send("dm:42:session:2", "dm reply", metadata={"thread_id": "TopicA"})
        call = adapter.client._client._sent_messages[0]
        assert {k: v for k, v in call.items() if k != "id"} == {
            "type": "private", "to": [42], "content": "dm reply",
        }


class TestTypingTopicRouting:
    """Typing indicators must show in the session's topic, not the last-seen one."""

    @pytest.mark.asyncio
    async def test_send_typing_routes_by_metadata(self, adapter):
        adapter._topic_cache["7"] = "TopicB"  # user is chatting in TopicB
        await adapter.send_typing("7", metadata={"thread_id": "TopicA"})
        call = adapter.client._client._typing_calls[-1]
        assert call == {"op": "start", "type": "stream", "stream_id": 7, "topic": "TopicA"}

    @pytest.mark.asyncio
    async def test_send_typing_falls_back_to_cache(self, adapter):
        adapter._topic_cache["7"] = "TopicB"
        await adapter.send_typing("7")
        call = adapter.client._client._typing_calls[-1]
        assert call["topic"] == "TopicB"

    @pytest.mark.asyncio
    async def test_send_typing_rotated_dm_still_direct(self, adapter):
        await adapter.send_typing("dm:42:session:2", metadata={"thread_id": "TopicA"})
        call = adapter.client._client._typing_calls[-1]
        assert call == {"op": "start", "type": "direct", "to": [42]}

    @pytest.mark.asyncio
    async def test_stop_typing_accepts_metadata_kwarg(self):
        """Introspection contract with the gateway base: stop_typing must
        accept ``metadata`` so _stop_typing_with_metadata forwards it."""
        from zulip.adapter import ZulipAdapter

        assert "metadata" in inspect.signature(ZulipAdapter.stop_typing).parameters

    @pytest.mark.asyncio
    async def test_stop_typing_with_metadata_routes_by_thread_id(self, adapter):
        """The gateway base hook (stop path) routes by the run's topic."""
        adapter._topic_cache["7"] = "TopicB"
        await adapter._stop_typing_with_metadata("7", {"thread_id": "TopicA"})
        call = adapter.client._client._typing_calls[-1]
        assert call == {"op": "stop", "type": "stream", "stream_id": 7, "topic": "TopicA"}

    @pytest.mark.asyncio
    async def test_stop_typing_without_metadata_still_works(self, adapter):
        """Legacy positional call (run_turn fallback) keeps working."""
        adapter._topic_cache["7"] = "TopicB"
        await adapter.stop_typing("7")
        call = adapter.client._client._typing_calls[-1]
        assert call["op"] == "stop"
        assert call["topic"] == "TopicB"


class TestExecApprovalTopicRouting:
    """Exec-approval prompts (zform buttons) must render in the session's topic."""

    def _prompt(self, metadata):
        from gateway.platforms.base import ExecApprovalPrompt

        return ExecApprovalPrompt(
            chat_id="7",
            session_key="agent:main:zulip:stream:7:TopicA",
            text="⚠️ Command Approval Required",
            actions=[("Allow Once", "once", "primary"), ("Deny", "deny", "danger")],
            command="ls -la",
            description="test command",
            smart_denied=False,
            metadata=metadata,
        )

    @pytest.mark.asyncio
    async def test_prompt_routes_by_thread_id(self, adapter):
        adapter._topic_cache["7"] = "TopicB"  # user moved on to TopicB
        result = await adapter._send_exec_approval_prompt(
            self._prompt(metadata={"thread_id": "TopicA"})
        )
        assert result.success is True
        calls = adapter.client._client._sent_messages
        assert len(calls) == 2  # context text + button widget
        assert all(c["topic"] == "TopicA" for c in calls)

    @pytest.mark.asyncio
    async def test_prompt_falls_back_to_cache_without_metadata(self, adapter):
        adapter._topic_cache["7"] = "TopicB"
        result = await adapter._send_exec_approval_prompt(self._prompt(metadata=None))
        assert result.success is True
        calls = adapter.client._client._sent_messages
        assert all(c["topic"] == "TopicB" for c in calls)
