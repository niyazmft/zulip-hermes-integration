"""Tests for mode A tool checkpoints (epic #139, issue #159).

Mode A turns finished tool calls into trace steps automatically. The hard part
is not the callback, it is **attribution**: the hook payload carries the
agent-side ``session_id``, which is derived differently from the adapter's
session key, so matching on it would be a guess. Attribution therefore comes
from the gateway's per-task session context, and a step whose run cannot be
identified is dropped rather than narrated into whatever topic happens to be
open.
"""

import asyncio
import dataclasses
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    import zulip.adapter as adapter_module
    import zulip.tracing as tracing_module
    import zulip.zulip_client as zulip_client_module

    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)
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

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    # The live-adapter registry is module-level, so adapters built by earlier tests
    # in the same process stay registered and can answer for this one — the handler
    # iterates them all. That made a "wrong topic is dropped" test report success on
    # CI, where GC timing kept a stale adapter alive. Isolate the registry.
    monkeypatch.setattr(
        adapter_module, "_LIVE_ADAPTERS", type(adapter_module._LIVE_ADAPTERS)()
    )
    adapter_module._clear_caches()

    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(mock_platform_config)
    # No real gateway in tests, so the per-task session context is simulated.
    # The guarded host import lives in zulip.tracing (the module that reads it).
    context: dict = {}
    monkeypatch.setattr(
        tracing_module,
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
    # The trace post runs through asyncio.to_thread, so a bare sleep(0) yield can
    # return before the fake SDK call has finished. That race was invisible locally
    # and lost on CI's slower runner, so give the worker thread a real moment.
    await asyncio.sleep(0.15)


async def started(adapter, topic="api-review", chat_id="573423"):
    event = stream_event(topic, chat_id)
    await adapter.on_processing_start(event)
    await _settle()
    return event


class TestAttribution:
    @pytest.mark.asyncio
    async def test_matching_session_context_adds_a_step(self, adapter):
        await started(adapter)
        adapter._test_context["HERMES_SESSION_CHAT_ID"] = "573423"
        adapter._test_context["HERMES_SESSION_THREAD_ID"] = "api-review"

        adapter.record_tool_step(tool_name="exec", status="ok", duration_ms=120)
        await _settle()

        final = adapter.client.edited[-1]["content"]
        assert "✓ exec" in final
        assert "120 ms" in final

    @pytest.mark.asyncio
    async def test_unknown_session_is_dropped_not_guessed(self, adapter):
        """Narrating another conversation's work would be worse than a gap."""
        await started(adapter)
        adapter._test_context["HERMES_SESSION_CHAT_ID"] = "999999"
        adapter._test_context["HERMES_SESSION_THREAD_ID"] = "somebody-elses-topic"

        adapter.record_tool_step(tool_name="exec", status="ok")
        await _settle()

        # Only the initial post exists; no edit, because no step was recorded.
        assert adapter.client.edited == []

    @pytest.mark.asyncio
    async def test_a_different_topic_in_the_same_stream_is_dropped(self, adapter):
        await started(adapter, topic="api-review")
        adapter._test_context["HERMES_SESSION_CHAT_ID"] = "573423"
        adapter._test_context["HERMES_SESSION_THREAD_ID"] = "deploys"

        adapter.record_tool_step(tool_name="exec", status="ok")
        await _settle()
        assert adapter.client.edited == []

    @pytest.mark.asyncio
    async def test_host_session_key_alias_also_matches(self, adapter, monkeypatch):
        """When the host exposes a session key, it is used as its own alias."""
        await started(adapter)
        assert any(a.startswith("route:") for a in adapter._trace_sessions)
        # Simulate the host providing a key alias for the same work item.
        only_key = next(iter(adapter._traces))
        adapter._trace_sessions["key:abc123"] = only_key
        adapter._test_context["HERMES_SESSION_KEY"] = "abc123"

        adapter.record_tool_step(tool_name="read_file", status="ok")
        await _settle()
        assert "✓ read_file" in adapter.client.edited[-1]["content"]

    @pytest.mark.asyncio
    async def test_failed_tool_calls_are_rendered_as_failures(self, adapter):
        await started(adapter)
        adapter._test_context["HERMES_SESSION_CHAT_ID"] = "573423"
        adapter._test_context["HERMES_SESSION_THREAD_ID"] = "api-review"

        adapter.record_tool_step(
            tool_name="exec", status="error", error_type="timeout"
        )
        await _settle()
        final = adapter.client.edited[-1]["content"]
        assert "✗ exec" in final
        assert "timeout" in final

    @pytest.mark.asyncio
    async def test_unknown_tool_name_still_renders(self, adapter):
        await started(adapter)
        adapter._test_context["HERMES_SESSION_CHAT_ID"] = "573423"
        adapter._test_context["HERMES_SESSION_THREAD_ID"] = "api-review"

        adapter.record_tool_step(status="ok")
        await _settle()
        assert "✓ tool" in adapter.client.edited[-1]["content"]

    @pytest.mark.asyncio
    async def test_no_context_at_all_is_dropped(self, adapter):
        await started(adapter)
        adapter.record_tool_step(tool_name="exec", status="ok")
        await _settle()
        assert adapter.client.edited == []

    @pytest.mark.asyncio
    async def test_finalized_traces_stop_accepting_steps(self, adapter):
        event = await started(adapter)
        adapter._test_context["HERMES_SESSION_CHAT_ID"] = "573423"
        adapter._test_context["HERMES_SESSION_THREAD_ID"] = "api-review"
        await adapter.on_processing_complete(event, SimpleNamespace(value="success"))

        assert adapter._trace_sessions == {}
        edits_before = len(adapter.client.edited)
        adapter.record_tool_step(tool_name="exec", status="ok")
        await _settle()
        assert len(adapter.client.edited) == edits_before


class TestToolMatcher:
    """#176: the matcher bounds which finished tools become checkpoints."""

    @pytest.mark.asyncio
    async def test_filtered_tool_produces_no_step(self, adapter):
        adapter._trace_cfg = dataclasses.replace(
            adapter._trace_cfg, tool_matcher="terminal"
        )
        await started(adapter)
        adapter._test_context["HERMES_SESSION_CHAT_ID"] = "573423"
        adapter._test_context["HERMES_SESSION_THREAD_ID"] = "api-review"

        before = len(adapter.client.edited)
        adapter.record_tool_step(tool_name="read", status="ok", duration_ms=5)
        await _settle()

        assert len(adapter.client.edited) == before

    @pytest.mark.asyncio
    async def test_allowed_tool_still_produces_a_step(self, adapter):
        adapter._trace_cfg = dataclasses.replace(
            adapter._trace_cfg, tool_matcher="terminal"
        )
        await started(adapter)
        adapter._test_context["HERMES_SESSION_CHAT_ID"] = "573423"
        adapter._test_context["HERMES_SESSION_THREAD_ID"] = "api-review"

        adapter.record_tool_step(tool_name="terminal", status="ok", duration_ms=5)
        await _settle()

        assert "terminal" in adapter.client.edited[-1]["content"]

    @pytest.mark.asyncio
    async def test_exclusion_form_filters_only_that_tool(self, adapter):
        adapter._trace_cfg = dataclasses.replace(
            adapter._trace_cfg, tool_matcher="!read"
        )
        await started(adapter)
        adapter._test_context["HERMES_SESSION_CHAT_ID"] = "573423"
        adapter._test_context["HERMES_SESSION_THREAD_ID"] = "api-review"

        before = len(adapter.client.edited)
        adapter.record_tool_step(tool_name="read", status="ok")
        await _settle()
        assert len(adapter.client.edited) == before

        adapter.record_tool_step(tool_name="terminal", status="ok", duration_ms=7)
        await _settle()
        assert "terminal" in adapter.client.edited[-1]["content"]


class TestMatcherParsing:
    """The matcher's rules, without the hook machinery."""

    @staticmethod
    def _cfg(matcher):
        from zulip.activity_trace import TraceConfig

        return TraceConfig(enabled=True, tool_matcher=matcher)

    def test_empty_matcher_allows_everything(self):
        assert self._cfg("").allows_tool("anything") is True
        assert self._cfg("   ").allows_tool("") is True

    def test_allowlist_admits_only_named_tools(self):
        cfg = self._cfg("terminal,read")
        assert cfg.allows_tool("terminal") is True
        assert cfg.allows_tool("READ") is True  # case-insensitive
        assert cfg.allows_tool("browser") is False

    def test_allowlist_rejects_an_unknown_tool_name(self):
        assert self._cfg("terminal").allows_tool("") is False

    def test_exclusion_form_admits_everything_else(self):
        cfg = self._cfg("!browser")
        assert cfg.allows_tool("browser") is False
        assert cfg.allows_tool("terminal") is True
        assert cfg.allows_tool("") is True

    def test_denial_wins_over_an_identical_allow(self):
        assert self._cfg("terminal,!terminal").allows_tool("terminal") is False


class TestIsolation:
    @pytest.mark.asyncio
    async def test_disabled_trace_records_nothing(self, adapter, monkeypatch):
        adapter._trace_cfg = type(adapter._trace_cfg).from_env()
        monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "0")
        adapter._trace_cfg = type(adapter._trace_cfg).from_env()
        adapter._test_context["HERMES_SESSION_CHAT_ID"] = "573423"

        adapter.record_tool_step(tool_name="exec", status="ok")
        await _settle()
        assert adapter.client.edited == []

    @pytest.mark.asyncio
    async def test_a_raising_observer_never_reaches_the_tool_call(self, adapter):
        """The callback is reached from the agent's tool path: it must not raise."""
        import zulip.adapter as adapter_module

        class StubAdapter:
            def record_tool_step(self, **kwargs):
                raise RuntimeError("boom")

        # Weak-referenceable, unlike SimpleNamespace, and held for the duration.
        stub = StubAdapter()
        adapter_module._LIVE_ADAPTERS.add(stub)
        try:
            adapter_module._on_post_tool_call(tool_name="exec", status="ok")
        finally:
            adapter_module._LIVE_ADAPTERS.discard(stub)


class TestHookRegistration:
    def _ctx(self):
        ctx = MagicMock()
        ctx.register_platform.return_value = None
        return ctx

    # The approval observers (#222) are registered unconditionally — the audit
    # entry is the feature, and the policy key only decides whether the refusal
    # is also said in the room. So these tests name the *trace* hook rather than
    # asserting an empty registration set.
    _APPROVAL_HOOKS = ["pre_approval_request", "post_approval_response"]

    def _trace_hooks(self, ctx):
        return [
            c.args[0]
            for c in ctx.register_hook.call_args_list
            if c.args[0] not in self._APPROVAL_HOOKS
        ]

    def test_hook_registered_when_enabled(self, monkeypatch):
        import zulip.adapter as adapter_module

        monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "1")
        ctx = self._ctx()
        adapter_module.register(ctx)
        ctx.register_hook.assert_any_call(
            "post_tool_call", adapter_module._on_post_tool_call
        )
        assert self._trace_hooks(ctx) == ["post_tool_call"]

    def test_hook_not_registered_when_disabled(self, monkeypatch):
        """Not registering is what keeps a disabled trace free: any
        registration flips the host's has_hook() and enables dispatch."""
        import zulip.adapter as adapter_module

        monkeypatch.delenv("ZULIP_ACTIVITY_TRACE", raising=False)
        ctx = self._ctx()
        adapter_module.register(ctx)
        assert self._trace_hooks(ctx) == []
        # The approval observers are independent of the trace.
        registered = [c.args[0] for c in ctx.register_hook.call_args_list]
        assert registered == list(self._APPROVAL_HOOKS)

    def test_pre_tool_call_is_never_registered(self, monkeypatch):
        """pre_tool_call is fail-closed: an observer could block the tool call."""
        import zulip.adapter as adapter_module

        monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "1")
        ctx = self._ctx()
        adapter_module.register(ctx)

        registered = [c.args[0] for c in ctx.register_hook.call_args_list]
        assert "pre_tool_call" not in registered
        assert self._trace_hooks(ctx) == ["post_tool_call"]

    def test_a_failing_registration_does_not_break_plugin_load(self, monkeypatch):
        import zulip.adapter as adapter_module

        monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "1")
        ctx = self._ctx()
        ctx.register_hook.side_effect = RuntimeError("no such hook")

        adapter_module.register(ctx)  # must not raise
        ctx.register_platform.assert_called_once()
