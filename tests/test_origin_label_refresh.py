"""Origin-label refresh — host workaround for hermes-agent#142402.

The host admits a background completion only when the source rebuilt from the
session store's ``origin`` equals the route's last committed envelope
(``gateway/session_automation.py::_route_identity`` compares the whole dict).
``origin`` is written once, at session creation, so a label the adapter starts
stamping later (``chat_topic``) or one that changes (topic/stream rename,
display name) never reaches it and every background result for that session is
refused forever with ``admission_conflict``.

These tests pin the contract of the refresh that closes the gap: labels only,
same lane only, idempotent, best-effort.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock

import pytest

import zulip.routing as routing_module
import zulip.zulip_client as zulip_client_module
from tests.conftest import MockZulipClient

ZULIP = SimpleNamespace(value="zulip")


@dataclass
class _Source:
    """Stand-in for ``gateway.session.SessionSource`` (only what we read)."""

    chat_id: str = ""
    chat_name: str = ""
    chat_type: str = ""
    chat_topic: str = ""
    user_id: str = ""
    user_name: str = ""
    thread_id: str = ""
    platform: object = field(default_factory=lambda: ZULIP)


@dataclass
class _Entry:
    """Stand-in for ``gateway.session.SessionEntry`` (routing index row)."""

    session_key: str
    session_id: str
    origin: object = None
    updated_at: datetime = field(
        default_factory=lambda: datetime(2026, 10, 20, 12, 53, tzinfo=timezone.utc)
    )


class _Store:
    """Fake session store exposing the internals the refresh touches."""

    def __init__(self, entries=None, single_row_saves=True):
        self._entries = dict(entries or {})
        self._lock = threading.Lock()
        self.save_calls = 0
        self.saved_keys = []
        if not single_row_saves:
            # Older host: only the whole-file save exists.
            self._save_entry = None

    def _save(self):
        self.save_calls += 1

    def _save_entry(self, session_key):
        self.saved_keys.append(session_key)

    def list_sessions(self, active_minutes=None):
        return list(self._entries.values())


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    """ZulipAdapter with stable topic sessions on and an isolated data dir."""
    monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
    monkeypatch.setenv("ZULIP_TOPIC_SESSIONS", "true")
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                self._client = MockZulipClient(**kwargs)

            def __getattr__(self, name):
                return getattr(self._client, name)

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())

    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(mock_platform_config)
    a.email = "bot@zulip.com"
    a.handle_message = AsyncMock()
    return a


def _stream_entry(key, conv, topic=None, stream_name="R241", session_id="s1"):
    return _Entry(
        session_key=key,
        session_id=session_id,
        origin=_Source(
            chat_id="645308",
            chat_name=stream_name,
            chat_type="stream",
            chat_topic=topic,
            user_id="user@example.com",
            user_name="Aung",
            thread_id=conv,
        ),
    )


def _wire(adapter, store):
    """Attach a store without running the startup hooks (unit scope)."""
    adapter._session_store = store
    return store


class TestStartupBackfill:
    def test_fills_missing_topic_from_registry(self, adapter):
        conv = adapter._conversations.resolve(645308, "Exp1+Exp2+Exp3")
        key = f"agent:main:zulip:stream:645308:{conv}"
        store = _wire(adapter, _Store({key: _stream_entry(key, conv, topic=None)}))

        assert adapter._refresh_session_origin_labels() == 1

        origin = store._entries[key].origin
        assert origin.chat_topic == "Exp1+Exp2+Exp3"
        # Labels only: the routing fields are byte-identical.
        assert origin.chat_id == "645308"
        assert origin.thread_id == conv
        assert origin.user_id == "user@example.com"
        assert origin.chat_type == "stream"
        assert store.saved_keys == [key]

    def test_is_idempotent_and_does_not_save_again(self, adapter):
        conv = adapter._conversations.resolve(645308, "Topic A")
        key = f"agent:main:zulip:stream:645308:{conv}"
        store = _wire(adapter, _Store({key: _stream_entry(key, conv, topic="Topic A")}))

        assert adapter._refresh_session_origin_labels() == 0
        assert store.saved_keys == []

    def test_unknown_conversation_is_left_alone(self, adapter):
        unknown = "c0123456789ab"
        key = f"agent:main:zulip:stream:645308:{unknown}"
        store = _wire(adapter, _Store({key: _stream_entry(key, unknown, topic=None)}))

        assert adapter._refresh_session_origin_labels() == 0
        assert store._entries[key].origin.chat_topic is None

    def test_dm_entries_are_skipped_by_the_backfill(self, adapter):
        key = "agent:main:zulip:dm:dm:1068796"
        entry = _Entry(
            session_key=key,
            session_id="s-dm",
            origin=_Source(chat_id="dm:1068796", chat_name="Aung", chat_type="dm"),
        )
        store = _wire(adapter, _Store({key: entry}))

        assert adapter._refresh_session_origin_labels() == 0
        assert store._entries[key].origin.chat_name == "Aung"

    def test_falls_back_to_whole_file_save_on_an_older_store(self, adapter):
        conv = adapter._conversations.resolve(645308, "Topic A")
        key = f"agent:main:zulip:stream:645308:{conv}"
        store = _wire(
            adapter,
            _Store({key: _stream_entry(key, conv, topic=None)}, single_row_saves=False),
        )

        assert adapter._refresh_session_origin_labels() == 1
        assert store.save_calls == 1


class TestPerMessageRefresh:
    def test_updates_a_renamed_topic(self, adapter):
        conv = "c0123456789ab"
        key = f"agent:main:zulip:stream:645308:{conv}"
        store = _wire(adapter, _Store({key: _stream_entry(key, conv, topic="Old name")}))
        source = _Source(
            chat_id="645308",
            chat_name="R241",
            chat_type="stream",
            chat_topic="New name",
            user_id="user@example.com",
            user_name="Aung",
            thread_id=conv,
        )

        assert adapter._refresh_session_origin_labels(source) == 1
        assert store._entries[key].origin.chat_topic == "New name"

    def test_updates_stream_name_but_not_the_speaker_name_on_streams(self, adapter):
        conv = "c0123456789ab"
        key = f"agent:main:zulip:stream:645308:{conv}"
        store = _wire(adapter, _Store({key: _stream_entry(key, conv, topic="T")}))
        source = _Source(
            chat_id="645308",
            chat_name="R241-renamed",
            chat_type="stream",
            chat_topic="T",
            user_id="someone-else@example.com",
            user_name="Someone Else",
            thread_id=conv,
        )

        assert adapter._refresh_session_origin_labels(source) == 1
        origin = store._entries[key].origin
        assert origin.chat_name == "R241-renamed"
        # A shared topic's origin keeps the creator's identity: copying the
        # latest speaker's name would mix it with a foreign user_id.
        assert origin.user_name == "Aung"
        assert origin.user_id == "user@example.com"

    def test_dm_refreshes_the_display_name(self, adapter):
        key = "agent:main:zulip:dm:dm:1068796"
        entry = _Entry(
            session_key=key,
            session_id="s-dm",
            origin=_Source(
                chat_id="dm:1068796", chat_name="Old Name", chat_type="dm",
                user_name="Old Name", user_id="user@example.com",
            ),
        )
        store = _wire(adapter, _Store({key: entry}))
        source = _Source(
            chat_id="dm:1068796", chat_name="New Name", chat_type="dm",
            user_name="New Name", user_id="user@example.com",
        )

        assert adapter._refresh_session_origin_labels(source) == 1
        origin = store._entries[key].origin
        assert origin.user_name == "New Name"
        assert origin.chat_name == "New Name"

    def test_other_lane_is_never_touched(self, adapter):
        conv = "c0123456789ab"
        key = f"agent:main:zulip:stream:645308:{conv}"
        store = _wire(adapter, _Store({key: _stream_entry(key, conv, topic="Mine")}))
        other_topic = _Source(
            chat_id="645308", chat_name="R241", chat_type="stream",
            chat_topic="Someone else's topic", user_name="Aung",
            thread_id="cffffffffffff",
        )
        other_stream = _Source(
            chat_id="999999", chat_name="Other", chat_type="stream",
            chat_topic="x", user_name="Aung", thread_id=conv,
        )

        assert adapter._refresh_session_origin_labels(other_topic) == 0
        assert adapter._refresh_session_origin_labels(other_stream) == 0
        assert store._entries[key].origin.chat_topic == "Mine"

    def test_per_user_suffixed_entries_of_the_lane_are_all_refreshed(self, adapter):
        conv = "c0123456789ab"
        keys = [
            f"agent:main:zulip:stream:645308:{conv}",
            f"agent:main:zulip:stream:645308:{conv}:user@example.com",
        ]
        store = _wire(
            adapter,
            _Store({key: _stream_entry(key, conv, topic="Old") for key in keys}),
        )
        source = _Source(
            chat_id="645308", chat_name="R241", chat_type="stream",
            chat_topic="New", user_name="Aung", thread_id=conv,
        )

        assert adapter._refresh_session_origin_labels(source) == 2
        assert [store._entries[k].origin.chat_topic for k in keys] == ["New", "New"]

    def test_routing_fields_and_activity_clock_are_untouched(self, adapter):
        conv = "c0123456789ab"
        key = f"agent:main:zulip:stream:645308:{conv}"
        entry = _stream_entry(key, conv, topic="Old")
        store = _wire(adapter, _Store({key: entry}))
        before = store._entries[key]
        source = _Source(
            chat_id="645308", chat_name="R241", chat_type="stream",
            chat_topic="New", user_name="Aung", thread_id=conv,
        )

        adapter._refresh_session_origin_labels(source)

        after = store._entries[key]
        assert after.session_id == before.session_id
        assert after.session_key == before.session_key
        assert after.updated_at == before.updated_at


class TestFailureModes:
    def test_flag_off_writes_nothing(self, adapter, monkeypatch):
        monkeypatch.setattr(routing_module, "_origin_label_refresh_enabled", lambda: False)
        conv = adapter._conversations.resolve(645308, "Topic A")
        key = f"agent:main:zulip:stream:645308:{conv}"
        store = _wire(adapter, _Store({key: _stream_entry(key, conv, topic=None)}))

        assert adapter._refresh_session_origin_labels() == 0
        assert store._entries[key].origin.chat_topic is None

    def test_store_without_internals_is_a_noop(self, adapter):
        adapter._session_store = object()
        assert adapter._refresh_session_origin_labels() == 0

    def test_delegate_contains_a_routing_failure(self, adapter, monkeypatch):
        def boom(_source=None):
            raise RuntimeError("store shape changed")

        monkeypatch.setattr(adapter._routing, "refresh_origin_labels", boom)
        assert adapter._refresh_session_origin_labels(_Source()) == 0

    def test_a_failing_save_is_a_noop_not_an_error(self, adapter):
        conv = adapter._conversations.resolve(645308, "Topic A")
        key = f"agent:main:zulip:stream:645308:{conv}"
        store = _wire(adapter, _Store({key: _stream_entry(key, conv, topic=None)}))

        def fail(_key):
            raise OSError("disk full")

        store._save_entry = fail
        assert adapter._refresh_session_origin_labels() == 0


class TestBackfillScheduling:
    def test_backfill_runs_on_the_first_message_when_wiring_was_early(self, adapter):
        # The store's routing index can still be empty when set_session_store
        # runs; the first inbound message must then carry the backfill for
        # every lane, not just its own.
        conv_a = adapter._conversations.resolve(645308, "Topic A")
        conv_b = "c0123456789ab"
        key_a = f"agent:main:zulip:stream:645308:{conv_a}"
        key_b = f"agent:main:zulip:stream:645308:{conv_b}"
        store = _wire(
            adapter,
            _Store({
                key_a: _stream_entry(key_a, conv_a, topic=None),
                key_b: _stream_entry(key_b, conv_b, topic="Other"),
            }),
        )
        source = _Source(
            chat_id="645308", chat_name="R241", chat_type="stream",
            chat_topic="Other", user_name="Aung", thread_id=conv_b,
        )

        assert adapter._refresh_session_origin_labels(source) == 1
        assert store._entries[key_a].origin.chat_topic == "Topic A"

    def test_backfill_does_not_repeat_on_later_messages(self, adapter):
        conv = adapter._conversations.resolve(645308, "Topic A")
        key = f"agent:main:zulip:stream:645308:{conv}"
        store = _wire(adapter, _Store({key: _stream_entry(key, conv, topic=None)}))
        first = _Source(
            chat_id="645308", chat_name="R241", chat_type="stream",
            chat_topic="Topic A", user_name="Aung", thread_id=conv,
        )
        assert adapter._refresh_session_origin_labels(first) == 1

        consulted = []
        adapter._conversations.current_name = lambda *a: consulted.append(a) or "x"
        renamed = _Source(
            chat_id="645308", chat_name="R241", chat_type="stream",
            chat_topic="Renamed", user_name="Aung", thread_id=conv,
        )
        assert adapter._refresh_session_origin_labels(renamed) == 1
        assert consulted == []

    def test_lazy_store_is_loaded_before_the_entries_are_read(self, adapter):
        conv = adapter._conversations.resolve(645308, "Topic A")
        key = f"agent:main:zulip:stream:645308:{conv}"

        class LazyStore(_Store):
            def __init__(self, entries):
                super().__init__({})
                self._pending = dict(entries)
                self._loaded = False
                self.load_calls = 0

            def _ensure_loaded(self):
                self.load_calls += 1
                if not self._loaded:
                    self._entries = dict(self._pending)
                    self._loaded = True

        store = _wire(adapter, LazyStore({key: _stream_entry(key, conv, topic=None)}))
        assert store._entries == {}

        assert adapter._refresh_session_origin_labels() == 1
        assert store.load_calls == 1
        assert store._entries[key].origin.chat_topic == "Topic A"
