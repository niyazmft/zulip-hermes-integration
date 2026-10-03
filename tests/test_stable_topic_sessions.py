"""Tests for stable topic sessions — rename-proof conversation identity.

Implements the routing rules R1-R10 from the project docs (ISSUE.md):

- R1: unmapped topic mints a new conversation id; label reuse after a
  rename starts a NEW conversation.
- R2: full rename (change_all) re-points the conversation and frees the
  old name.
- R3: partial moves (change_one/change_later) are splits — old name keeps
  its conversation, the new name gets a new one.
- R5: renaming onto a live name displaces the previous mapping.
- R6: outbound sends resolve conversation ids to the CURRENT topic name.
- R7: /continue re-binds a topic to the most recently renamed conversation.
- R8: cross-channel moves free the mapping without mapping a new one.
"""

import asyncio
import contextlib
import shutil
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import zulip.adapter as adapter_module
import zulip.commands as commands_module
import zulip.zulip_client as zulip_client_module
from tests.conftest import MockZulipClient
from zulip.conversations import TopicConversationRegistry


class RecordingTypingClient(MockZulipClient):
    """MockZulipClient that records set_typing_status calls."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._typing_calls = []

    def set_typing_status(self, request):
        self._typing_calls.append(request)
        return {"result": "success"}


@pytest.fixture
def registry(tmp_path):
    reg = TopicConversationRegistry(account_id="bot@test", data_dir=str(tmp_path))
    yield reg
    shutil.rmtree(tmp_path, ignore_errors=True)


def _stream_msg(topic: str, msg_id: int = 1, content: str = "hello") -> dict:
    return {
        "id": msg_id,
        "type": "stream",
        "stream_id": 7,
        "subject": topic,
        "display_recipient": "engineering",
        "content": content,
        "sender_email": "user@zulip.com",
        "sender_full_name": "User",
        "sender_id": 42,
    }


def _rename_event(
    orig_subject: str,
    subject: str,
    propagate_mode: str = "change_all",
    stream_id: int = 7,
    **extra,
) -> dict:
    event = {
        "id": 99,
        "type": "update_message",
        "stream_id": stream_id,
        "orig_subject": orig_subject,
        "subject": subject,
        "propagate_mode": propagate_mode,
        "message_ids": [1, 2],
    }
    event.update(extra)
    return event


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    """ZulipAdapter with topic sessions on (conversation-id keyed, the only
mode) and a recording client."""
    monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
    monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", "true")
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                self._client = RecordingTypingClient(**kwargs)

            def __getattr__(self, name):
                return getattr(self._client, name)

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(mock_platform_config)
    a.email = "bot@zulip.com"
    a.handle_message = AsyncMock()
    return a


class TestRegistryStore:
    """Store-level behavior of TopicConversationRegistry."""

    def test_resolve_mints_and_is_stable(self, registry):
        conv = registry.resolve(7, "deploys", anchor_message_id=100)
        assert conv.startswith("c")
        assert registry.resolve(7, "deploys") == conv
        assert registry.current_name(7, conv) == "deploys"

    def test_resolve_replaces_tombstone_label_reuse(self, registry):
        conv = registry.resolve(7, "deploys")
        registry.free(7, "deploys")
        # Label reuse after a free mints a NEW conversation (R4).
        conv2 = registry.resolve(7, "deploys")
        assert conv2 != conv

    def test_repoint_moves_conversation_and_frees_old_name(self, registry):
        conv = registry.resolve(7, "deploys")
        assert registry.repoint(7, "deploys", "deploys-2") == conv
        assert registry.current_name(7, conv) == "deploys-2"
        assert registry.current_name(7, "deploys") is None
        # The conversation belongs to the beneficiary now; its old-name
        # record is a NULL-membership audit row — a recreated "deploys"
        # cannot adopt it (inheritance model).
        row = registry._conn.execute(
            "SELECT topic_name FROM former_holders"
            " WHERE channel_id=7 AND conversation_id=?",
            (conv,),
        ).fetchone()
        assert row is not None and row[0] is None
        # Label reuse starts a fresh lineage (R4).
        assert registry.resolve(7, "deploys") != conv

    def test_repoint_unmapped_name_is_noop(self, registry):
        assert registry.repoint(7, "ghost", "ghost-2") is None

    def test_repoint_collision_displaces_previous_mapping(self, registry):
        conv_a = registry.resolve(7, "topic-a")
        conv_b = registry.resolve(7, "topic-b")
        # Rename topic-a onto topic-b's live name (R5).
        registry.repoint(7, "topic-a", "topic-b")
        # topic-b now belongs to conv_a; conv_b is displaced — into
        # topic-b's OWN session set, so it can be /continue'd back.
        assert registry.current_name(7, conv_a) == "topic-b"
        assert registry.current_name(7, conv_b) is None
        _cur, _org, members = registry.sessions_for_topic(7, "topic-b")
        assert [m for m, _o in members] == [conv_b]

    def test_displaced_last_topic_is_where_it_last_lived(self, registry):
        # A2: a displaced conversation's last_topic must be the name it
        # was pushed OUT of (the displacement target) — never its birth
        # topic. Probe-D chain: born "Old", renamed to live at
        # "Discuss XY", then displaced by a collision there.
        conv_old = registry.resolve(7, "Old")
        registry.repoint(7, "Old", "Discuss XY")
        assert registry.current_name(7, conv_old) == "Discuss XY"
        registry.resolve(7, "Incoming")
        # Collision: "Incoming" renames onto the live "Discuss XY" —
        # conv_old is displaced (R5).
        registry.repoint(7, "Incoming", "Discuss XY")
        assert registry.current_name(7, conv_old) is None
        assert registry.last_topic_of(7, conv_old) == "Discuss XY"  # NOT "Old"

    def test_rebind_displaced_last_topic_is_target_name(self, registry):
        # A2, rebind writer: a conversation displaced by a /continue must
        # record the name it lived at, not its origin.
        conv_c = registry.resolve(7, "BirthC")
        registry.repoint(7, "BirthC", "TopicC")  # now lives at "TopicC"
        conv_a = registry.resolve(7, "TopicA")
        # /continue conv_a into "TopicC" displaces conv_c (R5).
        registry.rebind(7, "TopicC", conv_a)
        assert registry.current_name(7, conv_c) is None
        assert registry.last_topic_of(7, conv_c) == "TopicC"  # NOT "BirthC"

    def test_rebind_consumes_tombstone(self, registry):
        conv = registry.resolve(7, "old-name")
        registry.free(7, "old-name")
        registry.resolve(7, "fresh-name")
        registry.rebind(7, "fresh-name", conv)
        assert registry.current_name(7, conv) == "fresh-name"
        # The re-bound conversation is live here; "fresh-name" keeps only
        # the conversation it displaced as a former session.
        _cur, _org, members = registry.sessions_for_topic(7, "fresh-name")
        assert conv not in {m for m, _o in members}

    def test_free_orphans_without_mapping(self, registry):
        conv = registry.resolve(7, "deploys")
        assert registry.free(7, "deploys") == conv
        # No beneficiary (cross-channel move): mapping gone, session
        # orphaned — unreachable by /continue from any topic.
        assert registry.current_name(7, conv) is None
        assert registry.lookup(7, "deploys") is None
        row = registry._conn.execute(
            "SELECT topic_name FROM former_holders"
            " WHERE channel_id=7 AND conversation_id=?",
            (conv,),
        ).fetchone()
        assert row is not None and row[0] is None  # orphaned audit row
        assert registry.free(7, "deploys") is None  # idempotent

    def test_persists_across_instances(self, tmp_path):
        reg1 = TopicConversationRegistry(account_id="bot@test", data_dir=str(tmp_path))
        conv = reg1.resolve(7, "deploys")
        reg2 = TopicConversationRegistry(account_id="bot@test", data_dir=str(tmp_path))
        assert reg2.resolve(7, "deploys") == conv

    def test_accounts_are_isolated(self, tmp_path):
        reg_a = TopicConversationRegistry(account_id="a@test", data_dir=str(tmp_path))
        reg_b = TopicConversationRegistry(account_id="b@test", data_dir=str(tmp_path))
        conv_a = reg_a.resolve(7, "deploys")
        assert reg_b.resolve(7, "deploys") != conv_a


    def test_continue_candidates_are_topic_scoped(self, registry):
        conv_a = registry.resolve(7, "TopicA")
        conv_b = registry.resolve(7, "TopicB")
        registry.repoint(7, "TopicA", "TopicB")  # R5: conv_a takes "TopicB"
        # topic-b's own set: ONLY the displaced conv_b. The renaming
        # conversation's old-name record is a NULL audit row — never a
        # candidate (channel-wide selection is gone).
        _cur, _org, members = registry.sessions_for_topic(7, "TopicB")
        assert [m for m, _o in members] == [conv_b]
        # An unrelated fresh topic has an empty set — nothing to steal.
        registry.resolve(7, "TopicZ")
        _cur_z, _org_z, members_z = registry.sessions_for_topic(7, "TopicZ")
        assert members_z == []


    def test_multi_merge_chain_keeps_every_tombstone(self, registry):
        # Multi-merge case: "Fix XY" and "Deploy XY" are merged into the
        # live "Discuss about XY" (two sequential change_all renames).
        conv_d = registry.resolve(7, "Discuss about XY")
        conv_f = registry.resolve(7, "Fix XY")
        conv_p = registry.resolve(7, "Deploy XY")
        registry.repoint(7, "Fix XY", "Discuss about XY")     # R5: conv_d displaced
        registry.repoint(7, "Deploy XY", "Discuss about XY")  # R5: conv_f displaced
        assert registry.current_name(7, conv_p) == "Discuss about XY"
        assert registry.current_name(7, conv_d) is None
        assert registry.current_name(7, conv_f) is None
        # The merged topic's own set: the two displaced sessions, most
        # recent first. The winner's old-name record is a NULL audit row —
        # invisible to every /continue (no beneficiary).
        _cur, _org, members = registry.sessions_for_topic(7, "Discuss about XY")
        assert [m for m, _o in members] == [conv_f, conv_d]
        rows = registry._conn.execute(
            "SELECT conversation_id FROM former_holders WHERE channel_id=7"
        ).fetchall()
        assert {str(r[0]) for r in rows} == {conv_d, conv_f, conv_p}


    def test_conversation_links_to_at_most_one_topic(self, registry):
        # Relation model: topic 1—N sessions over time, but a
        # session belongs to exactly ONE topic at any instant.
        import sqlite3
        conv = registry.resolve(7, "TopicA")
        with pytest.raises(sqlite3.IntegrityError):
            # Same conversation linked to a second topic name → rejected.
            registry._conn.execute(
                "INSERT INTO live_holders"
                " (account_id, channel_id, topic_name, conversation_id,"
                "  anchor_message_id, updated_at) VALUES (?,?,?,?,NULL,?)",
                (registry.account_id, 7, "TopicB", conv, 1.0),
            )
        # Different channels are independent topics (same name, own sessions).
        other_channel = registry.resolve(9, "TopicA")
        assert other_channel != conv


    def test_rename_carries_tombstone_membership(self, registry):
        # R2: the topic's former sessions move along with the rename
        # (membership tracked by tombstone topic_name; origin unchanged).
        conv_a = registry.resolve(7, "TopicA")
        conv_b = registry.resolve(7, "TopicB")
        registry.repoint(7, "TopicA", "TopicB")
        current, origin, members = registry.sessions_for_topic(7, "TopicB")
        assert (current, origin) == (conv_a, "TopicA")
        assert members == [(conv_b, "TopicB")]
        registry.repoint(7, "TopicB", "TopicC")
        current, origin, members = registry.sessions_for_topic(7, "TopicC")
        assert (current, origin) == (conv_a, "TopicA")
        assert members == [(conv_b, "TopicB")]  # carried A -> B -> C

    def test_rebind_moves_session_between_topics(self, registry):
        # The session belongs to exactly one topic: re-binding it to a
        # different topic moves it, and the old topic keeps it as a
        # tombstoned former session.
        conv = registry.resolve(7, "TopicA")
        registry.rebind(7, "TopicB", conv)
        assert registry.lookup(7, "TopicB") == conv
        assert registry.lookup(7, "TopicA") is None
        current, origin, members = registry.sessions_for_topic(7, "TopicA")
        assert current is None and origin is None
        assert members == [(conv, "TopicA")]


class TestInboundSessionIdentity:
    """Inbound messages key sessions on the conversation id (R1)."""

    @pytest.mark.asyncio
    async def test_session_key_is_conversation_id(self, adapter):
        await adapter._handle_message(_stream_msg("deploys"))
        source = adapter.handle_message.call_args[0][0].source
        assert source.thread_id.startswith("c")

    @pytest.mark.asyncio
    async def test_same_topic_same_conversation(self, adapter):
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        first = adapter.handle_message.call_args[0][0].source.thread_id
        await adapter._handle_message(_stream_msg("deploys", msg_id=2))
        second = adapter.handle_message.call_args[0][0].source.thread_id
        assert first == second

    @pytest.mark.asyncio
    async def test_label_reuse_after_rename_gets_new_conversation(self, adapter):
        # R1/R2/R4: rename frees the old name; reusing it mints a new one.
        await adapter._handle_message(_stream_msg("Discussion about XY", msg_id=1))
        original = adapter.handle_message.call_args[0][0].source.thread_id
        adapter._handle_topic_update(
            _rename_event("Discussion about XY", "Fix XY")
        )
        await adapter._handle_message(_stream_msg("Fix XY", msg_id=3))
        continued = adapter.handle_message.call_args[0][0].source.thread_id
        assert continued == original  # rename continues the session

        adapter._handle_topic_update(_rename_event("Fix XY", "Fix XY v2"))
        await adapter._handle_message(_stream_msg("Discussion about XY", msg_id=4))
        reused = adapter.handle_message.call_args[0][0].source.thread_id
        assert reused != original  # old label reuse = new conversation


class TestRenameEvents:
    """update_message event handling (R2/R3/R5/R8)."""

    @pytest.mark.asyncio
    async def test_change_all_repoints(self, adapter):
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter._handle_topic_update(_rename_event("deploys", "deploys-2"))
        assert adapter._conversations.current_name(7, conv) == "deploys-2"

    @pytest.mark.asyncio
    async def test_partial_move_is_a_split(self, adapter):
        # R3: change_one/change_later do NOT re-point.
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter._handle_topic_update(
            _rename_event("deploys", "split-out", propagate_mode="change_one")
        )
        assert adapter._conversations.current_name(7, conv) == "deploys"

    @pytest.mark.asyncio
    async def test_content_edit_ignored(self, adapter):
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter._handle_topic_update(
            _rename_event("deploys", "deploys", propagate_mode="")
        )
        assert adapter._conversations.current_name(7, conv) == "deploys"

    @pytest.mark.asyncio
    async def test_cross_channel_partial_move_keeps_source_session(self, adapter):
        # A PARTIAL move to another channel (change_one) leaves
        # the source topic alive with its remaining messages — it must keep
        # its session. No free, no orphaning.
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        adapter._handle_topic_update(
            _rename_event(
                "deploys", "deploys",
                propagate_mode="change_one", stream_id=7, new_stream_id=9,
            )
        )
        # The source topic still holds its session...
        assert adapter._conversations.lookup(7, "deploys") == conv
        # ...its former set is untouched, and nothing was orphaned.
        _cur, _org, members = adapter._conversations.sessions_for_topic(7, "deploys")
        assert members == []
        assert adapter._conversations.current_name(7, conv) == "deploys"
        # The next message continues the SAME conversation.
        await adapter._handle_message(_stream_msg("deploys", msg_id=2))
        assert adapter.handle_message.call_args[0][0].source.thread_id == conv

    @pytest.mark.asyncio
    async def test_cross_channel_change_later_keeps_source_session(self, adapter):
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        adapter._handle_topic_update(
            _rename_event(
                "deploys", "deploys",
                propagate_mode="change_later", stream_id=7, new_stream_id=9,
            )
        )
        assert adapter._conversations.lookup(7, "deploys") == conv

    @pytest.mark.asyncio
    async def test_cross_channel_move_frees(self, adapter):
        # R8 (full move): free + orphan the mapping, map nothing in the
        # new channel — the moved thread starts a fresh conversation there.
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter._handle_topic_update(
            _rename_event(
                "deploys", "deploys", stream_id=7, new_stream_id=9
            )
        )
        assert adapter._conversations.current_name(7, conv) is None
        assert adapter._conversations.lookup(7, "deploys") is None
    @pytest.mark.asyncio
    async def test_malformed_events_never_raise(self, adapter):
        adapter._handle_topic_update({"type": "update_message"})  # no fields
        adapter._handle_topic_update(
            _rename_event("deploys", "x", stream_id="not-an-int")
        )

    @pytest.mark.asyncio
    async def test_rename_onto_live_name_displaces(self, adapter):
        # R5: renaming topic-a onto topic-b's live name displaces topic-b.
        await adapter._handle_message(_stream_msg("topic-a", msg_id=1))
        conv_a = adapter.handle_message.call_args[0][0].source.thread_id
        await adapter._handle_message(_stream_msg("topic-b", msg_id=2))
        conv_b = adapter.handle_message.call_args[0][0].source.thread_id
        adapter._handle_topic_update(_rename_event("topic-a", "topic-b"))
        assert adapter._conversations.current_name(7, conv_a) == "topic-b"
        assert adapter._conversations.current_name(7, conv_b) is None


class TestOutboundRouting:
    """Outbound sends resolve conversation ids to the current name (R6)."""

    @pytest.mark.asyncio
    async def test_reply_after_rename_lands_in_new_topic(self, adapter):
        await adapter._handle_message(_stream_msg("TopicA", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter._handle_topic_update(_rename_event("TopicA", "TopicB"))
        # In-flight reply generated under the old name:
        result = await adapter.send("7", "Task done", metadata={"thread_id": conv})
        assert result.success is True
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "TopicB"  # new name, no ghost TopicA

    @pytest.mark.asyncio
    async def test_reply_to_orphaned_conversation_lands_on_last_topic(self, adapter):
        # R10 while a reply is in flight: the topic is deleted (orphaning
        # the conversation) and THEN the reply sends. It must land on the
        # conversation's LAST known topic name, not a ghost topic named
        # like the id.
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        # Genuinely orphan the conversation: the topic is gone from the
        # channel's list, and the handler runs the whole R10 chain. The
        # assertion below then exercises the last-known-name fallback.
        adapter.client._client.stream_topics[7] = []
        await adapter._handle_message_delete_event(
            _delete_event(stream_id=7, topic="Deploy XY", message_ids=[1])
        )
        assert adapter._conversations.current_name(7, conv) is None  # orphaned
        result = await adapter.send("7", "late reply",
                                    metadata={"thread_id": conv})
        assert result.success is True
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "Deploy XY"  # last name, not the conv id

    @pytest.mark.asyncio
    async def test_reply_after_rename_then_delete_lands_on_last_topic(self, adapter):
        # The user's ruling case: renamed ("Deploy XY" -> "Discussion XY"),
        # then the beneficiary topic is deleted while a reply is in flight.
        # Last known name = "Discussion XY" (NOT the origin "Deploy XY").
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter._handle_topic_update(_rename_event("Deploy XY", "Discussion XY"))
        adapter.client._client.stream_topics[7] = []
        await adapter._handle_message_delete_event(
            _delete_event(stream_id=7, topic="Discussion XY", message_ids=[1])
        )
        assert adapter._conversations.current_name(7, conv) is None  # orphaned
        result = await adapter.send("7", "late reply",
                                    metadata={"thread_id": conv})
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "Discussion XY"  # LAST name, not origin

    @pytest.mark.asyncio
    async def test_reply_to_displaced_conversation_lands_where_it_lived(self, adapter):
        # A2, user-visible: a conversation displaced by an R5 collision
        # keeps last_topic = the name it was pushed out of, so an in-flight
        # reply lands THERE — never on its birth topic.
        await adapter._handle_message(_stream_msg("Old", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter._handle_topic_update(_rename_event("Old", "Discuss XY"))
        assert adapter._conversations.current_name(7, conv) == "Discuss XY"
        # Collision: "Incoming" renames onto the live "Discuss XY".
        await adapter._handle_message(_stream_msg("Incoming", msg_id=2))
        adapter.handle_message.reset_mock()
        adapter._handle_topic_update(_rename_event("Incoming", "Discuss XY"))
        assert adapter._conversations.current_name(7, conv) is None  # displaced
        await adapter.send("7", "late reply", metadata={"thread_id": conv})
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "Discuss XY"  # last lived, NOT "Old"

    @pytest.mark.asyncio
    async def test_unknown_conv_id_without_record_stays_raw(self, adapter):
        # Ghost topic only remains for ids the registry has NO record of
        # (e.g. manual registry wipe): raw passthrough (documented).
        await adapter.send("7", "wipe case",
                           metadata={"thread_id": "caaaaaaaaaaaa1"})
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "caaaaaaaaaaaa1"

    @pytest.mark.asyncio
    async def test_id_shaped_legacy_name_not_rerouted(self, adapter):
        # A legacy name-keyed session for a topic literally named like an
        # id must pass through verbatim — the origin/last-name lookup
        # finds no row, so the name survives unchanged.
        await adapter.send("7", "legacy id-shaped",
                           metadata={"thread_id": "c4f2a9b1c3d5e"})
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "c4f2a9b1c3d5e"

    @pytest.mark.asyncio
    async def test_typing_lands_on_last_topic_after_orphan(self, adapter):
        # Typing hooks share the same resolution path: start-typing for an
        # orphaned conversation targets the last known topic.
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.client._client.stream_topics[7] = []
        await adapter._handle_message_delete_event(
            _delete_event(stream_id=7, topic="Deploy XY", message_ids=[1])
        )
        assert adapter._conversations.current_name(7, conv) is None  # orphaned
        await adapter.send_typing("7", metadata={"thread_id": conv})
        call = adapter.client._client._typing_calls[-1]
        assert call["topic"] == "Deploy XY"

    @pytest.mark.asyncio
    async def test_unknown_thread_id_used_verbatim(self, adapter):
        # Legacy name-keyed sessions (or stable mode off) keep working.
        await adapter.send("7", "legacy", metadata={"thread_id": "Legacy Topic"})
        call = adapter.client._client._sent_messages[0]
        assert call["topic"] == "Legacy Topic"

    @pytest.mark.asyncio
    async def test_typing_resolves_conversation_id(self, adapter):
        await adapter._handle_message(_stream_msg("TopicA", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter._handle_topic_update(_rename_event("TopicA", "TopicB"))
        await adapter.send_typing("7", metadata={"thread_id": conv})
        call = adapter.client._client._typing_calls[-1]
        assert call["topic"] == "TopicB"


class TestContinueCommand:
    """``/continue <session-id>`` re-binds a topic to a session from its
    OWN set (created there or inherited by full rename/merge). Bare
    ``/continue`` does nothing; foreign and orphaned sessions are
    unreachable."""

    @pytest.mark.asyncio
    async def test_continue_without_argument_does_nothing(self, adapter):
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        await adapter._handle_message(
            _stream_msg("deploys", msg_id=2, content="/continue")
        )
        cmd_call = adapter.client._client._sent_messages[0]
        assert "Usage:" in cmd_call["content"]
        assert "/continue <session-id>" in cmd_call["content"]
        # Registry untouched: the topic still holds its own session.
        assert adapter._conversations.lookup(7, "deploys") == conv

    @pytest.mark.asyncio
    async def test_recreated_name_cannot_adopt_renamed_away_session(self, adapter):
        # Inheritance model: after old-name -> new-name, the session
        # belongs to "new-name". A recreated "old-name" is a new lineage —
        # its /continue must NOT reach the original session.
        await adapter._handle_message(_stream_msg("old-name", msg_id=1))
        original = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        adapter._handle_topic_update(_rename_event("old-name", "new-name"))
        await adapter._handle_message(_stream_msg("old-name", msg_id=3))
        fresh = adapter.handle_message.call_args[0][0].source.thread_id
        assert fresh != original
        adapter.handle_message.reset_mock()
        store = _wire_sessions(adapter, {
            original: (None, ["20260928_163916_454e3786"]),
        })
        await adapter._handle_message(
            _stream_msg("old-name", msg_id=4,
                        content="/continue 20260928_163916_454e3786")
        )
        cmd_call = adapter.client._client._sent_messages[0]
        assert "not one of this topic's sessions" in cmd_call["content"]
        # And the session is still held by "new-name".
        assert adapter._conversations.lookup(7, "new-name") == original
        assert store.switch_calls == []

    @pytest.mark.asyncio
    async def test_continue_foreign_session_rejected(self, adapter):
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        # A session held by ANOTHER topic is not continuable here.
        await adapter._handle_message(_stream_msg("other", msg_id=2))
        other = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        store = _wire_sessions(adapter, {
            other: ("20260928_180039_ac06c9b7",
                    ["20260928_180039_ac06c9b7"]),
        })
        await adapter._handle_message(
            _stream_msg("deploys", msg_id=3,
                        content="/continue 20260928_180039_ac06c9b7")
        )
        cmd_call = adapter.client._client._sent_messages[0]
        assert "not one of this topic's sessions" in cmd_call["content"]
        # Nothing changed.
        assert adapter._conversations.lookup(7, "deploys") == conv
        assert store.switch_calls == []

    @pytest.mark.asyncio
    async def test_continue_malformed_id_rejected(self, adapter):
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        adapter.handle_message.reset_mock()
        await adapter._handle_message(
            _stream_msg("deploys", msg_id=2, content="/continue not-an-id")
        )
        cmd_call = adapter.client._client._sent_messages[0]
        assert "not a session id shown by" in cmd_call["content"]
        assert adapter._conversations.lookup(7, "deploys") is not None

    @pytest.mark.asyncio
    async def test_continue_current_session_is_noop(self, adapter):
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        _wire_sessions(adapter, {
            conv: ("20260930_131553_13d2b944",
                   ["20260930_131553_13d2b944"]),
        })
        await adapter._handle_message(
            _stream_msg("deploys", msg_id=2,
                        content="/continue 20260930_131553_13d2b944")
        )
        cmd_call = adapter.client._client._sent_messages[0]
        assert "Already talking in session" in cmd_call["content"]

    @pytest.mark.asyncio
    async def test_continue_repairs_displaced_conversation_after_collision(self, adapter):
        await adapter._handle_message(_stream_msg("TopicA", msg_id=1))
        conv_a = adapter.handle_message.call_args[0][0].source.thread_id
        await adapter._handle_message(_stream_msg("TopicB", msg_id=2))
        conv_b = adapter.handle_message.call_args[0][0].source.thread_id
        # R5 collision: rename TopicA onto the live TopicB name.
        adapter._handle_topic_update(_rename_event("TopicA", "TopicB"))
        # /continue <b's live gateway session id> restores TopicB's own
        # session (rebind + switch in one command).
        _wire_sessions(adapter, {
            conv_b: ("20260928_180207_789321ca",
                     ["20260928_180207_789321ca"]),
            conv_a: ("20260928_183850_c08faa57",
                     ["20260928_183850_c08faa57"]),
        })
        await adapter._handle_message(
            _stream_msg("TopicB", msg_id=3,
                        content="/continue 20260928_180207_789321ca")
        )
        cmd_call = adapter.client._client._sent_messages[0]
        assert "20260928_180207_789321ca" in cmd_call["content"]
        assert 'started in **TopicB**' in cmd_call["content"]
        await adapter._handle_message(_stream_msg("TopicB", msg_id=4))
        rebound = adapter.handle_message.call_args[0][0].source.thread_id
        assert rebound == conv_b  # TopicB's old session restored
        # Toggle back to the renaming conversation by its session id.
        await adapter._handle_message(
            _stream_msg("TopicB", msg_id=5,
                        content="/continue 20260928_183850_c08faa57")
        )
        await adapter._handle_message(_stream_msg("TopicB", msg_id=6))
        toggled = adapter.handle_message.call_args[0][0].source.thread_id
        assert toggled == conv_a

    @pytest.mark.asyncio
    async def test_multi_merge_then_continue_reaches_recent_conversations(self, adapter):
        convs = {}
        for i, name in enumerate(
            ["Discuss about XY", "Fix XY", "Deploy XY"], start=1
        ):
            await adapter._handle_message(_stream_msg(name, msg_id=i))
            convs[name] = adapter.handle_message.call_args[0][0].source.thread_id
        conv_d, conv_f, conv_p = (
            convs[n] for n in ["Discuss about XY", "Fix XY", "Deploy XY"]
        )
        adapter._handle_topic_update(_rename_event("Fix XY", "Discuss about XY"))
        adapter._handle_topic_update(_rename_event("Deploy XY", "Discuss about XY"))
        # Last renamer wins the name: the live session is Deploy XY's.
        await adapter._handle_message(_stream_msg("Discuss about XY", msg_id=9))
        assert adapter.handle_message.call_args[0][0].source.thread_id == conv_p
        # /continue <gateway-session-id> cycles through the topic's own
        # set: the inherited Fix XY session...
        _wire_sessions(adapter, {
            conv_f: ("20260928_180039_ac06c9b7",
                     ["20260928_180039_ac06c9b7"]),
            conv_d: ("20260928_183850_c08faa57",
                     ["20260928_183850_c08faa57"]),
            conv_p: ("20260930_131224_031f7ae1",
                     ["20260930_131224_031f7ae1"]),
        })
        await adapter._handle_message(
            _stream_msg("Discuss about XY", msg_id=10,
                        content="/continue 20260928_180039_ac06c9b7")
        )
        await adapter._handle_message(_stream_msg("Discuss about XY", msg_id=11))
        assert adapter.handle_message.call_args[0][0].source.thread_id == conv_f
        # ...the original Discuss about XY session...
        await adapter._handle_message(
            _stream_msg("Discuss about XY", msg_id=12,
                        content="/continue 20260928_183850_c08faa57")
        )
        await adapter._handle_message(_stream_msg("Discuss about XY", msg_id=13))
        assert adapter.handle_message.call_args[0][0].source.thread_id == conv_d
        # ...and back to the Deploy XY session.
        await adapter._handle_message(
            _stream_msg("Discuss about XY", msg_id=14,
                        content="/continue 20260930_131224_031f7ae1")
        )
        await adapter._handle_message(_stream_msg("Discuss about XY", msg_id=15))
        assert adapter.handle_message.call_args[0][0].source.thread_id == conv_p

    @pytest.mark.asyncio
    async def test_sessions_lists_topic_sessions(self, adapter):
        for i, name in enumerate(
            ["Discuss about XY", "Fix XY", "Deploy XY"], start=1
        ):
            await adapter._handle_message(_stream_msg(name, msg_id=i))
        adapter._handle_topic_update(_rename_event("Fix XY", "Discuss about XY"))
        adapter._handle_topic_update(_rename_event("Deploy XY", "Discuss about XY"))
        await adapter._handle_message(
            _stream_msg("Discuss about XY", msg_id=9, content="/topic-sessions")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        assert "Sessions in this topic: 3" in reply
        assert 'started in "Deploy XY"' in reply
        assert 'started in "Fix XY"' in reply
        assert 'started in "Discuss about XY"' in reply
        assert "(current)" in reply
        assert "/continue" in reply

    @pytest.mark.asyncio
    async def test_sessions_listing_shows_no_lineage_ids(self, adapter):
        """User ruling: the listing never shows conversation (lineage) ids —
        only gateway session ids, which are the switch tokens."""
        for i, name in enumerate(
            ["Discuss about XY", "Fix XY", "Deploy XY"], start=1
        ):
            await adapter._handle_message(_stream_msg(name, msg_id=i))
        adapter._handle_topic_update(_rename_event("Fix XY", "Discuss about XY"))
        adapter._handle_topic_update(_rename_event("Deploy XY", "Discuss about XY"))
        await adapter._handle_message(
            _stream_msg("Discuss about XY", msg_id=9, content="/topic-sessions")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        # No c+12hex token anywhere.
        assert not re.search(r"`c[0-9a-f]{12}`", reply)

    @pytest.mark.asyncio
    async def test_sessions_listing_lists_all_generations(self, adapter):
        """The listing shows EVERY gateway session in the topic's set,
        grouped by lineage (current first, newest first within each),
        numbered, with (current)/(last) markers — and no lineage ids."""
        for i, name in enumerate(
            ["Discuss about XY", "Fix XY", "Deploy XY"], start=1
        ):
            await adapter._handle_message(_stream_msg(name, msg_id=i))
        conv_d = adapter.handle_message.call_args_list[0][0][0].source.thread_id
        conv_f = adapter.handle_message.call_args_list[1][0][0].source.thread_id
        conv_p = adapter.handle_message.call_args_list[2][0][0].source.thread_id
        adapter._handle_topic_update(_rename_event("Fix XY", "Discuss about XY"))
        adapter._handle_topic_update(_rename_event("Deploy XY", "Discuss about XY"))
        # After both merges the current lineage is Deploy XY's (last renamer).

        entries = {}
        gens = {}
        for conv, session_ids in {
            conv_p: ["20260930_131224_031f7ae1", "20260928_180039_ac06c9b7"],
            conv_f: ["20260930_131553_13d2b944", "20260928_180207_789321ca"],
            conv_d: ["20260928_183850_c08faa57"],
        }.items():
            key = f"agent:main:zulip:stream:7:{conv}"
            entries[key] = _FakeRouteEntry(key, session_ids[0])
            gens[key] = [{"id": sid} for sid in session_ids]
        store = _FakeSessionStore(
            entries=entries, db=_FakeSessionDB(sessions_by_key=gens)
        )
        adapter.set_session_store(store)

        await adapter._handle_message(
            _stream_msg("Discuss about XY", msg_id=9, content="/topic-sessions")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        assert "📋 Sessions in this topic: 5" in reply
        # Current lineage's live generation: line 1, (current).
        assert '1) `20260930_131224_031f7ae1` **(current)** — started in "Deploy XY"' in reply
        # Its older generation: unmarked.
        assert '2) `20260928_180039_ac06c9b7` — started in "Deploy XY"' in reply
        # Member lineage's newest generation: (last).
        # Member lines (registry row order is not pinned — content is):
        assert '`20260930_131553_13d2b944` **(last)** — started in "Fix XY"' in reply
        assert '`20260928_180207_789321ca` — started in "Fix XY"' in reply
        assert '`20260928_183850_c08faa57` **(last)** — started in "Discuss about XY"' in reply
        # No lineage ids anywhere; footer points at /continue.
        assert not re.search(r"`c[0-9a-f]{12}`", reply)
        assert "Use `/continue <session-id>` to switch to another session." in reply

    @pytest.mark.asyncio
    async def test_sessions_listing_degrades_without_store(self, adapter):
        """No session store wired: the listing still renders (ids + origins),
        without live-session annotations."""
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=1))
        await adapter._handle_message(
            _stream_msg("Deploy XY", msg_id=2, content="/topic-sessions")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        assert "(current)" in reply
        assert "live session" not in reply

    @pytest.mark.asyncio
    async def test_degraded_listing_numbers_every_line(self, adapter):
        """Degraded mode (no store) numbers all lines uniformly — the
        current line and every member line — matching the normal listing."""
        for i, name in enumerate(
            ["Discuss about XY", "Fix XY", "Deploy XY"], start=1
        ):
            await adapter._handle_message(_stream_msg(name, msg_id=i))
        adapter._handle_topic_update(_rename_event("Fix XY", "Discuss about XY"))
        adapter._handle_topic_update(_rename_event("Deploy XY", "Discuss about XY"))
        await adapter._handle_message(
            _stream_msg("Discuss about XY", msg_id=9, content="/topic-sessions")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        assert "📋 Sessions in this topic: 3" in reply
        assert '1) **(current)** — started in "Deploy XY"' in reply
        # Every member line is numbered too (no unnumbered lines).
        assert '2) — started in "Fix XY"' in reply
        assert '3) — started in "Discuss about XY"' in reply
        assert not any(
            line.startswith("— started in")
            for line in reply.split("\n")
        )

    @pytest.mark.asyncio
    async def test_continue_rejects_lineage_id(self, adapter):
        """User ruling: /continue accepts ONLY gateway session ids — a
        conversation (lineage) id is a listing label, not a switch token."""
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        store = _wire_sessions(adapter, {
            conv: ("20260930_131553_13d2b944",
                   ["20260930_131553_13d2b944"]),
        })
        await adapter._handle_message(
            _stream_msg("deploys", msg_id=2, content=f"/continue {conv}")
        )
        cmd_call = adapter.client._client._sent_messages[0]
        assert "not a session id shown by" in cmd_call["content"]
        # Nothing was switched or rebound.
        assert store.switch_calls == []
        assert adapter._conversations.lookup(7, "deploys") == conv

    @pytest.mark.asyncio
    async def test_continue_gateway_session_id_switches_in_one_command(
        self, adapter
    ):
        """P2: /continue <gateway-session-id> verifies inheritance, rebinds
        and switches the gateway session in one command — even for a PAST
        generation that is not the lineage's live one."""
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=1))
        conv_deploy = adapter._conversations.lookup(7, "Deploy XY")
        await adapter._handle_message(_stream_msg("Fix XY", msg_id=2))
        conv_fix = adapter._conversations.lookup(7, "Fix XY")
        adapter._handle_topic_update(_rename_event("Fix XY", "Deploy XY"))
        # current is now Fix XY's conv; Deploy XY's conv is a former member.

        # The TARGET is a PAST generation of the DISPLACED (Deploy XY)
        # lineage — not its live one — so both the rebind and the switch
        # are genuinely exercised.
        dead_gen = "20260928_180207_789321ca"
        deploy_key = f"agent:main:zulip:stream:7:{conv_deploy}"
        store = _FakeSessionStore(
            entries={
                deploy_key: _FakeRouteEntry(
                    deploy_key, "20260930_131553_13d2b944"),
            },
            db=_FakeSessionDB(rows_by_id={
                dead_gen: {"session_id": dead_gen, "session_key": deploy_key},
            }),
        )
        adapter.set_session_store(store)

        await adapter._handle_message(
            _stream_msg("Deploy XY", msg_id=3,
                        content=f"/continue {dead_gen}")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        # the topic was rebound to the Deploy XY lineage (a former member)
        assert adapter._conversations.lookup(7, "Deploy XY") == conv_deploy
        # and the gateway session was switched to the requested generation
        assert (deploy_key, dead_gen, "20260930_131553_13d2b944") in store.switch_calls
        assert f"talks in session `{dead_gen}`" in reply
        assert 'started in **Deploy XY**' in reply

    @pytest.mark.asyncio
    async def test_continue_gateway_session_id_rejects_foreign_lineage(
        self, adapter
    ):
        """R7 law holds for session ids: a session whose conversation is not
        in this topic's set is never switchable."""
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=1))
        await adapter._handle_message(_stream_msg("Other Topic", msg_id=2))
        foreign_conv = adapter._conversations.lookup(7, "Other Topic")
        foreign_key = f"agent:main:zulip:stream:7:{foreign_conv}"
        store = _FakeSessionStore(
            entries={foreign_key: _FakeRouteEntry(foreign_key, "20260928_180039_ac06c9b7")},
            db=_FakeSessionDB(rows_by_id={
                "20260928_180039_ac06c9b7": {
                    "session_id": "20260928_180039_ac06c9b7",
                    "session_key": foreign_key},
            }),
        )
        adapter.set_session_store(store)
        await adapter._handle_message(
            _stream_msg("Deploy XY", msg_id=3,
                        content="/continue 20260928_180039_ac06c9b7")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        assert "not one of this topic's sessions" in reply
        assert store.switch_calls == []
        # binding untouched
        assert adapter._conversations.lookup(7, "Deploy XY") != foreign_conv

    @pytest.mark.asyncio
    async def test_continue_unknown_gateway_session_id_changes_nothing(
        self, adapter
    ):
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=1))
        store = _FakeSessionStore(entries={}, db=_FakeSessionDB())
        adapter.set_session_store(store)
        await adapter._handle_message(
            _stream_msg("Deploy XY", msg_id=2,
                        content="/continue 20260928_180207_789321ca")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        assert "No session" in reply
        assert "use a session id shown by" in reply
        assert store.switch_calls == []
        assert adapter._conversations.lookup(7, "Deploy XY") is not None

    @pytest.mark.asyncio
    async def test_continue_live_gateway_session_id_via_routing_index(
        self, adapter
    ):
        """A LIVE generation resolves through the public routing index even
        when the state-db seam is unavailable."""
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=1))
        conv_deploy = adapter._conversations.lookup(7, "Deploy XY")
        await adapter._handle_message(_stream_msg("Fix XY", msg_id=2))
        conv_fix = adapter._conversations.lookup(7, "Fix XY")
        adapter._handle_topic_update(_rename_event("Fix XY", "Deploy XY"))
        # Target: the DISPLACED (Deploy XY) lineage's LIVE generation, with
        # the state-db seam unavailable — resolution must come from the
        # public routing index alone.
        deploy_key = f"agent:main:zulip:stream:7:{conv_deploy}"
        store = _FakeSessionStore(
            entries={deploy_key: _FakeRouteEntry(
                deploy_key, "20260930_131553_13d2b944")},
        )
        adapter.set_session_store(store)
        await adapter._handle_message(
            _stream_msg("Deploy XY", msg_id=3,
                        content="/continue 20260930_131553_13d2b944")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        assert adapter._conversations.lookup(7, "Deploy XY") == conv_deploy
        assert (deploy_key, "20260930_131553_13d2b944",
                "20260930_131553_13d2b944") in store.switch_calls
        assert 'started in **Deploy XY**' in reply

    @pytest.mark.asyncio
    async def test_continue_current_live_generation_is_noop(self, adapter):
        """/continue <the current lineage's live session id> says so and
        switches nothing."""
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=1))
        conv = adapter._conversations.lookup(7, "Deploy XY")
        key = f"agent:main:zulip:stream:7:{conv}"
        store = _FakeSessionStore(
            entries={key: _FakeRouteEntry(key, "20260930_131224_031f7ae1")},
        )
        adapter.set_session_store(store)
        await adapter._handle_message(
            _stream_msg("Deploy XY", msg_id=2,
                        content="/continue 20260930_131224_031f7ae1")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        assert "Already talking in session" in reply
        assert store.switch_calls == []
        assert adapter._conversations.lookup(7, "Deploy XY") == conv

    @pytest.mark.asyncio
    async def test_sessions_passthrough_when_disabled(
        self, mock_platform_config, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", "false")
        monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = RecordingTypingClient(**kwargs)

                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
        core = MagicMock(return_value=MagicMock(handled=True, reply="core-listing"))
        monkeypatch.setattr(commands_module, "handle_command", core)
        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)
        adapter.email = "bot@zulip.com"
        adapter.handle_message = AsyncMock()

        await adapter._handle_message(
            _stream_msg("TopicA", msg_id=1, content="/topic-sessions")
        )
        # Topic sessions off: the plugin does not own /topic-sessions —
        # the core handler (mocked) receives it.
        sent = adapter.client._client._sent_messages
        assert not any("Sessions in this topic" in str(m) for m in sent)
        assert core.call_args[1]["content"] == "/topic-sessions"

    @pytest.mark.asyncio
    async def test_core_sessions_passthrough_when_enabled(self, adapter, monkeypatch):
        # Disambiguation guarantee: with topic sessions ON, the plugin owns
        # /topic-sessions and the core gateway keeps /sessions — typing the
        # core command reaches the core handler untouched.
        core = MagicMock(return_value=MagicMock(handled=True, reply="core-listing"))
        monkeypatch.setattr(commands_module, "handle_command", core)
        await adapter._handle_message(
            _stream_msg("Discuss about XY", msg_id=9, content="/sessions")
        )
        sent = adapter.client._client._sent_messages
        assert not any("Sessions in this topic" in str(m) for m in sent)
        assert core.call_args[1]["content"] == "/sessions"

@dataclass
class _FakeRouteEntry:
    """Minimal stand-in for the store's SessionEntry (a dataclass)."""
    session_key: str
    session_id: str


class _FakeSessionStore:
    """Minimal SessionStore surface the migration relies on:
    ``_entries`` dict, ``_lock``, ``_save()`` (same shape the store's own
    ``rekey_profile_routing`` uses)."""

    def __init__(self, entries=None, db=None):
        self._entries = dict(entries or {})
        self._lock = threading.Lock()
        self.save_calls = 0
        self.switch_calls = []
        self._db = db

    def _save(self):
        self.save_calls += 1

    # --- surface used by /topic-sessions (P1) and /continue (P2) ---
    def list_sessions(self, active_minutes=None):
        return list(self._entries.values())

    def peek_session_id(self, session_key):
        entry = self._entries.get(session_key)
        return entry.session_id if entry else None

    def lookup_by_session_id(self, session_id):
        for entry in self._entries.values():
            if entry.session_id == session_id:
                return entry
        return None

    def switch_session(self, session_key, target_session_id, *, expected_session_id=None):
        self.switch_calls.append((session_key, target_session_id, expected_session_id))
        entry = self._entries.get(session_key)
        if entry is None:
            return None
        if expected_session_id is not None and entry.session_id != expected_session_id:
            return None
        entry.session_id = target_session_id
        return entry


class _FakeSessionDB:
    """Stand-in for the state-db handle (SessionStore._db seam)."""

    def __init__(self, sessions_by_key=None, rows_by_id=None):
        # sessions_by_key: key -> list of row dicts (generations)
        self.sessions_by_key = dict(sessions_by_key or {})
        self.rows_by_id = dict(rows_by_id or {})

    def list_sessions_rich(self, **kwargs):
        return list(self.sessions_by_key.get(kwargs.get("session_key"), []))

    def get_session(self, session_id):
        return self.rows_by_id.get(session_id)


def _wire_sessions(adapter, conv_sessions):
    """Wire a fake gateway store so gateway session ids resolve to their
    conversation keys. ``conv_sessions``: {conv_id: (live_session_id_or_None,
    [generation_session_ids])} — mirrors the routing index (live only) and
    the state-db (all generations)."""
    entries, gens, rows = {}, {}, {}
    for conv, (live, generations) in conv_sessions.items():
        key = f"agent:main:zulip:stream:7:{conv}"
        if live is not None:
            entries[key] = _FakeRouteEntry(key, live)
        gens[key] = [{"id": g} for g in generations]
        for g in generations:
            rows[g] = {"session_id": g, "session_key": key}
    store = _FakeSessionStore(
        entries=entries,
        db=_FakeSessionDB(sessions_by_key=gens, rows_by_id=rows),
    )
    adapter.set_session_store(store)
    return store


class TestLegacySessionMigration:
    """Upgrade/first-enable migration: name-keyed sessions keep their
    sessions by being re-keyed onto conversation ids (no continuity loss)."""

    def test_rekeys_name_keyed_sessions_and_seeds_registry(self, adapter):
        old_key = "agent:main:zulip:stream:7:Discuss about XY"
        dm_key = "agent:main:zulip:dm:5:someone"
        conv_key = "agent:main:zulip:stream:9:cabcdef123456"
        # The conv-keyed key is recognized via the REGISTRY: seed the
        # conversation it references.
        adapter._conversations.resolve(9, "Old Topic")
        reg = adapter._conversations
        with reg._lock, reg._conn:
            reg._conn.execute(
                "INSERT OR REPLACE INTO live_holders"
                " (account_id, channel_id, topic_name, conversation_id,"
                "  origin_name, anchor_message_id, updated_at)"
                " VALUES (?,?,?,?,?,NULL,?)",
                (reg.account_id, 9, "Old Topic", "cabcdef123456", "Old Topic", 1.0),
            )
        store = _FakeSessionStore({
            old_key: _FakeRouteEntry(old_key, "s-old"),
            dm_key: _FakeRouteEntry(dm_key, "s-dm"),
            conv_key: _FakeRouteEntry(conv_key, "s-conv"),
        })
        adapter.set_session_store(store)  # gateway wiring hook triggers migration
        conv = adapter._conversations.lookup(7, "Discuss about XY")
        assert conv is not None
        new_key = f"agent:main:zulip:stream:7:{conv}"
        # The session MOVED — same session_id, new routing key.
        assert old_key not in store._entries
        assert store._entries[new_key].session_id == "s-old"
        assert store._entries[new_key].session_key == new_key
        # Non-matching keys untouched.
        assert dm_key in store._entries
        assert conv_key in store._entries
        assert store.save_calls == 1
        # Continuity: inbound resolution now lands on the same key.
        assert adapter._conversations.resolve(7, "Discuss about XY") == conv

    def test_migration_is_idempotent(self, adapter):
        store = _FakeSessionStore({
            "agent:main:zulip:stream:7:TopicA": _FakeRouteEntry(
                "agent:main:zulip:stream:7:TopicA", "s1"),
        })
        adapter.set_session_store(store)
        assert adapter._migrate_legacy_topic_sessions() == 0
        assert store.save_calls == 1  # no second save
        keys = list(store._entries)
        assert len(keys) == 1 and keys[0].endswith("TopicA") is False

    def test_collision_drops_stale_name_route(self, adapter):
        store = _FakeSessionStore({
            "agent:main:zulip:stream:7:TopicB": _FakeRouteEntry(
                "agent:main:zulip:stream:7:TopicB", "s-old"),
        })
        adapter.set_session_store(store)
        conv = adapter._conversations.lookup(7, "TopicB")
        # Residue: a name-keyed route reappears (previous enable cycle).
        stale_key = "agent:main:zulip:stream:7:TopicB"
        store._entries[stale_key] = _FakeRouteEntry(stale_key, "s-stale")
        assert adapter._migrate_legacy_topic_sessions() == 0
        assert stale_key not in store._entries
        assert store._entries[f"agent:main:zulip:stream:7:{conv}"].session_id == "s-old"

    def test_topic_names_with_colons_survive(self, adapter):
        old_key = "agent:main:zulip:stream:7:Deploy: XY"
        store = _FakeSessionStore({old_key: _FakeRouteEntry(old_key, "s-colon")})
        adapter.set_session_store(store)
        conv = adapter._conversations.lookup(7, "Deploy: XY")
        assert conv is not None
        assert f"agent:main:zulip:stream:7:{conv}" in store._entries

    def test_id_shaped_topic_name_is_migrated_not_skipped(self, adapter):
        # F5-2 closed: a legacy session for a topic literally named like a
        # conversation id ("c" + 12 hex) is MIGRATED normally — the blind
        # regex skip is gone; the registry decides. Empty registry => the
        # tail is not a known conversation => legacy name-keyed session.
        id_shaped = "c4f2a9b1c3d5e"
        old_key = f"agent:main:zulip:stream:7:{id_shaped}"
        store = _FakeSessionStore({old_key: _FakeRouteEntry(old_key, "s-shaped")})
        adapter.set_session_store(store)
        conv = adapter._conversations.lookup(7, id_shaped)
        assert conv is not None and conv != id_shaped  # fresh mint under the NAME
        new_key = f"agent:main:zulip:stream:7:{conv}"
        assert old_key not in store._entries
        assert store._entries[new_key].session_id == "s-shaped"  # continuity

    def test_known_conversation_key_is_skipped(self, adapter):
        # An id-shaped key whose conversation the REGISTRY knows is
        # already conv-keyed — skipped.
        adapter._conversations.resolve(7, "Deploy XY")
        reg = adapter._conversations
        with reg._lock, reg._conn:
            reg._conn.execute(
                "INSERT OR REPLACE INTO live_holders"
                " (account_id, channel_id, topic_name, conversation_id,"
                "  origin_name, anchor_message_id, updated_at)"
                " VALUES (?,?,?,?,?,NULL,?)",
                (reg.account_id, 7, "Deploy XY", "c4f2a9b1c3d5e", "Deploy XY", 1.0),
            )
        conv_key = "agent:main:zulip:stream:7:c4f2a9b1c3d5e"
        store = _FakeSessionStore({conv_key: _FakeRouteEntry(conv_key, "s-conv")})
        adapter.set_session_store(store)
        assert store._entries[conv_key].session_id == "s-conv"  # untouched

    def test_orphaned_conversation_key_is_skipped_on_re_enable(self, adapter):
        # Re-enable after an observed topic deletion: the orphaned
        # conversation has no live_holders row but IS recorded as an orphaned
        # audit row — its conv-keyed session must be skipped, not re-minted
        # (a recreated topic starts fresh per R4).
        conv = adapter._conversations.resolve(7, "Gone Topic")
        adapter._conversations.orphan_topic_sessions(7, "Gone Topic")
        conv_key = f"agent:main:zulip:stream:7:{conv}"
        store = _FakeSessionStore({conv_key: _FakeRouteEntry(conv_key, "s-orphan")})
        adapter.set_session_store(store)
        assert store._entries[conv_key].session_id == "s-orphan"  # untouched
        # Still no mapping for the deleted topic's name.
        assert adapter._conversations.lookup(7, "Gone Topic") is None

    def test_user_tail_session_is_left_in_place(self, adapter):
        # Feature-off legacy shape (group_sessions_per_user default): the
        # tail is the SENDER, not a topic. Leaving it in place avoids both
        # the junk conversation and the rekey-to-nowhere session loss.
        user_key = "agent:main:zulip:stream:7:user@x.com"
        store = _FakeSessionStore({user_key: _FakeRouteEntry(user_key, "s-user")})
        adapter.set_session_store(store)
        # Untouched, same key, same session...
        assert store._entries[user_key].session_id == "s-user"
        # ...no junk topic minted for the email...
        assert adapter._conversations.lookup(7, "user@x.com") is None
        # ...and nothing was rekeyed (no save).
        assert store.save_calls == 0

    def test_thread_per_user_suffix_preserved_on_migration(self, adapter):
        # thread_sessions_per_user deployment, old name-keyed era:
        # "...:<topic>:<user>" must rekey to "...:<conv>:<user>" so the
        # migrated key matches what the gateway builds post-enable.
        old_key = "agent:main:zulip:stream:7:Discuss about XY:user@x.com"
        store = _FakeSessionStore({old_key: _FakeRouteEntry(old_key, "s-tspu")})
        adapter.set_session_store(store)
        conv = adapter._conversations.lookup(7, "Discuss about XY")
        new_key = f"agent:main:zulip:stream:7:{conv}:user@x.com"
        assert old_key not in store._entries
        assert store._entries[new_key].session_id == "s-tspu"

    def test_colon_topic_with_user_suffix_preserved(self, adapter):
        # Colon topics still split correctly: the LAST colon separates the
        # user (emails contain no colons).
        old_key = "agent:main:zulip:stream:7:Deploy: XY:user@x.com"
        store = _FakeSessionStore({old_key: _FakeRouteEntry(old_key, "s-colon")})
        adapter.set_session_store(store)
        conv = adapter._conversations.lookup(7, "Deploy: XY")
        new_key = f"agent:main:zulip:stream:7:{conv}:user@x.com"
        assert store._entries[new_key].session_id == "s-colon"

    def test_thread_per_user_conv_key_skipped_on_re_enable(self, adapter):
        # Our feature + thread_sessions_per_user, disable/re-enable cycle:
        # conv-keyed keys carry the user suffix and must be skipped via the
        # registry (the head conversation is known).
        conv = adapter._conversations.resolve(7, "TopicT")
        conv_key = f"agent:main:zulip:stream:7:{conv}:user@x.com"
        store = _FakeSessionStore({conv_key: _FakeRouteEntry(conv_key, "s-live")})
        adapter.set_session_store(store)
        assert store._entries[conv_key].session_id == "s-live"  # untouched

    def test_id_shaped_name_with_user_suffix_migrated(self, adapter):
        # F5-2 x thread_sessions_per_user: an id-shaped head the registry
        # does NOT know is a legacy topic name — migrate it, suffix intact.
        old_key = "agent:main:zulip:stream:7:c4f2a9b1c3d5e:user@x.com"
        store = _FakeSessionStore({old_key: _FakeRouteEntry(old_key, "s-mix")})
        adapter.set_session_store(store)
        conv = adapter._conversations.lookup(7, "c4f2a9b1c3d5e")
        assert conv is not None and conv != "c4f2a9b1c3d5e"
        new_key = f"agent:main:zulip:stream:7:{conv}:user@x.com"
        assert store._entries[new_key].session_id == "s-mix"

    def test_missing_store_is_skipped_safely(self, adapter):
        adapter._session_store = None
        assert adapter._migrate_legacy_topic_sessions() == 0
        adapter._session_store = object()  # incompatible surface
        assert adapter._migrate_legacy_topic_sessions() == 0


class TestF1DispatchOrderResolve:
    """F1: a message event queued before a rename — in the same poll batch
    or while the handler awaits — must resolve at dispatch, in event-id
    order. The deferred handler reuses that conversation instead of
    minting a fresh one under the freed name, so the reply routes to the
    topic's CURRENT name (the rename's destination), never the resurrected
    old one."""

    @pytest.mark.asyncio
    async def test_pre_rename_message_continues_conversation_after_rename(
        self, adapter
    ):
        # 1) The topic already has a conversation.
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=30))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()

        # 2) Dispatch resolves the queued message's conversation inline,
        #    THEN the rename lands (inline, event-id order) before the
        #    handler task ever starts.
        queued = _stream_msg("Deploy XY", msg_id=31)
        adapter._pre_resolve_conversation(queued, "31")
        assert adapter._pending_conversations["31"] == conv

        adapter._handle_topic_update(
            _rename_event("Deploy XY", "Release XY", message_ids=[30, 31])
        )
        assert adapter._conversations.current_name(7, conv) == "Release XY"

        # 3) The deferred handler runs: it must reuse the stashed
        #    conversation — no fresh mint under the freed name.
        await adapter._handle_message(queued)
        assert adapter._pending_conversations == {}
        assert adapter.handle_message.call_args[0][0].source.thread_id == conv

        # 4) The reply routes to the conversation's CURRENT topic name.
        await adapter.send("7", "reply", metadata={"thread_id": conv})
        sent = adapter.client._client._sent_messages[-1]
        assert sent["topic"] == "Release XY"

    @pytest.mark.asyncio
    async def test_self_message_does_not_leak_stash_entry(self, adapter):
        """A3: the dispatch stash is popped BEFORE the self-message filter,
        so a pre-resolved self-message cannot leak a _pending_conversations
        entry. (The pop used to sit below the filter's early return — every
        pre-resolved self-message leaked one entry.)"""
        self_msg = _stream_msg("Deploy XY", msg_id=77)
        self_msg["sender_email"] = "bot@zulip.com"  # the bot's own message
        # Dispatch pre-resolves any stream message — self-messages included
        # (the filter only exists in the handler) — so the stash holds it.
        adapter._pre_resolve_conversation(self_msg, "77")
        assert "77" in adapter._pending_conversations
        # The handler returns early (self-message) but drains the stash.
        await adapter._handle_message(self_msg)
        assert "77" not in adapter._pending_conversations
        # And nothing was processed.
        adapter.handle_message.assert_not_called()
        # The eager mint itself is intentional (the topic exists; the
        # acknowledged cost of the F1 fix) — only the stash leak is a bug.
        assert adapter._conversations.lookup(7, "Deploy XY") is not None

    @pytest.mark.asyncio
    async def test_poll_loop_resolves_message_before_same_batch_rename(
        self, adapter
    ):
        """The probe_loop scenario: the REAL _listen_for_events handles one
        id-ordered batch [message, rename] — the message must key to the
        topic's existing conversation and the reply must land in the
        renamed topic."""
        await adapter._handle_message(_stream_msg("Deploy XY", msg_id=30))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()

        client = adapter.client._client
        client.inject_event(
            {
                "id": 101,
                "type": "message",
                "message": _stream_msg(
                    "Deploy XY", msg_id=31, content="pre-rename question"
                ),
            }
        )
        client.inject_event(
            _rename_event("Deploy XY", "Release XY", message_ids=[30, 31], id=102)
        )

        adapter._listening = True
        loop_task = asyncio.create_task(adapter._listen_for_events())
        try:
            for _ in range(250):
                await asyncio.sleep(0.02)
                if adapter.handle_message.call_count:
                    break
        finally:
            adapter._listening = False
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await loop_task

        assert adapter.handle_message.call_args is not None
        assert adapter.handle_message.call_args[0][0].source.thread_id == conv
        # The registry: the rename repointed; nothing was forked.
        assert adapter._conversations.current_name(7, conv) == "Release XY"
        # And the reply routes to the renamed topic.
        await adapter.send("7", "reply", metadata={"thread_id": conv})
        sent = adapter.client._client._sent_messages[-1]
        assert sent["topic"] == "Release XY"



def _delete_event(topic, message_ids, stream_id=7, event_id=50):
    return {
        "id": event_id,
        "type": "delete_message",
        "message_type": "stream",
        "stream_id": stream_id,
        "topic": topic,
        "message_ids": message_ids,
    }


class TestTopicDeletionOrphaning:
    """R10: a topic deleted WITHOUT rename/merge has no beneficiary — its
    whole session set is orphaned (unreachable by /continue from any
    topic) and a recreated same-name topic starts a fresh lineage.

    ANY delete_message event on a mapped topic triggers a verification
    (single-message deletes included — a topic emptied message-by-message
    is caught by its final delete); the channel's topic list
    (get_stream_topics) is the authority, so a partial delete (2 of 9
    messages) never orphans a living topic."""

    @pytest.mark.asyncio
    async def test_full_deletion_orphans_and_recreate_starts_fresh(self, adapter):
        await adapter._handle_message(_stream_msg("Discuss XY", msg_id=1))
        conv_d = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        adapter._handle_topic_update(_rename_event("Discuss XY", "Release XY"))
        # Recreate "Discuss XY": fresh lineage, with an inherited former
        # session so the orphaning has something to detach.
        await adapter._handle_message(_stream_msg("Discuss XY", msg_id=3))
        fresh = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        await adapter._handle_message(_stream_msg("Other", msg_id=4))
        conv_o = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        adapter._handle_topic_update(_rename_event("Other", "Discuss XY"))
        # "Discuss XY" now holds conv_o (current) + fresh (former member).
        _cur, _org, members = adapter._conversations.sessions_for_topic(7, "Discuss XY")
        assert [m for m, _o in members] == [fresh]

        # The whole topic is deleted: absent from the channel's topic list.
        adapter.client._client.stream_topics[7] = ["Release XY"]
        # Await the handler itself: trigger check -> verification -> orphan.
        await adapter._handle_message_delete_event(
            _delete_event("Discuss XY", [3, 4, 5])
        )

        # No beneficiary: mapping gone, set unreachable.
        assert adapter._conversations.lookup(7, "Discuss XY") is None
        _cur, _org, members = adapter._conversations.sessions_for_topic(7, "Discuss XY")
        assert members == []
        assert adapter._conversations.current_name(7, conv_o) is None
        assert adapter._conversations.current_name(7, fresh) is None

        # A recreated "Discuss XY" starts FRESH and cannot adopt anything.
        await adapter._handle_message(_stream_msg("Discuss XY", msg_id=6))
        recreated = adapter.handle_message.call_args[0][0].source.thread_id
        assert recreated not in {conv_o, fresh}
        adapter.handle_message.reset_mock()
        _wire_sessions(adapter, {
            conv_o: (None, ["20260928_180039_ac06c9b7"]),
        })
        await adapter._handle_message(
            _stream_msg("Discuss XY", msg_id=7,
                        content="/continue 20260928_180039_ac06c9b7")
        )
        cmd_call = adapter.client._client._sent_messages[0]
        assert "not one of this topic's sessions" in cmd_call["content"]

    @pytest.mark.asyncio
    async def test_partial_bulk_delete_keeps_living_topic(self, adapter):
        # Deleting 2 of 9 messages is a bulk event but NOT a topic deletion.
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        adapter.client._client.stream_topics[7] = ["deploys"]  # topic still exists
        await adapter._handle_message_delete_event(_delete_event("deploys", [1, 2]))
        # Mapping and sessions intact.
        assert adapter._conversations.lookup(7, "deploys") == conv

    @pytest.mark.asyncio
    async def test_single_delete_verifies_topic_present_ignored(self, adapter):
        # A single (non-bulk) delete triggers verification; a topic that
        # still exists is never orphaned — partial wipes change nothing.
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        adapter.client._client.stream_topics[7] = ["deploys"]  # still exists
        await adapter._handle_message_delete_event(_delete_event("deploys", [999]))
        assert adapter._conversations.lookup(7, "deploys") == conv

    @pytest.mark.asyncio
    async def test_topic_emptied_by_single_deletes_orphans_on_final_delete(self, adapter):
        # The anchor-first gap, closed: the anchor message is deleted
        # FIRST (topic still has messages — ignored), then the remaining
        # messages one by one. Each single delete verifies; the final one
        # finds the topic gone from the channel's topic list and orphans.
        await adapter._handle_message(_stream_msg("deploys", msg_id=41))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        for i in range(42, 46):  # 4 more messages -> 5 total
            await adapter._handle_message(_stream_msg("deploys", msg_id=i))
            adapter.handle_message.reset_mock()
        # Anchor (41) deleted first: topic still exists -> ignored.
        adapter.client._client.stream_topics[7] = ["deploys"]
        await adapter._handle_message_delete_event(
            _delete_event("deploys", [41], event_id=60)
        )
        assert adapter._conversations.lookup(7, "deploys") == conv  # mapping kept
        # Singles 42..44: topic still exists -> ignored.
        for mid in (42, 43, 44):
            await adapter._handle_message_delete_event(
                _delete_event("deploys", [mid], event_id=60 + mid)
            )
        assert adapter._conversations.lookup(7, "deploys") == conv
        # Final single delete (45): the topic is gone from the list.
        adapter.client._client.stream_topics[7] = []
        await adapter._handle_message_delete_event(
            _delete_event("deploys", [45], event_id=105)
        )
        # Orphaned: no beneficiary — recreate starts fresh.
        assert adapter._conversations.lookup(7, "deploys") is None
        assert adapter._conversations.current_name(7, conv) is None

    @pytest.mark.asyncio
    async def test_verification_failure_keeps_mapping(self, adapter, monkeypatch):
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()

        def _boom(stream_id):
            raise RuntimeError("network down")

        monkeypatch.setattr(adapter.client._client, "get_stream_topics", _boom)
        # The handler runs the whole chain and swallows the verification
        # error (fail-open, logged).
        await adapter._handle_message_delete_event(_delete_event("deploys", [1, 2]))
        # Fail-open: the mapping is kept.
        assert adapter._conversations.lookup(7, "deploys") == conv

    @pytest.mark.asyncio
    async def test_unmapped_topic_delete_event_ignored(self, adapter, monkeypatch):
        calls = []
        orig = adapter.client._client.get_stream_topics

        def _recording(stream_id):
            calls.append(stream_id)
            return orig(stream_id)

        monkeypatch.setattr(adapter.client._client, "get_stream_topics", _recording)
        await adapter._handle_message_delete_event(
            _delete_event("ghost", [1, 2, 3])
        )
        # Nothing mapped: no verification at all, no state change anywhere.
        assert calls == []
        assert adapter._conversations.lookup(7, "ghost") is None

    @pytest.mark.asyncio
    async def test_poll_loop_orphans_on_delete_event(self, adapter):
        """The probe_delete_trigger phase-1 scenario, as a regression pin:
        the REAL _listen_for_events handles one delete_message event — the
        spawned trigger task must run (topic list consulted) and the
        mapping must be orphaned. This is the dispatch path; awaiting the
        handler directly cannot cover it."""
        await adapter._handle_message(_stream_msg("deploys", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()

        client = adapter.client._client
        client.stream_topics[7] = []  # topic genuinely gone
        calls = []
        orig = client.get_stream_topics

        def _recording(stream_id):
            calls.append(stream_id)
            return orig(stream_id)

        client.get_stream_topics = _recording
        client.inject_event(_delete_event("deploys", [1], event_id=103))

        adapter._listening = True
        loop_task = asyncio.create_task(adapter._listen_for_events())
        try:
            orphaned = False
            for _ in range(250):
                await asyncio.sleep(0.02)
                if adapter._conversations.lookup(7, "deploys") is None:
                    orphaned = True
                    break
        finally:
            adapter._listening = False
            loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await loop_task
            await asyncio.sleep(0.05)  # let the spawned trigger task finish

        # The trigger RAN: verification consulted the channel's topic list
        # and the mapping was freed.
        assert calls, "get_stream_topics was never called — trigger is dead"
        assert orphaned
        assert adapter._conversations.current_name(7, conv) is None
        # Recreated topic starts fresh (R4).
        await adapter._handle_message(_stream_msg("deploys", msg_id=9))
        recreated = adapter.handle_message.call_args[0][0].source.thread_id
        assert recreated != conv


class TestIdShapedTopicNameProbe:
    """F5-2 verification: a topic literally named like a conversation id
    ("c" + 12 hex) while that conversation id is held by another topic.

    Key formats: legacy sessions key on the topic NAME
    (``agent:<ns>:zulip:stream:<ch>:<topic_name>``), feature sessions on
    the conversation ID (``agent:<ns>:zulip:stream:<ch>:<conv_id>``).
    When the topic name is id-shaped the two tails are identical strings,
    so the guards must come from the plugin's write paths — never from
    string shape."""

    ID_SHAPED = "c4f2a9b1c3d5e"  # "c" + 12 hex = valid conversation-id format
    HELD_TOPIC = "Deploy XY"

    def _seed_held_session(self, reg) -> None:
        """Conversation c4f2a9b1c3d5e live at "Deploy XY" (direct row —
        real mints are random; this forces the adversarial coincidence)."""
        with reg._lock, reg._conn:
            reg._conn.execute(
                "INSERT INTO live_holders (account_id, channel_id, topic_name,"
                " conversation_id, origin_name, anchor_message_id, updated_at)"
                " VALUES (?,?,?,?,?,NULL,?)",
                (reg.account_id, 7, self.HELD_TOPIC, self.ID_SHAPED,
                 self.HELD_TOPIC, 1.0),
            )
        assert reg.lookup(7, self.HELD_TOPIC) == self.ID_SHAPED

    @pytest.mark.asyncio
    async def test_case1_start_does_not_access_held_session(self, adapter):
        """Creating topic "c4f2a9b1c3d5e" must NOT bind it to the held
        conversation of the same id: the first message mints a fresh
        conversation, and the gateway thread_id (the session-key tail) is
        that fresh id — never the name/held id."""
        reg = adapter._conversations
        self._seed_held_session(reg)

        await adapter._handle_message(_stream_msg(self.ID_SHAPED, msg_id=1))
        fresh = adapter.handle_message.call_args[0][0].source.thread_id
        # The topic did NOT access the held session on start...
        assert fresh != self.ID_SHAPED
        # ...it got its own mapping under the id-shaped NAME...
        assert reg.lookup(7, self.ID_SHAPED) == fresh
        # ...and the session-key tail (thread_id) is the fresh id, so the
        # gateway key is "...:7:<fresh>", NOT "...:7:c4f2a9b1c3d5e" (the
        # held session's key).
        assert fresh == reg.lookup(7, self.ID_SHAPED)
        # The held session is untouched.
        assert reg.lookup(7, self.HELD_TOPIC) == self.ID_SHAPED
        assert reg.current_name(7, self.ID_SHAPED) == self.HELD_TOPIC
        # The id-shaped topic's own set is empty — nothing reachable.
        _cur, _org, members = reg.sessions_for_topic(7, self.ID_SHAPED)
        assert members == []

    @pytest.mark.asyncio
    async def test_case2_continue_cannot_steal_held_session(self, adapter):
        """/continue <held-id> inside the id-shaped topic is rejected —
        the candidate set is the topic's own, not any id-shaped match."""
        reg = adapter._conversations
        self._seed_held_session(reg)
        await adapter._handle_message(_stream_msg(self.ID_SHAPED, msg_id=1))
        fresh = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()

        # The theft attempt.
        await adapter._handle_message(
            _stream_msg(self.ID_SHAPED, msg_id=2, content=f"/continue {self.ID_SHAPED}")
        )
        cmd_call = adapter.client._client._sent_messages[0]
        assert "not a session id shown by" in cmd_call["content"]

        # Nothing moved: the held session is still live at "Deploy XY"...
        assert reg.lookup(7, self.HELD_TOPIC) == self.ID_SHAPED
        assert reg.current_name(7, self.ID_SHAPED) == self.HELD_TOPIC
        # ...and the id-shaped topic still routes to its own session.
        await adapter._handle_message(_stream_msg(self.ID_SHAPED, msg_id=3))
        assert adapter.handle_message.call_args[0][0].source.thread_id == fresh


class TestSessionStartLabels:
    """Per-session start labels (v6): every gateway session is labeled with
    the topic name it was actually created under — not the lineage origin,
    which only matches until the first rename between generations."""

    def test_session_start_first_record_wins(self, registry):
        conv = registry.resolve(7, "TopicA")
        registry.record_session_start(
            7, conv, "20260901_120000_aaaa", started_in="OldName"
        )
        # A later write must not relabel an existing record.
        registry.record_session_start(
            7, conv, "20260901_120000_aaaa", started_in="NewName"
        )
        assert registry.session_start(7, "20260901_120000_aaaa") == "OldName"
        # Unknown session -> no record.
        assert registry.session_start(7, "20260101_000000_zzzz") is None

    def test_derive_session_start_tracks_last_mapping_change(self, registry):
        conv = registry.resolve(7, "TopicA")
        future = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).strftime("%Y%m%d_%H%M%S")
        # A session created after the last mapping change was created
        # under the CURRENT name.
        assert (
            registry.derive_session_start(7, conv, f"{future}_bbbb") == "TopicA"
        )
        registry.repoint(7, "TopicA", "TopicB")
        assert (
            registry.derive_session_start(7, conv, f"{future}_bbbb") == "TopicB"
        )
        # A session predating every tracked change falls back to the
        # lineage origin (the one-time approximation).
        assert (
            registry.derive_session_start(7, conv, "20260101_120000_cccc")
            == "TopicA"
        )

    def test_schema_v5_upgrades_in_place(self, tmp_path):
        """A v5 registry upgrades additively through v6 (labels) and v7
        (rename): data preserved, no drop, no re-mint, no continuity
        loss."""
        db_path = tmp_path / "zulip_conversations_x.db"
        db = sqlite3.connect(str(db_path))
        db.execute(
            "CREATE TABLE topic_map ("
            " account_id TEXT NOT NULL, channel_id INTEGER NOT NULL,"
            " topic_name TEXT NOT NULL, conversation_id TEXT NOT NULL,"
            " origin_name TEXT NOT NULL, anchor_message_id INTEGER,"
            " updated_at REAL NOT NULL,"
            " PRIMARY KEY (account_id, channel_id, topic_name))"
        )
        db.execute(
            "CREATE UNIQUE INDEX idx_topic_map_conversation ON topic_map"
            " (account_id, channel_id, conversation_id)"
        )
        db.execute(
            "CREATE TABLE tombstones ("
            " account_id TEXT NOT NULL, channel_id INTEGER NOT NULL,"
            " conversation_id TEXT NOT NULL, topic_name TEXT,"
            " origin_name TEXT NOT NULL, last_topic TEXT,"
            " freed_at REAL NOT NULL,"
            " PRIMARY KEY (account_id, channel_id, conversation_id))"
        )
        db.execute(
            "INSERT INTO topic_map VALUES"
            " ('x', 7, 'Keep', 'cabc1234567890', 'Keep', NULL, 1.0)"
        )
        db.execute("PRAGMA user_version = 5")
        db.commit()
        db.close()

        reg = TopicConversationRegistry(account_id="x", data_dir=str(tmp_path))
        assert reg._conn.execute("PRAGMA user_version").fetchone()[0] == 7
        tables = {
            r[0]
            for r in reg._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "live_holders" in tables and "former_holders" in tables
        assert "topic_map" not in tables and "tombstones" not in tables
        # v5 data survives the upgrade untouched.
        assert reg.lookup(7, "Keep") == "cabc1234567890"
        # And the label store is live.
        reg.record_session_start(
            7, "cabc1234567890", "20260901_120000_aaaa", started_in="Keep"
        )
        assert reg.session_start(7, "20260901_120000_aaaa") == "Keep"


    def test_v6_rename_preserves_every_row(self, tmp_path):
        """The live-db path: a v6 registry (old table names) upgrades to
        v7 by metadata-only rename — every row survives, and the
        uniqueness constraints still hold under the new names."""
        db_path = tmp_path / "zulip_conversations_x.db"
        db = sqlite3.connect(str(db_path))
        db.execute(
            "CREATE TABLE topic_map ("
            " account_id TEXT NOT NULL, channel_id INTEGER NOT NULL,"
            " topic_name TEXT NOT NULL, conversation_id TEXT NOT NULL,"
            " origin_name TEXT NOT NULL, anchor_message_id INTEGER,"
            " updated_at REAL NOT NULL,"
            " PRIMARY KEY (account_id, channel_id, topic_name))"
        )
        db.execute(
            "CREATE UNIQUE INDEX idx_topic_map_conversation ON topic_map"
            " (account_id, channel_id, conversation_id)"
        )
        db.execute(
            "CREATE TABLE tombstones ("
            " account_id TEXT NOT NULL, channel_id INTEGER NOT NULL,"
            " conversation_id TEXT NOT NULL, topic_name TEXT,"
            " origin_name TEXT NOT NULL, last_topic TEXT,"
            " freed_at REAL NOT NULL,"
            " PRIMARY KEY (account_id, channel_id, conversation_id))"
        )
        db.execute(
            "CREATE TABLE session_starts ("
            " account_id TEXT NOT NULL, channel_id INTEGER NOT NULL,"
            " conversation_id TEXT NOT NULL, session_id TEXT NOT NULL,"
            " started_in TEXT NOT NULL, recorded_at REAL NOT NULL,"
            " PRIMARY KEY (account_id, channel_id, session_id))"
        )
        db.execute(
            "INSERT INTO topic_map VALUES"
            " ('x', 7, 'Keep', 'cabc1234567890', 'OldName', 11, 1.0)"
        )
        db.execute(
            "INSERT INTO tombstones VALUES"
            " ('x', 7, 'cdef0987654321', 'Keep', 'cdef0987654321',"
            "  'Keep', 2.0)"
        )
        db.execute(
            "INSERT INTO session_starts VALUES"
            " ('x', 7, 'cabc1234567890', '20260901_120000_aaaa',"
            "  'Keep', 3.0)"
        )
        db.execute("PRAGMA user_version = 6")
        db.commit()
        db.close()

        reg = TopicConversationRegistry(account_id="x", data_dir=str(tmp_path))
        assert reg._conn.execute("PRAGMA user_version").fetchone()[0] == 7
        tables = {
            r[0]
            for r in reg._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "live_holders" in tables and "former_holders" in tables
        assert "topic_map" not in tables and "tombstones" not in tables
        indexes = {
            r[0]
            for r in reg._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_live_holders_conversation" in indexes
        assert "idx_topic_map_conversation" not in indexes
        # Rows survive untouched.
        assert reg.lookup(7, "Keep") == "cabc1234567890"
        cur, origin, members = reg.sessions_for_topic(7, "Keep")
        assert cur == "cabc1234567890" and origin == "OldName"
        assert members == [("cdef0987654321", "cdef0987654321")]
        assert reg.session_start(7, "20260901_120000_aaaa") == "Keep"
        # The conversation-uniqueness constraint still holds post-rename.
        with pytest.raises(sqlite3.IntegrityError):
            reg._conn.execute(
                "INSERT INTO live_holders VALUES"
                " ('x', 7, 'Other', 'cabc1234567890', 'Other', NULL, 4.0)"
            )


class TestSessionStartLabelsEndToEnd:
    """The user's case: topic renamed between generations — each session
    keeps the name it was minted under."""

    @pytest.mark.asyncio
    async def test_started_in_tracks_session_mint_not_lineage_origin(
        self, adapter
    ):
        await adapter._handle_message(_stream_msg("Topic261001-1", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        adapter._handle_topic_update(
            _rename_event("Topic261001-1", "Topic261001-2")
        )
        # New generation minted AFTER the rename: its id timestamp
        # postdates the conversation's last mapping change.
        future = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).strftime("%Y%m%d_%H%M%S")
        new_sid = f"{future}_88125928"
        old_sid = "20260901_133036_4cd2432d"
        _wire_sessions(adapter, {conv: (new_sid, [old_sid, new_sid])})
        await adapter._handle_message(_stream_msg("Topic261001-2", msg_id=2))
        adapter.handle_message.reset_mock()
        await adapter._handle_message(
            _stream_msg("Topic261001-2", msg_id=3, content="/topic-sessions")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        assert "📋 Sessions in this topic: 2" in reply
        lines = [l for l in reply.split("\n") if "— started in" in l]
        # The post-rename generation is labeled with the name it was
        # minted under; the pre-rename generation keeps its own.
        assert any(
            f"`{new_sid}`" in l and 'started in "Topic261001-2"' in l
            for l in lines
        )
        assert any(
            f"`{old_sid}`" in l and 'started in "Topic261001-1"' in l
            for l in lines
        )
        # Labels differ per session — not one lineage origin for both.
        assert 'started in "Topic261001-1"' in reply
        assert 'started in "Topic261001-2"' in reply

    @pytest.mark.asyncio
    async def test_message_observation_records_new_generation(self, adapter):
        await adapter._handle_message(_stream_msg("TopicA", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        store = _wire_sessions(
            adapter, {conv: ("20260901_120000_aaaa", ["20260901_120000_aaaa"])}
        )
        # /new: the conversation's live session changes.
        key = f"agent:main:zulip:stream:7:{conv}"
        store._entries[key].session_id = "20260901_130000_bbbb"
        await adapter._handle_message(_stream_msg("TopicA", msg_id=2))
        # First sight of the new generation recorded its start label.
        assert (
            adapter._conversations.session_start(7, "20260901_130000_bbbb")
            == "TopicA"
        )

    @pytest.mark.asyncio
    async def test_listing_observes_new_generation_without_message_traffic(
        self, adapter
    ):
        """``/new`` followed directly by ``/topic-sessions``: the fresh
        generation has no record yet (``/new`` is core-handled — it never
        reaches this adapter, and no message has been processed since),
        so the listing must observe the live session before rendering
        instead of falling back to the lineage origin (Topic261002-2)."""
        await adapter._handle_message(_stream_msg("TopicA", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        adapter._handle_topic_update(_rename_event("TopicA", "TopicB"))
        old_sid = "20260901_180238_1e8320d8"
        store = _wire_sessions(adapter, {conv: (old_sid, [old_sid])})
        future = (
            datetime.now(timezone.utc) + timedelta(minutes=5)
        ).strftime("%Y%m%d_%H%M%S")
        new_sid = f"{future}_d08790be"
        # /new: the conversation's live session rotates — and no message
        # is processed afterwards, only the listing check.
        key = f"agent:main:zulip:stream:7:{conv}"
        store._entries[key].session_id = new_sid
        await adapter._handle_message(
            _stream_msg("TopicB", msg_id=2, content="/topic-sessions")
        )
        reply = adapter.client._client._sent_messages[0]["content"]
        assert "📋 Sessions in this topic: 2" in reply
        lines = [l for l in reply.split("\n") if "— started in" in l]
        assert any(
            f"`{new_sid}`" in l and 'started in "TopicB"' in l
            for l in lines
        )
        assert any(
            f"`{old_sid}`" in l and 'started in "TopicA"' in l
            for l in lines
        )

    @pytest.mark.asyncio
    async def test_backfill_derives_labels_and_is_idempotent(self, adapter):
        await adapter._handle_message(_stream_msg("TopicA", msg_id=1))
        conv = adapter.handle_message.call_args[0][0].source.thread_id
        adapter.handle_message.reset_mock()
        _wire_sessions(
            adapter,
            {conv: ("20260901_140000_dddd",
                    ["20260901_120000_aaaa", "20260901_140000_dddd"])},
        )
        # Wiring ran the backfill: both generations carry a label (the
        # origin here — neither postdates the last mapping change).
        assert (
            adapter._conversations.session_start(7, "20260901_120000_aaaa")
            == "TopicA"
        )
        assert (
            adapter._conversations.session_start(7, "20260901_140000_dddd")
            == "TopicA"
        )
        # Idempotent: a second run records nothing new.
        assert adapter._backfill_session_starts() == 0
