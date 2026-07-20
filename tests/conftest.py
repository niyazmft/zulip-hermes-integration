"""pytest fixtures for zulip-hermes-integration."""

import sys
from pathlib import Path

# Inject test stubs into sys.path so gateway.* imports resolve
STUBS_DIR = Path(__file__).parent / "stubs"
sys.path.insert(0, str(STUBS_DIR))

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

    def upload_file(self, file):
        uri = f"/user_uploads/{len(self._uploads)}"
        self._uploads.append({"uri": uri, "file": file})
        return {"result": "success", "uri": uri}

    def inject_event(self, event):
        """Helper: queue an event for get_events to return."""
        self._events.append(event)

    def inject_message(self, message_dict):
        """Helper: queue a message event."""
        self.inject_event({"id": self._last_event_id + 1, "type": "message", "message": message_dict})
        self._last_event_id += 1


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
