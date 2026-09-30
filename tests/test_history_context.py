"""Tests for bounded history-aware context (issue #148).

``ZULIP_HISTORY_MODE`` selects ``off`` (default), ``on-demand`` or ``always``.
A harvest is capped on messages, age and characters — newest lines win — and is
best-effort: a slow or failing Zulip client is logged and dropped inside a
short timeout so it can never delay or fail a dispatch. DMs are never
harvested across.
"""

import time
from unittest.mock import AsyncMock

import pytest

from zulip.adapter import (
    FETCHED_HISTORY_LABEL,
    _history_intent_matches,
    _render_history_block,
    _resolve_history_mode,
    _resolve_int_setting,
)


class _FakeClient:
    """Minimal Zulip SDK stand-in. ``get_messages`` behaviour is injectable."""

    def __init__(self, **kwargs):
        self.sent: list[dict] = []
        self.get_messages_calls: list[dict] = []
        self.get_messages_result = {"result": "success", "messages": []}
        self.get_messages_fn = None

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
        if self.get_messages_fn is not None:
            return self.get_messages_fn(request)
        return self.get_messages_result


@pytest.fixture
def make_adapter(mock_platform_config, monkeypatch):
    def _make(**env):
        import zulip.adapter as adapter_module

        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = _FakeClient(**kwargs)

                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)
        adapter.email = "bot@test.zulipchat.com"
        adapter.bot_full_name = "Test Bot"
        adapter.handle_message = AsyncMock()
        return adapter

    return _make


def _stream_msg(content, *, stream_id=1, topic="general", stream="test", msg_id=1):
    return {
        "id": msg_id,
        "type": "stream",
        "stream_id": stream_id,
        "subject": topic,
        "display_recipient": stream,
        "content": content,
        "sender_email": "user@zulip.com",
        "sender_full_name": "User",
        "sender_id": 42,
        "flags": [],
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


def _topic_message(text, *, msg_id, age_seconds=60, sender="Alice"):
    return {
        "id": msg_id,
        "content": text,
        "sender_full_name": sender,
        "timestamp": time.time() - age_seconds,
    }


class TestHistoryModeResolution:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("ZULIP_HISTORY_MODE", raising=False)
        assert _resolve_history_mode() == "off"

    @pytest.mark.parametrize("mode", ["off", "on-demand", "always"])
    def test_valid_modes(self, monkeypatch, mode):
        monkeypatch.setenv("ZULIP_HISTORY_MODE", mode)
        assert _resolve_history_mode() == mode

    def test_invalid_mode_falls_back_to_off(self, monkeypatch, caplog):
        monkeypatch.setenv("ZULIP_HISTORY_MODE", "sometimes")
        with caplog.at_level("WARNING"):
            assert _resolve_history_mode() == "off"
        assert "sometimes" in caplog.text

    def test_int_setting_default_and_override(self, monkeypatch):
        monkeypatch.delenv("ZULIP_HISTORY_MAX_MESSAGES", raising=False)
        assert _resolve_int_setting("ZULIP_HISTORY_MAX_MESSAGES", 8) == 8
        monkeypatch.setenv("ZULIP_HISTORY_MAX_MESSAGES", "3")
        assert _resolve_int_setting("ZULIP_HISTORY_MAX_MESSAGES", 8) == 3

    def test_int_setting_rejects_non_integer(self, monkeypatch, caplog):
        monkeypatch.setenv("ZULIP_HISTORY_MAX_MESSAGES", "many")
        with caplog.at_level("WARNING"):
            assert _resolve_int_setting("ZULIP_HISTORY_MAX_MESSAGES", 8) == 8


class TestHistoryIntent:
    @pytest.mark.parametrize(
        "text",
        [
            "have we seen this error before?",
            "did we decide anything about the retry budget?",
            "what did we decide last week?",
            "do we already know the answer?",
            "as we discussed, the deploy is manual",
        ],
    )
    def test_matches_history_questions(self, text):
        assert _history_intent_matches(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "please deploy the new build",
            "the tests are green",
            "can you review this PR?",
            "add a health check endpoint",
        ],
    )
    def test_ignores_ordinary_requests(self, text):
        assert _history_intent_matches(text) is False

    def test_empty_text_never_matches(self):
        assert _history_intent_matches("") is False


class TestRenderHistoryBlock:
    def test_selects_newest_lines(self):
        messages = [_topic_message(f"line {i}", msg_id=i) for i in range(1, 6)]
        block = _render_history_block(
            messages, max_messages=2, window_hours=72, max_chars=4000
        )
        assert block.splitlines() == ["[Alice] line 4", "[Alice] line 5"]

    def test_drops_messages_older_than_the_window(self):
        now = time.time()
        messages = [
            {"id": 1, "content": "ancient", "sender_full_name": "A", "timestamp": now - 100 * 3600},
            {"id": 2, "content": "recent", "sender_full_name": "A", "timestamp": now - 3600},
        ]
        block = _render_history_block(
            messages, max_messages=8, window_hours=72, max_chars=4000
        )
        assert block == "[A] recent"

    def test_character_cap_keeps_newest_lines(self):
        messages = [
            {"id": 1, "content": "z" * 50, "sender_full_name": "A", "timestamp": time.time()},
            {"id": 2, "content": "newest", "sender_full_name": "A", "timestamp": time.time()},
        ]
        block = _render_history_block(
            messages, max_messages=8, window_hours=72, max_chars=20
        )
        assert "newest" in block
        assert "z" * 50 not in block
        assert len(block) <= 20

    def test_orders_by_message_id(self):
        messages = [
            {"id": 2, "content": "second", "sender_full_name": "A", "timestamp": time.time()},
            {"id": 1, "content": "first", "sender_full_name": "A", "timestamp": time.time()},
        ]
        block = _render_history_block(
            messages, max_messages=8, window_hours=72, max_chars=4000
        )
        assert block.splitlines() == ["[A] first", "[A] second"]


class TestHistoryDispatch:
    @pytest.mark.asyncio
    async def test_off_adds_no_round_trip(self, make_adapter):
        adapter = make_adapter()
        await adapter._handle_message(_stream_msg("hello"))
        assert adapter.client._client.get_messages_calls == []
        event = adapter.handle_message.call_args[0][0]
        assert FETCHED_HISTORY_LABEL not in event.text

    @pytest.mark.asyncio
    async def test_always_harvests_and_labels_the_block(self, make_adapter):
        adapter = make_adapter(ZULIP_HISTORY_MODE="always")
        adapter.client._client.get_messages_result = {
            "result": "success",
            "messages": [_topic_message("we decided blue", msg_id=7)],
        }
        await adapter._handle_message(_stream_msg("what colour?"))
        calls = adapter.client._client.get_messages_calls
        assert calls[0]["narrow"] == [
            {"operator": "stream", "operand": "test"},
            {"operator": "topic", "operand": "general"},
        ]
        event = adapter.handle_message.call_args[0][0]
        assert event.text.startswith(FETCHED_HISTORY_LABEL)
        assert "we decided blue" in event.text
        assert event.text.endswith("what colour?")

    @pytest.mark.asyncio
    async def test_on_demand_harvests_for_history_question(self, make_adapter):
        adapter = make_adapter(ZULIP_HISTORY_MODE="on-demand")
        await adapter._handle_message(
            _stream_msg("have we seen this error before?")
        )
        assert len(adapter.client._client.get_messages_calls) == 1

    @pytest.mark.asyncio
    async def test_on_demand_skips_ordinary_message(self, make_adapter):
        adapter = make_adapter(ZULIP_HISTORY_MODE="on-demand")
        await adapter._handle_message(_stream_msg("please deploy the build"))
        assert adapter.client._client.get_messages_calls == []
        event = adapter.handle_message.call_args[0][0]
        assert event.text == "please deploy the build"

    @pytest.mark.asyncio
    async def test_message_cap_keeps_newest_lines(self, make_adapter):
        adapter = make_adapter(
            ZULIP_HISTORY_MODE="always", ZULIP_HISTORY_MAX_MESSAGES="1"
        )
        adapter.client._client.get_messages_result = {
            "result": "success",
            "messages": [
                _topic_message("older", msg_id=1, sender="Bob"),
                _topic_message("newest", msg_id=2, sender="Bob"),
            ],
        }
        await adapter._handle_message(_stream_msg("hello"))
        event = adapter.handle_message.call_args[0][0]
        assert "newest" in event.text
        assert "older" not in event.text

    @pytest.mark.asyncio
    async def test_dms_are_never_harvested(self, make_adapter):
        adapter = make_adapter(ZULIP_HISTORY_MODE="always")
        await adapter._handle_message(_dm_msg("have we seen this before?"))
        assert adapter.client._client.get_messages_calls == []
        event = adapter.handle_message.call_args[0][0]
        assert FETCHED_HISTORY_LABEL not in event.text

    @pytest.mark.asyncio
    async def test_slash_commands_are_not_harvested(self, make_adapter):
        adapter = make_adapter(ZULIP_HISTORY_MODE="always")
        await adapter._handle_message(_stream_msg("/help"))
        assert adapter.client._client.get_messages_calls == []


class TestHistoryBestEffort:
    @pytest.mark.asyncio
    async def test_slow_client_cannot_delay_dispatch(self, make_adapter, monkeypatch):
        import zulip.adapter as adapter_module

        monkeypatch.setattr(adapter_module, "HISTORY_FETCH_TIMEOUT", 0.05)
        adapter = make_adapter(ZULIP_HISTORY_MODE="always")

        def slow(request):
            time.sleep(0.3)
            return {"result": "success", "messages": []}

        adapter.client._client.get_messages_fn = slow
        started = time.monotonic()
        await adapter._handle_message(_stream_msg("hello"))
        elapsed = time.monotonic() - started
        adapter.handle_message.assert_called_once()
        assert elapsed < 0.25

    @pytest.mark.asyncio
    async def test_failing_client_cannot_fail_dispatch(self, make_adapter):
        adapter = make_adapter(ZULIP_HISTORY_MODE="always")

        def boom(request):
            raise RuntimeError("zulip is down")

        adapter.client._client.get_messages_fn = boom
        await adapter._handle_message(_stream_msg("hello"))
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert event.text == "hello"

    @pytest.mark.asyncio
    async def test_error_response_is_dropped(self, make_adapter):
        adapter = make_adapter(ZULIP_HISTORY_MODE="always")
        adapter.client._client.get_messages_result = {
            "result": "error",
            "msg": "nope",
        }
        await adapter._handle_message(_stream_msg("hello"))
        adapter.handle_message.assert_called_once()
        event = adapter.handle_message.call_args[0][0]
        assert FETCHED_HISTORY_LABEL not in event.text
