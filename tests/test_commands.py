"""Tests for admin command framework."""

import pytest
from unittest.mock import MagicMock, patch

from zulip.commands import (
    register_command,
    handle_command,
    is_command,
    _extract_command,
    _COMMANDS,
)


class TestCommandParsing:
    def test_extract_command_basic(self):
        assert _extract_command("/help") == ("help", "")

    def test_extract_command_with_args(self):
        assert _extract_command("/model gpt4") == ("model", "gpt4")

    def test_extract_command_with_spaces(self):
        assert _extract_command("/status all") == ("status", "all")

    def test_extract_command_case_insensitive(self):
        assert _extract_command("/HELP") == ("help", "")

    def test_extract_not_command(self):
        assert _extract_command("hello world") is None

    def test_extract_not_command_slash_in_middle(self):
        assert _extract_command("path/to/file") is None

    def test_is_command_true(self):
        assert is_command("/help") is True

    def test_is_command_false(self):
        assert is_command("hello") is False


class TestCommandRegistration:
    def test_register_and_run(self):
        # Clear any existing test command
        if "testcmd" in _COMMANDS:
            del _COMMANDS["testcmd"]

        @register_command("testcmd")
        def _handler(args, chat_id, sender_email, sender_name):
            return f"ok {args}"

        result = handle_command("/testcmd hello", "dm:1", "a@x.com", "Alice")
        assert result.handled is True
        assert result.reply == "ok hello"

        # Cleanup
        del _COMMANDS["testcmd"]

    def test_register_as_function_call(self):
        if "directcmd" in _COMMANDS:
            del _COMMANDS["directcmd"]

        def _handler(args, chat_id, sender_email, sender_name):
            return "direct"

        register_command("directcmd", _handler)
        result = handle_command("/directcmd", "dm:1", "a@x.com", "Alice")
        assert result.reply == "direct"

        del _COMMANDS["directcmd"]

    def test_unknown_command_falls_through(self):
        result = handle_command("/nonexistent xyz", "dm:1", "a@x.com", "Alice")
        assert result.handled is False
        assert result.reply == ""


class TestPluginCommands:
    def test_gateway_native_commands_fall_through(self):
        # Issue #190: /help, /status and /model belong to the gateway and must
        # NOT be handled by the plugin router (they would be shadowed otherwise).
        for cmd in ("/help", "/status", "/model", "/model gpt4", "/stop"):
            result = handle_command(cmd, "dm:1", "a@x.com", "Alice")
            assert result.handled is False, cmd
            assert result.reply == ""

    def test_streams_replies(self):
        result = handle_command("/streams", "dm:1", "a@x.com", "Alice")
        assert result.handled is True
        assert "Stream management" in result.reply

    def test_user_without_args_shows_usage(self):
        result = handle_command("/user", "dm:1", "a@x.com", "Alice")
        assert result.handled is True
        assert "Usage" in result.reply

    def test_user_with_args_replies(self):
        result = handle_command("/user someone@x.com", "dm:1", "a@x.com", "Alice")
        assert result.handled is True
        assert "someone@x.com" in result.reply

    def test_pin_and_unpin(self):
        assert handle_command("/pin 42", "dm:1", "a@x.com", "Alice").handled is True
        assert handle_command("/unpin 42", "dm:1", "a@x.com", "Alice").handled is True


class TestCommandErrorHandling:
    def test_handler_exception_returns_error(self):
        if "badcmd" in _COMMANDS:
            del _COMMANDS["badcmd"]

        @register_command("badcmd")
        def _bad(args, chat_id, sender_email, sender_name):
            raise RuntimeError("boom")

        result = handle_command("/badcmd", "dm:1", "a@x.com", "Alice")
        assert result.handled is True
        assert "Error processing /badcmd" in result.reply

        del _COMMANDS["badcmd"]


class TestAdapterIntegration:
    """Test that adapter properly intercepts commands."""

    @pytest.mark.asyncio
    async def test_command_bypasses_ai_dispatch(self, mock_platform_config, monkeypatch):
        import zulip.zulip_client as zulip_client_module
        from zulip.adapter import ZulipAdapter
        from tests.conftest import MockZulipClient

        monkeypatch.setenv("ZULIP_SITE", "https://test.zulipchat.com")
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key")
        monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = MockZulipClient(**kwargs)
                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())

        adapter = ZulipAdapter(mock_platform_config)

        message = {
            "id": 123,
            "type": "private",
            "sender_id": 42,
            "sender_email": "user@test.com",
            "sender_full_name": "User",
            "content": "/streams",
        }

        await adapter._handle_message(message)

        # Should have sent a command reply, not dispatched to AI
        assert len(adapter.client._sent_messages) > 0
        last_msg = adapter.client._sent_messages[-1]
        assert "Stream management" in last_msg["content"]

    @pytest.mark.asyncio
    async def test_command_reply_preserves_group_dm_recipients(
        self, mock_platform_config, monkeypatch
    ):
        """Issue #154: /streams in a group DM must answer the whole conversation."""
        import zulip.zulip_client as zulip_client_module
        from zulip.adapter import ZulipAdapter
        from tests.conftest import MockZulipClient

        monkeypatch.setenv("ZULIP_SITE", "https://test.zulipchat.com")
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key")
        monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = MockZulipClient(**kwargs)
                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
        adapter = ZulipAdapter(mock_platform_config)

        await adapter._handle_message(
            {
                "id": 123,
                "type": "private",
                "sender_id": 42,
                "sender_email": "user@test.com",
                "sender_full_name": "User",
                "display_recipient": [
                    {"id": 7, "email": "bot@test.com"},
                    {"id": 42, "email": "user@test.com"},
                    {"id": 99, "email": "other@test.com"},
                ],
                "content": "/streams",
            }
        )

        last_msg = adapter.client._sent_messages[-1]
        assert last_msg["to"] == [7, 42, 99]

    @pytest.mark.asyncio
    async def test_non_command_goes_to_ai(self, mock_platform_config, monkeypatch):
        import zulip.zulip_client as zulip_client_module
        from zulip.adapter import ZulipAdapter
        from tests.conftest import MockZulipClient

        monkeypatch.setenv("ZULIP_SITE", "https://test.zulipchat.com")
        monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key")
        monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = MockZulipClient(**kwargs)
                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())

        adapter = ZulipAdapter(mock_platform_config)

        # Track whether handle_message (AI dispatch) is called
        ai_called = False

        async def mock_handle(event):
            nonlocal ai_called
            ai_called = True

        adapter.handle_message = mock_handle

        message = {
            "id": 123,
            "type": "private",
            "sender_id": 42,
            "sender_email": "user@test.com",
            "sender_full_name": "User",
            "content": "Hello bot, how are you?",
        }

        await adapter._handle_message(message)
        assert ai_called is True


class TestNoPluginCommandShadowsTheGateway:
    """#251 / ledger row 5: the gateway-owned set is derived, not remembered.

    The set used to exist as three prose copies in ``commands.py``, one ending in
    a literal ``...`` — hand-maintained and incomplete by admission. This is the
    ``PLUGIN_FILES`` drift mechanism (row 1) applied to command names, and it has
    already bitten twice: #43 (``/version``) and #190/#191 (``/help``,
    ``/status``, ``/model``).
    """

    def test_the_plugin_registers_no_gateway_owned_name(self):
        from zulip.commands import GATEWAY_NATIVE_COMMANDS, plugin_command_names

        collides = plugin_command_names() & GATEWAY_NATIVE_COMMANDS
        assert not collides, (
            "the plugin registers a name the gateway owns: "
            f"{sorted(collides)} — that shadows the gateway command (#190)"
        )

    def test_the_recorded_set_names_its_host_source(self):
        """A constant that does not say where it came from is a memory, not a source."""
        from zulip.commands import GATEWAY_NATIVE_COMMANDS, GATEWAY_NATIVE_SOURCE

        assert GATEWAY_NATIVE_COMMANDS, "the gateway-owned set must not be empty"
        assert "GATEWAY_KNOWN_COMMANDS" in GATEWAY_NATIVE_SOURCE
        assert "v" in GATEWAY_NATIVE_SOURCE, "record the host version it was read from"

    def test_every_name_in_the_recorded_set_falls_through(self):
        """Behaviour follows the set, not just the constant."""
        from zulip.commands import GATEWAY_NATIVE_COMMANDS

        for name in sorted(GATEWAY_NATIVE_COMMANDS):
            result = handle_command(f"/{name}", "dm:1", "a@x.com", "Alice")
            assert result.handled is False, (
                f"/{name} is gateway-owned but the plugin router handled it (#190)"
            )


class TestCommandCollisions:
    """The comparison the gate uses, tested directly — including that it fires."""

    def _collisions(self, plugin, gateway):
        from zulip.commands import command_collisions

        return command_collisions(plugin, gateway)

    def test_a_shared_name_is_reported(self):
        assert self._collisions({"help", "pin"}, {"help", "model"}) == ["help"]

    def test_every_collision_is_reported_in_order(self):
        assert self._collisions({"stop", "help", "pin"}, {"stop", "help"}) == [
            "help",
            "stop",
        ]

    def test_disjoint_sets_report_nothing(self):
        assert self._collisions({"pin", "unpin"}, {"help", "model"}) == []

    def test_names_are_normalised(self):
        # A leading slash or mixed case must not hide a collision.
        assert self._collisions({"/Help", "PIN"}, {"help", "pin"}) == ["help", "pin"]

    def test_an_empty_plugin_set_is_safe(self):
        assert self._collisions(set(), {"help"}) == []


class TestTheNamespaceGateFires:
    """Negative coverage (#250): a gate that never fires is worse than none.

    The unit test above proves the plugin is clean *today*. This proves the
    check would say so if it were not -- otherwise a broken comparison would
    keep reporting "no collisions" forever.
    """

    @staticmethod
    def _checker():
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "scripts" / "check_compat.py"
        spec = importlib.util.spec_from_file_location("_check_compat_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_a_shadowing_command_fails_the_gate_and_names_it(self, monkeypatch):
        import zulip.commands as plugin_commands

        checker = self._checker()
        monkeypatch.setattr(
            plugin_commands, "plugin_command_names", lambda: frozenset({"help", "pin"})
        )

        failures = checker._check_command_namespace()

        assert failures, "the gate did not fire on a gateway-owned name"
        assert "/help" in failures[0]
        assert "/pin" not in failures[0], "only the colliding name may be named"

    def test_a_clean_plugin_passes_and_reports_what_it_checked(self, monkeypatch, capsys):
        checker = self._checker()

        failures = checker._check_command_namespace()

        assert failures == []
        out = capsys.readouterr().out
        assert "command namespace:" in out

    def test_the_check_says_which_source_it_compared_against(self, monkeypatch, capsys):
        checker = self._checker()
        out = capsys.readouterr().out
        checker._check_command_namespace()
        text = capsys.readouterr().out
        assert "GATEWAY_KNOWN_COMMANDS" in text or "GATEWAY_NATIVE_COMMANDS" in text, (
            "a check that does not say what it compared against cannot be reviewed"
        )
