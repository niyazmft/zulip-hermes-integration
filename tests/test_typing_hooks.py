"""Tests for the core typing hooks (send_typing / stop_typing).

The gateway core drives typing during an agent run via the adapter's
send_typing/stop_typing hooks (a refresh loop every ~2s, then a final stop).
The adapter must translate gateway chat ids into Zulip set_typing_status
params. Regression guard: _process_message must NOT emit typing calls of its
own (the old manual start only lasted ZULIP_TYPING_DELAY_SECONDS because
handle_message returns as soon as the background agent task is spawned).
"""

import asyncio
from unittest.mock import AsyncMock

import pytest


class TestTypingParamsForChat:
    @pytest.fixture
    def adapter(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, email=None, api_key=None, site=None):
                    pass

                def set_typing_status(self, *a, **k):
                    pass

                def add_reaction(self, *a, **k):
                    pass

                def remove_reaction(self, *a, **k):
                    pass

                def update_message_flags(self, *a, **k):
                    pass

                def send_message(self, *a, **k):
                    pass

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter
        a = ZulipAdapter(mock_platform_config)
        a.email = "[EMAIL]"
        a._sdk_call = AsyncMock(return_value={"result": "success"})
        return a

    def test_dm_chat_id_maps_to_direct_typing(self, adapter):
        params = adapter._typing_params_for_chat("dm:42", "start")
        assert params == {"op": "start", "type": "direct", "to": [42]}

    def test_dm_session_rotation_suffix_is_stripped(self, adapter):
        params = adapter._typing_params_for_chat("dm:42:session:2", "start")
        assert params == {"op": "start", "type": "direct", "to": [42]}

    def test_group_dm_chat_id_maps_to_every_recipient(self, adapter):
        """Issue #154: a comma-separated recipient list must not kill typing."""
        params = adapter._typing_params_for_chat("dm:7,42,99", "start")
        assert params == {"op": "start", "type": "direct", "to": [7, 42, 99]}

    def test_group_dm_session_rotation_suffix_is_stripped(self, adapter):
        params = adapter._typing_params_for_chat("dm:7,42,99:session:3", "start")
        assert params == {"op": "start", "type": "direct", "to": [7, 42, 99]}

    def test_stream_chat_id_maps_to_stream_typing_with_cached_topic(self, adapter):
        adapter._topic_cache["573423"] = "api-review"
        params = adapter._typing_params_for_chat("573423", "start")
        assert params == {
            "op": "start",
            "type": "stream",
            "stream_id": 573423,
            "topic": "api-review",
        }

    def test_stream_with_uncached_topic_sends_empty_topic(self, adapter):
        params = adapter._typing_params_for_chat("999", "start")
        assert params == {
            "op": "start",
            "type": "stream",
            "stream_id": 999,
            "topic": "",
        }

    def test_op_stop_flips_the_operation(self, adapter):
        params = adapter._typing_params_for_chat("dm:42", "stop")
        assert params["op"] == "stop"

    @pytest.mark.parametrize("bad", ["", "foo:bar", "dm:abc", "stream:42"])
    def test_unmappable_chat_ids_return_none(self, adapter, bad):
        assert adapter._typing_params_for_chat(bad, "start") is None


class TestHookCalls:
    @pytest.fixture
    def adapter(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, email=None, api_key=None, site=None):
                    pass

                def set_typing_status(self, *a, **k):
                    pass

                def add_reaction(self, *a, **k):
                    pass

                def remove_reaction(self, *a, **k):
                    pass

                def update_message_flags(self, *a, **k):
                    pass

                def send_message(self, *a, **k):
                    pass

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter
        a = ZulipAdapter(mock_platform_config)
        a.email = "[EMAIL]"
        a._sdk_call = AsyncMock(return_value={"result": "success"})
        return a

    def _typing_calls(self, adapter):
        return [
            c.args[1]
            for c in adapter._sdk_call.call_args_list
            if c.args and getattr(c.args[0], "__name__", "") == "set_typing_status"
        ]

    @pytest.mark.asyncio
    async def test_send_typing_issues_op_start(self, adapter):
        await adapter.send_typing("dm:42")
        calls = self._typing_calls(adapter)
        assert calls == [{"op": "start", "type": "direct", "to": [42]}]

    @pytest.mark.asyncio
    async def test_send_typing_accepts_core_metadata_kwarg(self, adapter):
        # The core's keep-typing loop calls send_typing(chat_id, metadata=...).
        await adapter.send_typing("dm:42", metadata={"thread_id": "general"})
        assert len(self._typing_calls(adapter)) == 1

    @pytest.mark.asyncio
    async def test_stop_typing_issues_op_stop(self, adapter):
        await adapter.stop_typing("dm:42")
        calls = self._typing_calls(adapter)
        assert calls == [{"op": "stop", "type": "direct", "to": [42]}]

    @pytest.mark.asyncio
    async def test_send_typing_swallows_sdk_errors(self, adapter):
        adapter._sdk_call = AsyncMock(side_effect=RuntimeError("network down"))
        await adapter.send_typing("dm:42")  # must not raise

    @pytest.mark.asyncio
    async def test_unmappable_chat_id_is_a_noop(self, adapter):
        await adapter.send_typing("not-a-chat-id")
        assert self._typing_calls(adapter) == []
