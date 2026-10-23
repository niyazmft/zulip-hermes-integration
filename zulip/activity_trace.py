"""Progressive activity trace — the engine (epic #139, issue #157).

Instead of a lone final reply, the agent's work becomes visible in the topic as
it happens: **one bot-owned status message per work item**, edited in place as
steps resolve.

The rules here come from the sibling plugin and matter more than they look:

* **Status detail edits the trace; actionable results post a new message.** A
  status board, not one message per tool call.
* The message is **never deleted** — the topic keeps an audit trail.
* Edits are **coalesced** (~400 ms) under a hard **rate ceiling** (2/second):
  Zulip edits are ~600 ms round-trips, so an unthrottled tool loop would hammer
  the API.
* An unchanged render spends **no** PATCH.
* A failed post drops the trace; a failed edit is logged and dropped — **no
  retry loop**, and never on the agent's critical path.

This module is deliberately host-independent: it takes the post and edit calls
as injected coroutines, so the lifecycle wiring (issue #158) can pass
``ZulipAdapter.send``/``update_message`` wrappers without this engine importing
the gateway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import runtime_scope

logger = logging.getLogger(__name__)

DEFAULT_COALESCE_MS = 400
DEFAULT_MAX_RATE = 2.0
DEFAULT_MAX_CONTENT = 3500
# Zulip refuses content edits after the realm's edit window (600 s by default).
# A run can outlive it, so the board is rotated onto a fresh message well
# before that deadline; 420 s leaves room for clock skew and a slow flush.
DEFAULT_BOARD_TTL_S = 420.0

# Step statuses.
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_FAILED = "failed"

_STATUS_MARK = {STATUS_RUNNING: "…", STATUS_OK: "✓", STATUS_FAILED: "✗"}

_FALSY = {"0", "false", "no", "off"}


def _truthy(raw: Optional[str], default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in _FALSY


@dataclass
class TraceConfig:
    """Trace settings. Disabled by default; the config surface is issue #160."""

    enabled: bool = False
    coalesce_ms: int = DEFAULT_COALESCE_MS
    max_rate: float = DEFAULT_MAX_RATE
    max_content: int = DEFAULT_MAX_CONTENT
    # Rotate the status board onto a fresh message before Zulip's edit window
    # closes (0 disables rotation).
    board_ttl: float = DEFAULT_BOARD_TTL_S
    # Which finished tool calls become checkpoints (#176). Empty means every
    # tool; otherwise a comma-separated list of names, `!name` to exclude.
    tool_matcher: str = ""

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "TraceConfig":
        if env is None:
            # Resolve through the active Hermes profile so trace settings cannot
            # leak across profiles under multiplexing (#156) -- and through the
            # preset gate, so ZULIP_PROFILE=recommended turns the trace on for a
            # fresh install (#213). The other four are not preset knobs, so for
            # them this resolves exactly as ``get_setting`` always has.
            env = {
                key: runtime_scope.effective_value(key)
                for key in (
                    "ZULIP_ACTIVITY_TRACE",
                    "ZULIP_TRACE_COALESCE_MS",
                    "ZULIP_TRACE_MAX_RATE",
                    "ZULIP_TRACE_MAX_CONTENT",
                    "ZULIP_TRACE_TOOL_MATCHER",
                    "ZULIP_TRACE_BOARD_TTL",
                )
            }

        def _num(key: str, default: float, cast: Callable[[str], Any]) -> Any:
            try:
                return cast(str(env.get(key, "")).strip())
            except (TypeError, ValueError):
                return cast(str(default))

        return cls(
            enabled=_truthy(env.get("ZULIP_ACTIVITY_TRACE"), False),
            coalesce_ms=max(0, _num("ZULIP_TRACE_COALESCE_MS", DEFAULT_COALESCE_MS, int)),
            max_rate=max(0.1, _num("ZULIP_TRACE_MAX_RATE", DEFAULT_MAX_RATE, float)),
            max_content=max(200, _num("ZULIP_TRACE_MAX_CONTENT", DEFAULT_MAX_CONTENT, int)),
            board_ttl=max(0.0, _num("ZULIP_TRACE_BOARD_TTL", DEFAULT_BOARD_TTL_S, float)),
            # Kept verbatim rather than parsed here, so the raw value round-trips
            # into logs; interpretation lives in allows_tool().
            tool_matcher=str(env.get("ZULIP_TRACE_TOOL_MATCHER") or "").strip(),
        )

    def allows_tool(self, tool_name: str) -> bool:
        """Whether mode A should checkpoint a finished ``tool_name`` (#176).

        An empty matcher allows everything, which is the default and preserves
        the pre-#176 behaviour. Otherwise the value is a comma-separated list:

        * plain names form an **allowlist** — ``terminal,read`` checkpoints only
          those two;
        * names prefixed with ``!`` are **exclusions** — ``!browser`` checkpoints
          everything except ``browser``;
        * both may be combined, and a denial always wins
          (``terminal,!terminal`` checkpoints nothing).

        Matching is exact and case-insensitive. An empty or unknown tool name is
        allowed only when no allowlist was given: with an allowlist we cannot
        confirm the tool is wanted, so it is filtered out rather than guessed in.
        """
        matcher = (self.tool_matcher or "").strip()
        if not matcher:
            return True

        allow: list[str] = []
        deny: list[str] = []
        for raw in matcher.split(","):
            item = raw.strip().lower()
            if not item:
                continue
            if item.startswith("!"):
                deny.append(item[1:])
            else:
                allow.append(item)

        name = (tool_name or "").strip().lower()
        if name and name in deny:
            return False
        if allow:
            return bool(name) and name in allow
        return True


@dataclass
class TraceStep:
    """One line of the status board."""

    label: str
    status: str = STATUS_RUNNING
    detail: str = ""


@dataclass
class TraceState:
    """Everything needed to render the trace, and nothing that needs a host."""

    title: str = "Working"
    steps: list[TraceStep] = field(default_factory=list)
    note: str = ""

    def add(self, label: str) -> TraceStep:
        step = TraceStep(label=label)
        self.steps.append(step)
        return step

    def render(self, max_content: int = DEFAULT_MAX_CONTENT) -> str:
        """Render the board as Zulip markdown, bounded by ``max_content``.

        Keeps the *most recent* steps when trimming: the tail is what a reader
        watching the topic needs, and the header already says what is happening.
        """
        lines = [f"**{self.title}**", ""]
        for step in self.steps:
            mark = _STATUS_MARK.get(step.status, "·")
            line = f"{mark} {step.label}"
            if step.detail:
                line += f" — {step.detail}"
            lines.append(line)
        if self.note:
            lines += ["", self.note]

        text = "\n".join(lines)
        if len(text) <= max_content:
            return text

        # Trim from the top of the step list, keeping the header.
        header = f"**{self.title}**\n\n"
        budget = max_content - len(header)
        kept: list[str] = []
        for line in reversed(lines[2:]):
            if len(line) + sum(len(k) + 1 for k in kept) > budget:
                break
            kept.insert(0, line)
        if not kept:
            return (header + "…").strip()
        return header + "\n".join(kept)


class EditPacer:
    """Hard ceiling on edit rate, independent of the coalescing delay.

    Coalescing bounds how *often* a burst is flushed; the pacer bounds how much
    of that survives when steps keep arriving. At most ``max_per_second``
    reservations fit in any rolling one-second window.
    """

    def __init__(self, max_per_second: float):
        self._limit = max(1, int(max_per_second))
        self._recent: deque[float] = deque()

    def delay_for(self, now: float) -> float:
        """Seconds to wait before the next edit is allowed; reserves the slot."""
        while self._recent and now - self._recent[0] >= 1.0:
            self._recent.popleft()

        if len(self._recent) < self._limit:
            self._recent.append(now)
            return 0.0

        wait = max(0.0, self._recent[0] + 1.0 - now)
        self._recent.popleft()
        self._recent.append(now + wait)
        return wait


PostFn = Callable[[str], Awaitable[Optional[int]]]
EditFn = Callable[[int, str], Awaitable[bool]]


class ActivityTrace:
    """One trace message for one work item, edited in place as steps resolve.

    ``post_fn(content)`` returns the new message id (or None on failure);
    ``edit_fn(message_id, content)`` returns whether the edit landed. Both are
    injected so the engine can be exercised without a gateway or a network.
    """

    def __init__(
        self,
        post_fn: PostFn,
        edit_fn: EditFn,
        config: Optional[TraceConfig] = None,
        *,
        title: str = "Working",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_rotate: Optional[Callable[[int, int], Awaitable[None]]] = None,
    ):
        self._post = post_fn
        self._edit = edit_fn
        self._config = config or TraceConfig()
        self._state = TraceState(title=title)
        self._clock = clock
        self._sleep = sleep
        self._on_rotate = on_rotate
        self._pacer = EditPacer(self._config.max_rate)
        self._message_id: Optional[int] = None
        self._last_render: Optional[str] = None
        self._pending: Optional[asyncio.Task] = None
        self._rotate_task: Optional[asyncio.Task] = None
        self._rotating = False
        self._final_posted = False
        self._closed = False
        self.edit_count = 0
        try:
            self._loop: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    # --- state -----------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def message_id(self) -> Optional[int]:
        return self._message_id

    @property
    def state(self) -> TraceState:
        return self._state

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> bool:
        """Post the initial trace message.

        A failed post **drops the trace entirely** (returns False) rather than
        retrying: the trace is a courtesy, and the agent's run must not wait on
        it.
        """
        if not self.enabled or self._closed:
            return False
        render = self._state.render(self._config.max_content)
        try:
            message_id = await self._post(render)
        except Exception as e:
            logger.warning("activity trace post failed, dropping trace: %s", e)
            self._message_id = None
            return False

        if message_id is None:
            logger.warning("activity trace post returned no message id; dropping trace")
            return False

        self._message_id = message_id
        self._last_render = render
        self._schedule_rotation()
        return True

    def step(self, label: str) -> Optional[TraceStep]:
        """Start a step and schedule the coalesced flush that shows it."""
        if not self.enabled or self._message_id is None or self._closed:
            return None
        trace_step = self._state.add(label)
        self._schedule_flush()
        return trace_step

    def complete(self, trace_step: Optional[TraceStep], ok: bool = True, detail: str = "") -> None:
        """Resolve a step previously returned by :meth:`step`."""
        if trace_step is None:
            return
        trace_step.status = STATUS_OK if ok else STATUS_FAILED
        if detail:
            trace_step.detail = detail
        if self._message_id is not None and not self._closed:
            self._schedule_flush()

    async def finish(self, note: str = "Done.") -> None:
        """Terminal flush: mark the board finished and apply a final edit."""
        if not self.enabled or self._closed:
            return
        self._state.title = "Done" if not note.lower().startswith("cancel") else "Cancelled"
        if note:
            self._state.note = note
        for step in self._state.steps:
            if step.status == STATUS_RUNNING:
                step.status = STATUS_OK
        self._closed = True
        await self._stop_rotation()
        await self._cancel_pending()
        if await self._flush() == "refused":
            await self._post_final()

    async def fail(self, reason: str) -> None:
        """Terminal flush for a failed run."""
        if not self.enabled or self._closed:
            return
        self._state.title = "Failed"
        if reason:
            self._state.note = reason
        for step in self._state.steps:
            if step.status == STATUS_RUNNING:
                step.status = STATUS_FAILED
        self._closed = True
        await self._stop_rotation()
        await self._cancel_pending()
        if await self._flush() == "refused":
            await self._post_final()

    async def aclose(self) -> None:
        """Cancel any pending flush. Never raises; used on shutdown paths."""
        self._closed = True
        await self._stop_rotation()
        await self._cancel_pending()

    # --- flushing --------------------------------------------------------

    def _schedule_flush(self) -> None:
        """Coalesce: at most one flush task pending, whatever the step rate.

        The ``post_tool_call`` hook is synchronous and may run on a worker
        thread, so scheduling must hop onto the loop that owns the trace;
        ``create_task`` from a bare thread raises and silently dropped every
        mid-run checkpoint (only the terminal flush ever applied).
        """
        if self._pending is not None and not self._pending.done():
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._pending = None
                return

        def _create() -> None:
            if self._pending is not None and not self._pending.done():
                return
            self._pending = loop.create_task(self._coalesced_flush())

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            _create()
        else:
            loop.call_soon_threadsafe(_create)

    async def _coalesced_flush(self) -> None:
        if self._config.coalesce_ms:
            await self._sleep(self._config.coalesce_ms / 1000.0)
        await self._flush()

    async def _cancel_pending(self) -> None:
        task = self._pending
        self._pending = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014 - cancellation is expected
            pass

    async def _flush(self) -> str:
        """Apply the current render.

        Returns ``"ok"`` (applied or unchanged), ``"refused"`` (the server
        rejected the edit — typically the message-edit window) or ``"error"``
        (the call raised). The terminal path posts a replacement board only for
        a refusal; a transient network failure must not duplicate a board that
        is still editable.
        """
        if self._message_id is None:
            return "ok"
        render = self._state.render(self._config.max_content)
        if render == self._last_render:
            return "ok"  # unchanged render spends no PATCH

        delay = self._pacer.delay_for(self._clock())
        if delay > 0:
            await self._sleep(delay)

        try:
            ok = await self._edit(self._message_id, render)
        except Exception as e:
            # Logged and dropped. No retry loop: the next step naturally
            # re-renders, and the run must never wait on the trace.
            logger.warning("activity trace edit failed (dropped): %s", e)
            return "error"

        if ok:
            self._last_render = render
            self.edit_count += 1
            return "ok"
        logger.warning(
            "activity trace edit refused (message %s); the board may have outlived "
            "the realm's message-edit window",
            self._message_id,
        )
        return "refused"

    # --- board rotation ---------------------------------------------------

    def _schedule_rotation(self, delay: Optional[float] = None) -> None:
        """Rotate the board before Zulip's message-edit window closes.

        The realm refuses content edits after a fixed window (600 s by default),
        and an agent run can easily outlive it. Rotating onto a fresh message
        keeps the board editable; the old one is closed as "continued".
        """
        ttl = self._config.board_ttl
        if ttl <= 0 or self._loop is None or self._loop.is_closed():
            return
        self._cancel_rotation()
        self._rotate_task = self._loop.create_task(
            self._rotate_after(ttl if delay is None else delay)
        )

    async def _rotate_after(self, delay: float) -> None:
        try:
            await self._sleep(delay)
        except asyncio.CancelledError:
            return
        await self._rotate()

    def _cancel_rotation(self) -> None:
        task = self._rotate_task
        self._rotate_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _stop_rotation(self) -> None:
        """Stop the rotation timer, waiting for an in-flight post.

        Cancelling is enough while the timer is only sleeping. Once ``_rotate``
        is inside ``_post`` (a worker thread under ``wait_for``), cancelling the
        task does not stop the thread: the message lands but ``new_id`` is never
        assigned. Wait for the task instead and let ``_rotate`` close the new
        board itself.
        """
        task = self._rotate_task
        self._rotate_task = None
        if task is None or task.done():
            return
        if not self._rotating:
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _rotate(self) -> None:
        """Move the status board to a fresh message, then close the old one."""
        if self._closed or self._message_id is None:
            return
        render = self._state.render(self._config.max_content)
        self._rotating = True
        try:
            new_id = await self._post(render)
        except Exception as e:
            logger.warning("activity trace rotation failed: %s", e)
            new_id = None
        finally:
            self._rotating = False
        if new_id is None:
            if not self._closed:
                self._rotate_task = None
                self._schedule_rotation(delay=60.0)
            return
        if self._closed:
            # finish() won the race while the post was in flight. The message
            # exists but cannot become the active board, so close it with the
            # terminal state instead of leaving an untracked Working board.
            try:
                await self._edit(new_id, self._state.render(self._config.max_content))
            except Exception:
                logger.debug("activity trace: could not close raced rotation", exc_info=True)
            return

        old_id = self._message_id
        self._message_id = new_id
        self._last_render = render
        self.edit_count += 1
        # Close the old board while it is still editable, so it cannot read as
        # an active run. A refusal here is cosmetic: the new board is active.
        try:
            await self._edit(
                old_id,
                "**Working**\n\nContinued in the next status message.",
            )
        except Exception:
            logger.debug("activity trace: could not close rotated board", exc_info=True)
        if self._on_rotate is not None:
            try:
                await self._on_rotate(old_id, new_id)
            except Exception:
                logger.debug("activity trace: rotation callback failed", exc_info=True)
        self._rotate_task = None
        self._schedule_rotation()

    async def _post_final(self) -> None:
        """Post the terminal state when the board can no longer be edited."""
        if self._final_posted:
            return
        self._final_posted = True
        render = self._state.render(self._config.max_content)
        try:
            message_id = await self._post(render)
        except Exception as e:
            logger.warning("activity trace final post failed: %s", e)
            return
        if message_id is not None:
            logger.info(
                "activity trace: final status posted as message %s "
                "(the original board could not be edited)",
                message_id,
            )
