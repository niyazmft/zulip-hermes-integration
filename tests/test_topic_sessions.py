"""Tests for ZULIP_TOPIC_SESSIONS — per-topic conversation scoping."""

from unittest.mock import AsyncMock

import pytest

from zulip.adapter import _topic_sessions_enabled


class TestTopicSessionsFlag:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("ZULIP_TOPIC_SESSIONS", raising=False)
        assert _topic_sessions_enabled() is False

    @pytest.mark.parametrize("value", ["true", "True", "1", "yes", "on"])
    def test_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", value)
        assert _topic_sessions_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "  "])
    def test_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", value)
        assert _topic_sessions_enabled() is False


class TestTopicScoping:
    @pytest.fixture
    def adapter(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, email=None, api_key=None, site=None):
                    pass

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter
        a = ZulipAdapter(mock_platform_config)
        a.email = "bot@zulip.com"
        a.handle_message = AsyncMock()
        return a

    def _stream_msg(self, topic: str) -> dict:
        return {
            "id": 1,
            "type": "stream",
            "stream_id": 7,
            "subject": topic,
            "display_recipient": "engineering",
            "content": "hello",
            "sender_email": "user@zulip.com",
            "sender_full_name": "User",
            "sender_id": 42,
        }

    def _dm(self) -> dict:
        return {
            "id": 2,
            "type": "private",
            "content": "hello",
            "sender_email": "user@zulip.com",
            "sender_full_name": "User",
            "sender_id": 42,
        }

    def _group_dm(self) -> dict:
        """A private message carrying the whole participant list, as Zulip sends."""
        return {
            **self._dm(),
            "display_recipient": [
                {"id": 7, "email": "bot@zulip.com", "full_name": "Test Bot"},
                {"id": 42, "email": "user@zulip.com", "full_name": "User"},
                {"id": 99, "email": "other@zulip.com", "full_name": "Other"},
            ],
        }

    @pytest.mark.asyncio
    async def test_topic_not_used_for_session_by_default(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.delenv("ZULIP_TOPIC_SESSIONS", raising=False)
        await adapter._handle_message(self._stream_msg("deploys"))
        source = adapter.handle_message.call_args[0][0].source
        assert not getattr(source, "thread_id", "")

    @pytest.mark.asyncio
    async def test_topic_scopes_session_when_enabled(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", "true")
        await adapter._handle_message(self._stream_msg("deploys"))
        source = adapter.handle_message.call_args[0][0].source
        assert source.thread_id == "deploys"

    @pytest.mark.asyncio
    async def test_different_topics_get_different_thread_ids(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", "true")
        await adapter._handle_message(self._stream_msg("deploys"))
        first = adapter.handle_message.call_args[0][0].source
        await adapter._handle_message(self._stream_msg("incidents"))
        second = adapter.handle_message.call_args[0][0].source
        assert first.thread_id == "deploys"
        assert second.thread_id == "incidents"
        # Same stream, so the chat_id is shared — only the thread differs.
        assert first.chat_id == second.chat_id

    @pytest.mark.asyncio
    async def test_empty_topic_does_not_set_thread_id(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", "true")
        await adapter._handle_message(self._stream_msg(""))
        source = adapter.handle_message.call_args[0][0].source
        assert not getattr(source, "thread_id", "")

    @pytest.mark.asyncio
    async def test_dms_are_unaffected(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", "true")
        await adapter._handle_message(self._dm())
        source = adapter.handle_message.call_args[0][0].source
        assert source.chat_type == "dm"
        assert not getattr(source, "thread_id", "")

    # --- Issue #154: a group DM's address is its whole recipient set ---

    @pytest.mark.asyncio
    async def test_group_dm_uses_the_full_recipient_set_as_chat_id(self, adapter):
        await adapter._handle_message(self._group_dm())
        event = adapter.handle_message.call_args[0][0]
        assert event.source.chat_type == "dm"
        assert event.source.chat_id == "dm:7,42,99"
        assert event.metadata["recipient_ids"] == [7, 42, 99]

    @pytest.mark.asyncio
    async def test_dm_without_recipient_list_falls_back_to_sender_id(self, adapter):
        """Events that omit display_recipient keep the pre-existing chat id."""
        await adapter._handle_message(self._dm())
        assert adapter.handle_message.call_args[0][0].source.chat_id == "dm:42"

    @pytest.mark.asyncio
    async def test_group_dm_chat_id_is_stable_across_delivery_order(self, adapter):
        """Zulip does not promise an order, so the address must not depend on it."""
        flipped = {
            **self._group_dm(),
            "display_recipient": list(reversed(self._group_dm()["display_recipient"])),
        }
        await adapter._handle_message(flipped)
        assert adapter.handle_message.call_args[0][0].source.chat_id == "dm:7,42,99"
