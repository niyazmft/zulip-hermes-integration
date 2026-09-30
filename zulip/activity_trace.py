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

logger = logging.getLogger(__name__)

DEFAULT_COALESCE_MS = 400
DEFAULT_MAX_RATE = 2.0
DEFAULT_MAX_CONTENT = 3500

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
    # Which finished tool calls become checkpoints (#176). Empty means every
    # tool; otherwise a comma-separated list of names, `!name` to exclude.
    tool_matcher: str = ""

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "TraceConfig":
        env = os.environ if env is None else env

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
    ):
        self._post = post_fn
        self._edit = edit_fn
        self._config = config or TraceConfig()
        self._state = TraceState(title=title)
        self._clock = clock
        self._sleep = sleep
        self._pacer = EditPacer(self._config.max_rate)
        self._message_id: Optional[int] = None
        self._last_render: Optional[str] = None
        self._pending: Optional[asyncio.Task] = None
        self._closed = False
        self.edit_count = 0

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
        await self._cancel_pending()
        await self._flush()

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
        await self._cancel_pending()
        await self._flush()

    async def aclose(self) -> None:
        """Cancel any pending flush. Never raises; used on shutdown paths."""
        self._closed = True
        await self._cancel_pending()

    # --- flushing --------------------------------------------------------

    def _schedule_flush(self) -> None:
        """Coalesce: at most one flush task pending, whatever the step rate."""
        if self._pending is not None and not self._pending.done():
            return
        try:
            self._pending = asyncio.get_running_loop().create_task(self._coalesced_flush())
        except RuntimeError:
            # No running loop (sync caller): the terminal flush still applies.
            self._pending = None

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

    async def _flush(self) -> None:
        """Apply the current render, if it differs and the pacer allows it."""
        if self._message_id is None:
            return
        render = self._state.render(self._config.max_content)
        if render == self._last_render:
            return  # unchanged render spends no PATCH

        delay = self._pacer.delay_for(self._clock())
        if delay > 0:
            await self._sleep(delay)

        try:
            ok = await self._edit(self._message_id, render)
        except Exception as e:
            # Logged and dropped. No retry loop: the next step naturally
            # re-renders, and the run must never wait on the trace.
            logger.warning("activity trace edit failed (dropped): %s", e)
            return

        if ok:
            self._last_render = render
            self.edit_count += 1
