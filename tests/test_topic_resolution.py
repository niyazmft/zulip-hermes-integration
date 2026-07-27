"""Tests for topic resolution support (Issue #67).

Verifies that resolve_topic() prepends ✔ to topic names and handles
edge cases correctly.
"""

import os
from unittest.mock import MagicMock, patch, AsyncMock
import pytest

from zulip.adapter import ZulipAdapter


class TestResolveTopic:
    """Test the resolve_topic() method."""

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
                adapter = ZulipAdapter(config)

        adapter._sdk_call = AsyncMock(return_value={"result": "success"})
        return adapter

    @pytest.mark.asyncio
    async def test_prepends_checkmark(self, mock_adapter):
        """Topic gets ✔ prefix."""
        result = await mock_adapter.resolve_topic(42, "deploy-issue")
        assert result["ok"] is True
        assert result["topic"] == "✔ deploy-issue"

    @pytest.mark.asyncio
    async def test_skips_already_resolved(self, mock_adapter):
        """Already-resolved topics are skipped."""
        result = await mock_adapter.resolve_topic(42, "✔ deploy-issue")
        assert result["skipped"] is True
        assert result["reason"] == "already resolved"
        # No API call made
        assert not mock_adapter._sdk_call.called

    @pytest.mark.asyncio
    async def test_skips_empty_topic(self, mock_adapter):
        """Empty topics are skipped."""
        result = await mock_adapter.resolve_topic(42, "")
        assert result["skipped"] is True
        assert result["reason"] == "empty topic"
        assert not mock_adapter._sdk_call.called

    @pytest.mark.asyncio
    async def test_api_failure_handled(self, mock_adapter):
        """API errors are caught and returned gracefully."""
        mock_adapter._sdk_call = AsyncMock(
            return_value={"result": "error", "msg": "no permission"}
        )
        result = await mock_adapter.resolve_topic(42, "deploy-issue")
        assert result["ok"] is False
        assert result["error"] == "no permission"

    @pytest.mark.asyncio
    async def test_exception_handled(self, mock_adapter):
        """Exceptions are caught and returned gracefully."""
        mock_adapter._sdk_call = AsyncMock(side_effect=RuntimeError("network down"))
        result = await mock_adapter.resolve_topic(42, "deploy-issue")
        assert result["ok"] is False
        assert "network down" in result["error"]

    @pytest.mark.asyncio
    async def test_uses_send_timeout(self, mock_adapter):
        """resolve_topic uses send_timeout."""
        mock_adapter._send_timeout = 45.0
        await mock_adapter.resolve_topic(42, "topic")
        call_kwargs = mock_adapter._sdk_call.call_args[1]
        assert call_kwargs["timeout"] == 45.0
