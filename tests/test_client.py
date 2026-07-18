"""Tests for zulip.client — resilient API wrapper with retry/backoff."""

import asyncio
from unittest.mock import MagicMock

import pytest

from zulip.client import ZulipApiClient, RETRY_STATUSES


class MockZulipError(Exception):
    """Simulates a Zulip API error with status code."""

    def __init__(self, msg, status: int = None, retry_after: float = None):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


@pytest.fixture
def mock_sdk():
    return MagicMock()


@pytest.fixture
def client(mock_sdk):
    return ZulipApiClient(mock_sdk)


class TestSuccessPaths:
    @pytest.mark.asyncio
    async def test_register(self, client, mock_sdk):
        mock_sdk.register.return_value = {"result": "success", "queue_id": "q1", "last_event_id": 1}
        result = await client.register(["message"])
        assert result["queue_id"] == "q1"
        mock_sdk.register.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_message(self, client, mock_sdk):
        mock_sdk.send_message.return_value = {"result": "success", "id": 42}
        result = await client.send_message({"type": "stream", "to": "general", "content": "hi"})
        assert result["id"] == 42

    @pytest.mark.asyncio
    async def test_send_typing(self, client, mock_sdk):
        mock_sdk.set_typing_status.return_value = {"result": "success"}
        result = await client.send_typing({"op": "start", "type": "stream", "stream_id": 1, "topic": "test"})
        assert result["result"] == "success"

    @pytest.mark.asyncio
    async def test_add_reaction(self, client, mock_sdk):
        mock_sdk.add_reaction.return_value = {"result": "success"}
        result = await client.add_reaction({"message_id": 1, "emoji_name": "eyes"})
        assert result["result"] == "success"

    @pytest.mark.asyncio
    async def test_remove_reaction(self, client, mock_sdk):
        mock_sdk.remove_reaction.return_value = {"result": "success"}
        result = await client.remove_reaction({"message_id": 1, "emoji_name": "eyes"})
        assert result["result"] == "success"

    @pytest.mark.asyncio
    async def test_upload_file(self, client, mock_sdk):
        mock_sdk.upload_file.return_value = {"result": "success", "uri": "/user_uploads/1"}
        result = await client.upload_file("/tmp/test.txt")
        assert result["uri"] == "/user_uploads/1"

    @pytest.mark.asyncio
    async def test_get_members(self, client, mock_sdk):
        mock_sdk.get_members.return_value = {"result": "success", "members": []}
        result = await client.get_members()
        assert result["members"] == []


class TestRetryLogic:
    @pytest.mark.asyncio
    async def test_503_retry_then_success(self, client, mock_sdk):
        mock_sdk.send_message.side_effect = [
            MockZulipError("Service Unavailable", status=503),
            {"result": "success", "id": 42},
        ]
        result = await client.send_message({"type": "stream", "to": "general", "content": "hi"})
        assert result["id"] == 42
        assert mock_sdk.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_429_respects_retry_after(self, client, mock_sdk):
        mock_sdk.send_message.side_effect = [
            MockZulipError("Rate limited", status=429, retry_after=2.5),
            {"result": "success", "id": 42},
        ]
        result = await client.send_message({"type": "stream", "to": "general", "content": "hi"})
        assert result["id"] == 42
        assert mock_sdk.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_500_no_retry(self, client, mock_sdk):
        mock_sdk.send_message.side_effect = MockZulipError("Internal Server Error", status=500)
        with pytest.raises(MockZulipError):
            await client.send_message({"type": "stream", "to": "general", "content": "hi"})
        assert mock_sdk.send_message.call_count == 1  # no retry

    @pytest.mark.asyncio
    async def test_network_error_retry(self, client, mock_sdk):
        mock_sdk.send_message.side_effect = [
            ConnectionError("network down"),
            {"result": "success", "id": 42},
        ]
        result = await client.send_message({"type": "stream", "to": "general", "content": "hi"})
        assert result["id"] == 42
        assert mock_sdk.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_exhaustion_raises(self, client, mock_sdk):
        mock_sdk.send_message.side_effect = [
            MockZulipError("Unavailable", status=503),
            MockZulipError("Unavailable", status=503),
            MockZulipError("Unavailable", status=503),
            MockZulipError("Unavailable", status=503),
        ]
        with pytest.raises(MockZulipError):
            await client.send_message({"type": "stream", "to": "general", "content": "hi"})
        assert mock_sdk.send_message.call_count == 4  # initial + 3 retries

    @pytest.mark.asyncio
    async def test_backoff_timing(self, client, mock_sdk, monkeypatch):
        sleeps = []
        async def fake_sleep(s):
            sleeps.append(s)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        mock_sdk.send_message.side_effect = [
            MockZulipError("Unavailable", status=503),
            MockZulipError("Unavailable", status=503),
            {"result": "success", "id": 42},
        ]
        await client.send_message({"type": "stream", "to": "general", "content": "hi"})
        # Exponential: 1.0, 2.0
        assert sleeps == [1.0, 2.0]

    @pytest.mark.asyncio
    async def test_rate_limit_capped(self, client, mock_sdk, monkeypatch):
        sleeps = []
        async def fake_sleep(s):
            sleeps.append(s)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        mock_sdk.send_message.side_effect = [
            MockZulipError("Rate limited", status=429, retry_after=100),
            {"result": "success", "id": 42},
        ]
        await client.send_message({"type": "stream", "to": "general", "content": "hi"})
        # Should cap at MAX_DELAY=30
        assert sleeps[0] == 30.0


class TestTypingIndicators:
    @pytest.mark.asyncio
    async def test_typing_dm_start(self, client, mock_sdk):
        mock_sdk.set_typing_status.return_value = {"result": "success"}
        await client.send_typing({"op": "start", "type": "direct", "to": [42]})
        mock_sdk.set_typing_status.assert_called_once_with(
            {"op": "start", "type": "direct", "to": [42]}
        )

    @pytest.mark.asyncio
    async def test_typing_stream_stop(self, client, mock_sdk):
        mock_sdk.set_typing_status.return_value = {"result": "success"}
        await client.send_typing({"op": "stop", "type": "stream", "stream_id": 1, "topic": "test"})
        mock_sdk.set_typing_status.assert_called_once_with(
            {"op": "stop", "type": "stream", "stream_id": 1, "topic": "test"}
        )


class TestRetryStatuses:
    def test_expected_statuses(self):
        assert RETRY_STATUSES == {429, 502, 503, 504}
