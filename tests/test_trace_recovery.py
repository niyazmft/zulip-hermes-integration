"""Tests for trace restart recovery (epic #139, issue #161).

A trace can only be finalized by the process that created it, so an abrupt
restart (deploy, crash, OOM) leaves the topic showing ``Working`` **forever** —
the one case where "no trace is left permanently in progress" fails. Recovery
closes those out at the next start.

Two behaviours are the whole point and both are asserted here: the record is
cleared **even when the edit is refused** (or every start retries an edit the
server keeps rejecting), and a record is dropped rather than retried when it is
unusable.
"""

import asyncio
import json
from types import SimpleNamespace

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
                self.edit_result = {"result": "success"}

            def send_message(self, request):
                self.sent.append(request)
                return {"result": "success", "id": 501}

            def update_message(self, request):
                self.edited.append(request)
                return self.edit_result

    monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
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
    a._audit_logger = SimpleNamespace(
        log_event=_async_recorder(a),
        log_dispatch_turn=_async_noop,
        log_deliver_payload=_async_noop,
        log_deliver_skipped=_async_noop,
        log_deliver_empty=_async_noop,
        log_deliver_failed=_async_noop,
    )
    return a


async def _async_noop(*args, **kwargs):
    """Awaitable stand-in for a delivery-audit helper (issue #145)."""


def _async_recorder(adapter):
    calls = []
    adapter._audit_events = calls

    async def _log(event, details=None):
        calls.append((event, details))

    return _log


def stream_event(topic="api-review", chat_id="573423"):
    return SimpleNamespace(
        source=SimpleNamespace(chat_id=chat_id), metadata={"thread_id": topic}
    )


async def _settle():
    # The trace post runs through asyncio.to_thread, so a bare sleep(0) yield can
    # return before the fake SDK call has finished. That race was invisible locally
    # and lost on CI's slower runner, so give the worker thread a real moment.
    await asyncio.sleep(0.15)


def records(adapter):
    return adapter._load_trace_records()


class TestPersistAndClear:
    @pytest.mark.asyncio
    async def test_start_persists_the_message_id(self, adapter):
        await adapter.on_processing_start(stream_event())
        await _settle()

        stored = records(adapter)
        assert len(stored) == 1
        (record,) = stored.values()
        assert record["message_id"] == 501, "only a posted trace can be finalized later"
        assert record["topic"] == "api-review"
        assert record["chat_id"] == "573423"

    @pytest.mark.asyncio
    async def test_nothing_persisted_when_the_post_fails(self, adapter):
        adapter.client.send_message = lambda request: {"result": "error"}
        await adapter.on_processing_start(stream_event())
        await _settle()
        assert records(adapter) == {}, "no message id means nothing to recover"

    @pytest.mark.asyncio
    async def test_finalize_clears_the_record(self, adapter):
        event = stream_event()
        await adapter.on_processing_start(event)
        await _settle()
        assert records(adapter)

        await adapter.on_processing_complete(event, SimpleNamespace(value="success"))
        assert records(adapter) == {}

    @pytest.mark.asyncio
    async def test_record_is_cleared_even_when_the_final_edit_fails(self, adapter):
        """Otherwise recovery would retry a finished run forever."""
        event = stream_event()
        await adapter.on_processing_start(event)
        await _settle()
        adapter.client.edit_result = {"result": "error"}

        await adapter.on_processing_complete(event, SimpleNamespace(value="failure"))
        assert records(adapter) == {}


class TestRecovery:
    @pytest.mark.asyncio
    async def test_interrupted_trace_is_closed_out(self, adapter):
        adapter._persist_trace_record("k1", "573423", {"thread_id": "api-review"}, 777)

        recovered = await adapter._recover_interrupted_traces()

        assert recovered == 1
        assert len(adapter.client.edited) == 1
        edit = adapter.client.edited[0]
        assert edit["message_id"] == 777
        assert "Cancelled" in edit["content"]
        assert "gateway restart" in edit["content"]
        assert records(adapter) == {}, "a recovered record must not be retried"

    @pytest.mark.asyncio
    async def test_a_refused_edit_still_clears_the_record(self, adapter):
        """The realm past its edit limit must not cause a retry on every start."""
        adapter._persist_trace_record("k1", "573423", {"thread_id": "t"}, 777)
        adapter.client.edit_result = {"result": "error", "msg": "edit limit"}

        recovered = await adapter._recover_interrupted_traces()

        assert recovered == 1
        assert len(adapter.client.edited) == 1, "it was attempted exactly once"
        assert records(adapter) == {}, "and dropped regardless of the outcome"

    @pytest.mark.asyncio
    async def test_a_raising_edit_still_clears_the_record(self, adapter):
        def boom(request):
            raise RuntimeError("network")

        adapter._persist_trace_record("k1", "573423", {"thread_id": "t"}, 777)
        adapter.client.update_message = boom

        assert await adapter._recover_interrupted_traces() == 1
        assert records(adapter) == {}

    @pytest.mark.asyncio
    async def test_recovery_is_audited(self, adapter):
        adapter._persist_trace_record("k1", "573423", {"thread_id": "api-review"}, 777)
        await adapter._recover_interrupted_traces()

        events = [e for e, _ in adapter._audit_events]
        assert "activity_trace_recovered" in events
        _event, details = next(
            (e, d) for e, d in adapter._audit_events if e == "activity_trace_recovered"
        )
        assert details["message_id"] == 777
        assert details["finalized"] is True
        assert details["topic"] == "api-review"

    @pytest.mark.asyncio
    async def test_unusable_record_is_dropped_not_retried(self, adapter):
        path = adapter._trace_records_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"bad": {"chat_id": "1"}}))

        assert await adapter._recover_interrupted_traces() == 0
        assert records(adapter) == {}
        assert adapter.client.edited == []

    @pytest.mark.asyncio
    async def test_no_records_is_a_noop(self, adapter):
        assert await adapter._recover_interrupted_traces() == 0
        assert adapter.client.edited == []

    @pytest.mark.asyncio
    async def test_corrupt_file_does_not_break_startup(self, adapter):
        path = adapter._trace_records_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")

        assert await adapter._recover_interrupted_traces() == 0

    @pytest.mark.asyncio
    async def test_multiple_interrupted_traces_are_all_closed(self, adapter):
        adapter._persist_trace_record("k1", "573423", {"thread_id": "a"}, 701)
        adapter._persist_trace_record("k2", "573423", {"thread_id": "b"}, 702)

        assert await adapter._recover_interrupted_traces() == 2
        assert {e["message_id"] for e in adapter.client.edited} == {701, 702}
        assert records(adapter) == {}


class TestShutdown:
    @pytest.mark.asyncio
    async def test_graceful_shutdown_persists_rather_than_clears(self, adapter):
        """A shutdown must leave the record for the next start to close out."""
        await adapter.on_processing_start(stream_event())
        await _settle()
        assert records(adapter)

        adapter._listening = True
        try:
            await adapter.disconnect()
        except Exception:
            pass  # teardown details are not what this test is about

        assert records(adapter), (
            "clearing on shutdown would leave the trace unfinalizable forever"
        )
