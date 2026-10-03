"""Tests for /events poll pacing and the long-poll budget (Issue #146).

The poll loop used to sleep only on *error*: a successful ``get_events`` that
yielded nothing was followed immediately by another ``get_events``. That is
fine while the server holds the long-poll, but a server answering immediately
(a heartbeat, or a short ``event_queue_longpoll_timeout_seconds``) made the
loop spin as fast as the API could answer.

The question the pacing asks is therefore not "were there events?" but "did
the server hold the poll?" — a held poll is already paced and must gain no
delay, so reply latency on healthy realms is unchanged.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from zulip.adapter import (
    LONGPOLL_GRACE_SECONDS,
    LONGPOLL_MAX_SECONDS,
    LONGPOLL_MIN_SECONDS,
    POLL_BACKOFF_MAX,
    POLL_BACKOFF_START,
    POLL_FAST_RETURN_SECONDS,
    _clamp_longpoll_budget,
    _next_poll_backoff,
)


@pytest.fixture
def adapter(mock_platform_config, monkeypatch):
    import zulip.zulip_client as zulip_client_module

    monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", "true")
    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)
    monkeypatch.delenv("ZULIP_READ_TIMEOUT", raising=False)

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                pass

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(mock_platform_config)
    a.client = MagicMock()
    a.client.register.return_value = {
        "result": "success",
        "queue_id": "q1",
        "last_event_id": 7,
        "event_queue_longpoll_timeout_seconds": 90,
    }
    return a


class TestNextPollBackoff:
    def test_held_longpoll_adds_no_delay(self):
        assert _next_poll_backoff(POLL_FAST_RETURN_SECONDS, False, 0.0) == 0.0
        # Even mid-backoff: the server held the poll, so it now paces us.
        assert _next_poll_backoff(52.0, False, POLL_BACKOFF_MAX) == 0.0

    def test_fast_return_backs_off_then_caps(self):
        backoff = 0.0
        sequence = []
        for _ in range(5):
            backoff = _next_poll_backoff(0.2, False, backoff)
            sequence.append(backoff)
        assert sequence == [1.0, 2.0, 4.0, 5.0, 5.0]

    def test_delivered_message_resets_backoff(self):
        assert _next_poll_backoff(0.1, True, POLL_BACKOFF_MAX) == 0.0

    def test_backoff_never_drops_below_start(self):
        assert _next_poll_backoff(0.1, False, 0.0) == POLL_BACKOFF_START

    def test_idle_poll_rate_is_recorded(self):
        """Recorded: 60s of idling at a 0.2s answer goes 300 polls -> 14.

        14, not 12, because the 1s/2s/4s ramp takes ~13s to reach the 5s cap
        and each poll also spends 0.2s answering. The point is the order of
        magnitude, which is what the log-volume complaint was about.
        """

        def polls_in_60s(apply_backoff: bool) -> int:
            backoff, polls, elapsed = 0.0, 0, 0.0
            while elapsed < 60.0:
                polls += 1
                elapsed += 0.2  # the server answers immediately, no events
                if apply_backoff:
                    backoff = _next_poll_backoff(0.2, False, backoff)
                    elapsed += backoff
            return polls

        unthrottled = polls_in_60s(False)
        throttled = polls_in_60s(True)
        assert unthrottled == 300
        assert throttled == 14, f"recorded 14, got {throttled}"


class TestClampLongpollBudget:
    def test_zulip_default_passes_through(self):
        assert _clamp_longpoll_budget(90) == 90.0

    def test_string_number_is_accepted(self):
        assert _clamp_longpoll_budget("45") == 45.0

    def test_above_max_is_clamped(self):
        assert _clamp_longpoll_budget(100000) == LONGPOLL_MAX_SECONDS

    def test_below_min_is_clamped(self):
        assert _clamp_longpoll_budget(0.2) == LONGPOLL_MIN_SECONDS

    @pytest.mark.parametrize("bad", [None, "", "abc", 0, -5, [], {}])
    def test_unusable_values_are_rejected(self, bad):
        assert _clamp_longpoll_budget(bad) is None


class TestRegisterQueueBudget:
    def test_register_asks_for_realm_event_types(self, adapter):
        """Without fetch_event_types the server omits the long-poll budget."""
        adapter._register_queue()
        kwargs = adapter.client.register.call_args.kwargs
        assert kwargs["fetch_event_types"] == ["realm"]
        # Stable topic sessions require the full subscription (rename and
        # deletion events) — a bare ["message"] would re-trigger the stale
        # queue-subscription bug the queue manager guards against.
        assert kwargs["event_types"] == [
            "message", "update_message", "delete_message",
        ]
        assert kwargs["fetch_event_id"] == 0

    def test_abort_budget_is_raised_above_the_servers_budget(self, adapter):
        """Our 60s read timeout is shorter than Zulip's 90s default."""
        assert adapter._events_timeout == adapter._read_timeout == 60.0
        adapter._register_queue()
        assert adapter._events_timeout == 90.0 + LONGPOLL_GRACE_SECONDS

    def test_missing_budget_keeps_the_configured_timeout(self, adapter):
        adapter.client.register.return_value = {"queue_id": "q", "last_event_id": 1}
        adapter._register_queue()
        assert adapter._events_timeout == adapter._read_timeout

    def test_out_of_range_budget_is_clamped(self, adapter):
        adapter.client.register.return_value = {
            "queue_id": "q",
            "last_event_id": 1,
            "event_queue_longpoll_timeout_seconds": 100000,
        }
        adapter._register_queue()
        assert adapter._events_timeout == LONGPOLL_MAX_SECONDS + LONGPOLL_GRACE_SECONDS

    def test_a_longer_configured_timeout_is_not_shortened(self, adapter):
        adapter._read_timeout = 300.0
        adapter._events_timeout = 300.0
        adapter.client.register.return_value = {
            "queue_id": "q",
            "last_event_id": 1,
            "event_queue_longpoll_timeout_seconds": 1,
        }
        adapter._register_queue()
        assert adapter._events_timeout == 300.0

    def test_non_dict_register_result_is_tolerated(self, adapter):
        adapter.client.register.return_value = {"queue_id": "q", "last_event_id": 1}
        assert adapter._register_queue()["queue_id"] == "q"


class TestSdkCallTimeoutContract:
    @pytest.mark.asyncio
    async def test_timeout_is_consumed_by_the_wrapper_not_sent_to_the_sdk(self, adapter):
        """``/events`` accepts only queue_id, last_event_id and dont_block.

        ``timeout=`` is our client-side abort budget and must stay on
        ``_sdk_call``. If it were forwarded it would end up in the query string,
        where Zulip silently ignores unknown parameters — so the regression
        would be invisible without ``spec_set`` failing the call outright.
        """
        client = MagicMock(spec_set=["get_events"])
        client.get_events.return_value = {"result": "success", "events": []}

        await adapter._sdk_call(
            client.get_events, queue_id="q", last_event_id=1, timeout=5.0
        )

        client.get_events.assert_called_once_with(queue_id="q", last_event_id=1)


class TestPollLoopPacing:
    @pytest.mark.asyncio
    async def test_fast_answers_are_paced_by_the_loop_itself(self, adapter, monkeypatch):
        """The loop must apply the delay, not merely compute it."""
        import zulip.settings as settings_module

        # The pacing constants are owned by zulip.settings and read by
        # zulip.settings.next_poll_backoff at call time, so they must be
        # patched there — patching zulip.adapter would not take effect.
        monkeypatch.setattr(settings_module, "POLL_BACKOFF_START", 0.01)
        monkeypatch.setattr(settings_module, "POLL_BACKOFF_MAX", 0.04)

        seen_timeouts = []

        async def fake_sdk_call(fn, *args, timeout=None, **kwargs):
            seen_timeouts.append(timeout)
            if len(seen_timeouts) >= 4:
                adapter._listening = False
            return {"result": "success", "events": []}

        monkeypatch.setattr(adapter, "_sdk_call", fake_sdk_call)
        adapter._queue_mgr.ensure_queue = AsyncMock(
            return_value=SimpleNamespace(queue_id="q", last_event_id=1)
        )
        adapter._listening = True

        loop = __import__("asyncio").get_event_loop()
        started = loop.time()
        await adapter._listen_for_events()
        elapsed = loop.time() - started

        assert len(seen_timeouts) == 4
        # 0.01 + 0.02 + 0.04 + 0.04 of pacing actually happened.
        assert elapsed >= 0.1
        # The pacing must come from the *patched* settings constants. The
        # unpatched defaults (1.0/5.0) would pace this loop for ~12s, so this
        # upper bound is what makes a stale patch (one that no-ops) fail
        # instead of passing slowly.
        assert elapsed < 5.0
        # ...and the poll used the learned abort budget, not the read default.
        assert seen_timeouts[0] == adapter._events_timeout
