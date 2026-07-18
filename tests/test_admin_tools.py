"""Tests for zulip.client admin/search methods."""

import pytest
from unittest.mock import MagicMock

from zulip.client import ZulipApiClient


@pytest.fixture
def client():
    return ZulipApiClient(MagicMock())


class TestAdminTools:
    @pytest.mark.asyncio
    async def test_get_members(self, client):
        client._client.get_members.return_value = {"result": "success", "members": []}
        result = await client.get_members()
        assert result["members"] == []
        client._client.get_members.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_messages(self, client):
        client._client.get_messages.return_value = {"result": "success", "messages": []}
        result = await client.get_messages({"anchor": "newest", "num_before": 10})
        assert result["messages"] == []

    @pytest.mark.asyncio
    async def test_add_subscriptions(self, client):
        client._client.add_subscriptions.return_value = {"result": "success"}
        result = await client.add_subscriptions([{"name": "new-stream"}])
        assert result["result"] == "success"

    @pytest.mark.asyncio
    async def test_update_stream(self, client):
        client._client.update_stream.return_value = {"result": "success"}
        result = await client.update_stream(1, description="updated")
        assert result["result"] == "success"

    @pytest.mark.asyncio
    async def test_delete_stream(self, client):
        client._client.delete_stream.return_value = {"result": "success"}
        result = await client.delete_stream(1)
        assert result["result"] == "success"

    @pytest.mark.asyncio
    async def test_delete_stream_error(self, client):
        err = Exception("stream not found")
        err.status = 404
        client._client.delete_stream.side_effect = err
        with pytest.raises(Exception, match="stream not found"):
            await client.delete_stream(999)
