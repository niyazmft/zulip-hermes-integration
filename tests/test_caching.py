"""Tests for performance caching: client cache + target cache (Issue #49)."""

import pytest
from unittest.mock import patch

from zulip.adapter import (
    _client_cache,
    _target_cache,
    _get_cached_client,
    _parse_target,
    _set_cached_target,
    _clear_caches,
    _MAX_CLIENT_CACHE,
    _MAX_TARGET_CACHE,
)


class TestClientCache:
    def teardown_method(self):
        _clear_caches()

    def test_creates_new_client(self, monkeypatch):
        """Cache miss creates a new client."""
        import zulip.adapter as adapter_module

        class FakeClient:
            def __init__(self, **kwargs):
                self._init_kwargs = kwargs

        class FakeZulipModule:
            Client = FakeClient

        client = _get_cached_client(
            "https://test.com", "a@test.com", "key1",
            _zulip_mod=FakeZulipModule()
        )
        assert client._init_kwargs["email"] == "a@test.com"
        assert client._init_kwargs["api_key"] == "key1"

    def test_returns_cached_client(self, monkeypatch):
        """Same credentials return the same instance."""

        class FakeClient:
            def __init__(self, **kwargs):
                pass

        class FakeZulipModule:
            Client = FakeClient

        mod = FakeZulipModule()
        c1 = _get_cached_client("https://test.com", "a@test.com", "key1", _zulip_mod=mod)
        c2 = _get_cached_client("https://test.com", "a@test.com", "key1", _zulip_mod=mod)
        assert c1 is c2

    def test_different_credentials_create_new_client(self, monkeypatch):
        """Different credentials create different instances."""

        class FakeClient:
            def __init__(self, **kwargs):
                pass

        class FakeZulipModule:
            Client = FakeClient

        mod = FakeZulipModule()
        c1 = _get_cached_client("https://test.com", "a@test.com", "key1", _zulip_mod=mod)
        c2 = _get_cached_client("https://test.com", "a@test.com", "key2", _zulip_mod=mod)
        assert c1 is not c2

    def test_lru_eviction(self, monkeypatch):
        """Oldest client is evicted when cache is full."""

        class FakeClient:
            instances = 0
            def __init__(self, **kwargs):
                FakeClient.instances += 1
                self.id = FakeClient.instances

        class FakeZulipModule:
            Client = FakeClient

        mod = FakeZulipModule()
        _clear_caches()

        # Fill cache to max
        for i in range(_MAX_CLIENT_CACHE + 5):
            _get_cached_client(f"https://site{i}.com", "a@test.com", "key", _zulip_mod=mod)

        # Only MAX_CLIENT_CACHE should remain
        assert len(_client_cache) == _MAX_CLIENT_CACHE

    def test_access_moves_to_front(self, monkeypatch):
        """Accessing a client moves it to the front (most-recently-used)."""

        class FakeClient:
            def __init__(self, **kwargs):
                pass

        class FakeZulipModule:
            Client = FakeClient

        mod = FakeZulipModule()
        _clear_caches()

        _get_cached_client("https://a.com", "a@test.com", "key", _zulip_mod=mod)
        _get_cached_client("https://b.com", "a@test.com", "key", _zulip_mod=mod)

        # Access a.com again (MRU)
        _get_cached_client("https://a.com", "a@test.com", "key", _zulip_mod=mod)

        # Oldest is now b.com
        oldest = next(iter(_client_cache))
        assert "b.com" in oldest


class TestTargetCache:
    def teardown_method(self):
        _clear_caches()

    def test_parse_dm_target(self):
        info = _parse_target("dm:42")
        assert info["type"] == "dm"
        assert info["user_id"] == 42

    def test_parse_stream_target(self):
        info = _parse_target("573423")
        assert info["type"] == "stream"
        assert info["stream_id"] == 573423

    def test_dm_cache_hit(self):
        _parse_target("dm:99")
        cached = _parse_target("dm:99")
        assert cached["type"] == "dm"
        assert cached["user_id"] == 99

    def test_stream_cache_hit(self):
        _parse_target("12345")
        cached = _parse_target("12345")
        assert cached["type"] == "stream"
        assert cached["stream_id"] == 12345

    def test_target_cache_lru_eviction(self):
        _clear_caches()
        for i in range(_MAX_TARGET_CACHE + 5):
            _set_cached_target(f"dm:{i}", {"type": "dm", "user_id": i})

        assert len(_target_cache) == _MAX_TARGET_CACHE

    def test_target_access_moves_to_front(self):
        _clear_caches()
        _set_cached_target("dm:1", {"type": "dm", "user_id": 1})
        _set_cached_target("dm:2", {"type": "dm", "user_id": 2})

        # Access dm:1 (MRU)
        _parse_target("dm:1")

        # Oldest is now dm:2
        oldest = next(iter(_target_cache))
        assert "dm:2" in oldest

    def test_parse_dm_with_colon_in_id(self):
        """Edge case: user_id with colon-like format should still parse."""
        with pytest.raises(ValueError):
            _parse_target("dm:invalid")


class TestCacheClear:
    def test_clear_caches(self):
        class FakeClient:
            def __init__(self, **kwargs):
                pass

        class FakeZulipModule:
            Client = FakeClient

        mod = FakeZulipModule()
        _get_cached_client("https://x.com", "a@test.com", "key", _zulip_mod=mod)
        _set_cached_target("dm:1", {"type": "dm", "user_id": 1})

        assert len(_client_cache) > 0
        assert len(_target_cache) > 0

        _clear_caches()

        assert len(_client_cache) == 0
        assert len(_target_cache) == 0
