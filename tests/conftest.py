"""pytest fixtures for zulip-hermes-integration."""

import os
import sys
import tempfile
from pathlib import Path

# Inject test stubs into sys.path so gateway.* imports resolve
STUBS_DIR = Path(__file__).parent / "stubs"
sys.path.insert(0, str(STUBS_DIR))

# ---------------------------------------------------------------------------
# Pin the profile data dir *at import time*, before any fixture can run (#243).
#
# ``runtime_scope.get_profile_data_dir()`` falls back to the developer's real
# ``~/.hermes`` when ``HERMES_DATA_DIR`` is unset, and the adapter hands that
# directory to things that write: the audit logger, the display-name cache, the
# dedupe store, the queue manager and the policy engine.
#
# A function-scoped autouse fixture is NOT enough. pytest instantiates
# higher-scoped fixtures first, so a class- or module-scoped fixture that builds
# an adapter runs *before* any function-scoped fixture has set the variable --
# which is exactly how ``~/.hermes/cache/zulip_display_names.json`` still appeared
# after the per-test fixture was added. Setting it here covers every scope.
# The autouse fixture below then narrows it to a per-test directory.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="zulip-hermes-tests-")
os.environ["HERMES_DATA_DIR"] = _TEST_DATA_DIR

import pytest
from unittest.mock import MagicMock


class MockZulipClient:
    """Drop-in mock for zulip.Client that simulates all SDK methods."""

    def __init__(self, email="test@zulip.com", api_key="fake-key", site="https://test.zulipchat.com"):
        self.email = email
        self.api_key = api_key
        self.site = site
        self._queue_id = "queue_123"
        self._last_event_id = 1
        self._members = {"result": "success", "members": []}
        self._profile = {"result": "success", "full_name": "Test Bot"}
        self._server_settings = {"result": "success", "zulip_version": "8.0"}
        self._subscriptions = {"result": "success", "subscriptions": []}
        self._events = []
        self._sent_messages = []
        self._reactions = []
        self._uploads = []
        # stream_id -> [topic names] for get_stream_topics (R10 deletion
        # verification). UNCONFIGURED streams return an error result — the
        # adapter's fail-open path keeps the mapping (safe default).
        self.stream_topics = {}

    def get_server_settings(self):
        return self._server_settings

    def get_profile(self):
        return self._profile

    def get_subscriptions(self):
        return self._subscriptions

    def get_members(self):
        return self._members

    def register(self, event_types=None, fetch_event_id=0, **kwargs):
        return {
            "result": "success",
            "queue_id": self._queue_id,
            "last_event_id": self._last_event_id,
        }

    def get_events(self, queue_id, last_event_id, **kwargs):
        events = self._events
        self._events = []  # clear after read
        return {"result": "success", "events": events}

    def send_message(self, request):
        msg_id = len(self._sent_messages) + 1000
        self._sent_messages.append({"id": msg_id, **request})
        return {"result": "success", "id": msg_id}

    def add_reaction(self, request):
        self._reactions.append(request)
        return {"result": "success"}

    def remove_reaction(self, request):
        self._reactions = [r for r in self._reactions if r != request]
        return {"result": "success"}

    def set_typing_status(self, request):
        return {"result": "success"}

    def update_presence(self, request):
        return {"result": "success"}

    def update_message(self, request):
        return {"result": "success"}

    def update_message_flags(self, request):
        return {"result": "success"}

    def upload_file(self, file):
        uri = f"/user_uploads/{len(self._uploads)}"
        self._uploads.append({"uri": uri, "file": file})
        return {"result": "success", "uri": uri}

    def get_stream_topics(self, stream_id):
        names = self.stream_topics.get(stream_id)
        if names is None:
            return {"result": "error", "msg": "stream topics not configured"}
        return {"result": "success", "topics": [{"name": n} for n in names]}

    def inject_event(self, event):
        """Helper: queue an event for get_events to return."""
        self._events.append(event)

    def inject_message(self, message_dict):
        """Helper: queue a message event."""
        self.inject_event({"id": self._last_event_id + 1, "type": "message", "message": message_dict})
        self._last_event_id += 1


@pytest.fixture(autouse=True)
def _isolate_hermes_home(tmp_path, monkeypatch):
    """Give each test its own data dir, so state cannot leak between tests (#243).

    ``tests/conftest.py`` already pins a process-wide temporary root at import
    time (see the comment there) so nothing can reach the real ``~/.hermes`` at
    any fixture scope. This narrows it further to a per-test directory, because
    the audit log, dedupe store, queue and allowlist all persist to that dir and
    would otherwise accumulate across the session.

    Tests that deliberately exercise the fallback (``tests/test_runtime_scope.py``)
    clear the variable themselves, which overrides this.
    """
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear client and target caches between tests to ensure isolation."""
    from zulip.adapter import _clear_caches
    _clear_caches()
    yield



@pytest.fixture(autouse=True)
def _clear_live_adapters_registry():
    """Adapters self-register in a module-level WeakSet (tool-step
    attribution, activity traces). A host context can outlive its test and
    keep a stale adapter alive, whose session context then matches the NEXT
    test and consumes its step. Clearing the registry between tests keeps
    attribution deterministic regardless of test order."""
    yield
    import zulip.adapter as _adapter_module
    live = getattr(_adapter_module, "_LIVE_ADAPTERS", None)
    if live is not None:
        live.clear()

@pytest.fixture
def mock_zulip_client():
    return MockZulipClient()


@pytest.fixture
def mock_platform_config():
    """Minimal PlatformConfig-like object for adapter instantiation."""
    class FakeConfig:
        extra = {
            "api_key": "fake-key",
            "email": "bot@test.zulipchat.com",
            "site": "https://test.zulipchat.com",
        }

    return FakeConfig()
