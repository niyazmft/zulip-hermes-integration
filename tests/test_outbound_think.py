"""Send-path wiring for inbound hygiene (issue #152).

The modules already exist; these tests are about the *wiring*:

* a reasoning/think block a model emits inline never reaches Zulip, on either
  the live adapter send path or the standalone/cron send path;
* the bot's own messages are never treated as inbound user input;
* sender display names resolve through the persisted ``DisplayNameCache``,
  refreshing on a miss with a bounded fetch and tolerating a corrupt file.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zulip import adapter as adapter_module
from zulip.adapter import _standalone_send
from zulip.text_utils import strip_think_blocks


_OPEN = "\x3cthink\x3e"
_CLOSE = "\x3c/think\x3e"
CLOSED_BLOCK_REPLY = (
    "Here is the answer.\n"
    + _OPEN
    + "the user is asking about the deploy, so check the logs\n"
    + "I should verify before answering."
    + _CLOSE
    + "\nDone."
)
UNCLOSED_BLOCK_REPLY = "Answer.\n\x3creasoning\x3estill thinking and cut off mid"

_STANDALONE_ENV = {
    "ZULIP_SITE": "https://zulip.example.test",
    "ZULIP_EMAIL": "bot@example.test",
    "ZULIP_API_KEY": "k" * 32,
}


class _RecordingClient:
    """Minimal SDK stand-in: records sends and answers user lookups."""

    def __init__(self, **kwargs):
        self.sent = []

    def send_message(self, request):
        self.sent.append(dict(request))
        return {"result": "success", "id": 700 + len(self.sent)}

    def get_user(self, user_id):
        return {"result": "success", "user": {"full_name": f"User {user_id}"}}

    def update_message_flags(self, request):
        return {"result": "success"}

    def set_typing_status(self, request):
        return {"result": "success"}

    def add_reaction(self, request):
        return {"result": "success"}

    def remove_reaction(self, request):
        return {"result": "success"}


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch):
    """Swap the real SDK for the recording client on every test."""
    monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

    class MockZulipModule:
        Client = _RecordingClient

    monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
    adapter_module._clear_caches()
    yield
    adapter_module._clear_caches()


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    # Keep audit logs and the display-name cache out of ~/.hermes.
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))

    from zulip.adapter import ZulipAdapter

    return ZulipAdapter(mock_platform_config)


class TestStripOnLiveSendPath:
    @pytest.mark.asyncio
    async def test_closed_block_is_removed(self, adapter):
        result = await adapter.send("dm:42", CLOSED_BLOCK_REPLY)

        assert result.success is True
        content = adapter.client.sent[0]["content"]
        assert _OPEN not in content
        assert "the user is asking" not in content
        assert "Here is the answer." in content
        assert "Done." in content

    @pytest.mark.asyncio
    async def test_unclosed_block_is_removed_to_the_end(self, adapter):
        result = await adapter.send("dm:42", UNCLOSED_BLOCK_REPLY)

        assert result.success is True
        content = adapter.client.sent[0]["content"]
        assert content == "Answer.\n"
        assert "reasoning" not in content

    @pytest.mark.asyncio
    async def test_a_reply_without_blocks_is_unchanged(self, adapter):
        result = await adapter.send("dm:42", "A perfectly ordinary reply.")

        assert result.success is True
        assert adapter.client.sent[0]["content"] == "A perfectly ordinary reply."


class TestStripOnStandaloneSendPath:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        adapter_module._clear_caches()
        monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
        for key in _STANDALONE_ENV:
            monkeypatch.delenv(key, raising=False)
        yield
        adapter_module._clear_caches()

    @pytest.mark.asyncio
    async def test_standalone_send_strips_the_block(self, monkeypatch):
        for key, value in _STANDALONE_ENV.items():
            monkeypatch.setenv(key, value)
        client = MagicMock(spec_set=["send_message"])
        client.send_message.return_value = {"result": "success", "id": 1}

        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(
                SimpleNamespace(extra={}), "20", CLOSED_BLOCK_REPLY
            )

        assert result["success"] is True
        content = client.send_message.call_args[0][0]["content"]
        assert _OPEN not in content
        assert "the user is asking" not in content
        assert "Here is the answer." in content


class TestSelfMessageFiltering:
    @pytest.mark.asyncio
    async def test_own_email_is_never_dispatched(self, adapter):
        adapter.handle_message = AsyncMock()

        await adapter._handle_message(
            {
                "id": 1,
                "type": "stream",
                "stream_id": 1,
                "subject": "general",
                "display_recipient": "test",
                "content": "my own output",
                "sender_email": adapter.email,
                "sender_full_name": "Test Bot",
                "sender_id": 5,
            }
        )

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_own_email_comparison_is_case_insensitive(self, adapter):
        adapter.handle_message = AsyncMock()

        await adapter._handle_message(
            {
                "id": 1,
                "type": "private",
                "content": "my own output",
                "sender_email": adapter.email.upper(),
                "sender_full_name": "Test Bot",
                "sender_id": 5,
            }
        )

        adapter.handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_own_user_id_is_never_dispatched(self, adapter):
        adapter._bot_user_id = "42"
        adapter.handle_message = AsyncMock()

        await adapter._handle_message(
            {
                "id": 1,
                "type": "private",
                "content": "my own output",
                "sender_email": "renamed-bot@example.test",
                "sender_full_name": "Test Bot",
                "sender_id": 42,
            }
        )

        adapter.handle_message.assert_not_awaited()


class TestDisplayNameCacheWiring:
    @pytest.mark.asyncio
    async def test_payload_name_is_cached(self, adapter):
        name = await adapter._resolve_display_name(
            {"sender_id": 11, "sender_full_name": "Alice"}
        )

        assert name == "Alice"
        assert adapter._display_names.get(11) == "Alice"

    @pytest.mark.asyncio
    async def test_a_miss_refreshes_once_and_then_hits(self, adapter):
        first = await adapter._resolve_display_name({"sender_id": 7})
        assert first == "User 7"
        assert adapter._display_names.get(7) == "User 7"

        # Second resolution must be served from the cache, not the API. Guard
        # every lookup the SDK may expose -- get_user_by_id is the real method on
        # zulip 0.9.1 (issue #196), so guarding only get_user would let a cache
        # miss slip through unnoticed.
        adapter.client.get_user = MagicMock(side_effect=AssertionError("no fetch"))
        adapter.client.get_user_by_id = MagicMock(
            side_effect=AssertionError("no fetch")
        )
        assert await adapter._resolve_display_name({"sender_id": 7}) == "User 7"

    @pytest.mark.asyncio
    async def test_the_fetch_is_time_bounded(self, adapter):
        seen = {}

        async def fake_sdk(fn, *args, timeout=None, **kwargs):
            seen["timeout"] = timeout
            return {"result": "success", "user": {"full_name": "Bounded"}}

        adapter._sdk_call = fake_sdk

        assert await adapter._resolve_display_name({"sender_id": 9}) == "Bounded"
        assert seen["timeout"] == adapter._send_timeout

    def test_a_corrupt_cache_file_is_non_fatal(
        self, mock_platform_config, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
        cache_file = tmp_path / "cache" / "zulip_display_names.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("{ this is not json", encoding="utf-8")

        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)
        assert adapter._display_names.get("42") is None


class TestStripHelperContract:
    """The wiring relies on the module this was extracted into."""

    def test_plain_text_passes_through_unchanged(self):
        assert strip_think_blocks("just an answer") == "just an answer"
