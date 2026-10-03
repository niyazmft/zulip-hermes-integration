"""Patch-ownership pins for the Wave 8 extraction (``platform_api`` + ``cli``).

The rule (learned in Wave 5, re-applied in Wave 7): the OWNING module is the
module that DEFINES a name, not the module that CALLS it, and the production
read and the test patch must point at the SAME module. A ``patch.object`` aimed
at the caller otherwise leaves the test green while exercising production: a
silently dead test.

Wave 8 moved

* Zulip's ``BasePlatformAdapter`` data surface into ``zulip.platform_api``
  (the ten methods stay overrides on ``ZulipAdapter`` and delegate), and
* the standalone / CLI concern into ``zulip.cli`` (re-exported from
  ``zulip.adapter`` under its historical private names),

and migrated ``_get_cached_client`` off its adapter binding onto the module
that defines it, ``zulip.zulip_client``. ``adapter._get_cached_client`` is gone
on purpose, so a stale patch raises ``AttributeError`` instead of quietly
no-oping — the same treatment ``render_refs`` / ``upload_file_to_zulip`` got in
Wave 5 and ``probe_zulip`` / ``handle_command`` in Wave 7.

These tests fail loudly if any of that regresses.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import zulip.adapter as adapter_module
import zulip.cli as cli_module
import zulip.platform_api as platform_api_module
import zulip.zulip_client as zulip_client_module

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "zulip"

PLATFORM_API_METHODS = (
    "get_chat_info",
    "resolve_topic",
    "fetch_messages",
    "search_messages",
    "list_streams",
    "subscribe_stream",
    "delete_message",
    "get_user_presence",
    "star_message",
    "get_user_info",
)

# The ``BasePlatformAdapter`` contract as it was before the extraction.
EXPECTED_PARAMETERS = {
    "get_chat_info": ["self", "chat_id"],
    "resolve_topic": ["self", "stream_id", "topic"],
    "fetch_messages": ["self", "stream", "topic", "limit"],
    "search_messages": ["self", "query", "stream", "topic", "limit"],
    "list_streams": ["self"],
    "subscribe_stream": ["self", "stream_name"],
    "delete_message": ["self", "message_id"],
    "get_user_presence": ["self", "user_id_or_email"],
    "star_message": ["self", "message_id", "starred"],
    "get_user_info": ["self", "user_id_or_email"],
}

# Call arguments, and (implicitly) the exact positional forwarding the
# delegate performs — every parameter is passed through, defaults included.
DELEGATE_ARGS = {
    "get_chat_info": ("573423",),
    "resolve_topic": (42, "deploy-issue"),
    "fetch_messages": ("general", None, 50),
    "search_messages": ("q", None, None, 50),
    "list_streams": (),
    "subscribe_stream": ("general",),
    "delete_message": (1,),
    "get_user_presence": ("7",),
    "star_message": (1, True),
    "get_user_info": ("7",),
}

_STANDALONE_ENV = {
    "ZULIP_SITE": "https://zulip.example.test",
    "ZULIP_EMAIL": "bot@example.test",
    "ZULIP_API_KEY": "k" * 32,
}

_EXPECTED_PLATFORM_HINT = (
    "You are chatting via Zulip. Messages are organized into streams and topics. "
    "When replying to a stream message, preserve the original topic unless asked to change it."
)


class TestGetCachedClientIsOwnedByZulipClient:
    def test_zulip_client_defines_it(self):
        assert (
            zulip_client_module.get_cached_client.__module__
            == "zulip.zulip_client"
        )

    def test_not_reachable_through_adapter(self):
        assert not hasattr(adapter_module, "_get_cached_client")

    def test_a_stale_adapter_patch_fails_loudly(self):
        """The negative control: this is what a missed migration looks like."""
        with pytest.raises(AttributeError):
            with patch.object(adapter_module, "_get_cached_client"):
                pass

    def test_adapter_init_reads_the_owning_module(self, mock_platform_config, monkeypatch):
        class _FakeSDK:
            pass

        sentinel = MagicMock()
        monkeypatch.setattr(zulip_client_module, "import_zulip_sdk", lambda: _FakeSDK)
        with patch.object(
            zulip_client_module, "get_cached_client", return_value=sentinel
        ) as get_client:
            from zulip.adapter import ZulipAdapter

            adapter = ZulipAdapter(mock_platform_config)

        assert adapter.client is sentinel
        assert get_client.call_args.args[:3] == (
            "https://test.zulipchat.com",
            "bot@test.zulipchat.com",
            "fake-key",
        )


class TestStandaloneSendReadsTheOwningModule:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        # Keep the delivery audit log (#145) out of ~/.hermes.
        monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
        for key in _STANDALONE_ENV:
            monkeypatch.setenv(key, _STANDALONE_ENV[key])
        yield

    @pytest.mark.asyncio
    async def test_a_patch_of_the_owner_drives_the_moved_sender(self):
        sentinel = MagicMock(spec_set=["send_message"])
        sentinel.send_message.return_value = {"result": "success", "id": 4242}

        with patch.object(
            zulip_client_module, "get_cached_client", return_value=sentinel
        ) as get_client:
            result = await cli_module.standalone_send(
                SimpleNamespace(extra={}), "20", "weekly report"
            )

        assert result == {"success": True, "message_id": "4242"}
        get_client.assert_called_once_with(
            _STANDALONE_ENV["ZULIP_SITE"],
            _STANDALONE_ENV["ZULIP_EMAIL"],
            _STANDALONE_ENV["ZULIP_API_KEY"],
        )
        assert sentinel.send_message.call_args[0][0]["topic"] == "general"


class TestPlatformApiOwnsTheDataSurface:
    @pytest.mark.parametrize("name", PLATFORM_API_METHODS)
    def test_owner_module_defines_it(self, name):
        assert getattr(platform_api_module, name).__module__ == "zulip.platform_api"

    @pytest.mark.parametrize("name", PLATFORM_API_METHODS)
    @pytest.mark.asyncio
    async def test_adapter_method_delegates_to_it(self, name):
        sentinel = object()
        with patch.object(
            platform_api_module, name, new=AsyncMock(return_value=sentinel)
        ) as delegate:
            result = await getattr(adapter_module.ZulipAdapter, name)(
                object(), *DELEGATE_ARGS[name]
            )

        assert result is sentinel
        assert delegate.await_args.args[1:] == DELEGATE_ARGS[name]

    @pytest.mark.parametrize("name", PLATFORM_API_METHODS)
    def test_the_owner_function_takes_the_adapter_first(self, name):
        params = list(inspect.signature(getattr(platform_api_module, name)).parameters)
        assert params[0] == "adapter"

    @pytest.mark.parametrize("name", PLATFORM_API_METHODS)
    def test_host_contract_signature_unchanged(self, name):
        method = getattr(adapter_module.ZulipAdapter, name)
        assert inspect.iscoroutinefunction(method)
        params = list(inspect.signature(method).parameters.values())
        assert [p.name for p in params] == EXPECTED_PARAMETERS[name]
        assert all(p.default is p.empty for p in params[:2]), name

    def test_optional_parameters_keep_their_defaults(self):
        fetch = inspect.signature(adapter_module.ZulipAdapter.fetch_messages).parameters
        assert fetch["topic"].default is None
        assert fetch["limit"].default == 50
        search = inspect.signature(adapter_module.ZulipAdapter.search_messages).parameters
        assert (search["stream"].default, search["topic"].default) == (None, None)
        assert search["limit"].default == 50
        assert inspect.signature(adapter_module.ZulipAdapter.star_message).parameters[
            "starred"
        ].default is True


class TestCliOwnsTheStandaloneConcern:
    def test_cli_defines_the_public_names(self):
        for name in (
            "check_requirements",
            "validate_config",
            "env_enablement",
            "interactive_setup",
            "resolve_standalone_credentials",
            "standalone_send",
            "STANDALONE_DEFAULT_TOPIC",
        ):
            assert hasattr(cli_module, name), name
        assert cli_module.standalone_send.__module__ == "zulip.cli"

    def test_adapter_re_exports_under_the_historical_names(self):
        assert adapter_module._standalone_send is cli_module.standalone_send
        assert adapter_module._env_enablement is cli_module.env_enablement
        assert adapter_module.check_requirements is cli_module.check_requirements
        assert adapter_module.validate_config is cli_module.validate_config
        assert adapter_module.interactive_setup is cli_module.interactive_setup
        assert (
            adapter_module.STANDALONE_DEFAULT_TOPIC
            == cli_module.STANDALONE_DEFAULT_TOPIC
            == "general"
        )

    def test_validate_string_length_moved_with_the_surface(self):
        assert not hasattr(adapter_module, "_validate_string_length")
        assert callable(platform_api_module._validate_string_length)

    def test_neither_new_module_imports_the_adapter(self):
        """A relative ``adapter`` import here would be a cycle."""
        for name in ("platform_api.py", "cli.py"):
            imports = [
                line
                for line in (PLUGIN_DIR / name).read_text(encoding="utf-8").splitlines()
                if line.startswith(("import ", "from "))
            ]
            assert not [line for line in imports if "adapter" in line], name


class TestRegisterKwargsUnchanged:
    def _register_kwargs(self):
        calls = []
        ctx = SimpleNamespace(
            register_platform=lambda **kw: calls.append(kw),
            register_hook=lambda *a, **k: None,
            register_tool=lambda **kw: None,
        )
        adapter_module.register(ctx)
        assert len(calls) == 1
        return calls[0]

    def test_exact_kwarg_set(self):
        assert set(self._register_kwargs()) == {
            "name",
            "label",
            "adapter_factory",
            "check_fn",
            "validate_config",
            "required_env",
            "install_hint",
            "env_enablement_fn",
            "allowed_users_env",
            "allow_all_env",
            "cron_deliver_env_var",
            "standalone_sender_fn",
            "max_message_length",
            "platform_hint",
            "emoji",
            "setup_fn",
        }

    def test_plain_values_unchanged(self):
        kw = self._register_kwargs()
        assert kw["name"] == "zulip"
        assert kw["label"] == "Zulip"
        assert kw["required_env"] == ["ZULIP_API_KEY", "ZULIP_EMAIL", "ZULIP_SITE"]
        assert kw["install_hint"] == "pip install zulip"
        assert kw["allowed_users_env"] == "ZULIP_ALLOWED_USERS"
        assert kw["allow_all_env"] == "ZULIP_ALLOW_ALL_USERS"
        assert kw["cron_deliver_env_var"] == "ZULIP_HOME_CHANNEL"
        assert kw["max_message_length"] == 10000
        assert kw["emoji"] == "📬"
        assert kw["platform_hint"] == _EXPECTED_PLATFORM_HINT

    def test_callables_are_the_live_objects(self):
        kw = self._register_kwargs()
        assert callable(kw["adapter_factory"])
        assert kw["check_fn"] is cli_module.check_requirements
        assert kw["validate_config"] is cli_module.validate_config
        assert kw["env_enablement_fn"] is cli_module.env_enablement
        assert kw["setup_fn"] is cli_module.interactive_setup
        # The host calls this from another process: it must be the real
        # coroutine function, not a stub bound at registration time.
        assert kw["standalone_sender_fn"] is cli_module.standalone_send
        assert inspect.iscoroutinefunction(kw["standalone_sender_fn"])
