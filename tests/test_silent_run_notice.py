"""A run that ends silently says so in the room (issue #219).

Two shapes used to leave ✅ and then nothing, which is the worst diagnostic
outcome — an answered-looking message with no answer:

* a run the gateway reports as FAILURE or CANCELLED, which never takes back the
  ✅ that ``inbound_queue._dispatch_turn`` placed on a clean *dispatch* return;
* a run that finished having sent nothing at all, visible only in the host's
  audit log.

Either way "the bot didn't answer" was indistinguishable from "the bot answered
somewhere else" without reading that log.

The outcome is injected as a plain object carrying ``value`` — the shape
``tracing.finish_trace`` reads — so these tests do not depend on the host enum
being importable in the test environment.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SUCCESS = SimpleNamespace(value="success")
FAILURE = SimpleNamespace(value="failure")
CANCELLED = SimpleNamespace(value="cancelled")

TOPIC = "api-review"
CHAT_ID = "573423"
MESSAGE_ID = 4242


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    import zulip.adapter as adapter_module
    import zulip.zulip_client as zulip_client_module

    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)
    monkeypatch.setenv("ZULIP_SITE", "https://test.zulipchat.com")
    monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
    monkeypatch.setenv("ZULIP_API_KEY", "k" * 32)
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    # Traces off: this file is about the room-facing notice, and a trace status
    # message would land in the same list the notice does.
    monkeypatch.delenv("ZULIP_ACTIVITY_TRACE", raising=False)

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                self.sent = []
                self.reactions_added = []
                self.reactions_removed = []
                self._next_id = 900

            def send_message(self, request):
                self.sent.append(request)
                self._next_id += 1
                return {"result": "success", "id": self._next_id}

            def add_reaction(self, request):
                self.reactions_added.append(request)
                return {"result": "success"}

            def remove_reaction(self, request):
                self.reactions_removed.append(request)
                return {"result": "success"}

            def set_typing_status(self, *a, **k):
                return {"result": "success"}

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    # The live-adapter registry is module-level, so adapters built by earlier
    # tests in the same process stay registered and can answer for this one.
    monkeypatch.setattr(
        adapter_module, "_LIVE_ADAPTERS", type(adapter_module._LIVE_ADAPTERS)()
    )
    adapter_module._clear_caches()

    from zulip.adapter import ZulipAdapter

    return ZulipAdapter(mock_platform_config)


def stream_event(topic=TOPIC, chat_id=CHAT_ID, message_id=MESSAGE_ID):
    return SimpleNamespace(
        source=SimpleNamespace(chat_id=chat_id),
        metadata={"thread_id": topic},
        message_id=message_id,
    )


def notice_texts(adapter):
    """Only the silent-run notices, never a reply or a trace status message."""
    return [
        m["content"] for m in adapter.client.sent if "without a reply" in str(m.get("content"))
    ]


def emojis(adapter, which):
    return [r["emoji_name"] for r in getattr(adapter.client, which)]


async def run_to_completion(adapter, event, outcome, delivered=False):
    """Mirror the real call order: arm, optionally deliver, then complete."""
    await adapter.on_processing_start(event)
    if delivered:
        adapter._note_delivery_attempt(event.source.chat_id, event.metadata)
    await adapter.on_processing_complete(event, outcome)
    # Reaction placement goes through asyncio.to_thread, so give the worker
    # thread a real moment rather than a bare yield.
    await asyncio.sleep(0.1)


class TestErrorReaction:
    @pytest.mark.asyncio
    async def test_failure_after_a_delivered_reply_marks_the_message(self, adapter):
        """✅ already answered the dispatch; ⚠️ has to replace it, not join it."""
        await run_to_completion(adapter, stream_event(), FAILURE, delivered=True)

        assert adapter._reaction_cfg.on_error in emojis(adapter, "reactions_added")
        assert adapter._reaction_cfg.on_success in emojis(adapter, "reactions_removed"), (
            "a failed run must not keep the ✅ that its dispatch return placed"
        )

    @pytest.mark.asyncio
    async def test_a_delivered_success_keeps_todays_reaction(self, adapter):
        """The common case must be untouched: no extra marker, no extra message."""
        await run_to_completion(adapter, stream_event(), SUCCESS, delivered=True)

        assert adapter.client.reactions_added == []
        assert adapter.client.reactions_removed == []
        assert notice_texts(adapter) == []

    @pytest.mark.asyncio
    async def test_never_dispatched_turn_is_untouched(self, adapter):
        """A native command the host answers itself: no arming, so no report."""
        event = stream_event()
        await adapter.on_processing_complete(event, FAILURE)
        await asyncio.sleep(0.1)

        assert adapter.client.reactions_added == []
        assert notice_texts(adapter) == []


class TestSilentRunNotice:
    @pytest.mark.asyncio
    async def test_failure_without_a_reply_marks_and_says_so(self, adapter):
        await run_to_completion(adapter, stream_event(), FAILURE)

        assert adapter._reaction_cfg.on_error in emojis(adapter, "reactions_added")
        notices = notice_texts(adapter)
        assert len(notices) == 1
        assert "failed without a reply" in notices[0]

    @pytest.mark.asyncio
    async def test_empty_successful_run_says_so(self, adapter):
        """The run finished cleanly having sent nothing — the original blind spot."""
        await run_to_completion(adapter, stream_event(), SUCCESS)

        notices = notice_texts(adapter)
        assert len(notices) == 1
        assert "ended without a reply" in notices[0]

    @pytest.mark.asyncio
    async def test_cancelled_run_says_so(self, adapter):
        await run_to_completion(adapter, stream_event(), CANCELLED)

        notices = notice_texts(adapter)
        assert len(notices) == 1
        assert "cancelled" in notices[0]

    @pytest.mark.asyncio
    async def test_a_delivered_failure_does_not_post_a_notice(self, adapter):
        """The reply speaks for itself; only the marker needs correcting."""
        await run_to_completion(adapter, stream_event(), FAILURE, delivered=True)

        assert notice_texts(adapter) == []

    @pytest.mark.asyncio
    async def test_notice_goes_to_the_runs_own_route(self, adapter, monkeypatch):
        event = stream_event(topic="deploy-help", chat_id="999")
        sent = AsyncMock()
        monkeypatch.setattr(adapter, "send", sent)

        await adapter.on_processing_start(event)
        await adapter.on_processing_complete(event, SUCCESS)

        assert sent.await_count == 1
        chat_id, text = sent.await_args.args
        assert chat_id == "999"
        assert "without a reply" in text
        assert sent.await_args.kwargs["metadata"] == {"thread_id": "deploy-help"}

    @pytest.mark.asyncio
    async def test_notice_is_sent_once_per_work_item(self, adapter):
        """A queue drain completing the same item again must not re-report it."""
        event = stream_event()
        await run_to_completion(adapter, event, FAILURE)
        assert len(notice_texts(adapter)) == 1

        # The arming entry was popped by the first completion, so this reports
        # nothing further — the once-per-item guarantee.
        await adapter.on_processing_complete(event, FAILURE)
        await asyncio.sleep(0.1)

        assert len(notice_texts(adapter)) == 1

    @pytest.mark.asyncio
    async def test_a_notice_that_cannot_be_sent_is_dropped(self, adapter, monkeypatch):
        """A failing run must not raise into the gateway loop on its way out."""
        event = stream_event()
        await adapter.on_processing_start(event)

        async def boom(*a, **k):
            raise RuntimeError("zulip is unreachable")

        monkeypatch.setattr(adapter, "send", boom)
        await adapter.on_processing_complete(event, FAILURE)
        await asyncio.sleep(0.1)

        assert notice_texts(adapter) == []

    @pytest.mark.asyncio
    async def test_outcome_reason_is_included_when_the_outcome_carries_one(self, adapter):
        outcome = SimpleNamespace(value="failure", reason="tool timeout")
        await run_to_completion(adapter, stream_event(), outcome)

        notices = notice_texts(adapter)
        assert len(notices) == 1
        assert "tool timeout" in notices[0]

    @pytest.mark.asyncio
    async def test_no_reason_is_invented_when_the_outcome_has_none(self, adapter):
        await run_to_completion(adapter, stream_event(), FAILURE)

        notice = notice_texts(adapter)[0]
        assert "(" not in notice, "the notice must not invent a cause it cannot know"
