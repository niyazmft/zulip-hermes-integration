"""Bounded topic-history harvesting and non-addressed observation.

Owns the two pieces of quoted context the adapter can prepend to an
agent-facing body (issues #148, #153):

* :class:`ObservedContextBuffer` — a bounded, deterministic buffer of stream
  messages the bot was addressed in but was not itself named in.
* :func:`render_history_block` — the newest slice of a fetched topic history,
  bounded on messages, age and characters.

The module is pure: it renders and bounds already-fetched data and depends on
nothing but :mod:`zulip.text_utils`. The history fetch itself stays in the
caller and is injected, so no SDK or adapter dependency leaks in here.

Layering: L1 — depends only on the standard library and ``zulip.text_utils``.

The bounds and label constants are owned here. Production code reads them
through this module at call time so a test that patches ``zulip.history``
takes effect; ``zulip.adapter`` re-exports them only for backwards-compatible
``from zulip.adapter import <name>`` imports.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .text_utils import strip_html_to_text, truncate_text

#: Caps for the observed-topic buffer (#153). Per topic *and* across topics, so
#: neither one busy topic nor a long-running bot can grow without limit.
OBSERVED_MAX_MESSAGES = 20
OBSERVED_MAX_CHARS = 4000
OBSERVED_MAX_TOPICS = 200

#: Labels marking quoted context so the agent can tell it from the live
#: message (#153, #148).
OBSERVED_HISTORY_LABEL = "[Observed topic history - not addressed to you]"
FETCHED_HISTORY_LABEL = "[Topic history - recent messages quoted for context]"

#: Best-effort budget for one history harvest (#148). A slow or failing fetch
#: is dropped rather than awaited beyond this.
HISTORY_FETCH_TIMEOUT = 2.0

#: How a history harvest ended (issue #225). The status board has to separate
#: "there was nothing to quote" from "we could not look": a slow realm is not an
#: empty one, and only the first is a fact about the conversation.
HARVEST_NOT_ATTEMPTED = ""
HARVEST_INJECTED = "injected"
HARVEST_EMPTY = "empty"
HARVEST_TIMEOUT = "timeout"
HARVEST_FAILED = "failed"

#: Phrasing for the outcomes worth showing on the board. ``injected`` speaks for
#: itself through its character count, and ``not attempted`` cost nothing, so it
#: is not shown at all -- a turn that harvested nothing must leave the board
#: exactly as short as it was before #225.
_HARVEST_PHRASE = {
    HARVEST_EMPTY: "history harvest empty",
    HARVEST_TIMEOUT: "history harvest timed out",
    HARVEST_FAILED: "history harvest failed",
}

#: Characters per token for the approximate count. Deliberately crude: this
#: plugin ships no tokenizer, and a wrong-but-precise-looking number is worse
#: than an honest estimate.
APPROX_CHARS_PER_TOKEN = 4

#: Narrow "do we already know this?" question shapes for ``on-demand`` history
#: (#148). Deliberately conservative: a harvest costs one Zulip round-trip and
#: a slice of the context budget, so a passing mention of history is not enough.
_HISTORY_INTENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bhave we\b[^?]{0,80}\b(seen|had|discussed|covered|decided|talked)\b",
        r"\bdid we\b[^?]{0,80}\b(discuss|decide|agree|see|cover|mention|talk)\b",
        r"\bwhat did we\b",
        r"\bwhen did we\b",
        r"\bdo we (already )?(know|have)\b",
        r"\b(as|like) (we|i) (discussed|mentioned|said|agreed)\b",
        r"\bwe (already|previously)\b",
        r"\b(earlier|previously|before now|last (week|month|time))\b",
        r"\bhas this (come up|been (asked|raised|seen))\b",
        r"\bany (prior|previous|earlier)\b",
    )
)


def history_intent_matches(text: str) -> bool:
    """Whether ``text`` reads like a question about something already discussed."""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _HISTORY_INTENT_PATTERNS)


def _joined_len(lines: list[str]) -> int:
    """Length of ``"\\n".join(lines)`` without building the string."""
    if not lines:
        return 0
    return sum(len(line) for line in lines) + len(lines) - 1


class ObservedContextBuffer:
    """Bounded per-topic memory of stream messages the bot was not addressed in.

    Deliberately in-adapter state (issue #153). The design source appended to
    the host session transcript through ``gateway.session`` internals, which
    this plugin cannot rely on, so observation is kept here instead: a
    deterministic buffer keyed by ``(stream_id, topic)`` so topics never leak
    into each other, bounded on three axes — lines per topic, characters per
    topic, and tracked topics. The oldest line (or the least-recently-used
    topic) is dropped first.
    """

    def __init__(
        self,
        max_messages: int = OBSERVED_MAX_MESSAGES,
        max_chars: int = OBSERVED_MAX_CHARS,
        max_topics: int = OBSERVED_MAX_TOPICS,
    ):
        self._max_messages = max(1, max_messages)
        self._max_chars = max(1, max_chars)
        self._max_topics = max(1, max_topics)
        self._topics: OrderedDict[tuple[str, str], list[str]] = OrderedDict()

    def add(self, stream_id: Any, topic: Any, line: str) -> None:
        """Append one quoted line, evicting oldest entries to stay in budget."""
        line = (line or "").strip()
        if not line:
            return
        key = (str(stream_id), str(topic or ""))
        lines = self._topics.get(key)
        if lines is None:
            lines = []
            self._topics[key] = lines
        self._topics.move_to_end(key)
        lines.append(line)
        self._trim(lines)
        while len(self._topics) > self._max_topics:
            self._topics.popitem(last=False)

    def render(self, stream_id: Any, topic: Any) -> str:
        """The buffered lines for one topic, oldest first; "" when none."""
        lines = self._topics.get((str(stream_id), str(topic or "")))
        if not lines:
            return ""
        return "\n".join(lines)

    def _trim(self, lines: list[str]) -> None:
        while len(lines) > self._max_messages:
            lines.pop(0)
        # Character budget: drop the oldest whole lines first, then truncate the
        # oldest survivor if a single line still overshoots on its own.
        while len(lines) > 1 and _joined_len(lines) > self._max_chars:
            lines.pop(0)
        if lines and len(lines[0]) > self._max_chars:
            lines[0] = truncate_text(lines[0], self._max_chars)


class AddressedTopicTracker:
    """Bounded memory of the topics the bot has been addressed in (#217).

    The recommended profile's memory is *conversation-scoped*: a topic is
    remembered only once the bot was addressed in it. The observed-message buffer
    answers "what was said here"; this answers "was the bot ever part of this
    conversation", which is the question that decides whether the first answer is
    allowed to be kept at all.

    Bounded and least-recently-used for the same reason the buffer is: an install
    attached to a busy realm must not grow without limit just because it was
    watching. Eviction is the documented trade-off -- a topic that has been quiet
    for a long time stops being "engaged", and starts fresh.
    """

    def __init__(self, max_topics: int = OBSERVED_MAX_TOPICS):
        self._max_topics = max(1, max_topics)
        self._topics: OrderedDict[tuple[str, str], None] = OrderedDict()

    @staticmethod
    def _key(stream_id: Any, topic: Any) -> tuple[str, str]:
        """The same key the observed buffer uses, so the two cannot disagree."""
        return (str(stream_id), str(topic or ""))

    def mark(self, stream_id: Any, topic: Any) -> None:
        """Record that the bot was addressed in this topic."""
        key = self._key(stream_id, topic)
        self._topics[key] = None
        self._topics.move_to_end(key)
        while len(self._topics) > self._max_topics:
            self._topics.popitem(last=False)

    def was_addressed(self, stream_id: Any, topic: Any) -> bool:
        """Whether the bot has been addressed in this topic, and still tracks it."""
        return self._key(stream_id, topic) in self._topics

    def __len__(self) -> int:
        return len(self._topics)


def history_sort_key(message: dict[str, Any]) -> int:
    """Integer message id for a stable oldest-first order; 0 when unusable."""
    try:
        return int(message.get("id"))
    except (TypeError, ValueError):
        return 0


def render_history_block(
    messages: list[dict[str, Any]],
    *,
    max_messages: int,
    window_hours: int,
    max_chars: int,
    now: float | None = None,
) -> str:
    """Render the newest slice of ``messages`` as quoted history lines (#148).

    Bounded on all three axes: at most ``max_messages`` lines, nothing older
    than ``window_hours``, at most ``max_chars`` characters. The newest lines
    win — oldest lines are dropped (and the oldest survivor truncated) so a
    topic with months of history cannot blow up the context window.
    """
    if now is None:
        now = time.time()
    cutoff = now - window_hours * 3600

    lines: list[str] = []
    for message in sorted(messages, key=history_sort_key):
        timestamp = message.get("timestamp")
        if isinstance(timestamp, (int, float)) and timestamp < cutoff:
            continue
        text = strip_html_to_text(str(message.get("content") or "")).strip()
        if not text:
            continue
        sender = (
            str(message.get("sender_full_name") or "").strip()
            or str(message.get("sender_email") or "").strip()
            or "Unknown"
        )
        lines.append(f"[{sender}] {text}")

    lines = lines[-max_messages:]
    while len(lines) > 1 and _joined_len(lines) > max_chars:
        lines.pop(0)
    if lines and len(lines[0]) > max_chars:
        lines[0] = truncate_text(lines[0], max_chars)
    return "\n".join(lines)


@dataclass(frozen=True)
class ContextReport:
    """What quoted context one turn carried, in counts only (issue #225).

    Counts and outcomes, never text: the status board is visible in the room, and
    quoting a message body there would leak context into the channel. Sizes are
    measured on the rendered block *body* — the part that costs context — so the
    fixed labels are not counted.

    ``harvest`` records how the history harvest ended even when nothing was
    injected, which is the whole point: "no history matched" and "the fetch timed
    out" both render nothing, and an owner looking at a thin answer cannot act on
    either without being told which happened.
    """

    history_chars: int = 0
    history_messages: int = 0
    observed_chars: int = 0
    observed_messages: int = 0
    harvest: str = HARVEST_NOT_ATTEMPTED


def measure_block(text: str) -> tuple[int, int]:
    """``(characters, messages)`` of one rendered quoted block, or ``(0, 0)``.

    Messages are counted as rendered lines, which is exactly what the block
    carries: ``render_history_block`` emits one line per message and the observed
    buffer stores one line per message.
    """
    if not text:
        return 0, 0
    return len(text), text.count("\n") + 1


def render_context_line(report: ContextReport | None) -> str:
    """One compact board line describing a turn's injected context (#225).

    Returns ``""`` when the turn carried nothing *and* nothing went wrong, so an
    install that never harvests sees exactly the board it saw before this existed.
    A harvest that was attempted and produced nothing is shown, because that is a
    fact about our behaviour (a slow realm), not about the conversation.

    Approximate tokens are labelled approximate on purpose: no tokenizer ships
    with this plugin, and a precise-looking wrong number is worse than an honest
    estimate.
    """
    if report is None:
        return ""

    parts: list[str] = []
    if report.history_chars:
        parts.append(f"history {report.history_chars:,} chars")
    if report.observed_chars:
        count = report.observed_messages
        parts.append(
            f"observed {report.observed_chars:,} chars "
            f"({count} msg{'s' if count != 1 else ''})"
        )
    phrase = _HARVEST_PHRASE.get(report.harvest, "")
    if phrase:
        parts.append(phrase)
    if not parts:
        return ""

    chars = report.history_chars + report.observed_chars
    if chars:
        parts.append(f"~{max(1, chars // APPROX_CHARS_PER_TOKEN):,} tok")
    return "context: " + " · ".join(parts)
