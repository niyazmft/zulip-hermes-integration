"""Tests for the per-session message queue (issue #151).

A Zulip topic is one session, so a message arriving mid-run used to be steered
into the running turn — letting one person in a shared topic redirect another
person's work. These tests drive the adapter's inbound path directly (the same
way the other adapter tests do) and assert the ordering rules: same session
waits its turn, different topics and DMs do not, the cap never drops a message,
and the whole thing is inert unless ZULIP_SESSION_QUEUE is set.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from zulip.reactions import ReactionConfig, ReactionLifecycle
from zulip.session_queue import (
    DEFAULT_QUEUE_CAP,
    PendingTurn,
    SessionQueue,
    SessionQueueConfig,
)

SUCCESS = SimpleNamespace(value="success")

EMAIL = "bot@test.zulipchat.com"


# --- helpers ---------------------------------------------------------------


def stream_message(message_id, topic="api-review", stream_id=573423):
    return {
        "id": message_id,
        "type": "stream",
        "stream_id": stream_id,
        "subject": topic,
        "display_recipient": "engineering",
        "content": "hello",
        "sender_email": "user@zulip.com",
        "sender_full_name": "User",
        "sender_id": 42,
        "flags": [],
    }


def dm_message(message_id, sender_id=42):
    return {
        "id": message_id,
        "type": "private",
        "content": "hello",
        "sender_email": "user@zulip.com",
        "sender_full_name": "User",
        "sender_id": sender_id,
        "display_recipient": [
            {"id": 7, "email": EMAIL, "full_name": "Bot"},
            {"id": sender_id, "email": "user@zulip.com", "full_name": "User"},
        ],
    }


def build_adapter(monkeypatch, mock_platform_config, tmp_path):
    """An adapter whose client and gateway dispatch are both recorded."""
    import zulip.adapter as adapter_module
    import zulip.zulip_client as zulip_client_module

    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)
    monkeypatch.setenv("ZULIP_SITE", "https://test.zulipchat.com")
    monkeypatch.setenv("ZULIP_EMAIL", EMAIL)
    monkeypatch.setenv("ZULIP_API_KEY", "k" * 32)
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                pass

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    adapter_module._clear_caches()

    from zulip.adapter import ZulipAdapter

    adapter = ZulipAdapter(mock_platform_config)
    adapter.client = MagicMock()
    adapter.handle_message = AsyncMock()
    return adapter


def stream_message_event(chat_id, topic):
    return SimpleNamespace(
        source=SimpleNamespace(chat_id=chat_id),
        metadata={"topic": topic},
    )


def reactions_of(client, method, message_id, emoji):
    return [
        call.args[0]
        for call in getattr(client, method).call_args_list
        if call.args[0].get("message_id") == message_id
        and call.args[0].get("emoji_name") == emoji
    ]


async def deliver(adapter, message):
    """Send one message on the arrival path; return the event the gateway got."""
    before = adapter.handle_message.call_count
    await adapter._handle_message(message)
    assert adapter.handle_message.call_count == before + 1, "expected a dispatch"
    return adapter.handle_message.call_args[0][0]


def delivered_ids(adapter):
    return [call.args[0].message_id for call in adapter.handle_message.call_args_list]


def audit_events(tmp_path, event_type):
    """Every audit line of one type, oldest first."""
    path = Path(tmp_path) / "audit" / f"{EMAIL}.audit.log"
    if not path.exists():
        return []
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [entry for entry in lines if entry.get("event") == event_type]


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    monkeypatch.setenv("ZULIP_SESSION_QUEUE", "1")
    return build_adapter(monkeypatch, mock_platform_config, tmp_path)


@pytest.fixture
def capped_adapter(mock_platform_config, monkeypatch, tmp_path):
    monkeypatch.setenv("ZULIP_SESSION_QUEUE", "1")
    monkeypatch.setenv("ZULIP_QUEUE_CAP", "2")
    return build_adapter(monkeypatch, mock_platform_config, tmp_path)


# --- the queue itself ------------------------------------------------------


class TestSessionQueueState:
    def test_fifo_order_and_depth(self):
        queue = SessionQueue(SessionQueueConfig(enabled=True, cap=3))
        first, second = object(), object()
        assert queue.enqueue("s", PendingTurn(first, None, None, "1")) == (True, 1)
        assert queue.enqueue("s", PendingTurn(second, None, None, "2")) == (True, 2)
        assert queue.depth("s") == 2
        assert queue.dequeue("s").event is first
        assert queue.dequeue("s").event is second
        assert queue.dequeue("s") is None
        assert queue.depth("s") == 0

    def test_sessions_are_independent(self):
        queue = SessionQueue(SessionQueueConfig(enabled=True, cap=2))
        turn = PendingTurn(object(), None, None, "1")
        queue.enqueue("a", turn)
        assert queue.depth("a") == 1
        assert queue.depth("b") == 0
        assert queue.dequeue("b") is None
        assert queue.depth("a") == 1, "another session's dequeue must not drain this one"

    def test_full_queue_refuses_and_reports_depth(self):
        queue = SessionQueue(SessionQueueConfig(enabled=True, cap=1))
        assert queue.enqueue("s", PendingTurn(object(), None, None, "1")) == (True, 1)
        assert queue.enqueue("s", PendingTurn(object(), None, None, "2")) == (False, 1)

    def test_activity_is_per_session(self):
        queue = SessionQueue(SessionQueueConfig(enabled=True))
        queue.mark_active("a")
        assert queue.is_active("a") is True
        assert queue.is_active("b") is False
        queue.release("a")
        assert queue.is_active("a") is False

    def test_default_cap(self):
        assert SessionQueueConfig().cap == DEFAULT_QUEUE_CAP == 20
        assert SessionQueueConfig().enabled is False


# --- the queue wired into the adapter -------------------------------------


class TestMidRunMessages:
    @pytest.mark.asyncio
    async def test_mid_run_message_waits_then_runs_in_order(self, adapter):
        first = await deliver(adapter, stream_message(1))
        await adapter.on_processing_start(first)

        # Arrive while the first run is in flight: neither may be dispatched.
        await adapter._handle_message(stream_message(2))
        await adapter._handle_message(stream_message(3))
        assert delivered_ids(adapter) == ["1"], "a mid-run message must not steer the run"

        await adapter.on_processing_complete(first, SUCCESS)
        assert delivered_ids(adapter) == ["1", "2"], "oldest queued turn runs first"

        # The host reports the newly dispatched run, then its completion.
        second = adapter.handle_message.call_args[0][0]
        await adapter.on_processing_start(second)
        await adapter.on_processing_complete(second, SUCCESS)
        assert delivered_ids(adapter) == ["1", "2", "3"]

    @pytest.mark.asyncio
    async def test_a_queued_turn_is_dispatched_with_its_own_event(self, adapter):
        first = await deliver(adapter, stream_message(1))
        await adapter.on_processing_start(first)
        await adapter._handle_message(stream_message(2, topic="api-review"))

        await adapter.on_processing_complete(first, SUCCESS)
        queued_event = adapter.handle_message.call_args[0][0]
        assert queued_event.message_id == "2"
        assert queued_event.source.chat_id == "573423"
        assert queued_event.metadata["topic"] == "api-review"

    @pytest.mark.asyncio
    async def test_a_queued_turn_is_not_queued_twice(self, adapter):
        """The drained turn's own completion must not re-run it."""
        first = await deliver(adapter, stream_message(1))
        await adapter.on_processing_start(first)
        await adapter._handle_message(stream_message(2))
        await adapter.on_processing_complete(first, SUCCESS)

        second = adapter.handle_message.call_args[0][0]
        await adapter.on_processing_start(second)
        await adapter.on_processing_complete(second, SUCCESS)
        await adapter.on_processing_complete(second, SUCCESS)
        assert delivered_ids(adapter) == ["1", "2"]


class TestWaitingMarker:
    @pytest.mark.asyncio
    async def test_waiting_message_shows_hourglass_which_dispatch_removes(self, adapter):
        first = await deliver(adapter, stream_message(1))
        await adapter.on_processing_start(first)
        await adapter._handle_message(stream_message(2))

        assert reactions_of(adapter.client, "add_reaction", "2", "hourglass"), (
            "a waiting message must show the hourglass"
        )
        # 👀 still means received and ✅ still means finished.
        assert reactions_of(adapter.client, "add_reaction", "2", "eyes")

        await adapter.on_processing_complete(first, SUCCESS)
        assert reactions_of(adapter.client, "remove_reaction", "2", "hourglass"), (
            "dispatch must clear the waiting marker"
        )
        assert reactions_of(adapter.client, "add_reaction", "2", "check_mark"), (
            "dispatch leaves the normal finished marker behind"
        )

    @pytest.mark.asyncio
    async def test_a_dispatched_on_arrival_message_never_shows_the_hourglass(self, adapter):
        await deliver(adapter, stream_message(1))
        assert not reactions_of(adapter.client, "add_reaction", "1", "hourglass")


class TestWaitingReaction:
    @pytest.mark.asyncio
    async def test_queued_adds_the_hourglass_and_unqueued_removes_it(self):
        client = MagicMock()
        lifecycle = ReactionLifecycle(client, "msg_1", ReactionConfig())
        await lifecycle.queued()
        client.add_reaction.assert_called_once_with(
            {"message_id": "msg_1", "emoji_name": "hourglass"}
        )
        await lifecycle.unqueued()
        client.remove_reaction.assert_called_once_with(
            {"message_id": "msg_1", "emoji_name": "hourglass"}
        )

    @pytest.mark.asyncio
    async def test_disabled_reactions_mean_no_hourglass(self):
        client = MagicMock()
        lifecycle = ReactionLifecycle(client, "msg_1", ReactionConfig(enabled=False))
        await lifecycle.queued()
        await lifecycle.unqueued()
        client.add_reaction.assert_not_called()
        client.remove_reaction.assert_not_called()


class TestNoGlobalSerialization:
    @pytest.mark.asyncio
    async def test_two_topics_in_one_stream_run_in_parallel(self, adapter):
        first = await deliver(adapter, stream_message(1, topic="api-review"))
        await adapter.on_processing_start(first)

        second = await deliver(adapter, stream_message(2, topic="deploys"))
        assert delivered_ids(adapter) == ["1", "2"]
        assert second.metadata["topic"] == "deploys"
        assert not reactions_of(adapter.client, "add_reaction", "2", "hourglass")

    @pytest.mark.asyncio
    async def test_two_dms_run_in_parallel(self, adapter):
        first = await deliver(adapter, dm_message(1, sender_id=42))
        await adapter.on_processing_start(first)

        second = await deliver(adapter, dm_message(2, sender_id=99))
        assert delivered_ids(adapter) == ["1", "2"]
        assert second.source.chat_type == "dm"
        assert not reactions_of(adapter.client, "add_reaction", "2", "hourglass")

    @pytest.mark.asyncio
    async def test_a_dm_inside_one_chat_waits_for_that_chat(self, adapter):
        first = await deliver(adapter, dm_message(1, sender_id=42))
        await adapter.on_processing_start(first)

        await adapter._handle_message(dm_message(2, sender_id=42))
        assert delivered_ids(adapter) == ["1"]
        assert reactions_of(adapter.client, "add_reaction", "2", "hourglass")

        await adapter.on_processing_complete(first, SUCCESS)
        assert delivered_ids(adapter) == ["1", "2"]

    @pytest.mark.asyncio
    async def test_the_host_session_key_wins_when_the_host_exposes_one(self, adapter):
        """The queue must agree with the host about what a session is."""
        adapter._event_session_key = lambda event: f"host:{event.source.chat_id}"
        assert adapter._queue_session_key(stream_message_event("573423", "a")) == "host:573423"
        assert adapter._queue_session_key(stream_message_event("573423", "b")) == "host:573423"


class TestCap:
    @pytest.mark.asyncio
    async def test_past_the_cap_a_message_dispatches_immediately(self, capped_adapter):
        first = await deliver(capped_adapter, stream_message(1))
        await capped_adapter.on_processing_start(first)

        await capped_adapter._handle_message(stream_message(2))
        await capped_adapter._handle_message(stream_message(3))
        assert delivered_ids(capped_adapter) == ["1"]

        # The third waiting turn is over the cap: dispatch, never drop.
        third = await deliver(capped_adapter, stream_message(4))
        assert delivered_ids(capped_adapter) == ["1", "4"]
        assert third.message_id == "4"
        assert not reactions_of(capped_adapter.client, "add_reaction", "4", "hourglass")

    @pytest.mark.asyncio
    async def test_an_over_cap_message_is_not_queued_behind_the_cap(
        self, capped_adapter
    ):
        first = await deliver(capped_adapter, stream_message(1))
        await capped_adapter.on_processing_start(first)
        await capped_adapter._handle_message(stream_message(2))
        await capped_adapter._handle_message(stream_message(3))
        await capped_adapter._handle_message(stream_message(4))
        assert delivered_ids(capped_adapter) == ["1", "4"]

        await capped_adapter.on_processing_complete(first, SUCCESS)
        assert delivered_ids(capped_adapter) == ["1", "4", "2"]

        second = capped_adapter.handle_message.call_args[0][0]
        await capped_adapter.on_processing_start(second)
        await capped_adapter.on_processing_complete(second, SUCCESS)
        # 4 ran on arrival; only 3 was left waiting behind 2.
        assert delivered_ids(capped_adapter) == ["1", "4", "2", "3"]


class TestAuditTrail:
    @pytest.mark.asyncio
    async def test_enqueue_and_dequeue_record_the_depth(self, adapter, tmp_path):
        first = await deliver(adapter, stream_message(1))
        await adapter.on_processing_start(first)
        await adapter._handle_message(stream_message(2))
        await adapter._handle_message(stream_message(3))
        await adapter.on_processing_complete(first, SUCCESS)

        enqueued = audit_events(tmp_path, "session_queue_enqueue")
        assert [entry["details"]["depth"] for entry in enqueued] == [1, 2]
        assert [entry["details"]["message_id"] for entry in enqueued] == ["2", "3"]

        dequeued = audit_events(tmp_path, "session_queue_dequeue")
        assert [entry["details"]["depth"] for entry in dequeued] == [1]
        assert dequeued[0]["details"]["message_id"] == "2"
        assert dequeued[0]["details"]["chat_id"] == "573423"

    @pytest.mark.asyncio
    async def test_overflow_is_audited(self, capped_adapter, tmp_path):
        first = await deliver(capped_adapter, stream_message(1))
        await capped_adapter.on_processing_start(first)
        await capped_adapter._handle_message(stream_message(2))
        await capped_adapter._handle_message(stream_message(3))
        await capped_adapter._handle_message(stream_message(4))

        overflow = audit_events(tmp_path, "session_queue_overflow")
        assert len(overflow) == 1
        assert overflow[0]["details"] == {
            "chat_id": "573423",
            "message_id": "4",
            "depth": 2,
        }

    @pytest.mark.asyncio
    async def test_the_audit_records_ids_not_bodies(self, adapter, tmp_path):
        first = await deliver(adapter, stream_message(1))
        await adapter.on_processing_start(first)
        await adapter._handle_message(stream_message(2))

        path = Path(tmp_path) / "audit" / f"{EMAIL}.audit.log"
        assert "hello" not in path.read_text()


class TestInertByDefault:
    @pytest.mark.asyncio
    async def test_no_queue_is_built_without_the_flag(
        self, mock_platform_config, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("ZULIP_SESSION_QUEUE", raising=False)
        adapter = build_adapter(monkeypatch, mock_platform_config, tmp_path)
        assert adapter._session_queue is None

    @pytest.mark.asyncio
    async def test_a_mid_run_message_dispatches_as_before(
        self, mock_platform_config, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("ZULIP_SESSION_QUEUE", raising=False)
        adapter = build_adapter(monkeypatch, mock_platform_config, tmp_path)

        first = await deliver(adapter, stream_message(1))
        await adapter.on_processing_start(first)
        await deliver(adapter, stream_message(2))

        assert delivered_ids(adapter) == ["1", "2"]
        emojis = [
            call.args[0]["emoji_name"]
            for call in adapter.client.add_reaction.call_args_list
        ]
        assert "hourglass" not in emojis, (
            "with the flag off no marker may change: \U0001f440 and \u2705 only"
        )

    @pytest.mark.asyncio
    async def test_completion_writes_no_queue_audit(
        self, mock_platform_config, monkeypatch, tmp_path
    ):
        monkeypatch.delenv("ZULIP_SESSION_QUEUE", raising=False)
        adapter = build_adapter(monkeypatch, mock_platform_config, tmp_path)
        first = await deliver(adapter, stream_message(1))
        await adapter.on_processing_start(first)
        await adapter.on_processing_complete(first, SUCCESS)

        for event_type in (
            "session_queue_enqueue",
            "session_queue_dequeue",
            "session_queue_overflow",
        ):
            assert audit_events(tmp_path, event_type) == []

    @pytest.mark.asyncio
    async def test_without_host_lifecycle_hooks_the_queue_is_disabled(
        self, mock_platform_config, monkeypatch, tmp_path
    ):
        """No ProcessingOutcome means no completion signal, so no queueing."""
        import zulip.adapter as adapter_module

        monkeypatch.setenv("ZULIP_SESSION_QUEUE", "1")
        monkeypatch.setattr(adapter_module, "ProcessingOutcome", None)
        adapter = build_adapter(monkeypatch, mock_platform_config, tmp_path)

        assert adapter._session_queue is None
        # Behaviour is today's: a second mid-run message dispatches immediately.
        first = await deliver(adapter, stream_message(1))
        await adapter.on_processing_start(first)
        await deliver(adapter, stream_message(2))
        assert delivered_ids(adapter) == ["1", "2"]
