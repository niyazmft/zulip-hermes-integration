"""Integration tests: full message flow through adapter."""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


class TestFullFlow:
    @pytest.fixture
    def adapter(self, mock_platform_config, monkeypatch, tmp_path):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        calls = {"send_message": [], "set_typing_status": [], "add_reaction": []}

        class MockClient:
            def __init__(self, **kw):
                self.calls = calls
            def send_message(self, req):
                msg_id = len(self.calls["send_message"]) + 1000
                self.calls["send_message"].append(req)
                return {"result": "success", "id": msg_id}
            def set_typing_status(self, req):
                self.calls["set_typing_status"].append(req)
                return {"result": "success"}
            def add_reaction(self, req):
                self.calls["add_reaction"].append(req)
                return {"result": "success"}
            def remove_reaction(self, req):
                return {"result": "success"}
            def get_members(self):
                return {"result": "success", "members": []}
            def get_server_settings(self):
                return {"result": "success", "zulip_version": "8.0"}
            def get_profile(self):
                return {"result": "success", "full_name": "Test Bot"}
            def get_subscriptions(self):
                return {"result": "success", "subscriptions": []}
            def update_presence(self, req):
                return {"result": "success"}
            def update_message(self, req):
                return {"result": "success"}
            def update_message_flags(self, req):
                return {"result": "success"}
            def register(self, **kw):
                return {"result": "success", "queue_id": "q1", "last_event_id": 1}
            def get_events(self, **kw):
                return {"result": "success", "events": []}

        class MockZulipModule:
            Client = MockClient

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))

        from zulip.adapter import ZulipAdapter
        a = ZulipAdapter(mock_platform_config)
        a.email = "bot@zulip.com"
        a.handle_message = AsyncMock()
        return a

    @pytest.mark.asyncio
    async def test_message_arrives_and_dispatched(self, adapter):
        msg = {
            "id": 42,
            "type": "stream",
            "stream_id": 1,
            "subject": "general",
            "display_recipient": "test",
            "content": "hello @bot",
            "sender_email": "user@zulip.com",
            "sender_full_name": "User",
            "sender_id": 99,
        }
        await adapter._handle_message(msg)

        # Reaction attempted (best-effort, may fail on mock but called)
        assert len(adapter.client.calls["add_reaction"]) >= 1
        # handle_message called
        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_chunking_send(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_TEXT_CHUNK_LIMIT", "5")
        result = await adapter.send("dm:42", "one two three")
        assert result.success is True
        assert len(adapter.client.calls["send_message"]) > 1

    @pytest.mark.asyncio
    async def test_reconnect_with_queue(self, adapter):
        await adapter.connect()
        assert adapter._queue_mgr.get_queue() is not None
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_queue_expiration_recovery(self, adapter):
        # Simulate queue expiration on get_events
        call_count = 0
        original_get_events = adapter.client.get_events

        def fake_get_events(**kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"result": "error", "code": "BAD_EVENT_QUEUE_ID", "msg": "bad queue"}
            return {"result": "success", "events": []}

        adapter.client.get_events = fake_get_events
        await adapter.connect()
        # Give the loop a few cycles
        for _ in range(3):
            await asyncio.sleep(0.01)
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_topic_directive_end_to_end(self, adapter):
        result = await adapter.send("573423", "[[zulip_topic: New Topic]] Hello")
        assert result.success is True
        call = adapter.client.calls["send_message"][0]
        assert call["topic"] == "New Topic"
        assert call["content"] == "Hello"
