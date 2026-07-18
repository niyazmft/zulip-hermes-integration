"""Tests for adapter.py send() chunking and topic directives."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from zulip.adapter import _resolve_chunk_config


class TestResolveChunkConfig:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("ZULIP_TEXT_CHUNK_LIMIT", raising=False)
        monkeypatch.delenv("ZULIP_CHUNK_MODE", raising=False)
        limit, mode = _resolve_chunk_config()
        assert limit == 10000
        assert mode == "length"

    def test_custom_values(self, monkeypatch):
        monkeypatch.setenv("ZULIP_TEXT_CHUNK_LIMIT", "4000")
        monkeypatch.setenv("ZULIP_CHUNK_MODE", "newline")
        limit, mode = _resolve_chunk_config()
        assert limit == 4000
        assert mode == "newline"

    def test_invalid_mode_fallback(self, monkeypatch):
        monkeypatch.setenv("ZULIP_CHUNK_MODE", "invalid")
        limit, mode = _resolve_chunk_config()
        assert mode == "length"


class TestSendChunking:
    @pytest.fixture
    def adapter(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._calls = []
                def send_message(self, request):
                    msg_id = len(self._calls) + 100
                    self._calls.append(request)
                    return {"result": "success", "id": msg_id}

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter
        return ZulipAdapter(mock_platform_config)

    @pytest.mark.asyncio
    async def test_short_message_single_send(self, adapter):
        result = await adapter.send("dm:42", "Hello world")
        assert result.success is True
        assert adapter.client._calls == [{"type": "private", "to": [42], "content": "Hello world"}]

    @pytest.mark.asyncio
    async def test_long_message_chunked(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_TEXT_CHUNK_LIMIT", "10")
        # Reload config inside the adapter method uses env at call time
        text = "word1 word2 word3 word4 word5"
        result = await adapter.send("dm:42", text)
        assert result.success is True
        assert len(adapter.client._calls) > 1
        # All chunks together should contain all words
        all_content = " ".join(c["content"] for c in adapter.client._calls)
        assert "word1" in all_content
        assert "word5" in all_content

    @pytest.mark.asyncio
    async def test_topic_directive_extracted(self, adapter):
        result = await adapter.send(
            "573423",
            "[[zulip_topic: New Topic]] Hello stream",
            metadata={},
        )
        assert result.success is True
        call = adapter.client._calls[0]
        assert call["topic"] == "New Topic"
        assert call["content"] == "Hello stream"

    @pytest.mark.asyncio
    async def test_topic_directive_overrides_metadata(self, adapter):
        result = await adapter.send(
            "573423",
            "[[zulip_topic: Override]] content here",
            metadata={"topic": "Original"},
        )
        assert result.success is True
        assert adapter.client._calls[0]["topic"] == "Override"

    @pytest.mark.asyncio
    async def test_metadata_topic_used_when_no_directive(self, adapter):
        result = await adapter.send(
            "573423",
            "plain message",
            metadata={"topic": "MetaTopic"},
        )
        assert result.success is True
        assert adapter.client._calls[0]["topic"] == "MetaTopic"

    @pytest.mark.asyncio
    async def test_dm_ignores_topic_directive(self, adapter):
        result = await adapter.send(
            "dm:42",
            "[[zulip_topic: ShouldIgnore]] Hello DM",
        )
        assert result.success is True
        call = adapter.client._calls[0]
        assert call["type"] == "private"
        assert call["content"] == "Hello DM"

    @pytest.mark.asyncio
    async def test_empty_content_after_directive(self, adapter):
        result = await adapter.send("dm:42", "[[zulip_topic: X]]")
        assert result.success is True
        assert adapter.client._calls[0]["content"] == ""

    @pytest.mark.asyncio
    async def test_chunking_preserves_threading(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_TEXT_CHUNK_LIMIT", "5")
        result = await adapter.send(
            "573423",
            "one two three four five six",
            metadata={"topic": "Thread"},
        )
        assert result.success is True
        # All chunks should use the same topic
        for call in adapter.client._calls:
            assert call["topic"] == "Thread"
