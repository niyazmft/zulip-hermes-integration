"""Injected-context measurement on the activity-trace board (#225).

The board is visible in the room, so this feature carries one rule beyond its
format: **counts and outcomes only, never a message body**. Every test below
either pins that, or pins the two things the issue is explicit about — a board
with no context must stay exactly as short as it was, and a harvest that was
dropped must not look like a harvest that found nothing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from zulip import history
from zulip.activity_trace import TraceConfig, TraceState


# --------------------------------------------------------------- the line


def test_both_blocks_render_one_compact_line():
    report = history.ContextReport(
        history_chars=1204,
        history_messages=5,
        observed_chars=380,
        observed_messages=3,
        harvest=history.HARVEST_INJECTED,
    )
    assert history.render_context_line(report) == (
        "context: history 1,204 chars · observed 380 chars (3 msgs) · ~396 tok"
    )


def test_only_the_injected_block_is_named():
    """An absent block is omitted, never shown as zero (acceptance 3)."""
    line = history.render_context_line(
        history.ContextReport(observed_chars=380, observed_messages=3)
    )
    assert "history" not in line
    assert line == "context: observed 380 chars (3 msgs) · ~95 tok"


def test_one_message_is_singular():
    line = history.render_context_line(
        history.ContextReport(observed_chars=40, observed_messages=1)
    )
    assert "(1 msg)" in line
    assert "msgs" not in line


def test_a_turn_that_harvested_nothing_keeps_the_board_unchanged():
    """Turn-on cost is nil for installs that never harvest (acceptance 3)."""
    assert history.render_context_line(history.ContextReport()) == ""
    assert history.render_context_line(None) == ""


@pytest.mark.parametrize(
    "outcome, phrase",
    [
        (history.HARVEST_TIMEOUT, "timed out"),
        (history.HARVEST_FAILED, "failed"),
        (history.HARVEST_EMPTY, "empty"),
    ],
)
def test_a_dropped_harvest_is_named_not_shown_as_zero(outcome, phrase):
    """A slow realm must not look like an empty one (acceptance 4)."""
    line = history.render_context_line(history.ContextReport(harvest=outcome))
    assert phrase in line
    assert "0 chars" not in line
    assert "tok" not in line


def test_dropped_outcomes_are_distinguishable_from_each_other():
    lines = {
        outcome: history.render_context_line(history.ContextReport(harvest=outcome))
        for outcome in (
            history.HARVEST_TIMEOUT,
            history.HARVEST_FAILED,
            history.HARVEST_EMPTY,
        )
    }
    assert len(set(lines.values())) == 3


def test_an_injected_block_and_a_dropped_harvest_both_show():
    line = history.render_context_line(
        history.ContextReport(
            observed_chars=40,
            observed_messages=1,
            harvest=history.HARVEST_TIMEOUT,
        )
    )
    assert "observed 40 chars (1 msg)" in line
    assert "timed out" in line


def test_measure_block_counts_rendered_lines():
    assert history.measure_block("") == (0, 0)
    assert history.measure_block("[a] one") == (7, 1)
    assert history.measure_block("[a] one\n[b] two") == (15, 2)


# ------------------------------------------------------------- the board


def test_the_context_line_sits_under_the_title():
    state = TraceState(title="Working")
    state.context = "context: history 12 chars · ~3 tok"
    state.add("read config")
    lines = state.render().splitlines()
    assert lines[0] == "**Working**"
    assert lines[2] == state.context
    assert "… read config" in lines


def test_no_measurement_renders_exactly_as_it_did_before():
    state = TraceState(title="Working")
    state.add("only step")
    assert state.render() == "**Working**\n\n… only step"


def test_a_long_run_trims_steps_not_the_measurement():
    state = TraceState(title="Working")
    state.context = "context: history 120,000 chars · ~30,000 tok"
    for i in range(200):
        state.add(f"step-number-{i:03d}-with-some-length")
    text = state.render(max_content=400)
    assert len(text) <= 400
    assert state.context in text
    assert "step-number-199" in text
    assert "step-number-000" not in text


# ------------------------------------------------------ the adapter seam


@pytest.fixture
def make_adapter(mock_platform_config, monkeypatch):
    """An adapter with a fake client; the harvest itself is patched per test."""

    def _make(**env):
        import zulip.zulip_client as zulip_client_module

        monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

        class _Client:
            def get_messages(self, request):
                return {"result": "success", "messages": []}

            def send_message(self, request):
                return {"result": "success", "id": 1}

            def update_message(self, request):
                return {"result": "success"}

            def set_typing_status(self, request):
                return {"result": "success"}

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = _Client()

                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
        for key in ("ZULIP_OBSERVE_GROUP", "ZULIP_PROFILE", "ZULIP_HISTORY_MODE"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)
        adapter.email = "bot@test.zulipchat.com"
        adapter.bot_full_name = "Test Bot"
        adapter.handle_message = AsyncMock()
        return adapter

    return _make


def _stream_msg(content, *, topic="deploy", stream="engineering"):
    """A stream message shaped the way ``_observe_stream_message`` reads it."""
    return {
        "id": 1,
        "type": "stream",
        "stream_id": 42,
        "subject": topic,
        "display_recipient": stream,
        "content": content,
        "sender_email": "alice@zulip.com",
        "sender_full_name": "Alice",
        "sender_id": 9,
        "flags": [],
    }


async def _turn(adapter, *, content="what do you think?"):
    """One addressed stream turn; returns (quoted text, ContextReport)."""
    report: list = []
    quoted = await adapter._quoted_topic_history(
        "engineering",
        42,
        "deploy",
        content=content,
        addressed=True,
        report_out=report,
    )
    assert len(report) == 1
    return quoted, report[0]


@pytest.mark.asyncio
async def test_observed_block_is_measured(make_adapter):
    adapter = make_adapter(ZULIP_OBSERVE_GROUP="true")
    adapter._observe_stream_message(_stream_msg("the build is green"), "the build is green")

    quoted, report = await _turn(adapter)

    assert history.OBSERVED_HISTORY_LABEL in quoted
    # Measured on the rendered block body, not on the label.
    assert report.observed_chars == len("[Alice] the build is green")
    assert report.observed_messages == 1
    assert report.history_chars == 0


@pytest.mark.asyncio
async def test_a_turn_with_nothing_to_quote_reports_nothing(make_adapter):
    adapter = make_adapter()
    quoted, report = await _turn(adapter)
    assert quoted == ""
    assert report == history.ContextReport()


@pytest.mark.asyncio
async def test_a_command_is_never_measured_or_harvested(make_adapter):
    """A slash command belongs to the gateway: no harvest, no measurement."""
    adapter = make_adapter(ZULIP_HISTORY_MODE="always")
    adapter._fetch_stream_history = AsyncMock(return_value=[])

    quoted, report = await _turn(adapter, content="/status")

    assert quoted == ""
    adapter._fetch_stream_history.assert_not_awaited()
    assert report.harvest == history.HARVEST_NOT_ATTEMPTED


@pytest.mark.asyncio
async def test_an_empty_harvest_is_labelled(make_adapter):
    adapter = make_adapter(ZULIP_HISTORY_MODE="always")
    adapter._fetch_stream_history = AsyncMock(return_value=[])

    quoted, report = await _turn(adapter)

    assert quoted == ""
    assert report.harvest == history.HARVEST_EMPTY


@pytest.mark.asyncio
async def test_a_timed_out_harvest_is_labelled(make_adapter):
    adapter = make_adapter(ZULIP_HISTORY_MODE="always")

    async def _slow(*a, **k):
        raise asyncio.TimeoutError

    adapter._fetch_stream_history = _slow

    quoted, report = await _turn(adapter)

    assert quoted == ""
    assert report.harvest == history.HARVEST_TIMEOUT


@pytest.mark.asyncio
async def test_a_failed_harvest_is_labelled(make_adapter):
    adapter = make_adapter(ZULIP_HISTORY_MODE="always")
    adapter._fetch_stream_history = AsyncMock(side_effect=RuntimeError("boom"))

    quoted, report = await _turn(adapter)

    assert quoted == ""
    assert report.harvest == history.HARVEST_FAILED


@pytest.mark.asyncio
async def test_an_injected_history_block_is_measured(make_adapter):
    adapter = make_adapter(ZULIP_HISTORY_MODE="always")
    adapter._fetch_stream_history = AsyncMock(
        return_value=[
            {
                "id": 1,
                "timestamp": 2**31,
                "sender_full_name": "Alice",
                "content": "we agreed on plan B",
            },
            {
                "id": 2,
                "timestamp": 2**31,
                "sender_full_name": "Bob",
                "content": "yes, plan B",
            },
        ]
    )

    quoted, report = await _turn(adapter)

    assert history.FETCHED_HISTORY_LABEL in quoted
    assert report.harvest == history.HARVEST_INJECTED
    assert report.history_messages == 2
    assert report.history_chars == len("[Alice] we agreed on plan B\n[Bob] yes, plan B")


@pytest.mark.asyncio
async def test_the_report_carries_counts_not_content(make_adapter):
    """The board is in the room: a message body must never be renderable there."""
    adapter = make_adapter(ZULIP_OBSERVE_GROUP="true")
    secret = "PROJECT-X-ACQUISITION-PRICE-42"
    adapter._observe_stream_message(_stream_msg(secret), secret)

    _quoted, report = await _turn(adapter)

    rendered = history.render_context_line(report)
    assert secret not in rendered
    assert not any(
        secret in str(getattr(report, field)) for field in report.__dataclass_fields__
    )


# ------------------------------------------------------ the trace wiring


def _event(report, *, thread_id="conv-1"):
    return SimpleNamespace(
        source=SimpleNamespace(chat_id="573423", thread_id=thread_id),
        _zulip_meta={"thread_id": thread_id, "topic": "deploy", "context_report": report},
    )


def _stub_adapter(monkeypatch):
    from zulip import tracing

    async def _no_task(*a, **k):
        return None

    monkeypatch.setattr(tracing, "start_trace_and_persist", _no_task)
    monkeypatch.setattr(tracing, "_session_aliases", lambda adapter, event: [])

    return SimpleNamespace(
        _trace_cfg=TraceConfig(enabled=True, coalesce_ms=0, board_ttl=0),
        _traces={},
        _trace_sessions={},
        _trace_started={},
        _trace_start_tasks={},
        _send_timeout=5.0,
        client=SimpleNamespace(send_message=None, update_message=None),
        _sdk_call=AsyncMock(return_value=None),
        _audit_logger=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_start_trace_records_the_measured_context(monkeypatch):
    from zulip import tracing

    adapter = _stub_adapter(monkeypatch)
    report = history.ContextReport(
        history_chars=1204,
        history_messages=5,
        observed_chars=380,
        observed_messages=3,
        harvest=history.HARVEST_INJECTED,
    )

    tracing.start_trace(adapter, _event(report))

    trace = next(iter(adapter._traces.values()))
    assert trace.state.context == (
        "context: history 1,204 chars · observed 380 chars (3 msgs) · ~396 tok"
    )


@pytest.mark.asyncio
async def test_a_broken_measurement_leaves_the_board_unchanged(monkeypatch):
    """Best-effort by contract: a failure must never break or shorten the trace."""
    from zulip import tracing

    adapter = _stub_adapter(monkeypatch)

    tracing.start_trace(adapter, _event("not a report"))

    trace = next(iter(adapter._traces.values()))
    assert trace.state.context == ""
    assert trace.state.render() == "**Working**\n"
