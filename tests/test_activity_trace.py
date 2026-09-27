"""Tests for the activity-trace engine (epic #139, issue #157).

The engine owns three properties that are easy to get subtly wrong, so each has
a test that fails loudly if it regresses:

* **coalescing** — a burst of steps becomes one edit, not one edit per step;
* **a hard rate ceiling** — the burst cannot outrun Zulip's edit API, whose
  round-trip is ~600 ms;
* **failure isolation** — a failing post or edit can never fail or delay the
  agent's run, and nothing is ever deleted.

Time is injected (a fake clock advanced by the fake sleep) so the pacing is
deterministic instead of timing-dependent.
"""

import asyncio

import pytest

from zulip.activity_trace import (
    DEFAULT_MAX_CONTENT,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_RUNNING,
    ActivityTrace,
    EditPacer,
    TraceConfig,
    TraceState,
)


class FakeClock:
    """Monotonic clock advanced only by the injected sleep."""

    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.slept += seconds


class Recorder:
    """Records post/edit calls. No delete method exists, by design."""

    def __init__(self, post_result=101, edit_result=True, post_raises=None, edit_raises=None):
        self.posts = []
        self.edits = []
        self._post_result = post_result
        self._edit_result = edit_result
        self._post_raises = post_raises
        self._edit_raises = edit_raises

    async def post(self, content):
        self.posts.append(content)
        if self._post_raises:
            raise self._post_raises
        return self._post_result

    async def edit(self, message_id, content):
        self.edits.append((message_id, content))
        if self._edit_raises:
            raise self._edit_raises
        return self._edit_result


def make_trace(recorder, clock=None, **cfg):
    clock = clock or FakeClock()
    settings = {"enabled": True, "coalesce_ms": 400, "max_rate": 2.0}
    settings.update(cfg)
    return (
        ActivityTrace(
            recorder.post,
            recorder.edit,
            TraceConfig(**settings),
            clock=clock.now,
            sleep=clock.sleep,
        ),
        clock,
    )


class TestEditPacer:
    def test_allows_the_limit_then_makes_you_wait(self):
        pacer = EditPacer(2)
        assert pacer.delay_for(0.0) == 0.0
        assert pacer.delay_for(0.0) == 0.0
        assert pacer.delay_for(0.0) == pytest.approx(1.0)

    def test_window_slides(self):
        pacer = EditPacer(2)
        pacer.delay_for(0.0)
        pacer.delay_for(0.0)
        # A second later the earlier reservations have aged out.
        assert pacer.delay_for(1.0) == 0.0

    def test_single_edit_per_second_floor(self):
        """A configured rate below 1 still cannot exceed one edit per second."""
        pacer = EditPacer(0)
        assert pacer.delay_for(0.0) == 0.0
        assert pacer.delay_for(0.0) == pytest.approx(1.0)


class TestRender:
    def test_renders_title_steps_and_note(self):
        state = TraceState(title="Working")
        state.add("read config")
        ok = state.add("run tests")
        ok.status = STATUS_OK
        bad = state.add("deploy")
        bad.status = STATUS_FAILED
        state.note = "Done."
        text = state.render()
        assert "**Working**" in text
        assert "… read config" in text
        assert "✓ run tests" in text
        assert "✗ deploy" in text
        assert "Done." in text

    def test_truncation_keeps_the_most_recent_steps(self):
        state = TraceState(title="Working")
        for i in range(200):
            state.add(f"step-number-{i:03d}-with-some-length")
        text = state.render(max_content=400)
        assert len(text) <= 400
        assert "step-number-199" in text
        assert "step-number-000" not in text

    def test_step_detail_is_rendered(self):
        state = TraceState()
        step = state.add("fetch")
        step.status = STATUS_OK
        step.detail = "12 rows"
        assert "✓ fetch — 12 rows" in state.render()


class TestConfig:
    def test_disabled_by_default(self):
        assert TraceConfig.from_env({}).enabled is False

    def test_enabled_by_env(self):
        assert TraceConfig.from_env({"ZULIP_ACTIVITY_TRACE": "1"}).enabled is True

    def test_tuning_knobs(self):
        cfg = TraceConfig.from_env(
            {
                "ZULIP_ACTIVITY_TRACE": "1",
                "ZULIP_TRACE_COALESCE_MS": "250",
                "ZULIP_TRACE_MAX_RATE": "5",
            }
        )
        assert cfg.coalesce_ms == 250
        assert cfg.max_rate == 5.0

    @pytest.mark.parametrize(
        "raw", ["", "abc", "  ", "0x10"]
    )
    def test_bad_numbers_fall_back_to_defaults(self, raw):
        cfg = TraceConfig.from_env({"ZULIP_TRACE_COALESCE_MS": raw})
        assert cfg.coalesce_ms == 400

    def test_rate_has_a_positive_floor(self):
        assert TraceConfig.from_env({"ZULIP_TRACE_MAX_RATE": "0"}).max_rate >= 0.1


class TestOneMessagePerWorkItem:
    @pytest.mark.asyncio
    async def test_steps_edit_the_same_message_and_never_post_again(self):
        rec = Recorder()
        trace, clock = make_trace(rec, coalesce_ms=0)
        assert await trace.start() is True
        assert len(rec.posts) == 1

        for i in range(5):
            step = trace.step(f"step {i}")
            if i == 4:
                await trace._flush()
            trace.complete(step)
        await trace.finish()

        assert len(rec.posts) == 1, "a work item gets exactly one trace message"
        assert rec.edits, "steps must show up as edits"
        assert {msg_id for msg_id, _ in rec.edits} == {101}

    @pytest.mark.asyncio
    async def test_no_delete_path_exists(self):
        """The topic keeps an audit trail, so the interface has no delete."""
        rec = Recorder()
        trace, _ = make_trace(rec, coalesce_ms=0)
        assert not hasattr(trace, "delete")
        assert not hasattr(rec, "delete")
        await trace.start()
        await trace.finish()
        # Only post and edit were ever used.
        assert rec.posts and all(isinstance(c, str) for c in rec.posts)


class TestCoalescing:
    @pytest.mark.asyncio
    async def test_a_burst_of_steps_becomes_one_edit(self):
        rec = Recorder()
        trace, clock = make_trace(rec, coalesce_ms=400)
        await trace.start()

        for i in range(10):
            trace.step(f"step {i}")

        pending = trace._pending
        assert pending is not None
        await pending

        assert len(rec.edits) == 1, f"10 steps should coalesce to 1 edit, got {len(rec.edits)}"
        assert "step 9" in rec.edits[0][1]

    @pytest.mark.asyncio
    async def test_unchanged_render_spends_no_patch(self):
        rec = Recorder()
        trace, _ = make_trace(rec, coalesce_ms=0)
        await trace.start()
        trace.step("only step")
        await trace._flush()
        assert len(rec.edits) == 1

        await trace._flush()
        await trace._flush()
        assert len(rec.edits) == 1, "an identical render must not spend a PATCH"


class TestRateCeiling:
    @pytest.mark.asyncio
    async def test_burst_cannot_outrun_the_ceiling(self):
        """Every step still renders, but never faster than the ceiling allows.

        The engine is *made to wait* rather than to drop updates, so the right
        invariant is edits-per-second, not a total edit count: the fake clock
        advances with each wait and the window keeps sliding.
        """
        rec = Recorder()
        trace, clock = make_trace(rec, coalesce_ms=0, max_rate=2.0)
        await trace.start()

        times: list[float] = []
        original_edit = rec.edit

        async def timed_edit(message_id, content):
            times.append(clock.now())
            return await original_edit(message_id, content)

        trace._edit = timed_edit

        for i in range(6):
            trace.step(f"step {i}")
            await trace._flush()

        # No one-second window may contain more than the configured rate.
        for start in times:
            in_window = [t for t in times if start <= t < start + 1.0]
            assert len(in_window) <= 2, (
                f"max_rate=2/s exceeded: {len(in_window)} edits in [{start}, {start + 1.0})"
            )
        # ...and the engine really did wait rather than ignore the ceiling.
        assert clock.slept >= 1.0
        # The final state still renders (pacing delays, it does not drop).
        assert "step 5" in rec.edits[-1][1]

    @pytest.mark.asyncio
    async def test_all_steps_eventually_render(self):
        """Pacing delays edits; it must not drop the final state."""
        rec = Recorder()
        trace, _ = make_trace(rec, coalesce_ms=0, max_rate=1.0)
        await trace.start()
        for i in range(4):
            trace.step(f"step {i}")
            await trace._flush()
        await trace.finish()

        assert "step 3" in rec.edits[-1][1]
        assert "**Done**" in rec.edits[-1][1]

    @pytest.mark.asyncio
    async def test_final_flush_states_the_terminal_state(self):
        rec = Recorder()
        trace, _ = make_trace(rec, coalesce_ms=0)
        await trace.start()
        trace.step("work")
        await trace.finish(note="finished in 2s")
        assert "**Done**" in rec.edits[-1][1]
        assert "finished in 2s" in rec.edits[-1][1]

        rec2 = Recorder()
        trace2, _ = make_trace(rec2, coalesce_ms=0)
        await trace2.start()
        trace2.step("work")
        await trace2.fail("tool exploded")
        assert "**Failed**" in rec2.edits[-1][1]
        assert "tool exploded" in rec2.edits[-1][1]


class TestFailureIsolation:
    @pytest.mark.asyncio
    async def test_failed_post_drops_the_trace_without_raising(self):
        rec = Recorder(post_raises=RuntimeError("zulip down"))
        trace, _ = make_trace(rec, coalesce_ms=0)

        assert await trace.start() is False
        assert trace.message_id is None

        # The run continues: steps are no-ops and edits never happen.
        assert trace.step("work") is None
        await trace.finish()
        assert rec.edits == []

    @pytest.mark.asyncio
    async def test_post_without_a_message_id_drops_the_trace(self):
        rec = Recorder(post_result=None)
        trace, _ = make_trace(rec, coalesce_ms=0)
        assert await trace.start() is False
        assert rec.edits == []

    @pytest.mark.asyncio
    async def test_failed_edit_is_logged_and_dropped(self):
        rec = Recorder(edit_raises=RuntimeError("edit rejected"))
        trace, _ = make_trace(rec, coalesce_ms=0)
        await trace.start()
        trace.step("work")

        await trace._flush()  # must not raise

        assert trace.edit_count == 0
        assert len(rec.edits) == 1, "a failure must not be retried in a loop"

    @pytest.mark.asyncio
    async def test_rejected_edit_leaves_the_trace_stale_and_converges_later(self):
        """A rejected edit retries on the *next change*, not in a loop.

        ``_last_render`` only advances on success, so while the edit is being
        refused the message on screen is stale and the next change re-sends the
        whole render. That is convergence — the engine never reschedules itself,
        so the attempts are bounded by the number of steps.
        """
        rec = Recorder(edit_result=False)
        trace, _ = make_trace(rec, coalesce_ms=0)
        await trace.start()
        trace.step("first")
        await trace._flush()
        assert len(rec.edits) == 1
        assert trace.edit_count == 0, "a rejected edit is not counted as applied"

        rec._edit_result = True
        trace.step("second")
        await trace._flush()
        assert len(rec.edits) == 2
        assert "second" in rec.edits[-1][1]
        assert trace.edit_count == 1

        # Nothing has changed since the successful edit, so no PATCH is spent.
        await trace._flush()
        assert len(rec.edits) == 2

    @pytest.mark.asyncio
    async def test_disabled_trace_does_nothing(self):
        rec = Recorder()
        trace, _ = make_trace(rec, enabled=False, coalesce_ms=0)
        assert await trace.start() is False
        assert trace.step("work") is None
        await trace.finish()
        assert rec.posts == [] and rec.edits == []

    @pytest.mark.asyncio
    async def test_aclose_cancels_a_pending_flush(self):
        rec = Recorder()
        trace, _ = make_trace(rec, coalesce_ms=5000)
        await trace.start()
        trace.step("work")
        assert trace._pending is not None
        await trace.aclose()
        assert trace._pending is None
        assert rec.edits == [], "a cancelled flush must not edit after close"
