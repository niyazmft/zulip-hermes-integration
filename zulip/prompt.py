"""The plugin's prompt surface, split into a non-removable core and sections (#223).

Every turn used to carry one large block of instructions with no distinction
between two very different things:

* **product behaviour** — who the bot is, how a reply is produced, what makes a
  message *addressed* to it, and how topics work. Remove any of it and the bot
  stops answering correctly; none of it is optional.
* **domain guidance** — repos, pull requests, engineering discipline, tool
  conventions. Worth its place for a coding bot, mostly irrelevant to a
  greeter, a summariser or a triage bot, and re-sent on every turn.

An operator may trim the second. An operator must never be able to trim the
first, so the split lives here as data rather than as prose an editor can
soften: :data:`CORE` is a fixed tuple of rules, :data:`SECTIONS` is what a
preset may choose, and :func:`render` always emits every core rule.

Two properties this module exists to guarantee:

* **the core is a byte-stable prefix.** :func:`render` puts the static text
  first, in a fixed order, and appends any dynamic content after it, so a
  provider that caches prompt prefixes can cache the core across turns. Nothing
  static may embed a timestamp, a message id or a per-turn count.
* **a preset cannot remove a core rule.** Every preset is a subset of
  :data:`SECTIONS`; the core is not addressable by a preset at all. The test
  suite renders every preset and asserts the core verbatim.

An unknown preset name warns once and falls back to ``full`` — the complete set,
never a trimmed one — because falling back to the lean set would silently
withhold guidance an operator never asked to drop.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import runtime_scope

logger = logging.getLogger(__name__)

#: The setting that selects a preset. Resolved at call time through the profile
#: gate (:func:`zulip.runtime_scope.effective_value`), so env > profile >
#: default, and a patched module takes effect without a restart of the import.
PRESET_ENV = "ZULIP_PROMPT_PRESET"

#: No prompt is injected at all — byte-identical to the behaviour before this
#: module existed. The default, so an install that never sets the marker is
#: unaffected (the #153 contract the whole epic inherits).
PRESET_OFF = "off"

#: Everything: core plus every section. The complete block, and the fallback.
PRESET_FULL = "full"

#: Core plus only what a chat-shaped bot needs.
PRESET_LEAN = "lean"

#: The core, in render order. Each rule is one ``(heading, body)`` pair.
#:
#: These four categories are the issue's list, and nothing else is allowed in:
#: identity, how a reply is produced, how a message is addressed to the bot, and
#: how threading/topics work. A rule that is merely *useful* belongs in a
#: section, where a preset may drop it.
CORE: tuple[tuple[str, str], ...] = (
    (
        "Identity",
        "You are a Zulip bot. Each turn is one message in a Zulip conversation.\n"
        "Your mention handle is your email prefix. Users address you by mention, "
        "and by the trigger configured for the stream.",
    ),
    (
        "How a reply is produced",
        "Answer with your message text. The gateway delivers it: it chunks long "
        "replies, sets and clears status reactions, keeps a typing indicator, and "
        "threads the reply into the conversation the message came from.\n"
        "Do not narrate that machinery, describe your own tooling, or announce what "
        "you are about to do unless the user asked. If you have nothing worth "
        "saying, say nothing.",
    ),
    (
        "How a message reaches you",
        "A turn reaches you because you were mentioned, a trigger prefix fired, or "
        "the stream is configured to answer everything. The mention is removed from "
        "the text before you see it, so answer the content, not the mention.\n"
        "A turn that opens with `[Not addressed to you - observed stream traffic; "
        "answer only if it plainly needs you]` was not addressed to you: you are "
        "watching that stream. Speak only if it plainly needs a reply; otherwise "
        "stay silent and remember what you saw.",
    ),
    (
        "Threading and topics",
        "Zulip organises streams into topics, and a topic is the unit of "
        "conversation. Keep every reply in the topic you were addressed in. Never "
        "start a new topic unless the user asks for one; when they do, the gateway "
        "extracts the directive and your reply follows it automatically.",
    ),
)

#: Sections a preset may select. Same shape as :data:`CORE`, keyed by name so a
#: preset is a list of names rather than a copy of the text.
SECTIONS: dict[str, tuple[str, str]] = {
    "domain": (
        "Domain guidance",
        "This install is used for software work. Prefer reading the code over "
        "guessing about it, match the conventions already in the files you touch, "
        "and separate what you verified from what you assumed.",
    ),
    "repos": (
        "Repositories and pull requests",
        "When reviewing or opening a pull request, state the defect class it "
        "belongs to rather than only the line that is wrong, and prefer a change "
        "that makes the whole class visible to one that patches the instance.\n"
        "Link work you refer to with a real URL; the gateway validates GitHub "
        "links before sending and renders confirmed ones as references.",
    ),
    "tooling": (
        "Tooling",
        "Use the tools you are given rather than describing what a person could "
        "do. A tool failure is information: report what it said, not that "
        "something went wrong.",
    ),
    "formatting": (
        "Formatting",
        "Prefer short paragraphs and plain sentences over long lists. Use a code "
        "block when the content is code, a command or a file path list, and "
        "inline code for identifiers. Zulip renders Markdown; there is no need "
        "to escape it.",
    ),
}

#: Render order for sections, so the prefix cannot drift between renders or
#: between a preset and its own test expectation.
SECTION_ORDER: tuple[str, ...] = ("domain", "repos", "tooling", "formatting")

#: What each preset selects. ``off`` renders nothing at all; every other preset
#: renders the whole of :data:`CORE` and then only these sections.
PRESETS: dict[str, tuple[str, ...]] = {
    PRESET_FULL: SECTION_ORDER,
    # A chat-shaped bot: it answers people, so it keeps formatting (a wall of
    # text reads badly in a room). It has no repos and no engineering domain, so
    # those two go; tooling stays because a bot that can act should not pretend
    # it cannot.
    PRESET_LEAN: ("tooling", "formatting"),
}

#: Presets already warned about, so a typo does not log on every turn. Mirrors
#: the profile resolver's contract in :mod:`zulip.runtime_scope`.
_warned: set[str] = set()


def resolve_preset() -> str:
    """The preset this install selected, resolved at call time.

    Empty or unset means :data:`PRESET_OFF`. An unknown value warns once and
    falls back to :data:`PRESET_FULL` — the complete set. Degrading to the lean
    set would withhold guidance the operator never chose to drop, which is the
    failure mode this module is least willing to have.
    """
    raw = (runtime_scope.effective_value(PRESET_ENV, PRESET_OFF) or "").strip().lower()
    if not raw:
        return PRESET_OFF
    if raw in (PRESET_OFF, PRESET_FULL, PRESET_LEAN):
        return raw
    if raw not in _warned:
        _warned.add(raw)
        logger.warning(
            "%s=%r is not a known preset; using %r (known: %s)",
            PRESET_ENV,
            raw,
            PRESET_FULL,
            ", ".join(sorted(PRESETS) + [PRESET_OFF]),
        )
    return PRESET_FULL


def selected_sections(preset: str) -> tuple[str, ...]:
    """The section names one preset renders, in :data:`SECTION_ORDER` order."""
    if preset == PRESET_OFF:
        return ()
    chosen = PRESETS.get(preset, PRESETS[PRESET_FULL])
    return tuple(name for name in SECTION_ORDER if name in chosen)


def render_static(preset: Optional[str] = None) -> str:
    """The byte-stable prefix: every core rule, then the preset's sections.

    No timestamps, ids or per-turn counts may appear here. Two calls with the
    same preset must return identical text, which is what makes it cacheable.
    """
    preset = resolve_preset() if preset is None else preset
    if preset == PRESET_OFF:
        return ""

    blocks: list[str] = []
    for heading, body in CORE:
        blocks.append(f"## {heading}\n{body}")
    for name in selected_sections(preset):
        heading, body = SECTIONS[name]
        blocks.append(f"## {heading}\n{body}")
    return "\n\n".join(blocks)


def render(preset: Optional[str] = None, dynamic: str = "") -> str:
    """The whole prompt: the static prefix, then ``dynamic`` after it.

    Dynamic content — injected history, observed context, turn metadata — goes
    after the prefix and never inside it, so the cacheable part stays
    byte-identical from turn to turn.
    """
    static = render_static(preset)
    dynamic = (dynamic or "").strip()
    if not static:
        return dynamic
    return f"{static}\n\n{dynamic}" if dynamic else static
