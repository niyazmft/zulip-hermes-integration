"""Tests for the zform-button exec-approval prompt (native approval buttons).

The adapter attaches a Zulip zform widget to the exec-approval prompt so
web/desktop clients render clickable Allow/Deny buttons. Each button's canned
reply is the gateway's plain-text approval command, so a click resolves through
the same path as typing it.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import zulip.adapter as adapter_module
from gateway.platforms.base import ExecApprovalPrompt

REPO_ROOT = Path(__file__).resolve().parents[1]

FULL_ACTIONS = [
    ("Allow Once", "once", "primary"),
    ("Allow Session", "session", ""),
    ("Always Allow", "always", ""),
    ("Deny", "deny", "danger"),
]

EXPECTED_REPLIES = {
    "once": "/approve",
    "session": "/approve session",
    "always": "/approve always",
    "deny": "/deny",
}


def _prompt(chat_id="635267", actions=None, metadata=None, smart_denied=False):
    return ExecApprovalPrompt(
        chat_id=chat_id,
        session_key="agent:main:zulip:stream:635267",
        text="⚠️ Command Approval Required\n\n```\nls -la\n```\nReason: test command",
        actions=FULL_ACTIONS if actions is None else actions,
        command="ls -la",
        description="test command",
        smart_denied=smart_denied,
        metadata=metadata,
    )


@pytest.fixture
def adapter(mock_platform_config, monkeypatch):
    """ZulipAdapter with the real SDK swapped for a mock client."""
    monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)
    from tests.conftest import MockZulipClient

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                self._client = MockZulipClient(**kwargs)

            def __getattr__(self, name):
                return getattr(self._client, name)

    monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
    from zulip.adapter import ZulipAdapter

    return ZulipAdapter(mock_platform_config)


class TestZformWidgetPayload:
    """Widget payload must satisfy the web client's zod schema (zform_data.ts):
    every choice carries type/short_name/long_name/reply strings plus a heading,
    even though the server-side validator does not enforce the per-choice type."""

    # Gateway choice vocabulary (gateway/platforms/base.py _exec_approval_actions)
    # and the exact command each button's reply must replay.
    ACTION_TO_REPLY = [
        ("once", "Allow Once", "/approve"),
        ("session", "Allow Session", "/approve session"),
        ("always", "Always Allow", "/approve always"),
        ("deny", "Deny", "/deny"),
    ]

    def test_reply_table_covers_exactly_the_gateway_vocabulary(self, adapter):
        """Drift guard: the mapping table must cover every choice the gateway
        core can put in prompt.actions — and nothing else. A new gateway choice
        without a reply here would render no widget (bail-out), which is safe
        but would silently lose buttons."""
        assert set(adapter._EA_ZFORM_REPLY) == {a for a, _label, _r in self.ACTION_TO_REPLY}
        assert set(adapter._EA_ZFORM_INSTRUCTIONS) == set(adapter._EA_ZFORM_REPLY)

    @pytest.mark.parametrize("action,label,expected_reply", ACTION_TO_REPLY)
    def test_each_action_replays_its_exact_command(self, adapter, action, label, expected_reply):
        """Each of /approve, /approve session, /approve always, /deny — in
        isolation, so a wrong mapping for one tier fails its own test."""
        widget = json.loads(
            adapter._zform_widget_for_approval(_prompt(actions=[(label, action, "")]))
        )
        (choice,) = widget["extra_data"]["choices"]
        assert choice["reply"] == expected_reply
        assert choice["long_name"] == label

    def test_payload_matches_client_schema(self, adapter):
        payload = adapter._zform_widget_for_approval(_prompt())
        assert payload is not None
        widget = json.loads(payload)

        assert widget["widget_type"] == "zform"
        extra = widget["extra_data"]
        assert extra["type"] == "choices"
        assert isinstance(extra["heading"], str) and extra["heading"]

        short_names = []
        for choice, (label, action, _style) in zip(extra["choices"], FULL_ACTIONS):
            assert choice["type"] == "multiple_choice"
            assert isinstance(choice["short_name"], str) and choice["short_name"]
            assert choice["long_name"] == label
            assert choice["reply"] == EXPECTED_REPLIES[action]
            short_names.append(choice["short_name"])
        assert len(short_names) == len(set(short_names))  # unique button keys

    def test_replies_are_gateway_approval_commands(self, adapter):
        """Every canned reply must be a command the gateway's approval resolver
        already accepts, so a click resolves exactly like typing it."""
        widget = json.loads(adapter._zform_widget_for_approval(_prompt()))
        replies = {c["reply"] for c in widget["extra_data"]["choices"]}
        assert replies == set(EXPECTED_REPLIES.values())

    def test_smart_denied_offers_only_once_and_deny(self, adapter):
        actions = [("Allow Once", "once", "primary"), ("Deny", "deny", "danger")]
        widget = json.loads(adapter._zform_widget_for_approval(_prompt(actions=actions)))
        assert [c["reply"] for c in widget["extra_data"]["choices"]] == [
            "/approve",
            "/deny",
        ]

    def test_unknown_choice_bails_out(self, adapter):
        """An unrecognized choice vocabulary must yield no widget (gateway falls
        back to its text prompt) rather than buttons that resolve nothing."""
        assert adapter._zform_widget_for_approval(
            _prompt(actions=[("Allow Once", "once", "primary"), ("Maybe", "maybe", "")])
        ) is None

    def test_empty_actions_bail_out(self, adapter):
        assert adapter._zform_widget_for_approval(_prompt(actions=[])) is None


class TestApprovalFallbackInstructions:
    def test_instructions_cover_all_actions(self, adapter):
        text = adapter._approval_fallback_instructions(_prompt())
        assert "/approve" in text and "/approve session" in text
        assert "/approve always" in text and "/deny" in text
        assert text.startswith("Reply ") and text.endswith(".")

    def test_instructions_match_reduced_action_set(self, adapter):
        actions = [("Allow Once", "once", "primary"), ("Deny", "deny", "danger")]
        text = adapter._approval_fallback_instructions(_prompt(actions=actions))
        assert "/approve " not in text  # no session/always tier offered
        assert "/approve" in text and "/deny" in text

    def test_single_action_instruction(self, adapter):
        text = adapter._approval_fallback_instructions(
            _prompt(actions=[("Deny", "deny", "danger")])
        )
        assert text == "Reply `/deny` to cancel."


@pytest.mark.asyncio
class TestApprovalPromptSend:
    @staticmethod
    def _capture_sdk_call(adapter, responses=None):
        """Capture every send. `responses` is an optional per-call queue: dict
        items are returned, Exception items are raised; absent entries get a
        default success."""
        captured = {"requests": []}
        queue = list(responses or [])

        async def fake_sdk_call(fn, request, timeout=None):
            captured["requests"].append(request)
            if queue:
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            return {"result": "success", "id": 4000 + len(captured["requests"])}

        adapter._sdk_call = fake_sdk_call
        return captured

    async def test_stream_sends_context_then_widget(self, adapter):
        """Two messages: the context text (visible on every client) first, then
        the button widget — a rendered zform replaces its own message body on
        web, so context must not live in the widget message."""
        captured = self._capture_sdk_call(adapter)
        prompt = _prompt(metadata={"topic": "deploys"})

        result = await adapter._send_exec_approval_prompt(prompt)

        assert result.success is True
        assert result.message_id == "4002"  # last-sent (interactive) id
        context, widget = captured["requests"]
        assert len(captured["requests"]) == 2

        assert context["type"] == "stream"
        assert context["to"] == 635267
        assert context["topic"] == "deploys"
        assert "ls -la" in context["content"]
        assert "Reason: test command" in context["content"]
        assert "`/approve` to execute this one operation" in context["content"]
        assert "`/deny` to cancel" in context["content"]
        assert "widget_content" not in context

        assert widget["content"] == "Choose an action:"
        parsed = json.loads(widget["widget_content"])
        assert parsed["widget_type"] == "zform"
        assert len(parsed["extra_data"]["choices"]) == 4

    async def test_stream_topic_falls_back_to_cache_then_general(self, adapter):
        adapter._topic_cache["635267"] = "cached-topic"
        captured = self._capture_sdk_call(adapter)
        await adapter._send_exec_approval_prompt(_prompt(metadata={}))
        assert all(r["topic"] == "cached-topic" for r in captured["requests"])

        adapter._topic_cache.clear()
        captured = self._capture_sdk_call(adapter)
        await adapter._send_exec_approval_prompt(_prompt(metadata=None))
        assert all(r["topic"] == "general" for r in captured["requests"])

    async def test_dm_send(self, adapter):
        captured = self._capture_sdk_call(adapter)
        prompt = _prompt(chat_id="dm:1032616", metadata={"topic": "ignored"})

        result = await adapter._send_exec_approval_prompt(prompt)

        assert result.success is True
        assert len(captured["requests"]) == 2
        for request in captured["requests"]:
            assert request["type"] == "private"
            assert request["to"] == [1032616]
        assert "widget_content" in captured["requests"][1]

    async def test_widgetless_prompt_sends_single_text_message(self, adapter):
        """An unrenderable action set must still deliver the text prompt."""
        captured = self._capture_sdk_call(adapter)
        prompt = _prompt(actions=[("Maybe", "maybe", "")])

        result = await adapter._send_exec_approval_prompt(prompt)

        assert result.success is True
        assert len(captured["requests"]) == 1
        assert "widget_content" not in captured["requests"][0]
        assert "ls -la" in captured["requests"][0]["content"]

    async def test_widget_send_failure_still_success(self, adapter):
        """Context delivered + widget failed = the exact text-fallback
        experience; no runner re-send (it would duplicate the context)."""
        captured = self._capture_sdk_call(
            adapter, responses=[{"result": "success", "id": 111}, RuntimeError("boom")]
        )

        result = await adapter._send_exec_approval_prompt(_prompt())

        assert result.success is True
        assert result.message_id == "111"

    async def test_context_send_failure_still_success_if_widget_sent(self, adapter):
        captured = self._capture_sdk_call(
            adapter, responses=[RuntimeError("boom"), {"result": "success", "id": 222}]
        )

        result = await adapter._send_exec_approval_prompt(_prompt())

        assert result.success is True
        assert result.message_id == "222"

    async def test_both_sends_fail_returns_failure(self, adapter):
        self._capture_sdk_call(
            adapter,
            responses=[RuntimeError("boom"), RuntimeError("boom")],
        )

        result = await adapter._send_exec_approval_prompt(_prompt())

        assert result.success is False

    async def test_api_error_response_counts_as_failure(self, adapter):
        self._capture_sdk_call(
            adapter,
            responses=[
                {"result": "error", "msg": "nope"},
                {"result": "error", "msg": "nope"},
            ],
        )

        result = await adapter._send_exec_approval_prompt(_prompt())

        assert result.success is False


class TestNativeButtonModeEnabled:
    def test_hook_overrides_base(self):
        """The gateway enables native-button mode exactly when the adapter class
        overrides the base _send_exec_approval_prompt no-op."""
        from gateway.platforms.base import BasePlatformAdapter

        from zulip.adapter import ZulipAdapter

        assert "_send_exec_approval_prompt" in ZulipAdapter.__dict__
        assert (
            ZulipAdapter._send_exec_approval_prompt
            is not BasePlatformAdapter.__dict__.get("_send_exec_approval_prompt")
        )


class TestGatewayVersionCompatibility:
    """The exec-approval hook only exists on Hermes >= 0.21.3. On older
    gateways the adapter must still import (feature inactive, gateway keeps its
    plain-text approval prompt) rather than failing to load entirely."""

    def test_imports_on_gateway_without_exec_approval_prompt(self):
        script = textwrap.dedent(
            '''
            import sys, types
            from dataclasses import dataclass

            gateway = types.ModuleType("gateway")
            platforms = types.ModuleType("gateway.platforms")
            base = types.ModuleType("gateway.platforms.base")

            @dataclass
            class SendResult:
                success: bool
                message_id: str = ""

            class MessageType:
                TEXT = "text"

            @dataclass
            class MessageEvent:
                text: str = ""

            class BasePlatformAdapter:
                pass

            base.SendResult = SendResult
            base.MessageType = MessageType
            base.MessageEvent = MessageEvent
            base.BasePlatformAdapter = BasePlatformAdapter

            # Deliberately NO ExecApprovalPrompt / send_exec_approval here,
            # mirroring Hermes < 0.21.3.
            gateway.platforms = platforms
            platforms.base = base
            sys.modules["gateway"] = gateway
            sys.modules["gateway.platforms"] = platforms
            sys.modules["gateway.platforms.base"] = base

            config = types.ModuleType("gateway.config")
            config.Platform = type("Platform", (), {})
            config.PlatformConfig = type("PlatformConfig", (), {})
            sys.modules["gateway.config"] = config

            import zulip.adapter  # must not raise ImportError
            print("OK")
            '''
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "OK"
