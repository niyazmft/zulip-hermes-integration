"""The flat choice picker — ``send_choice_picker`` (issue #193).

``/reasoning`` and ``/fast`` render one-tap buttons instead of a text status
card. This is the smaller sibling of the ``/model`` picker (#192), and it
deliberately shares that plumbing: same token shape, same registry rules, same
delete-and-repost flow. What differs is the *shape* of the form — one heading,
one flat list, normally one page — so those are the properties tested here.

The hook has **no custom text fallback**: returning ``success=False`` is what
makes the gateway keep its own status card, and that contract is asserted
directly.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from zulip import model_picker
from zulip.model_picker import (
    CHOICE_PAGE_SIZE,
    ChoiceState,
    PickerAction,
    PickerRegistry,
)

REASONING = [
    {"value": "none", "label": "none", "is_current": False},
    {"value": "low", "label": "low", "is_current": False},
    {"value": "medium", "label": "medium", "is_current": True},
    {"value": "high", "label": "high", "is_current": False},
    {"value": "reset", "label": "reset to default", "is_current": False},
]


def _state(**kwargs) -> ChoiceState:
    choices = kwargs.pop("choices", REASONING)
    registry = PickerRegistry(factory=ChoiceState)
    return registry.open(
        chat_id="573423",
        initiator="9",
        title="Reasoning effort",
        choices=choices,
        session_key="conv-1",
        **kwargs,
    )


def _choices(widget: str) -> list[dict]:
    return json.loads(widget)["extra_data"]["choices"]


def _replies(widget: str) -> list[str]:
    return [c["reply"] for c in _choices(widget)]


# ------------------------------------------------------------- the payload


def test_every_button_satisfies_the_web_client_schema():
    _text, widget = model_picker.render_choices(_state())
    payload = json.loads(widget)
    assert payload["widget_type"] == "zform"
    assert payload["extra_data"]["type"] == "choices"
    for choice in payload["extra_data"]["choices"]:
        assert set(choice) == {"type", "short_name", "long_name", "reply"}
        assert all(isinstance(choice[k], str) and choice[k] for k in choice)


def test_the_gateway_title_is_the_heading():
    text, widget = model_picker.render_choices(_state())
    assert text == "Reasoning effort"
    assert json.loads(widget)["extra_data"]["heading"] == "Reasoning effort"


def test_the_current_choice_is_marked_and_only_that_one():
    _text, widget = model_picker.render_choices(_state())
    marked = [c["long_name"] for c in _choices(widget) if c["long_name"].startswith("✓")]
    assert marked == ["✓ medium"]


def test_a_short_list_has_no_navigation_row():
    """Today's commands fit on one page; a page row would be noise."""
    replies = _replies(model_picker.render_choices(_state())[1])
    assert not any(r.startswith("cp:") for r in replies)
    assert any(r.startswith("cg:") for r in replies)
    assert any(r.startswith("cx:") for r in replies), "cancel is always offered"


def test_a_long_list_paginates_with_the_shared_row():
    many = [{"value": f"v{i}", "label": f"value {i}", "is_current": False} for i in range(30)]
    state = _state(choices=many)

    first = model_picker.render_choices(state, 0)
    buttons = [c for c in _choices(first[1]) if c["reply"].startswith("cg:")]
    assert len(buttons) == CHOICE_PAGE_SIZE
    assert "1/3" in first[0]
    assert f"cp:{state.token}:1" in _replies(first[1])

    middle = model_picker.render_choices(state, 1)
    replies = _replies(middle[1])
    assert f"cp:{state.token}:0" in replies
    assert f"cp:{state.token}:2" in replies


def test_a_page_past_the_end_clamps():
    _text, widget = model_picker.render_choices(_state(), page=99)
    assert _choices(widget)


# --------------------------------------------------------------- the token


@pytest.mark.parametrize(
    "content, kind",
    [
        ("cp:abcd1234:2", "choices"),
        ("cg:abcd1234:3", "choose"),
        ("cx:abcd1234", "cancel"),
    ],
)
def test_the_choice_reply_forms_parse(content, kind):
    parsed = model_picker.parse_reply(content)
    assert parsed is not None
    assert parsed[1].kind == kind


def test_a_choice_token_resolves_to_the_gateway_value():
    state = _state()
    token, action = model_picker.parse_reply(f"cg:{state.token}:4")
    assert token == state.token
    assert action == PickerAction(kind="choose", model_index=4)
    assert state.value(4) == "reset"


def test_both_picker_families_are_claimed_by_the_same_gate():
    assert model_picker.is_picker_reply("cg:abcd1234:0")
    assert model_picker.is_picker_reply("mp:abcd1234:0")
    assert not model_picker.is_picker_reply("/reasoning low")


# ----------------------------------------------------------- the hook


@pytest.fixture
def adapter(mock_platform_config, monkeypatch):
    import zulip.adapter as adapter_module
    import zulip.zulip_client as zulip_client_module

    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)
    monkeypatch.setenv("ZULIP_SITE", "https://test.zulipchat.com")
    monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
    monkeypatch.setenv("ZULIP_API_KEY", "k" * 32)
    monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
    monkeypatch.setenv("ZULIP_REQUIRE_MENTION", "false")

    class _Client:
        def __init__(self):
            self.sent: list[dict] = []
            self.deleted: list[int] = []
            self._next = 700

        def send_message(self, request):
            self.sent.append(request)
            self._next += 1
            return {"result": "success", "id": self._next}

        def update_message(self, request):
            return {"result": "success"}

        def delete_message(self, message_id):
            self.deleted.append(message_id)
            return {"result": "success"}

        def set_typing_status(self, request):
            return {"result": "success"}

        def update_message_flags(self, request):
            return {"result": "success"}

        def add_reaction(self, request):
            return {"result": "success"}

        def remove_reaction(self, request):
            return {"result": "success"}

        def get_messages(self, request):
            return {"result": "success", "messages": []}

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                self._client = _Client()

            def __getattr__(self, name):
                return getattr(self._client, name)

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    monkeypatch.setattr(
        adapter_module, "_LIVE_ADAPTERS", type(adapter_module._LIVE_ADAPTERS)()
    )
    adapter_module._clear_caches()

    from zulip.adapter import ZulipAdapter

    instance = ZulipAdapter(mock_platform_config)
    instance.handle_message = AsyncMock()
    return instance


async def _open(adapter, choices=None) -> ChoiceState:
    adapter.note_picker_initiator("573423", 9)
    result = await adapter.send_choice_picker(
        chat_id="573423",
        title="Reasoning effort",
        choices=choices or REASONING,
        session_key="conv-1",
        on_choice_selected=AsyncMock(return_value="Reasoning effort: high."),
        metadata={"thread_id": "conv-1", "topic": "deploy"},
    )
    assert result.success
    return next(iter(adapter._choice_pickers._pickers.values()))


@pytest.mark.asyncio
async def test_the_hook_posts_buttons_and_remembers_the_picker(adapter):
    state = await _open(adapter)
    sent = adapter.client._client.sent
    assert "widget_content" in sent[-1]
    payload = json.loads(sent[-1]["widget_content"])
    assert payload["extra_data"]["heading"] == "Reasoning effort"
    assert state.initiator == "9"


@pytest.mark.asyncio
async def test_a_tap_applies_the_choice_and_posts_the_result(adapter):
    state = await _open(adapter)
    callback = state.on_choice_selected

    consumed = await adapter.handle_picker_reply(
        f"cg:{state.token}:3", sender_id=9, chat_id="573423"
    )

    assert consumed is True
    callback.assert_awaited_once_with("573423", "high")
    assert len(adapter._choice_pickers) == 0, "single use"
    contents = [m["content"] for m in adapter.client._client.sent]
    assert "Reasoning effort: high." in contents
    assert adapter.client._client.deleted, "the used form is removed"


@pytest.mark.asyncio
async def test_only_the_initiator_can_tap(adapter):
    state = await _open(adapter)
    before = len(adapter.client._client.sent)

    consumed = await adapter.handle_picker_reply(
        f"cg:{state.token}:3", sender_id=4242, chat_id="573423"
    )

    assert consumed is True
    notices = [m["content"] for m in adapter.client._client.sent[before:]]
    assert any(model_picker.NOT_YOURS_NOTICE in n for n in notices)
    assert adapter._choice_pickers.peek(state.token) is not None


@pytest.mark.asyncio
async def test_an_expired_tap_says_so_and_is_still_consumed(adapter):
    state = await _open(adapter)
    adapter._choice_pickers.pop(state.token)

    consumed = await adapter.handle_picker_reply(
        f"cg:{state.token}:3", sender_id=9, chat_id="573423"
    )

    assert consumed is True
    contents = [m["content"] for m in adapter.client._client.sent]
    assert model_picker.EXPIRED_NOTICE in contents


@pytest.mark.asyncio
async def test_a_page_turn_replaces_the_form(adapter):
    many = [{"value": f"v{i}", "label": f"value {i}", "is_current": False} for i in range(30)]
    state = await _open(adapter, choices=many)
    first_id = state.message_id

    await adapter.handle_picker_reply(
        f"cp:{state.token}:1", sender_id=9, chat_id="573423"
    )

    assert state.message_id != first_id
    assert first_id in adapter.client._client.deleted


@pytest.mark.asyncio
async def test_a_failed_send_returns_failure_so_the_host_keeps_its_text_card(
    adapter, monkeypatch
):
    """The issue is explicit: no custom text fallback on this hook."""

    async def _boom(*a, **k):
        return {"result": "error"}

    adapter._sdk_call = _boom
    adapter.note_picker_initiator("573423", 9)

    result = await adapter.send_choice_picker(
        chat_id="573423",
        title="Fast mode",
        choices=[{"value": "fast", "label": "fast", "is_current": True}],
        session_key="conv-1",
        on_choice_selected=AsyncMock(return_value=""),
    )

    assert result.success is False
    assert len(adapter._choice_pickers) == 0


@pytest.mark.asyncio
async def test_no_choices_means_no_widget(adapter):
    result = await adapter.send_choice_picker(
        chat_id="573423",
        title="Fast mode",
        choices=[],
        session_key="conv-1",
        on_choice_selected=AsyncMock(return_value=""),
    )
    assert result.success is False


@pytest.mark.asyncio
async def test_a_typed_reasoning_level_is_left_alone(adapter):
    """``/reasoning <level>`` must not be captured by the picker path."""
    adapter.handle_message.reset_mock()

    await adapter._handle_message(
        {
            "id": 1,
            "type": "stream",
            "stream_id": 573423,
            "subject": "general",
            "display_recipient": "test",
            "content": "/reasoning low",
            "sender_email": "user@zulip.com",
            "sender_full_name": "User",
            "sender_id": 9,
            "flags": [],
        }
    )

    assert not model_picker.is_picker_reply("/reasoning low")
    # It reaches the gateway, which owns the command.
    adapter.handle_message.assert_awaited()
