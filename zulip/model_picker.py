"""Interactive ``/model`` picker for the Zulip adapter (issue #192).

Zulip's ``zform`` widget is **buttons only** — no dropdown, no select, no text
input — and a click sends its ``reply`` string back as an ordinary message. So a
provider → model drill-down is a paginated set of buttons whose clicks arrive as
chat messages, and this module owns the pure half of that: the token scheme, the
picker registry, and the rendering.

Deliberately host-independent, like :mod:`zulip.activity_trace`: it builds
payloads and decides what a click means, and the adapter does the sending. That
keeps the pagination, expiry and authorization rules testable without a gateway.

**Reply tokens** are ``mp:`` (providers page), ``mm:`` (models page), ``mg:``
(model chosen) and ``mx:`` (cancel), each followed by a short opaque token and
then **integers only**:

    mp:<token>:<page>              providers page
    mm:<token>:<provider>:<page>   models of provider <provider>, page <page>
    mg:<token>:<provider>:<model>  select model <model> of provider <provider>
    mx:<token>                     cancel

Indices rather than ids on purpose: a provider slug may contain ``:`` (custom
endpoints) and a model id may contain ``:`` or ``/``, so encoding them into a
token would need escaping at every boundary. The registry holds the real ids, the
token is opaque, and the reply stays a simple ``split(":")``.

Every picker is **single-use**, **initiator-only** and expires after
:data:`TTL_SECONDS`; those three rules are what stop a stale form in a shared
topic from being clickable by whoever wanders past it later.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Providers per page. Models get one fewer slot so the same widget can carry
#: the navigation row (prev / page / next / back / cancel) without the choice
#: list growing past what a chat client renders comfortably.
PROVIDER_PAGE_SIZE = 10
MODEL_PAGE_SIZE = 9

#: How long a picker stays clickable. A form left in a topic overnight must not
#: still switch the session model when somebody taps it by accident.
TTL_SECONDS = 600.0

#: Bound on simultaneously-open pickers, so a busy realm cannot grow the
#: registry without limit. Oldest is evicted first.
MAX_PICKERS = 64

#: Reply prefixes. ``mp``/``mm`` are navigation, ``mg`` is a selection, ``mx`` cancels.
#: Reply prefixes. ``mp``/``mm`` are model-picker navigation, ``mg`` selects a
#: model, ``mx`` cancels; ``cp``/``cg``/``cx`` are the flat choice picker's page
#: turn, selection and cancel.
TOKEN_PREFIXES = ("mp", "mm", "mg", "mx", "cp", "cg", "cx")

#: Choices per page for the flat picker (#193). Today's commands (``/reasoning``
#: at ~10, ``/fast`` at 4) fit on one page; the row only appears when a future
#: list does not.
CHOICE_PAGE_SIZE = 12

EXPIRED_NOTICE = "Picker expired — run `/model` again."
NOT_YOURS_NOTICE = "That picker belongs to someone else — run `/model` yourself."
CANCELLED_NOTICE = "Picker cancelled."


def is_picker_reply(content: str) -> bool:
    """Whether ``content`` is one of this picker's reply tokens.

    Cheap and strict: a leading prefix followed by a colon. Nothing else in the
    plugin generates these, so this cannot capture ordinary chat.
    """
    text = (content or "").strip()
    prefix, sep, _rest = text.partition(":")
    return bool(sep) and prefix in TOKEN_PREFIXES


@dataclass
class PickerState:
    """One open picker: what it can render, and who may drive it."""

    token: str
    chat_id: str
    initiator: str
    session_key: str
    providers: list[dict[str, Any]] = field(default_factory=list)
    current_model: str = ""
    current_provider: str = ""
    created_at: float = 0.0
    message_id: Optional[int] = None
    #: The turn's routing metadata and the topic the form lives in, so a page
    #: turn re-posts into exactly the same place (and deletes exactly the
    #: message it replaced).
    metadata: Any = None
    topic: Optional[str] = None
    #: ``async (chat_id, model_id, provider_slug) -> str`` — the gateway's own
    #: switch path, injected. The plugin never switches a model itself.
    on_model_selected: Optional[Callable[..., Any]] = None

    def provider(self, index: int) -> Optional[dict[str, Any]]:
        if 0 <= index < len(self.providers):
            return self.providers[index]
        return None

    def model(self, provider_index: int, model_index: int) -> Optional[tuple[str, str]]:
        """``(model_id, provider_slug)`` for one button, or None when out of range."""
        provider = self.provider(provider_index)
        if provider is None:
            return None
        models = list(provider.get("models") or [])
        if not (0 <= model_index < len(models)):
            return None
        return str(models[model_index]), str(provider.get("slug") or "")


@dataclass
class ChoiceState:
    """One open flat choice picker (issue #193).

    The sibling of :class:`PickerState` with a much smaller shape: one heading,
    one flat list of ``{"value", "label", "is_current"}`` choices, one callback.
    ``/reasoning`` and ``/fast`` are the users today; both fit on one page.
    """

    token: str
    chat_id: str
    initiator: str
    title: str
    choices: list[dict[str, Any]] = field(default_factory=list)
    session_key: str = ""
    created_at: float = 0.0
    message_id: Optional[int] = None
    metadata: Any = None
    topic: Optional[str] = None
    #: ``async (chat_id, value) -> str`` — the gateway applies the change and
    #: returns the text to post in place. The plugin never changes state itself.
    on_choice_selected: Optional[Callable[..., Any]] = None

    def value(self, index: int) -> Optional[str]:
        """The ``value`` behind one button, or None when out of range."""
        if 0 <= index < len(self.choices):
            return str(self.choices[index].get("value") or "")
        return None


class PickerRegistry:
    """Bounded, expiring, single-use store of open pickers."""

    def __init__(
        self,
        *,
        ttl: float = TTL_SECONDS,
        max_pickers: int = MAX_PICKERS,
        clock: Callable[[], float] = time.monotonic,
        factory: Callable[..., Any] = PickerState,
    ):
        self._ttl = ttl
        self._max = max(1, max_pickers)
        self._clock = clock
        self._factory = factory
        self._pickers: dict[str, Any] = {}

    def open(self, **kwargs: Any) -> Any:
        """Create, remember and return a new picker."""
        self._expire()
        token = secrets.token_hex(4)
        while token in self._pickers:  # 32 bits; loop rather than trust luck
            token = secrets.token_hex(4)
        state = self._factory(token=token, created_at=self._clock(), **kwargs)
        self._pickers[token] = state
        while len(self._pickers) > self._max:
            self._pickers.pop(next(iter(self._pickers)))
        return state

    def peek(self, token: str) -> Any:
        """The picker for ``token`` if it is still live, else None."""
        state = self._pickers.get(token)
        if state is None:
            return None
        if self._expired(state):
            self._pickers.pop(token, None)
            return None
        return state

    def pop(self, token: str) -> Any:
        """Consume the picker: a second click can never act on it again."""
        state = self.peek(token)
        if state is not None:
            self._pickers.pop(token, None)
        return state

    def __len__(self) -> int:
        return len(self._pickers)

    def _expired(self, state: Any) -> bool:
        return (self._clock() - state.created_at) > self._ttl

    def _expire(self) -> None:
        for token in [t for t, s in self._pickers.items() if self._expired(s)]:
            self._pickers.pop(token, None)


# ----------------------------------------------------------------- rendering


def _choice(short: str, long: str, reply: str) -> dict[str, str]:
    """One zform choice. Every field is required by the web client's schema.

    ``short_name`` is what the button shows, so it must be unique *and* short;
    ``long_name`` is the label the client may show on hover or in a list.
    """
    return {
        "type": "multiple_choice",
        "short_name": short,
        "long_name": long,
        "reply": reply,
    }


def _widget(heading: str, choices: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "widget_type": "zform",
            "extra_data": {
                "type": "choices",
                "heading": heading,
                "choices": choices,
            },
        }
    )


def _page_count(items: int, size: int) -> int:
    return max(1, (items + size - 1) // size)


def _clamp_page(page: int, items: int, size: int) -> int:
    return min(max(0, page), _page_count(items, size) - 1)


def render_providers(state: PickerState, page: int = 0) -> tuple[str, str]:
    """``(context_text, widget_json)`` for the provider page of a picker.

    The text is sent as its own message because a rendered zform widget replaces
    its message body on web/desktop (``web/src/zform.ts``), so anything sent as
    the widget message's text is invisible exactly where the buttons render.
    """
    providers = state.providers
    page = _clamp_page(page, len(providers), PROVIDER_PAGE_SIZE)
    total = _page_count(len(providers), PROVIDER_PAGE_SIZE)
    start = page * PROVIDER_PAGE_SIZE
    window = providers[start : start + PROVIDER_PAGE_SIZE]

    choices: list[dict[str, str]] = []
    for offset, provider in enumerate(window):
        index = start + offset
        slug = str(provider.get("slug") or "")
        name = str(provider.get("name") or slug or "unknown")
        count = int(provider.get("total_models") or len(provider.get("models") or []))
        current = bool(provider.get("is_current")) or slug == state.current_provider
        label = f"{'✓ ' if current else ''}{name} ({count})"
        choices.append(
            _choice(chr(ord("A") + len(choices)), label, f"mm:{state.token}:{index}:0")
        )

    if total > 1:
        if page > 0:
            choices.append(_choice("◀", "Previous page", f"mp:{state.token}:{page - 1}"))
        if page + 1 < total:
            choices.append(_choice("▶", "Next page", f"mp:{state.token}:{page + 1}"))
    choices.append(_choice("✕", "Cancel", f"mx:{state.token}"))

    current = state.current_model or "unknown"
    heading = f"Select a provider — current model: {current}"
    if total > 1:
        heading += f" (page {page + 1}/{total})"
    return heading, _widget(heading, choices)


def render_models(state: PickerState, provider_index: int, page: int = 0) -> tuple[str, str]:
    """``(context_text, widget_json)`` for one provider's model page."""
    provider = state.provider(provider_index)
    if provider is None:
        return render_providers(state, 0)

    models = [str(m) for m in (provider.get("models") or [])]
    page = _clamp_page(page, len(models), MODEL_PAGE_SIZE)
    total = _page_count(len(models), MODEL_PAGE_SIZE)
    start = page * MODEL_PAGE_SIZE

    choices: list[dict[str, str]] = []
    for offset, model_id in enumerate(models[start : start + MODEL_PAGE_SIZE]):
        index = start + offset
        current = model_id == state.current_model
        label = f"{'✓ ' if current else ''}{model_id}"
        choices.append(
            _choice(
                chr(ord("A") + len(choices)),
                label,
                f"mg:{state.token}:{provider_index}:{index}",
            )
        )

    if total > 1:
        if page > 0:
            choices.append(
                _choice("◀", "Previous page", f"mm:{state.token}:{provider_index}:{page - 1}")
            )
        if page + 1 < total:
            choices.append(
                _choice("▶", "Next page", f"mm:{state.token}:{provider_index}:{page + 1}")
            )
    choices.append(_choice("↩", "Back to providers", f"mp:{state.token}:0"))
    choices.append(_choice("✕", "Cancel", f"mx:{state.token}"))

    name = str(provider.get("name") or provider.get("slug") or "provider")
    heading = f"Select a model — {name}"
    if total > 1:
        heading += f" (page {page + 1}/{total})"
    return heading, _widget(heading, choices)


def render_text_listing(state: PickerState) -> str:
    """Plain-text fallback for clients that do not render zform widgets.

    ``/pick-model`` owns its fallback (the gateway owns its own text listing on
    the ``/model`` path), and it must be actionable without the buttons: each
    line names the exact ``/model <provider>/<model>`` that would switch.
    """
    lines = [f"Current model: **{state.current_model or 'unknown'}**", ""]
    for provider in state.providers:
        slug = str(provider.get("slug") or "")
        name = str(provider.get("name") or slug)
        count = int(provider.get("total_models") or len(provider.get("models") or []))
        lines.append(f"- **{name}** ({count}) — `{slug}`")
        for model_id in list(provider.get("models") or [])[:PROVIDER_PAGE_SIZE]:
            lines.append(f"  - `/model {slug}/{model_id}`")
    lines += ["", "Reply with one of the switches above."]
    return "\n".join(lines)


@dataclass(frozen=True)
class PickerAction:
    """What one reply token means, resolved against a live picker."""

    kind: str  # "providers" | "models" | "select" | "cancel"
    page: int = 0
    provider_index: int = 0
    model_index: int = 0


def parse_reply(content: str) -> Optional[tuple[str, PickerAction]]:
    """``(token, action)`` for a picker reply, or None when it is not one.

    Integers only, so a malformed reply is rejected rather than interpreted: a
    token that cannot be parsed is *not* a picker click, and the caller lets it
    fall through to normal handling.
    """
    text = (content or "").strip()
    parts = text.split(":")
    if len(parts) < 2 or parts[0] not in TOKEN_PREFIXES:
        return None
    token = parts[1]
    try:
        if parts[0] in ("mx", "cx") and len(parts) == 2:
            return token, PickerAction(kind="cancel")
        if parts[0] == "mp" and len(parts) == 3:
            return token, PickerAction(kind="providers", page=int(parts[2]))
        if parts[0] == "mm" and len(parts) == 4:
            return token, PickerAction(
                kind="models", provider_index=int(parts[2]), page=int(parts[3])
            )
        if parts[0] == "mg" and len(parts) == 4:
            return token, PickerAction(
                kind="select", provider_index=int(parts[2]), model_index=int(parts[3])
            )
        if parts[0] == "cp" and len(parts) == 3:
            return token, PickerAction(kind="choices", page=int(parts[2]))
        if parts[0] == "cg" and len(parts) == 3:
            return token, PickerAction(kind="choose", model_index=int(parts[2]))
    except ValueError:
        return None
    return None


# ------------------------------------------------------- the flat picker


def render_choices(state: ChoiceState, page: int = 0) -> tuple[str, str]:
    """``(context_text, widget_json)`` for a flat choice picker (#193).

    Same payload shape, same delete-and-repost flow and the same ``✓`` marking as
    the model picker — only the shape of the list differs: one heading, one button
    per choice, no drill-down. The navigation row appears only when a list is long
    enough to need it, so today's commands render as a single flat row.
    """
    choices = list(state.choices)
    page = _clamp_page(page, len(choices), CHOICE_PAGE_SIZE)
    total = _page_count(len(choices), CHOICE_PAGE_SIZE)
    start = page * CHOICE_PAGE_SIZE

    buttons: list[dict[str, str]] = []
    for offset, choice in enumerate(choices[start : start + CHOICE_PAGE_SIZE]):
        index = start + offset
        label = str(choice.get("label") or choice.get("value") or "")
        if choice.get("is_current"):
            label = f"✓ {label}"
        buttons.append(
            _choice(chr(ord("A") + len(buttons)), label, f"cg:{state.token}:{index}")
        )

    if total > 1:
        if page > 0:
            buttons.append(_choice("◀", "Previous page", f"cp:{state.token}:{page - 1}"))
        if page + 1 < total:
            buttons.append(_choice("▶", "Next page", f"cp:{state.token}:{page + 1}"))
    buttons.append(_choice("✕", "Cancel", f"cx:{state.token}"))

    heading = state.title or "Choose one"
    if total > 1:
        heading += f" (page {page + 1}/{total})"
    return heading, _widget(heading, buttons)
