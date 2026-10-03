"""Ownership, delegation and state-single-owner pins for Wave 10 (``routing``).

Wave 10 moved stable conversation identity out of ``ZulipAdapter`` into
``zulip.routing``: the state (``_conversations``, ``_pending_conversations``,
``_topic_cache``), the session-key derivation, the topic rename/delete
maintenance, the poll-loop dispatch pre-resolution, and the legacy-session
migration / start backfill.

Two invariants are pinned here because their failure mode is silent:

* the poll-loop → handler stash handoff. ``pre_resolve_conversation`` is called
  from the poll loop and stashes ``msg_id -> conversation_id``; the deferred
  handler in ``zulip.inbound`` POPS that stash. Both must read the SAME dict
  object, or a rename in the same batch forks the session.
* single-owner state. ``RoutingState`` owns the state; the adapter's
  ``_conversations`` / ``_pending_conversations`` / ``_topic_cache`` are live
  views onto that one object (never a copy), and production reads go through
  ``adapter._routing``. A test reading one path while production wrote another
  would assert on an empty object — the waves 5/8 hazard.

The negative control: a stale patch target on ``zulip.adapter`` for a moved
module-level name raises ``AttributeError`` instead of quietly no-oping.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import zulip.adapter as adapter_module
import zulip.routing as routing_module
import zulip.zulip_client as zulip_client_module

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "zulip"

MOVED_METHODS = (
    "pre_resolve_conversation",
    "handle_topic_update",
    "apply_topic_deletion",
    "routed_topic",
    "routed_topic_for_chat",
    "migrate_legacy_topic_sessions",
    "backfill_session_starts",
    "_session_db",
)

# (adapter delegate, RoutingState owner method, exact args the delegate forwards)
DELEGATE_CALLS = (
    ("_pre_resolve_conversation", "pre_resolve_conversation", ({"id": 1}, "1")),
    ("_handle_topic_update", "handle_topic_update", ({"id": 1},)),
    ("_routed_topic", "routed_topic", (7, {"thread_id": "c"})),
    ("_routed_topic_for_chat", "routed_topic_for_chat", ("7", {"thread_id": "c"})),
    ("_migrate_legacy_topic_sessions", "migrate_legacy_topic_sessions", ()),
    ("_backfill_session_starts", "backfill_session_starts", ()),
    ("_session_db", "_session_db", ()),
)


def _plain_adapter(mock_platform_config, monkeypatch, tmp_path):
    monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", "true")
    monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                pass

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    from zulip.adapter import ZulipAdapter

    adapter = ZulipAdapter(mock_platform_config)
    adapter.email = "bot@zulip.com"
    return adapter


class TestRoutingOwnsTheNames:
    def test_module_defines_the_helpers(self):
        for name in (
            "split_session_key_tail",
            "session_key_for_event",
            "session_aliases",
        ):
            assert getattr(routing_module, name).__module__ == "zulip.routing", name

    def test_routing_state_defines_the_moved_methods(self):
        for name in MOVED_METHODS:
            method = getattr(routing_module.RoutingState, name)
            assert method.__module__ == "zulip.routing", name

    def test_routing_never_imports_the_adapter(self):
        imports = [
            line
            for line in (PLUGIN_DIR / "routing.py")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.startswith(("import ", "from "))
        ]
        assert not [line for line in imports if "adapter" in line]

    def test_no_stale_module_level_target_remains_on_the_adapter(self):
        # ``_LEGACY_ZULIP_STREAM_KEY`` was definition-moved to ``routing`` and
        # is not re-exported, so a patch aimed at the old target fails loudly.
        assert not hasattr(adapter_module, "_LEGACY_ZULIP_STREAM_KEY")
        with pytest.raises(AttributeError):
            with patch.object(adapter_module, "_LEGACY_ZULIP_STREAM_KEY"):
                pass

    def test_re_exports_resolve_to_the_owner(self):
        assert (
            adapter_module._split_session_key_tail
            is routing_module.split_session_key_tail
        )
        assert (
            adapter_module._CONVERSATION_ID_RE is routing_module._CONVERSATION_ID_RE
        )


class TestAdapterDelegatesForwardToRoutingState:
    @pytest.mark.parametrize("adapter_name,owner_name,args", DELEGATE_CALLS)
    def test_sync_delegates_forward(self, adapter_name, owner_name, args):
        fake = MagicMock()
        fake._routing = MagicMock()
        sentinel = object()
        getattr(fake._routing, owner_name).return_value = sentinel

        result = getattr(adapter_module.ZulipAdapter, adapter_name)(fake, *args)

        assert result is sentinel, adapter_name
        getattr(fake._routing, owner_name).assert_called_once_with(*args)

    def test_handler_delegate_forwards_message_and_id(self):
        fake = MagicMock()
        fake._routing = MagicMock()
        adapter_module.ZulipAdapter._pre_resolve_conversation(
            fake, {"id": 9}, "9"
        )
        fake._routing.pre_resolve_conversation.assert_called_once_with({"id": 9}, "9")

    def test_apply_topic_deletion_delegate_is_async(self):
        fake = MagicMock()
        fake._routing = MagicMock()
        sentinel = object()
        fake._routing.apply_topic_deletion = AsyncMock(return_value=sentinel)

        coro = adapter_module.ZulipAdapter._apply_topic_deletion(fake, 7, "t")
        assert inspect.iscoroutine(coro)
        assert asyncio.run(coro) is sentinel
        fake._routing.apply_topic_deletion.assert_called_once_with(7, "t")

    def test_session_key_for_event_delegate_reads_the_module_helper(self):
        fake = MagicMock()
        sentinel = object()
        with patch.object(
            routing_module, "session_key_for_event", return_value=sentinel
        ) as helper:
            result = adapter_module.ZulipAdapter._session_key_for_event(fake, "evt")
        assert result is sentinel
        helper.assert_called_once_with(fake, "evt")


class TestStateHasExactlyOneOwner:
    def test_adapter_views_are_the_routing_state_objects(
        self, mock_platform_config, monkeypatch, tmp_path
    ):
        adapter = _plain_adapter(mock_platform_config, monkeypatch, tmp_path)

        assert isinstance(adapter._routing, routing_module.RoutingState)
        assert adapter._conversations is adapter._routing._conversations
        assert (
            adapter._pending_conversations is adapter._routing._pending_conversations
        )
        assert adapter._topic_cache is adapter._routing._topic_cache


class TestHandoffReadsGoThroughTheOwner:
    def test_inbound_reads_the_routing_state(self):
        """A regression to a second read path would fork the session silently."""
        source = (PLUGIN_DIR / "inbound.py").read_text(encoding="utf-8")
        assert "adapter._routing._pending_conversations.pop" in source
        assert "adapter._routing._conversations" in source
        assert "adapter._routing._topic_cache" in source
        # the old adapter-local spellings must be gone from the handler
        assert "adapter._pending_conversations" not in source
        assert "adapter._conversations" not in source
        assert "adapter._topic_cache" not in source

    def test_production_reads_state_through_routing(self):
        """No production module reads the routing state off the adapter root."""
        for name in ("inbound.py", "outbound.py", "tracing.py", "approvals.py"):
            source = (PLUGIN_DIR / name).read_text(encoding="utf-8")
            assert "adapter._pending_conversations" not in source, name
            assert "adapter._conversations" not in source, name
            assert "adapter._topic_cache" not in source, name

    def test_pre_resolve_writes_the_dict_the_handler_pops(
        self, mock_platform_config, monkeypatch, tmp_path
    ):
        """The write↔pop handoff must be the same dict object, keyed the same."""
        adapter = _plain_adapter(mock_platform_config, monkeypatch, tmp_path)

        msg = {
            "id": 31,
            "type": "stream",
            "stream_id": 7,
            "subject": "Deploy XY",
            "content": "hello",
            "sender_email": "user@zulip.com",
        }
        adapter._pre_resolve_conversation(msg, "31")

        stash = adapter._routing._pending_conversations
        assert "31" in stash
        conversation_id = stash["31"]
        # the handler side pops from the owner's dict — same object, same key.
        assert adapter._pending_conversations is stash
        assert adapter._pending_conversations.pop("31", None) == conversation_id
        assert "31" not in stash
