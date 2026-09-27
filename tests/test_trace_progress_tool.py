"""Tests for mode B — the ``zulip_progress`` tool (epic #139, issue #160).

Mode B covers intent that a tool call does not reveal ("about to ask a
clarifying question"). It records through the **same** attribution path as mode
A, so a note lands on the right topic or is dropped — never guessed into
another conversation — and a run with no active trace must not look like a
failure to the model.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    import zulip.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)
    monkeypatch.setenv("ZULIP_SITE", "https://test.zulipchat.com")
    monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
    monkeypatch.setenv("ZULIP_API_KEY", "k" * 32)
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "1")
    monkeypatch.setenv("ZULIP_TRACE_COALESCE_MS", "0")

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                self.sent = []
                self.edited = []

            def send_message(self, request):
                self.sent.append(request)
                return {"result": "success", "id": 501}

            def update_message(self, request):
                self.edited.append(request)
                return {"result": "success"}

    monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
    adapter_module._clear_caches()

    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(mock_platform_config)
    context: dict = {}
    monkeypatch.setattr(
        adapter_module,
        "_host_get_session_env",
        lambda name, default=None: context.get(name, default),
    )
    a._test_context = context
    return a


def stream_event(topic="api-review", chat_id="573423"):
    return SimpleNamespace(
        source=SimpleNamespace(chat_id=chat_id), metadata={"thread_id": topic}
    )


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def started(adapter, topic="api-review"):
    event = stream_event(topic)
    await adapter.on_processing_start(event)
    await _settle()
    return event


def in_topic(adapter, topic="api-review", chat_id="573423"):
    adapter._test_context["HERMES_SESSION_CHAT_ID"] = chat_id
    adapter._test_context["HERMES_SESSION_THREAD_ID"] = topic


class TestProgressHandler:
    @pytest.mark.asyncio
    async def test_note_lands_on_the_active_trace(self, adapter):
        import zulip.adapter as adapter_module

        await started(adapter)
        in_topic(adapter)

        result = adapter_module._zulip_progress_handler(
            {"note": "checking the logs"}
        )
        await _settle()

        assert result == "noted"
        assert "✓ checking the logs" in adapter.client.edited[-1]["content"]

    @pytest.mark.asyncio
    async def test_no_active_trace_is_not_an_error(self, adapter):
        """A run without a trace must not look like a tool failure."""
        import zulip.adapter as adapter_module

        result = adapter_module._zulip_progress_handler({"note": "hello"})
        assert result == "no activity trace is active for this conversation; note not shown"

    def test_missing_note_is_reported(self, adapter):
        import zulip.adapter as adapter_module

        for args in ({}, {"note": "   "}, {"note": None}, "not-a-dict"):
            result = adapter_module._zulip_progress_handler(args)
            assert result.startswith("error:")

    @pytest.mark.asyncio
    async def test_note_for_another_topic_is_dropped(self, adapter):
        import zulip.adapter as adapter_module

        await started(adapter, topic="api-review")
        in_topic(adapter, topic="deploys")  # a different conversation

        result = adapter_module._zulip_progress_handler({"note": "not for you"})
        await _settle()

        assert result.startswith("no activity trace")
        assert adapter.client.edited == []

    @pytest.mark.asyncio
    async def test_accepts_alternate_argument_names(self, adapter):
        import zulip.adapter as adapter_module

        await started(adapter)
        in_topic(adapter)
        assert adapter_module._zulip_progress_handler({"step": "via step"}) == "noted"
        assert adapter_module._zulip_progress_handler({"message": "via message"}) == "noted"
        await _settle()
        final = adapter.client.edited[-1]["content"]
        assert "✓ via step" in final
        assert "✓ via message" in final

    @pytest.mark.asyncio
    async def test_raising_adapter_never_reaches_the_model(self, adapter):
        import zulip.adapter as adapter_module

        class Stub:
            def record_progress_step(self, note):
                raise RuntimeError("boom")

        stub = Stub()
        adapter_module._LIVE_ADAPTERS.add(stub)
        try:
            result = adapter_module._zulip_progress_handler({"note": "x"})
            assert result.startswith("no activity trace")
        finally:
            adapter_module._LIVE_ADAPTERS.discard(stub)


class TestSharedAttribution:
    @pytest.mark.asyncio
    async def test_both_modes_use_one_lookup_path(self, adapter):
        """A note and a tool step must resolve identically."""
        await started(adapter)
        in_topic(adapter)

        assert adapter.record_progress_step("note") is True
        adapter.record_tool_step(tool_name="exec", status="ok")
        await _settle()

        final = adapter.client.edited[-1]["content"]
        assert "✓ note" in final
        assert "✓ exec" in final

    @pytest.mark.asyncio
    async def test_progress_step_reports_false_without_a_trace(self, adapter):
        assert adapter.record_progress_step("ignored") is False


class TestToolRegistration:
    def _ctx(self):
        ctx = MagicMock()
        return ctx

    def test_tool_and_hook_registered_together_when_enabled(self, monkeypatch):
        import zulip.adapter as adapter_module

        monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "1")
        ctx = self._ctx()
        adapter_module.register(ctx)

        names = [c.kwargs.get("name") for c in ctx.register_tool.call_args_list]
        assert names == ["zulip_progress"]
        ctx.register_hook.assert_called_once()

    def test_nothing_registered_when_disabled(self, monkeypatch):
        """A disabled trace must not expose the tool to the model."""
        import zulip.adapter as adapter_module

        monkeypatch.delenv("ZULIP_ACTIVITY_TRACE", raising=False)
        ctx = self._ctx()
        adapter_module.register(ctx)

        ctx.register_tool.assert_not_called()
        ctx.register_hook.assert_not_called()

    def test_schema_is_json_schema_shaped(self, monkeypatch):
        """The host builds tools from a plain dict; no dependency is needed."""
        import zulip.adapter as adapter_module

        monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "1")
        ctx = self._ctx()
        adapter_module.register(ctx)

        schema = ctx.register_tool.call_args.kwargs["schema"]
        assert schema["name"] == "zulip_progress"
        assert schema["parameters"]["type"] == "object"
        assert schema["parameters"]["required"] == ["note"]
        assert "note" in schema["parameters"]["properties"]

    def test_failing_tool_registration_does_not_break_plugin_load(self, monkeypatch):
        import zulip.adapter as adapter_module

        monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "1")
        ctx = self._ctx()
        ctx.register_tool.side_effect = RuntimeError("no such toolset")

        adapter_module.register(ctx)  # must not raise
        ctx.register_platform.assert_called_once()
