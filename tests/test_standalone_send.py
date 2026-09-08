"""Tests for ``_standalone_send`` — out-of-process delivery for Hermes cron.

Hermes calls ``PlatformEntry.standalone_sender_fn`` from
``tools/send_message_tool._send_via_adapter`` when no gateway adapter is live
in the current process (``hermes cron run <job>`` from the CLI, or cron in a
separate process). The contract (``gateway/platform_registry.py``):

    async (pconfig, chat_id, message, *, thread_id=None,
           media_files=None, force_document=False) -> dict

returning ``{"success": True, "message_id": ...}`` or ``{"error": str}``.
"""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zulip import adapter as adapter_module
from zulip.adapter import _standalone_send


# Mirrors the send-related surface of ``zulip.Client`` (python-zulip-api
# 0.9.x). ``spec_set`` makes any other attribute access raise AttributeError,
# like the real SDK would.
_SDK_CLIENT_METHODS = ["send_message", "get_messages", "upload_file"]

_ENV = {
    "ZULIP_SITE": "https://zulip.example.test",
    "ZULIP_EMAIL": "bot@example.test",
    "ZULIP_API_KEY": "k" * 32,
}


def _fake_client(result=None):
    client = MagicMock(spec_set=_SDK_CLIENT_METHODS)
    client.send_message.return_value = result or {"result": "success", "id": 4242}
    return client


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    adapter_module._clear_caches()
    for key in ("ZULIP_SITE", "ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SEND_TIMEOUT", "ZULIP_RESPONSE_PREFIX"):
        monkeypatch.delenv(key, raising=False)
    yield
    adapter_module._clear_caches()


@pytest.fixture
def env(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)


def _pconfig(extra=None):
    return SimpleNamespace(extra=extra or {})


class TestContract:
    def test_registered_as_standalone_sender_fn(self):
        """The plugin entry point must hand Hermes the sender on PlatformEntry."""
        calls = []
        ctx = SimpleNamespace(register_platform=lambda **kw: calls.append(kw))
        adapter_module.register(ctx)
        assert len(calls) == 1
        assert calls[0]["standalone_sender_fn"] is _standalone_send

    def test_signature_matches_hermes_contract(self):
        sig = inspect.signature(_standalone_send)
        params = list(sig.parameters.values())
        assert [p.name for p in params[:3]] == ["pconfig", "chat_id", "message"]
        assert all(p.kind is p.POSITIONAL_OR_KEYWORD for p in params[:3])
        kw_only = {p.name: p for p in params[3:]}
        assert set(kw_only) == {"thread_id", "media_files", "force_document"}
        assert all(p.kind is p.KEYWORD_ONLY and p.default is not p.empty for p in kw_only.values())
        assert inspect.iscoroutinefunction(_standalone_send)


class TestStreamDelivery:
    @pytest.mark.asyncio
    async def test_sends_to_stream_with_default_topic(self, env):
        """``deliver: zulip:20`` → stream 20, topic falls back to the default."""
        client = _fake_client()
        with patch.object(adapter_module, "_get_cached_client", return_value=client) as get_client:
            result = await _standalone_send(_pconfig(), "20", "weekly report body")

        assert result == {"success": True, "message_id": "4242"}
        get_client.assert_called_once_with(_ENV["ZULIP_SITE"], _ENV["ZULIP_EMAIL"], _ENV["ZULIP_API_KEY"])
        client.send_message.assert_called_once_with(
            {
                "type": "stream",
                "to": 20,
                "topic": adapter_module.STANDALONE_DEFAULT_TOPIC,
                "content": "weekly report body",
            }
        )

    @pytest.mark.asyncio
    async def test_thread_id_becomes_topic(self, env):
        """``deliver: zulip:20:Weekly currency`` → Hermes passes the 3rd segment as thread_id."""
        client = _fake_client()
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(_pconfig(), "20", "hi", thread_id="Weekly currency")

        assert result["success"] is True
        assert client.send_message.call_args[0][0]["topic"] == "Weekly currency"

    @pytest.mark.asyncio
    async def test_inline_topic_directive_wins_and_is_stripped(self, env):
        client = _fake_client()
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            await _standalone_send(_pconfig(), "20", "[[zulip_topic: Alerts]]\nDisk 91% full", thread_id="ignored")

        payload = client.send_message.call_args[0][0]
        assert payload["topic"] == "Alerts"
        assert "zulip_topic" not in payload["content"]
        assert "Disk 91% full" in payload["content"]

    @pytest.mark.asyncio
    async def test_response_prefix_applied(self, env, monkeypatch):
        monkeypatch.setenv("ZULIP_RESPONSE_PREFIX", "🤖 ")
        client = _fake_client()
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            await _standalone_send(_pconfig(), "20", "hello")
        assert client.send_message.call_args[0][0]["content"] == "🤖 hello"

    @pytest.mark.asyncio
    async def test_max_message_length_truncates(self, env, monkeypatch):
        monkeypatch.setenv("ZULIP_MAX_MESSAGE_LENGTH", "100")
        client = _fake_client()
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(_pconfig(), "20", "x" * 5000)
        assert result["success"] is True
        content = client.send_message.call_args[0][0]["content"]
        assert len(content) == 100
        assert content.endswith("[...message truncated]")


class TestDmDelivery:
    @pytest.mark.asyncio
    async def test_sends_private_message(self, env):
        """``deliver: zulip:dm:8`` → private message to user 8; thread_id is irrelevant."""
        client = _fake_client()
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(_pconfig(), "dm:8", "ping", thread_id="whatever")

        assert result == {"success": True, "message_id": "4242"}
        client.send_message.assert_called_once_with(
            {"type": "private", "to": [8], "content": "ping"}
        )


class TestCredentials:
    @pytest.mark.asyncio
    async def test_missing_credentials_reports_error_without_network(self):
        with patch.object(adapter_module, "_get_cached_client") as get_client:
            result = await _standalone_send(_pconfig(), "20", "x")
        assert "error" in result and "ZULIP_SITE" in result["error"]
        get_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_pconfig_extra(self):
        """Out-of-process callers may carry creds on PlatformConfig.extra, not env."""
        client = _fake_client()
        extra = {"site": "https://cfg.example.test", "email": "cfg@example.test", "api_key": "x" * 32}
        with patch.object(adapter_module, "_get_cached_client", return_value=client) as get_client:
            result = await _standalone_send(_pconfig(extra), "20", "x")
        assert result["success"] is True
        get_client.assert_called_once_with(extra["site"], extra["email"], extra["api_key"])

    @pytest.mark.asyncio
    async def test_env_wins_over_extra(self, env):
        client = _fake_client()
        extra = {"site": "https://cfg.example.test", "email": "cfg@example.test", "api_key": "x" * 32}
        with patch.object(adapter_module, "_get_cached_client", return_value=client) as get_client:
            await _standalone_send(_pconfig(extra), "20", "x")
        get_client.assert_called_once_with(_ENV["ZULIP_SITE"], _ENV["ZULIP_EMAIL"], _ENV["ZULIP_API_KEY"])

    @pytest.mark.asyncio
    async def test_missing_sdk_reports_error(self, env):
        with patch.object(adapter_module, "_get_cached_client", side_effect=ImportError("zulip package not installed")):
            result = await _standalone_send(_pconfig(), "20", "x")
        assert result == {"error": "zulip package not installed"}


class TestFailures:
    """Every failure must come back as ``{"error": ...}``, never an exception —
    Hermes records the dict as the job's ``last_delivery_error``."""

    @pytest.mark.asyncio
    async def test_invalid_target(self, env):
        client = _fake_client()
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(_pconfig(), "#platform", "x")
        assert "error" in result and "Invalid Zulip target" in result["error"]
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_error_result(self, env):
        client = _fake_client({"result": "error", "msg": "Stream does not exist", "code": "STREAM_DOES_NOT_EXIST"})
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(_pconfig(), "999", "x")
        assert "error" in result and "STREAM_DOES_NOT_EXIST" in result["error"]

    @pytest.mark.asyncio
    async def test_sdk_exception(self, env):
        client = _fake_client()
        client.send_message.side_effect = ConnectionError("boom")
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(_pconfig(), "20", "x")
        assert result == {"error": "Zulip send failed: boom"}

    @pytest.mark.asyncio
    async def test_timeout_is_bounded_by_zulip_send_timeout(self, env, monkeypatch):
        monkeypatch.setenv("ZULIP_SEND_TIMEOUT", "0.05")
        client = _fake_client()

        def _hang(_payload):
            import time
            time.sleep(0.5)
            return {"result": "success", "id": 1}

        client.send_message.side_effect = _hang
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(_pconfig(), "20", "x")
        assert "error" in result and "timed out" in result["error"]


class TestMedia:
    @pytest.mark.asyncio
    async def test_uploads_files_and_appends_links(self, env, tmp_path):
        f = tmp_path / "report.csv"
        f.write_text("a,b\n")
        client = _fake_client()
        upload = AsyncMock(return_value="/user_uploads/1/ab/report.csv")
        with patch.object(adapter_module, "_get_cached_client", return_value=client), \
             patch.object(adapter_module, "upload_file_to_zulip", upload):
            result = await _standalone_send(_pconfig(), "20", "see attached", media_files=[str(f)])

        assert result["success"] is True
        upload.assert_awaited_once()
        assert upload.await_args[0][0] is client
        assert upload.await_args[0][1] == str(f)
        content = client.send_message.call_args[0][0]["content"]
        assert content.startswith("see attached")
        assert "[report.csv](/user_uploads/1/ab/report.csv)" in content

    @pytest.mark.asyncio
    async def test_rejects_urls_in_media_files(self, env):
        client = _fake_client()
        upload = AsyncMock()
        with patch.object(adapter_module, "_get_cached_client", return_value=client), \
             patch.object(adapter_module, "upload_file_to_zulip", upload):
            result = await _standalone_send(_pconfig(), "20", "x", media_files=["https://evil.test/a.png"])
        assert result["success"] is True
        upload.assert_not_awaited()
        assert client.send_message.call_args[0][0]["content"] == "x"

    @pytest.mark.asyncio
    async def test_force_document_is_accepted_and_ignored(self, env):
        client = _fake_client()
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(_pconfig(), "20", "x", force_document=True)
        assert result["success"] is True
