"""Tests for stream filtering and response prefix (Issue #65).

Verifies that:
- ZULIP_STREAMS filters inbound stream messages by name
- ZULIP_RESPONSE_PREFIX prepends to outbound messages
- Prefix does not interfere with placeholder editing
"""

import asyncio
import os
from unittest.mock import MagicMock, patch, AsyncMock
import pytest

from zulip.adapter import _resolve_streams_filter, _resolve_response_prefix


class TestResolveStreamsFilter:
    """Test stream filter config parsing."""

    def test_default_none_when_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            result = _resolve_streams_filter()
        assert result is None

    def test_default_none_when_star(self):
        with patch.dict(os.environ, {"ZULIP_STREAMS": "*"}, clear=True):
            result = _resolve_streams_filter()
        assert result is None

    def test_single_stream(self):
        with patch.dict(os.environ, {"ZULIP_STREAMS": "engineering"}, clear=True):
            result = _resolve_streams_filter()
        assert result == {"engineering"}

    def test_multiple_streams(self):
        with patch.dict(os.environ, {"ZULIP_STREAMS": "engineering, ops, general"}, clear=True):
            result = _resolve_streams_filter()
        assert result == {"engineering", "ops", "general"}

    def test_lowercased(self):
        with patch.dict(os.environ, {"ZULIP_STREAMS": "Engineering"}, clear=True):
            result = _resolve_streams_filter()
        assert result == {"engineering"}

    def test_whitespace_trimmed(self):
        with patch.dict(os.environ, {"ZULIP_STREAMS": "  eng  ,  ops  "}, clear=True):
            result = _resolve_streams_filter()
        assert result == {"eng", "ops"}


class TestResolveResponsePrefix:
    """Test response prefix config parsing."""

    def test_default_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            result = _resolve_response_prefix()
        assert result == ""

    def test_custom_prefix(self):
        with patch.dict(os.environ, {"ZULIP_RESPONSE_PREFIX": "🤖 "}, clear=True):
            result = _resolve_response_prefix()
        assert result == "🤖 "

    def test_whitespace_trimmed(self):
        """Whitespace is preserved, not stripped."""
        with patch.dict(os.environ, {"ZULIP_RESPONSE_PREFIX": "  Bot:  "}, clear=True):
            result = _resolve_response_prefix()
        assert result == "  Bot:  "


class TestStreamFilterIntegration:
    """Test stream filtering in adapter._handle_message."""

    @pytest.fixture
    def mock_adapter(self):
        """Create a minimally initialized adapter with mocked internals."""
        config = MagicMock()
        config.extra = {}

        with patch.dict(
            os.environ,
            {
                "ZULIP_API_KEY": "test-key",
                "ZULIP_EMAIL": "bot@test.com",
                "ZULIP_SITE": "https://test.zulipchat.com",
            },
            clear=True,
        ):
            with patch("zulip.adapter._import_zulip_sdk") as mock_sdk:
                client = MagicMock()
                mock_sdk.return_value = MagicMock(Client=lambda **kw: client)
                from zulip.adapter import ZulipAdapter
                adapter = ZulipAdapter(config)

        # Mock out internal methods that would trigger side effects
        async def _mock_sdk_call(fn, *args, timeout, **kwargs):
            return {"result": "success", "id": 999}

        adapter._sdk_call = _mock_sdk_call
        adapter.build_source = MagicMock(return_value=MagicMock())
        adapter.handle_message = AsyncMock()

        return adapter

    @pytest.mark.asyncio
    async def test_allowed_stream_passes(self, mock_adapter):
        """Message from allowed stream is processed."""
        mock_adapter._streams_filter = {"engineering"}
        msg = {
            "id": 1,
            "type": "stream",
            "stream_id": 42,
            "display_recipient": "engineering",
            "subject": "deploy",
            "content": "hello",
            "sender_email": "alice@test.com",
            "sender_full_name": "Alice",
            "sender_id": 100,
        }
        await mock_adapter._handle_message(msg)
        # Should have dispatched to handle_message
        assert mock_adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_blocked_stream_dropped(self, mock_adapter):
        """Message from non-allowed stream is silently dropped."""
        mock_adapter._streams_filter = {"engineering"}
        msg = {
            "id": 2,
            "type": "stream",
            "stream_id": 99,
            "display_recipient": "random",
            "subject": "offtopic",
            "content": "hello",
            "sender_email": "bob@test.com",
            "sender_full_name": "Bob",
            "sender_id": 200,
        }
        await mock_adapter._handle_message(msg)
        # Should NOT have dispatched to handle_message
        assert not mock_adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_dm_not_affected_by_stream_filter(self, mock_adapter):
        """DMs are not affected by stream filtering."""
        mock_adapter._streams_filter = {"engineering"}
        msg = {
            "id": 3,
            "type": "private",
            "content": "hello",
            "sender_email": "alice@test.com",
            "sender_full_name": "Alice",
            "sender_id": 100,
        }
        await mock_adapter._handle_message(msg)
        # Should have dispatched to handle_message (DMs always processed)
        assert mock_adapter.handle_message.called


class TestResponsePrefixIntegration:
    """Test response prefix in adapter._send_single."""

    @pytest.fixture
    def mock_adapter(self):
        config = MagicMock()
        config.extra = {}

        with patch.dict(
            os.environ,
            {
                "ZULIP_API_KEY": "test-key",
                "ZULIP_EMAIL": "bot@test.com",
                "ZULIP_SITE": "https://test.zulipchat.com",
            },
            clear=True,
        ):
            with patch("zulip.adapter._import_zulip_sdk") as mock_sdk:
                client = MagicMock()
                mock_sdk.return_value = MagicMock(Client=lambda **kw: client)
                from zulip.adapter import ZulipAdapter
                adapter = ZulipAdapter(config)

        adapter._sdk_call = AsyncMock(return_value={"result": "success", "id": 123})
        adapter._pending_placeholders = {}
        return adapter

    @pytest.mark.asyncio
    async def test_prefix_prepended(self, mock_adapter):
        """Prefix is added before content."""
        mock_adapter._response_prefix = "🤖 "
        await mock_adapter._send_single("dm:42", "hello", {}, None)

        call_args = mock_adapter._sdk_call.call_args_list
        # First call is send_message (no placeholder to edit)
        assert call_args[0][0][1]["content"] == "🤖 hello"

    @pytest.mark.asyncio
    async def test_no_prefix_when_empty(self, mock_adapter):
        """No prefix added when config is empty."""
        mock_adapter._response_prefix = ""
        await mock_adapter._send_single("dm:42", "hello", {}, None)

        call_args = mock_adapter._sdk_call.call_args_list
        assert call_args[0][0][1]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_empty_content_unchanged(self, mock_adapter):
        """Empty content stays empty even with prefix."""
        mock_adapter._response_prefix = "🤖 "
        await mock_adapter._send_single("dm:42", "", {}, None)

        call_args = mock_adapter._sdk_call.call_args_list
        assert call_args[0][0][1]["content"] == ""
