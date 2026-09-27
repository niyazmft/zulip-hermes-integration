"""Tests for the activity-trace lifecycle wiring (epic #139, issue #158).

The engine (issue #157) is inert until something calls it, so these tests are
about the wiring: the trace must start on dispatch, finalize with the outcome
the gateway reports (success / failure / cancelled), land in the *same topic the
reply uses*, and never let a trace failure reach the agent run.

The outcome is injected as a plain object with a ``value`` attribute, which is
what the engine reads, so these tests do not depend on the host enum being
importable in the test environment.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

SUCCESS = SimpleNamespace(value="success")
FAILURE = SimpleNamespace(value="failure")
CANCELLED = SimpleNamespace(value="cancelled")


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
                self._next_id = 500

            def send_message(self, request):
                self.sent.append(request)
                self._next_id += 1
                return {"result": "success", "id": self._next_id}

            def update_message(self, request):
                self.edited.append(request)
                return {"result": "success"}

            def set_typing_status(self, *a, **k):
                return {"result": "success"}

    monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
    adapter_module._clear_caches()

    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(mock_platform_config)
    return a


def stream_event(topic="api-review", chat_id="573423"):
    return SimpleNamespace(
        source=SimpleNamespace(chat_id=chat_id), metadata={"thread_id": topic}
    )


def dm_event(chat_id="dm:42"):
    return SimpleNamespace(source=SimpleNamespace(chat_id=chat_id), metadata=None)


async def _settle():
    """Let the background trace-start task run."""
    for _ in range(5):
        await asyncio.sleep(0)


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_successful_run_posts_one_message_and_finalizes_it(self, adapter):
        event = stream_event()
        await adapter.on_processing_start(event)
        await _settle()
        assert len(adapter.client.sent) == 1, "one status message per work item"

        for i in range(3):
            step = adapter._traces[next(iter(adapter._traces))].step(f"step {i}")
            adapter._traces[next(iter(adapter._traces))].complete(step)
        # The run replied, which is what the terminal note distinguishes.
        adapter._note_trace_reply("573423", {"thread_id": "api-review"})
        await adapter.on_processing_complete(event, SUCCESS)

        sent_id = 501
        assert all(e["message_id"] == sent_id for e in adapter.client.edited), (
            "every update must edit the trace message, never post a new one"
        )
        final = adapter.client.edited[-1]["content"]
        assert "**Done**" in final
        assert "replied in" in final
        assert "no reply sent" not in final

    @pytest.mark.asyncio
    async def test_a_run_that_produced_nothing_says_so(self, adapter):
        """The case this whole epic exists for: silence was indistinguishable."""
        event = stream_event()
        await adapter.on_processing_start(event)
        await _settle()
        await adapter.on_processing_complete(event, SUCCESS)

        final = adapter.client.edited[-1]["content"]
        assert "no reply sent" in final
        assert "**Done**" in final

    @pytest.mark.asyncio
    async def test_failure_outcome_is_recorded(self, adapter):
        event = stream_event()
        await adapter.on_processing_start(event)
        await _settle()
        await adapter.on_processing_complete(event, FAILURE)

        final = adapter.client.edited[-1]["content"]
        assert "**Failed**" in final
        assert "run failed" in final

    @pytest.mark.asyncio
    async def test_cancelled_outcome_is_recorded(self, adapter):
        event = stream_event()
        await adapter.on_processing_start(event)
        await _settle()
        await adapter.on_processing_complete(event, CANCELLED)

        final = adapter.client.edited[-1]["content"]
        assert "Cancelled" in final
        assert "no reply sent" in final

    @pytest.mark.asyncio
    async def test_trace_is_forgotten_once_finalized(self, adapter):
        event = stream_event()
        await adapter.on_processing_start(event)
        await _settle()
        await adapter.on_processing_complete(event, SUCCESS)
        assert adapter._traces == {}


class TestRouting:
    @pytest.mark.asyncio
    async def test_stream_trace_uses_the_same_topic_as_the_reply(self, adapter):
        event = stream_event(topic="deploys")
        await adapter.on_processing_start(event)
        await _settle()

        payload = adapter.client.sent[0]
        assert payload["type"] == "stream"
        assert payload["to"] == 573423, "stream ids are sent as ints"
        assert payload["topic"] == "deploys", (
            "the trace must be routed by the shared topic resolution, "
            "not a second implementation (#143)"
        )

    @pytest.mark.asyncio
    async def test_dm_trace_addresses_the_recipient_set(self, adapter):
        event = dm_event("dm:7,42")
        await adapter.on_processing_start(event)
        await _settle()

        payload = adapter.client.sent[0]
        assert payload["type"] == "private"
        assert payload["to"] == [7, 42]

    @pytest.mark.asyncio
    async def test_two_topics_in_one_stream_do_not_share_a_trace(self, adapter):
        """Topic sessions share a chat_id, so the key must include the topic."""
        first, second = stream_event("topic-a"), stream_event("topic-b")
        await adapter.on_processing_start(first)
        await adapter.on_processing_start(second)
        await _settle()

        assert len(adapter.client.sent) == 2
        assert {p["topic"] for p in adapter.client.sent} == {"topic-a", "topic-b"}
        assert len(adapter._traces) == 2

        await adapter.on_processing_complete(first, SUCCESS)
        # Finalizing one topic must not touch the other's trace.
        assert len(adapter._traces) == 1
        assert adapter.client.edited[-1]["content"] is not None


class TestIsolation:
    @pytest.mark.asyncio
    async def test_disabled_trace_does_nothing(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "0")
        adapter._trace_cfg = type(adapter._trace_cfg).from_env()

        event = stream_event()
        await adapter.on_processing_start(event)
        await _settle()
        await adapter.on_processing_complete(event, SUCCESS)

        assert adapter.client.sent == []
        assert adapter.client.edited == []

    @pytest.mark.asyncio
    async def test_a_failed_post_never_reaches_the_run(self, adapter):
        adapter.client.send_message = MagicMock(
            return_value={"result": "error", "msg": "zulip down"}
        )
        event = stream_event()

        await adapter.on_processing_start(event)  # must not raise
        await _settle()
        await adapter.on_processing_complete(event, SUCCESS)  # must not raise

        assert adapter.client.edited == [], "no trace message means nothing to edit"

    @pytest.mark.asyncio
    async def test_a_raising_post_never_reaches_the_run(self, adapter):
        adapter.client.send_message = MagicMock(side_effect=RuntimeError("boom"))
        event = stream_event()

        await adapter.on_processing_start(event)
        await _settle()
        await adapter.on_processing_complete(event, SUCCESS)

        assert adapter.client.edited == []

    @pytest.mark.asyncio
    async def test_a_raising_edit_never_reaches_the_run(self, adapter):
        adapter.client.update_message = MagicMock(side_effect=RuntimeError("boom"))
        event = stream_event()
        await adapter.on_processing_start(event)
        await _settle()
        trace = next(iter(adapter._traces.values()))
        trace.step("work")

        await trace._flush()  # the engine swallows it
        await adapter.on_processing_complete(event, SUCCESS)  # must not raise

    @pytest.mark.asyncio
    async def test_complete_without_start_is_a_noop(self, adapter):
        await adapter.on_processing_complete(stream_event(), SUCCESS)
        assert adapter.client.edited == []

    @pytest.mark.asyncio
    async def test_starting_twice_does_not_duplicate_the_board(self, adapter):
        event = stream_event()
        await adapter.on_processing_start(event)
        await adapter.on_processing_start(event)
        await _settle()
        assert len(adapter.client.sent) == 1


class TestReplyDetection:
    @pytest.mark.asyncio
    async def test_a_real_send_marks_the_work_item_as_replied(self, adapter):
        event = stream_event()
        await adapter.on_processing_start(event)
        await _settle()

        result = await adapter.send("573423", "here is the answer", metadata={"thread_id": "api-review"})
        assert result.success is True

        await adapter.on_processing_complete(event, SUCCESS)
        final = adapter.client.edited[-1]["content"]
        assert "replied in" in final
        assert "no reply sent" not in final

    @pytest.mark.asyncio
    async def test_a_reply_in_another_topic_does_not_mark_this_one(self, adapter):
        event = stream_event("topic-a")
        await adapter.on_processing_start(event)
        await _settle()

        await adapter.send("573423", "answer", metadata={"thread_id": "topic-b"})
        await adapter.on_processing_complete(event, SUCCESS)

        final = adapter.client.edited[-1]["content"]
        assert "no reply sent" in final, (
            "reply tracking must be per work item, not per chat"
        )
