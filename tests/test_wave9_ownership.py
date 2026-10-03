"""Ownership, delegation and gate pins for the Wave 9 extraction (``inbound``).

Wave 9 moved the inbound gate chain out of ``ZulipAdapter._handle_message`` into
``zulip.inbound.handle_message``. The method stays on the adapter as a one-line
delegate because the connection event loop (``zulip/connection.py``), the
recovery path (``zulip/recovery.py``) and the reaction-trigger path all call
``adapter._handle_message`` by name, so the delegate is load-bearing.

Unlike Waves 5/7/8 this wave moves NO module-level name. Every name the chain
reads (``_resolve_chatmode``, ``create_mention_regex``, ``ReactionLifecycle``,
``is_stop_listening_message``, ``_private_chat_id``, …) keeps its owner, and the
chain reads it through that owner — so there is no adapter-module patch target
to migrate and a ``patch.object(zulip.adapter, …)`` aimed at the chain was never
live in the first place. These tests pin the reads to the owners anyway, so a
future move that reintroduces an adapter-local binding fails loudly.

Two gates that had NO coverage before this wave are pinned here end-to-end
through the chain: the per-sender rate limit and the DM-policy block. Both are
security controls whose position in the order matters, so a regression would
otherwise be silent.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

import zulip.adapter as adapter_module
import zulip.commands as commands_module
import zulip.engagement as engagement_module
import zulip.inbound as inbound_module
import zulip.reactions as reactions_module
import zulip.settings as settings_module
import zulip.text_utils as text_utils_module
import zulip.zulip_client as zulip_client_module

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "zulip"


class TestInboundOwnsTheChain:
    def test_inbound_defines_handle_message(self):
        assert inbound_module.handle_message.__module__ == "zulip.inbound"

    def test_is_a_coroutine_function(self):
        assert inspect.iscoroutinefunction(inbound_module.handle_message)

    def test_the_adapter_takes_it_as_the_first_collaborator(self):
        params = list(
            inspect.signature(inbound_module.handle_message).parameters
        )
        assert params[:2] == ["adapter", "message"]

    def test_adapter_method_remains_a_callable_delegate(self):
        # Load-bearing: connection.py / recovery.py / the reaction-trigger path
        # call adapter._handle_message by name.
        assert callable(getattr(adapter_module.ZulipAdapter, "_handle_message"))

    def test_adapter_signature_unchanged(self):
        params = list(
            inspect.signature(
                adapter_module.ZulipAdapter._handle_message
            ).parameters
        )
        assert params == ["self", "message"]


class TestAdapterDelegateForwards:
    @pytest.mark.asyncio
    async def test_delegate_calls_inbound_with_self_and_message(
        self, monkeypatch
    ):
        sentinel_adapter = object()
        message = {"id": 7}
        delegate = AsyncMock()
        monkeypatch.setattr(inbound_module, "handle_message", delegate)

        await adapter_module.ZulipAdapter._handle_message(sentinel_adapter, message)

        delegate.assert_awaited_once_with(sentinel_adapter, message)


class TestChainReadsTheOwningModules:
    def test_module_level_functions_come_from_their_owners(self):
        assert inbound_module._resolve_chatmode is settings_module.resolve_chatmode
        assert (
            inbound_module.create_mention_regex
            is text_utils_module.create_mention_regex
        )
        assert (
            inbound_module.normalize_mention is text_utils_module.normalize_mention
        )
        assert (
            inbound_module.strip_onchar_prefix
            is text_utils_module.strip_onchar_prefix
        )
        assert (
            inbound_module.strip_html_to_text
            is text_utils_module.strip_html_to_text
        )
        assert (
            inbound_module.ReactionLifecycle is reactions_module.ReactionLifecycle
        )
        assert (
            inbound_module.is_stop_listening_message
            is engagement_module.is_stop_listening_message
        )
        assert (
            inbound_module.is_end_session_message
            is engagement_module.is_end_session_message
        )
        assert (
            inbound_module._private_chat_id
            is zulip_client_module.private_chat_id
        )
        assert (
            inbound_module._private_recipient_ids
            is zulip_client_module.private_recipient_ids
        )
        assert inbound_module.commands is commands_module

    def test_adapter_re_export_still_resolves_to_the_owner(self):
        # ``from zulip.adapter import _resolve_chatmode`` is a published import
        # (it predates the extraction); the re-export must stay the owner.
        assert (
            adapter_module._resolve_chatmode is settings_module.resolve_chatmode
        )


class TestNoCycleAndRationaleDocumented:
    def test_inbound_never_imports_the_adapter(self):
        imports = [
            line
            for line in (PLUGIN_DIR / "inbound.py")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.startswith(("import ", "from "))
        ]
        assert not [line for line in imports if "adapter" in line]

    def test_module_docstring_records_the_rejected_decision_split(self):
        doc = inbound_module.__doc__ or ""
        assert "REJECTED" in doc
        for gate in (
            "rate limit",
            "observe buffer",
            "group policy",
            "engagement stop",
            "command interception",
            "DM policy",
        ):
            assert gate in doc, gate


# --------------------------------------------------------------------------
# Gates that had no coverage before Wave 9 — pinned through the chain so the
# extraction cannot silently drop them.
# --------------------------------------------------------------------------


def _build(mock_platform_config, monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

    class MockZulipModule:
        class Client:
            def __init__(self, email=None, api_key=None, site=None):
                pass

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(mock_platform_config)
    a.email = "bot@zulip.com"
    a.handle_message = AsyncMock()
    a.client = MagicMock()
    return a


def _dm(content, *, sender="user@zulip.com"):
    return {
        "id": 2,
        "type": "private",
        "content": content,
        "sender_email": sender,
        "sender_full_name": sender.split("@")[0],
        "sender_id": 42,
    }


@pytest.fixture(autouse=True)
def _hermes_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))


class TestRateLimitGate:
    @pytest.mark.asyncio
    async def test_second_message_from_same_sender_is_dropped(
        self, mock_platform_config, monkeypatch
    ):
        from zulip.rate_limiter import RateLimiter

        a = _build(mock_platform_config, monkeypatch)
        a._rate_limiter = RateLimiter(max_per_minute=1)
        a._audit_logger.log_rate_limit_exceeded = AsyncMock()

        await a._handle_message(_dm("first"))
        await a._handle_message(_dm("second"))

        assert a.handle_message.call_count == 1
        a._audit_logger.log_rate_limit_exceeded.assert_awaited_once()


class TestDmPolicyGate:
    @pytest.mark.asyncio
    async def test_disabled_dm_policy_blocks_and_does_not_dispatch(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(mock_platform_config, monkeypatch, ZULIP_DM_POLICY="disabled")
        a._policy.refresh_if_changed()
        assert a._policy.mode == "disabled"

        await a._handle_message(_dm("let me in"))

        assert a.handle_message.call_count == 0
        a.client.send_message.assert_called()
