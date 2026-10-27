"""The interactive ``/model`` picker (issue #192).

Two halves are tested here, and the split matters:

* **the pure half** — token scheme, pagination, zform payload, registry expiry
  and single-use. These are the rules that decide whether a click is allowed to
  do anything, so they are tested without a gateway.
* **the adapter half** — that a click is consumed before the agent sees it, that
  a stranger cannot drive someone else's picker, and that a failed widget send
  degrades to an actionable text list instead of leaving the user with nothing.

Nothing here asserts a provider or model name: the listing comes from the
gateway, and a test that hard-coded one would be the bug the issue forbids.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from zulip import model_picker
from zulip.model_picker import (
    MODEL_PAGE_SIZE,
    PROVIDER_PAGE_SIZE,
    PickerAction,
    PickerRegistry,
    PickerState,
)


def _provider(slug: str, count: int, *, current: bool = False) -> dict:
    return {
        "slug": slug,
        "name": slug.title(),
        "models": [f"{slug}-model-{i}" for i in range(count)],
        "total_models": count,
        "is_current": current,
    }


def _state(**kwargs) -> PickerState:
    providers = kwargs.pop(
        "providers", [_provider("alpha", 3), _provider("beta", 25)]
    )
    registry = PickerRegistry()
    return registry.open(
        chat_id="573423",
        initiator="9",
        session_key="conv-1",
        current_model="alpha-model-0",
        current_provider="alpha",
        providers=providers,
        **kwargs,
    )


def _choices(widget: str) -> list[dict]:
    return json.loads(widget)["extra_data"]["choices"]


def _replies(widget: str) -> list[str]:
    return [choice["reply"] for choice in _choices(widget)]


# ------------------------------------------------------------- the payload


def test_every_choice_satisfies_the_web_client_schema():
    _text, widget = model_picker.render_providers(_state())
    payload = json.loads(widget)
    assert payload["widget_type"] == "zform"
    assert payload["extra_data"]["type"] == "choices"
    assert payload["extra_data"]["heading"]
    for choice in payload["extra_data"]["choices"]:
        assert set(choice) == {"type", "short_name", "long_name", "reply"}
        assert choice["type"] == "multiple_choice"
        assert all(isinstance(choice[k], str) and choice[k] for k in choice)


def test_short_names_are_unique_within_a_page():
    """Two buttons with the same label are indistinguishable on the client."""
    _text, widget = model_picker.render_models(_state(), 1, 0)
    shorts = [c["short_name"] for c in _choices(widget)]
    assert len(shorts) == len(set(shorts))


def test_the_current_provider_is_marked():
    text, widget = model_picker.render_providers(_state())
    labels = [c["long_name"] for c in _choices(widget)]
    assert any(label.startswith("✓ ") for label in labels)
    assert "alpha" in text.lower() or "current model" in text


# ------------------------------------------------------------ pagination


def test_providers_paginate_at_ten_per_page():
    providers = [_provider(f"p{i}", 2) for i in range(23)]
    state = _state(providers=providers)

    first = model_picker.render_providers(state, 0)
    assert len(_choices(first[1])) == PROVIDER_PAGE_SIZE + 2  # + next + cancel
    assert "1/3" in first[0]

    middle = model_picker.render_providers(state, 1)
    middle_replies = _replies(middle[1])
    assert f"mp:{state.token}:0" in middle_replies  # prev
    assert f"mp:{state.token}:2" in middle_replies  # next

    last = model_picker.render_providers(state, 2)
    last_replies = _replies(last[1])
    assert f"mp:{state.token}:1" in last_replies
    assert f"mp:{state.token}:3" not in last_replies  # no next past the end


def test_models_paginate_at_nine_per_page():
    state = _state()
    first = model_picker.render_models(state, 1, 0)  # beta has 25 models
    model_choices = [c for c in _choices(first[1]) if c["reply"].startswith("mg:")]
    assert len(model_choices) == MODEL_PAGE_SIZE
    assert "1/3" in first[0]

    middle = model_picker.render_models(state, 1, 1)
    replies = _replies(middle[1])
    assert f"mm:{state.token}:1:0" in replies  # prev
    assert f"mm:{state.token}:1:2" in replies  # next
    assert f"mp:{state.token}:0" in replies  # back to providers
    assert f"mx:{state.token}" in replies  # cancel


def test_a_page_past_the_end_clamps_instead_of_rendering_nothing():
    state = _state(providers=[_provider("alpha", 3)])
    _text, widget = model_picker.render_providers(state, 99)
    assert _choices(widget), "an out-of-range page must render a usable page"


def test_a_model_page_click_resolves_to_the_right_model():
    """The token carries indices; the registry holds the real ids."""
    state = _state()
    token, action = model_picker.parse_reply(f"mg:{state.token}:1:24")
    assert token == state.token
    assert action == PickerAction(kind="select", provider_index=1, model_index=24)
    assert state.model(1, 24) == ("beta-model-24", "beta")


# --------------------------------------------------------------- the token


@pytest.mark.parametrize(
    "content, kind",
    [
        ("mp:abcd1234:0", "providers"),
        ("mm:abcd1234:1:2", "models"),
        ("mg:abcd1234:0:3", "select"),
        ("mx:abcd1234", "cancel"),
    ],
)
def test_every_reply_form_parses(content, kind):
    parsed = model_picker.parse_reply(content)
    assert parsed is not None
    assert parsed[1].kind == kind


@pytest.mark.parametrize(
    "content",
    [
        "",
        "hello there",
        "/model",
        "mp:abcd",  # missing page
        "mp:abcd:x",  # not an integer
        "mg:abcd:1",  # missing model index
        "zz:abcd:0",
        "mmm:abcd:0:0",
    ],
)
def test_a_malformed_token_is_not_a_picker_click(content):
    """Rejecting rather than guessing: a token that does not parse must fall
    through to normal handling instead of being half-interpreted."""
    assert model_picker.parse_reply(content) is None


def test_only_our_prefixes_are_claimed():
    assert model_picker.is_picker_reply("mp:abcd1234:0")
    assert model_picker.is_picker_reply("mx:abcd1234")
    assert not model_picker.is_picker_reply("model: something")
    assert not model_picker.is_picker_reply("/model")


# ------------------------------------------------------------- the registry


def test_a_picker_expires_after_ten_minutes():
    now = [0.0]
    registry = PickerRegistry(clock=lambda: now[0])
    state = registry.open(
        chat_id="1", initiator="9", session_key="k", providers=[_provider("a", 1)]
    )
    now[0] = model_picker.TTL_SECONDS - 1
    assert registry.peek(state.token) is not None
    now[0] = model_picker.TTL_SECONDS + 1
    assert registry.peek(state.token) is None


def test_a_picker_is_single_use():
    registry = PickerRegistry()
    state = registry.open(
        chat_id="1", initiator="9", session_key="k", providers=[_provider("a", 1)]
    )
    assert registry.pop(state.token) is not None
    assert registry.pop(state.token) is None, "a used picker must not act twice"


def test_the_registry_is_bounded():
    registry = PickerRegistry(max_pickers=4)
    tokens = [
        registry.open(
            chat_id=str(i), initiator="9", session_key="k", providers=[]
        ).token
        for i in range(10)
    ]
    assert len(registry) == 4
    assert registry.peek(tokens[0]) is None, "the oldest picker is evicted first"


# -------------------------------------------------------- the text fallback


def test_the_text_listing_is_actionable_without_buttons():
    state = _state()
    text = model_picker.render_text_listing(state)
    assert f"`/model alpha/alpha-model-0`" in text
    assert "Reply with one of the switches above." in text


# ----------------------------------------------------------- the adapter


@pytest.fixture
def adapter(mock_platform_config, monkeypatch):
    import zulip.adapter as adapter_module
    import zulip.zulip_client as zulip_client_module

    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)
    monkeypatch.setenv("ZULIP_SITE", "https://test.zulipchat.com")
    monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
    monkeypatch.setenv("ZULIP_API_KEY", "k" * 32)
    # Answer everything, so the inbound gate chain reaches the interception point
    # instead of dropping the message earlier for an unrelated reason.
    monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
    monkeypatch.setenv("ZULIP_REQUIRE_MENTION", "false")

    class _Client:
        def __init__(self):
            self.sent: list[dict] = []
            self.deleted: list[int] = []
            self._next = 500

        def send_message(self, request):
            self.sent.append(request)
            self._next += 1
            return {"result": "success", "id": self._next}

        def update_message(self, request):
            return {"result": "success"}

        def delete_message(self, message_id):
            # The Zulip SDK takes the id positionally (``client.delete_message(id)``).
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


async def _open(adapter, providers=None) -> model_picker.PickerState:
    providers = providers or [_provider("alpha", 3), _provider("beta", 25)]
    adapter.note_picker_initiator("573423", 9)
    result = await adapter.send_model_picker(
        chat_id="573423",
        providers=providers,
        current_model="alpha-model-0",
        current_provider="alpha",
        session_key="conv-1",
        on_model_selected=AsyncMock(return_value="Now using beta-1."),
        metadata={"thread_id": "conv-1", "topic": "deploy"},
    )
    assert result.success, "the widget send must succeed on a healthy client"
    return next(iter(adapter._model_pickers._pickers.values()))


@pytest.mark.asyncio
async def test_the_hook_posts_a_widget_and_remembers_the_picker(adapter):
    state = await _open(adapter)
    sent = adapter.client._client.sent
    assert sent, "the hook must post a message"
    assert "widget_content" in sent[-1]
    assert json.loads(sent[-1]["widget_content"])["widget_type"] == "zform"
    assert state.initiator == "9", "the recorded initiator is the one who asked"


@pytest.mark.asyncio
async def test_only_the_initiator_can_drive_the_picker(adapter):
    state = await _open(adapter)
    before = len(adapter.client._client.sent)

    consumed = await adapter.handle_model_picker_reply(
        f"mg:{state.token}:1:1", sender_id=4242, chat_id="573423"
    )

    assert consumed is True, "a token is always consumed, never leaked as chat"
    notices = [m["content"] for m in adapter.client._client.sent[before:]]
    assert any(model_picker.NOT_YOURS_NOTICE in n for n in notices)
    assert adapter._model_pickers.peek(state.token) is not None, "the form stays open"


@pytest.mark.asyncio
async def test_selecting_a_model_switches_once_and_deletes_the_form(adapter):
    state = await _open(adapter)
    callback = state.on_model_selected

    consumed = await adapter.handle_model_picker_reply(
        f"mg:{state.token}:1:1", sender_id=9, chat_id="573423"
    )

    assert consumed is True
    callback.assert_awaited_once_with("573423", "beta-model-1", "beta")
    assert adapter._model_pickers.peek(state.token) is None, "single use"
    contents = [m["content"] for m in adapter.client._client.sent]
    assert "Now using beta-1." in contents
    assert adapter.client._client.deleted, "the used form is removed"


@pytest.mark.asyncio
async def test_a_page_turn_replaces_the_form_rather_than_stacking_them(adapter):
    state = await _open(adapter)
    first_id = state.message_id

    await adapter.handle_model_picker_reply(
        f"mm:{state.token}:1:1", sender_id=9, chat_id="573423"
    )

    assert state.message_id != first_id
    assert first_id in adapter.client._client.deleted


@pytest.mark.asyncio
async def test_an_expired_click_says_so_in_place(adapter):
    state = await _open(adapter)
    adapter._model_pickers.pop(state.token)  # simulate the TTL sweep

    consumed = await adapter.handle_model_picker_reply(
        f"mg:{state.token}:1:1", sender_id=9, chat_id="573423"
    )

    assert consumed is True
    contents = [m["content"] for m in adapter.client._client.sent]
    assert model_picker.EXPIRED_NOTICE in contents


@pytest.mark.asyncio
async def test_cancel_consumes_the_picker(adapter):
    state = await _open(adapter)
    consumed = await adapter.handle_model_picker_reply(
        f"mx:{state.token}", sender_id=9, chat_id="573423"
    )
    assert consumed is True
    assert adapter._model_pickers.peek(state.token) is None
    contents = [m["content"] for m in adapter.client._client.sent]
    assert model_picker.CANCELLED_NOTICE in contents


@pytest.mark.asyncio
async def test_a_failed_widget_send_returns_failure_so_the_host_can_fall_back(
    adapter, monkeypatch
):
    async def _boom(*a, **k):
        return {"result": "error", "msg": "nope"}

    adapter._sdk_call = _boom
    adapter.note_picker_initiator("573423", 9)

    result = await adapter.send_model_picker(
        chat_id="573423",
        providers=[_provider("alpha", 3)],
        current_model="",
        current_provider="alpha",
        session_key="conv-1",
        on_model_selected=AsyncMock(return_value=""),
    )

    assert result.success is False
    assert len(adapter._model_pickers) == 0, "a picker that was never shown is dropped"


# --------------------------------------------------------------- the wiring


def _stream_msg(content, *, msg_id=1, topic="general", stream_id=573423):
    return {
        "id": msg_id,
        "type": "stream",
        "stream_id": stream_id,
        "subject": topic,
        "display_recipient": "test",
        "content": content,
        "sender_email": "user@zulip.com",
        "sender_full_name": "User",
        "sender_id": 9,
        "flags": [],
    }


@pytest.mark.asyncio
async def test_a_token_is_consumed_before_the_agent_ever_sees_it(adapter):
    """A button click must never reach the agent as chat text."""
    state = await _open(adapter)
    adapter.handle_message.reset_mock()

    await adapter._handle_message(_stream_msg(f"mg:{state.token}:1:1"))

    adapter.handle_message.assert_not_awaited()
    callback = state.on_model_selected
    callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_pick_model_is_a_command_not_chat(adapter):
    """/pick-model is handled by the plugin, never dispatched to the agent."""
    adapter.handle_message.reset_mock()

    await adapter._handle_message(_stream_msg("/pick-model"))

    adapter.handle_message.assert_not_awaited()
    contents = [m.get("content", "") for m in adapter.client._client.sent]
    assert any("/model" in c for c in contents), (
        "with no provider listing on this host the command must point at /model"
    )


@pytest.mark.asyncio
async def test_a_bare_model_command_records_who_asked(adapter):
    """The hook carries no sender, so the identity is captured inbound."""
    adapter.handle_message.reset_mock()

    await adapter._handle_message(_stream_msg("/model"))

    assert adapter._picker_initiators.get("573423") == "9"
