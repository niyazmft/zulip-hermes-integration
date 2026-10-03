"""User-lookup resolution across zulip SDK shapes (issue #196).

``zulip`` 0.9.1 exposes ``get_user_by_id`` and ``call_endpoint`` but has **no**
``get_user`` method. Calling the missing one raised ``AttributeError``, which the
old ``get_user_info`` swallowed — so every reaction trigger was dropped, the
``/user`` command failed, and display-name cache misses silently returned
``None``. These tests pin the resolution for each SDK shape.
"""

import pytest

from zulip import admin_actions
from zulip.adapter import _user_lookup_call

USER = {
    "user_id": 1032616,
    "email": "user@test.zulipchat.com",
    "full_name": "Test User",
    "is_admin": False,
    "is_bot": False,
}


class Sdk091Client:
    """Mimics zulip 0.9.1: ``get_user_by_id`` + ``call_endpoint``, no ``get_user``."""

    def __init__(self, user=None):
        self.user = user if user is not None else USER
        self.calls = []

    def get_user_by_id(self, user_id, **request):
        self.calls.append(("get_user_by_id", user_id))
        return {"result": "success", "msg": "", "user": self.user}

    def call_endpoint(self, url=None, method="POST", request=None, **kwargs):
        self.calls.append(("call_endpoint", url, method))
        return {"result": "success", "msg": "", "user": self.user}


class LegacyClient:
    """A build that only exposes ``get_user`` (the method the plugin used to call)."""

    def __init__(self, user=None):
        self.user = user if user is not None else USER
        self.calls = []

    def get_user(self, user_id_or_email):
        self.calls.append(("get_user", user_id_or_email))
        return {"result": "success", "msg": "", "user": self.user}


class BareClient:
    """No user-lookup surface at all."""


class TestUserLookupCall:
    def test_numeric_id_prefers_get_user_by_id(self):
        client = Sdk091Client()
        fn, args = _user_lookup_call(client, "1032616")
        assert fn == client.get_user_by_id
        assert args == (1032616,)

    def test_email_falls_back_to_the_rest_endpoint(self):
        client = Sdk091Client()
        fn, args = _user_lookup_call(client, "user@test.zulipchat.com")
        assert args == ()
        assert fn()["result"] == "success"
        # /users/{value} accepts an email, which get_user_by_id cannot express.
        assert client.calls == [("call_endpoint", "users/user@test.zulipchat.com", "GET")]

    def test_legacy_get_user_still_works_when_nothing_else_exists(self):
        client = LegacyClient()
        fn, args = _user_lookup_call(client, "user@test.zulipchat.com")
        assert fn == client.get_user
        assert args == ("user@test.zulipchat.com",)

    def test_no_supported_lookup_returns_none_instead_of_raising(self):
        assert _user_lookup_call(BareClient(), "1032616") == (None, ())

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_empty_input_is_not_a_lookup(self, value):
        assert _user_lookup_call(Sdk091Client(), value) == (None, ())

    def test_regression_196_client_has_no_get_user_attribute(self):
        """The exact shape that broke #183: 0.9.1 has no ``get_user``."""
        client = Sdk091Client()
        assert not hasattr(client, "get_user")
        fn, args = _user_lookup_call(client, "1032616")
        assert callable(fn)


class TestAdminActionsGetUserInfo:
    @pytest.mark.asyncio
    async def test_by_numeric_id(self):
        client = Sdk091Client()
        info = await admin_actions.get_user_info(client, "1032616")
        assert info["email"] == "user@test.zulipchat.com"
        assert info["full_name"] == "Test User"
        assert client.calls == [("get_user_by_id", 1032616)]

    @pytest.mark.asyncio
    async def test_by_email_uses_the_endpoint(self):
        client = Sdk091Client()
        info = await admin_actions.get_user_info(client, "user@test.zulipchat.com")
        assert info["email"] == "user@test.zulipchat.com"
        assert any(call[0] == "call_endpoint" for call in client.calls)

    @pytest.mark.asyncio
    async def test_legacy_client_works(self):
        info = await admin_actions.get_user_info(LegacyClient(), "1032616")
        assert info["email"] == "user@test.zulipchat.com"

    @pytest.mark.asyncio
    async def test_no_lookup_surface_returns_none(self):
        assert await admin_actions.get_user_info(BareClient(), "1032616") is None

    @pytest.mark.asyncio
    async def test_tolerates_a_list_shaped_user(self):
        info = await admin_actions.get_user_info(Sdk091Client(user=[USER]), "1032616")
        assert info["email"] == "user@test.zulipchat.com"

    @pytest.mark.asyncio
    async def test_unsuccessful_result_returns_none(self):
        class Failing:
            def get_user_by_id(self, user_id, **request):
                return {"result": "error", "msg": "no such user"}

        assert await admin_actions.get_user_info(Failing(), "1") is None


def _adapter_with(monkeypatch, mock_platform_config, client):
    """Build a real adapter (construction uses the conftest mock) then swap in
    ``client`` so the lookup methods run against the SDK shape under test."""
    import zulip.zulip_client as zulip_client_module
    from tests.conftest import MockZulipClient

    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

    class FakeZulipModule:
        class Client:
            def __init__(self, **kwargs):
                self._client = MockZulipClient(**kwargs)

            def __getattr__(self, name):
                return getattr(self._client, name)

    monkeypatch.setattr(zulip_client_module, "zulip", FakeZulipModule())
    from zulip.adapter import ZulipAdapter

    adapter = ZulipAdapter(mock_platform_config)
    adapter.client = client
    return adapter


class TestAdapterUserLookup:
    @pytest.mark.asyncio
    async def test_get_user_info_with_sdk091_client(self, mock_platform_config, monkeypatch):
        adapter = _adapter_with(monkeypatch, mock_platform_config, Sdk091Client())
        info = await adapter.get_user_info("1032616")
        assert info["email"] == "user@test.zulipchat.com"

    @pytest.mark.asyncio
    async def test_get_user_info_without_any_lookup_returns_none(
        self, mock_platform_config, monkeypatch
    ):
        adapter = _adapter_with(monkeypatch, mock_platform_config, BareClient())
        assert await adapter.get_user_info("1032616") is None

    @pytest.mark.asyncio
    async def test_display_name_lookup_resolves_on_cache_miss(
        self, mock_platform_config, monkeypatch
    ):
        adapter = _adapter_with(monkeypatch, mock_platform_config, Sdk091Client())
        assert await adapter._fetch_display_name("1032616") == "Test User"

    @pytest.mark.asyncio
    async def test_display_name_lookup_is_none_without_a_surface(
        self, mock_platform_config, monkeypatch
    ):
        adapter = _adapter_with(monkeypatch, mock_platform_config, BareClient())
        assert await adapter._fetch_display_name("1032616") is None
