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
