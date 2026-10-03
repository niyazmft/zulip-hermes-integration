"""Branch coverage for the guards wave 10 disclosed as untested in ``routing.py``.

Every path here is a defensive early return or a swallow-and-log. They went
untested for the obvious reason — they are hard to reach — and they are worth
pinning for a less obvious one: a guard that silently stops returning does not
raise, it just changes which conversation a reply lands on. The failure mode is
a forked or mis-routed topic session, which no other test would notice.

Covers, per the wave-10 disclosure:

* ``pre_resolve_conversation``: registry-less, non-stream, empty topic,
  non-int ``stream_id``, and the resolve-failure swallow
* ``handle_topic_update``: registry-less, malformed event, same-topic touch,
  and the outer ``except`` (never raises)
* ``routed_topic``: the ``(TypeError, ValueError)`` guards for a non-int
  ``stream_id``
* ``routed_topic_for_chat``: the non-digit branch
* ``session_aliases``: the empty-``chat_id`` and raising-getter branches
* ``_session_db``: no store, and a store that exposes no ``_db``
* ``apply_topic_deletion``: verification raising, verification unavailable,
  topic still present, and the confirmed-deletion (orphan) path
* ``migrate_legacy_topic_sessions`` / ``backfill_session_starts``: registry-less
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import zulip.routing as routing


def _state(monkeypatch, *, topic_sessions: bool = False, tmp_path=None, **attrs):
    """A ``RoutingState`` over a stub adapter.

    ``topic_sessions=False`` yields the registry-less state (``_conversations``
    is ``None``), which is what most of the guards key on.
    """
    monkeypatch.setattr(routing, "_topic_sessions_enabled", lambda: topic_sessions)
    values = {
        "email": "bot@test.zulipchat.com",
        "_data_dir": str(tmp_path) if tmp_path else None,
        "_session_store": None,
        "_route_entries": lambda: [],
        "_entry_for_conversation": lambda entries, conversation_id: (None, None),
    }
    values.update(attrs)
    return routing.RoutingState(SimpleNamespace(**values))


class _ExplodingRegistry:
    """Any attribute use raises, to drive the swallow-and-log paths."""

    def __getattr__(self, name):  # pragma: no cover - exercised via callers
        raise RuntimeError(f"registry boom: {name}")


class _RecordingRegistry:
    """Records ``orphan_topic_sessions`` calls and nothing else."""

    def __init__(self):
        self.orphaned = []

    def orphan_topic_sessions(self, stream_id, topic):
        self.orphaned.append((stream_id, topic))
        return 0


class TestRegistrylessGuards:
    def test_pre_resolve_does_not_stash(self, monkeypatch):
        state = _state(monkeypatch)
        state.pre_resolve_conversation(
            {"type": "stream", "stream_id": 1, "subject": "t"}, "5"
        )
        assert state._pending_conversations == {}

    def test_handle_topic_update_is_a_noop(self, monkeypatch):
        state = _state(monkeypatch)
        state.handle_topic_update(
            {"type": "update_message", "stream_id": 1, "orig_subject": "t", "subject": "u"}
        )
        assert state._topic_cache == {}

    def test_migrate_legacy_topic_sessions_returns_zero(self, monkeypatch):
        assert _state(monkeypatch).migrate_legacy_topic_sessions() == 0

    def test_backfill_session_starts_returns_zero(self, monkeypatch):
        assert _state(monkeypatch).backfill_session_starts() == 0


class TestPreResolveGuards:
    def test_ignores_a_non_stream_message(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        state.pre_resolve_conversation({"type": "private", "sender_id": 7}, "9")
        assert state._pending_conversations == {}

    def test_ignores_an_empty_topic(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        state.pre_resolve_conversation(
            {"type": "stream", "stream_id": 1, "subject": ""}, "9"
        )
        assert state._pending_conversations == {}

    def test_ignores_a_non_int_stream_id(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        state.pre_resolve_conversation(
            {"type": "stream", "stream_id": "1", "subject": "t"}, "9"
        )
        assert state._pending_conversations == {}

    def test_swallows_a_resolve_failure(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        state._conversations = _ExplodingRegistry()
        state.pre_resolve_conversation(
            {"type": "stream", "stream_id": 1, "subject": "t"}, "9"
        )
        assert state._pending_conversations == {}

    def test_stashes_the_resolved_conversation(self, monkeypatch, tmp_path):
        """The positive path, so the guards above are pinned against it."""
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        state.pre_resolve_conversation(
            {"type": "stream", "stream_id": 1, "subject": "t"}, "9"
        )
        assert list(state._pending_conversations) == ["9"]


class TestHandleTopicUpdateGuards:
    def test_ignores_a_malformed_event(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        state.handle_topic_update(
            {"type": "update_message", "stream_id": "x", "orig_subject": "t", "subject": "u"}
        )
        state.handle_topic_update(
            {"type": "update_message", "stream_id": 1, "orig_subject": "", "subject": "u"}
        )

    def test_ignores_a_same_topic_touch(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        state.handle_topic_update(
            {"type": "update_message", "stream_id": 1, "orig_subject": "t", "subject": "t"}
        )

    def test_never_raises_when_the_registry_fails(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        state._conversations = _ExplodingRegistry()
        # A full rename drives registry.repoint(); it raises and is swallowed.
        state.handle_topic_update(
            {
                "type": "update_message",
                "stream_id": 1,
                "orig_subject": "old",
                "subject": "new",
                "propagate_mode": "change_all",
            }
        )


class TestRoutedTopicGuards:
    def test_non_int_stream_id_returns_the_raw_value(self, monkeypatch, tmp_path):
        """Both the ``int(stream_id)`` and ``int(stream_id)``-in-fallback guards."""
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        raw = "c0123456789ab"
        assert state.routed_topic("not-an-int", {"thread_id": raw}) == raw

    def test_unknown_plain_topic_returns_the_raw_value(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        assert state.routed_topic(1, {"topic": "general"}) == "general"

    def test_non_digit_chat_id_uses_the_metadata_topic(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        assert state.routed_topic_for_chat("dm:42", {"topic": "general"}) == "general"

    def test_digit_chat_id_delegates_to_routed_topic(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        assert state.routed_topic_for_chat("1", {"topic": "general"}) == "general"

    def test_missing_metadata_returns_none(self, monkeypatch, tmp_path):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        assert state.routed_topic(1, {}) is None


class TestSessionAliases:
    def test_empty_without_a_source_or_host_key(self):
        assert routing.session_aliases(SimpleNamespace(), SimpleNamespace()) == []

    def test_host_key_and_route_alias_both_present(self):
        adapter = SimpleNamespace(_event_session_key=lambda event: "host-key")
        event = SimpleNamespace(
            source=SimpleNamespace(chat_id="573423"),
            metadata={"topic": "general"},
        )
        assert routing.session_aliases(adapter, event) == [
            "key:host-key",
            "route:573423\x00general",
        ]

    def test_route_alias_uses_an_empty_thread_when_metadata_has_no_topic(self):
        event = SimpleNamespace(source=SimpleNamespace(chat_id="573423"), metadata={})
        assert routing.session_aliases(SimpleNamespace(), event) == [
            "route:573423\x00",
        ]

    def test_session_key_for_event_swallows_a_raising_getter(self):
        def boom(event):
            raise RuntimeError("boom")

        adapter = SimpleNamespace(_event_session_key=boom)
        assert routing.session_key_for_event(adapter, object()) is None

    def test_session_key_for_event_returns_none_without_a_getter(self):
        assert routing.session_key_for_event(SimpleNamespace(), object()) is None


class TestSessionDbSeam:
    def test_none_without_a_store(self, monkeypatch):
        assert _state(monkeypatch)._session_db() is None

    def test_none_when_the_store_exposes_no_db(self, monkeypatch):
        state = _state(monkeypatch, _session_store=object())
        assert state._session_db() is None

    def test_returns_the_handles_db(self, monkeypatch):
        db = object()
        state = _state(monkeypatch, _session_store=SimpleNamespace(_db=db))
        assert state._session_db() is db


class TestApplyTopicDeletion:
    @staticmethod
    def _ready(monkeypatch, tmp_path, *, sdk_call):
        state = _state(monkeypatch, topic_sessions=True, tmp_path=tmp_path)
        state._adapter._sdk_call = sdk_call
        state._adapter.client = SimpleNamespace(get_stream_topics=lambda *a, **k: None)
        registry = _RecordingRegistry()
        state._conversations = registry
        return state, registry

    @pytest.mark.asyncio
    async def test_verification_raising_keeps_the_mapping(self, monkeypatch, tmp_path):
        async def boom(fn, *args, **kwargs):
            raise RuntimeError("nope")

        state, registry = self._ready(monkeypatch, tmp_path, sdk_call=boom)
        await state.apply_topic_deletion(1, "gone")
        assert registry.orphaned == []

    @pytest.mark.asyncio
    async def test_verification_unavailable_keeps_the_mapping(
        self, monkeypatch, tmp_path
    ):
        async def unavailable(fn, *args, **kwargs):
            return {"result": "error"}

        state, registry = self._ready(monkeypatch, tmp_path, sdk_call=unavailable)
        await state.apply_topic_deletion(1, "gone")
        assert registry.orphaned == []

    @pytest.mark.asyncio
    async def test_a_topic_that_still_exists_is_ignored(self, monkeypatch, tmp_path):
        async def still_there(fn, *args, **kwargs):
            return {"result": "success", "topics": [{"name": "still-here"}]}

        state, registry = self._ready(monkeypatch, tmp_path, sdk_call=still_there)
        await state.apply_topic_deletion(1, "still-here")
        assert registry.orphaned == []

    @pytest.mark.asyncio
    async def test_a_confirmed_deletion_orphans_the_session_set(
        self, monkeypatch, tmp_path
    ):
        async def gone(fn, *args, **kwargs):
            return {"result": "success", "topics": []}

        state, registry = self._ready(monkeypatch, tmp_path, sdk_call=gone)
        await state.apply_topic_deletion(1, "gone")
        assert registry.orphaned == [(1, "gone")]

    @pytest.mark.asyncio
    async def test_a_non_dict_result_keeps_the_mapping(self, monkeypatch, tmp_path):
        async def weird(fn, *args, **kwargs):
            return ["not", "a", "dict"]

        state, registry = self._ready(monkeypatch, tmp_path, sdk_call=weird)
        await state.apply_topic_deletion(1, "gone")
        assert registry.orphaned == []
