"""Send-path wiring for delivery auditing (issue #145).

The audit helpers exist; these tests prove the *outbound paths call them*, and
call them exactly once each, so an undelivered reply becomes a lookup:

* ``deliver_payload`` for a reply handed to Zulip (normal, media, standalone,
  approval prompt, activity trace);
* ``deliver_failed`` when a send raises, times out, or the API refuses it;
* ``deliver_skipped`` with a machine-readable reason when a reply is suppressed;
* ``dispatch_turn`` + ``deliver_empty`` so "no reply" is distinguishable from
  "never ran".

The events are read back from the real audit artifact on purpose: the failure
mode being guarded against is a path that claims to be audited while nothing was
written. Neither a message body nor a credential may appear in it.
"""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zulip import adapter as adapter_module
from zulip.adapter import _standalone_send
from zulip.activity_trace import TraceConfig

SUCCESS = SimpleNamespace(value="success")

_STANDALONE_ENV = {
    "ZULIP_SITE": "https://zulip.example.test",
    "ZULIP_EMAIL": "bot@example.test",
    "ZULIP_API_KEY": "k" * 32,
}

FULL_ACTIONS = [
    ("Allow Once", "once", "primary"),
    ("Allow Session", "session", ""),
    ("Always Allow", "always", ""),
    ("Deny", "deny", "danger"),
]


def _events(data_dir) -> list[dict]:
    out: list[dict] = []
    for path in sorted(Path(data_dir).rglob("*.audit.log")):
        for line in path.read_text().splitlines():
            if line:
                out.append(json.loads(line))
    return out


def _named(data_dir, name: str) -> list[dict]:
    return [e for e in _events(data_dir) if e["event"] == name]


def _artifact_text(data_dir) -> str:
    return "".join(p.read_text() for p in Path(data_dir).rglob("*.audit.log"))


class _RecordingClient:
    """SDK stand-in whose send outcome can be scripted per test."""

    def __init__(self, **kwargs):
        self.sent = []
        self.edited = []
        self.send_result = {"result": "success", "id": 700}
        self.send_error: Exception | None = None

    def send_message(self, request):
        if self.send_error is not None:
            raise self.send_error
        result = dict(self.send_result)
        if result.get("result") == "success":
            result["id"] = result.get("id") or 700 + len(self.sent)
        self.sent.append(dict(request))
        return result

    def update_message(self, request):
        self.edited.append(request)
        return {"result": "success"}

    def update_message_flags(self, request):
        return {"result": "success"}

    def add_reaction(self, request):
        return {"result": "success"}

    def remove_reaction(self, request):
        return {"result": "success"}

    def set_typing_status(self, request):
        return {"result": "success"}

    def get_user(self, user_id):
        return {"result": "success", "user": {"full_name": "Fetched"}}


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch):
    monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

    class MockZulipModule:
        Client = _RecordingClient

    monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
    adapter_module._clear_caches()
    yield
    adapter_module._clear_caches()


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    # A real credential value, so a refusal has something it must never write.
    monkeypatch.setenv("ZULIP_API_KEY", "k" * 32)

    from zulip.adapter import ZulipAdapter

    return ZulipAdapter(mock_platform_config)


class TestNormalSend:
    @pytest.mark.asyncio
    async def test_success_records_deliver_payload(self, adapter, tmp_path):
        result = await adapter.send(
            "573423", "hello", metadata={"thread_id": "deploy"}
        )

        assert result.success is True
        events = _named(tmp_path, "deliver_payload")
        assert len(events) == 1
        details = events[0]["details"]
        assert details["chat_id"] == "573423"
        assert details["topic"] == "deploy"
        assert details["message_id"]

    @pytest.mark.asyncio
    async def test_dm_payload_carries_no_topic(self, adapter, tmp_path):
        result = await adapter.send("dm:42", "hello")

        assert result.success is True
        details = _named(tmp_path, "deliver_payload")[0]["details"]
        assert details["chat_id"] == "dm:42"
        assert "topic" not in details

    @pytest.mark.asyncio
    async def test_api_error_records_deliver_failed(self, adapter, tmp_path):
        adapter.client.send_result = {
            "result": "error",
            "code": "STREAM_DOES_NOT_EXIST",
        }

        result = await adapter.send("999", "hello")

        assert result.success is False
        events = _named(tmp_path, "deliver_failed")
        assert len(events) == 1
        assert events[0]["details"]["error"] == "api_error"

    @pytest.mark.asyncio
    async def test_exception_records_deliver_failed(self, adapter, tmp_path):
        adapter.client.send_error = ConnectionError("boom")

        result = await adapter.send("573423", "hello")

        assert result.success is False
        events = _named(tmp_path, "deliver_failed")
        assert len(events) == 1
        assert events[0]["details"]["error"] == "send_exception"


class TestMediaSend:
    @pytest.mark.asyncio
    async def test_media_send_records_deliver_payload(
        self, adapter, tmp_path, monkeypatch
    ):
        attachment = tmp_path / "report.csv"
        attachment.write_text("a,b\n")
        upload = AsyncMock(return_value="/user_uploads/1/report.csv")
        monkeypatch.setattr(adapter_module, "upload_file_to_zulip", upload)

        result = await adapter.send(
            "573423",
            "see attached",
            metadata={"thread_id": "deploy"},
            media_files=[str(attachment)],
        )

        assert result.success is True
        events = _named(tmp_path, "deliver_payload")
        assert len(events) == 1
        assert events[0]["details"]["chat_id"] == "573423"


class TestStandaloneSend:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        adapter_module._clear_caches()
        monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
        for key in _STANDALONE_ENV:
            monkeypatch.delenv(key, raising=False)
        yield
        adapter_module._clear_caches()

    @pytest.mark.asyncio
    async def test_success_records_deliver_payload(self, monkeypatch, tmp_path):
        for key, value in _STANDALONE_ENV.items():
            monkeypatch.setenv(key, value)
        client = MagicMock(spec_set=["send_message"])
        client.send_message.return_value = {"result": "success", "id": 42}

        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(
                SimpleNamespace(extra={}), "20", "body", thread_id="Weekly"
            )

        assert result["success"] is True
        events = _named(tmp_path, "deliver_payload")
        assert len(events) == 1
        details = events[0]["details"]
        assert details["chat_id"] == "20"
        assert details["topic"] == "Weekly"
        assert details["message_id"] == "42"

    @pytest.mark.asyncio
    async def test_timeout_records_deliver_failed(self, monkeypatch, tmp_path):
        for key, value in _STANDALONE_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("ZULIP_SEND_TIMEOUT", "0.05")

        def _hang(_payload):
            time.sleep(0.5)
            return {"result": "success", "id": 1}

        client = MagicMock(spec_set=["send_message"])
        client.send_message.side_effect = _hang

        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(SimpleNamespace(extra={}), "20", "body")

        assert "timed out" in result["error"]
        events = _named(tmp_path, "deliver_failed")
        assert len(events) == 1
        assert events[0]["details"]["error"] == "send_timeout"

    @pytest.mark.asyncio
    async def test_api_error_records_deliver_failed(self, monkeypatch, tmp_path):
        for key, value in _STANDALONE_ENV.items():
            monkeypatch.setenv(key, value)
        client = MagicMock(spec_set=["send_message"])
        client.send_message.return_value = {
            "result": "error",
            "code": "STREAM_DOES_NOT_EXIST",
        }

        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(SimpleNamespace(extra={}), "999", "body")

        assert "error" in result
        events = _named(tmp_path, "deliver_failed")
        assert len(events) == 1
        assert events[0]["details"]["error"] == "api_error"


class TestApprovalPrompt:
    @pytest.mark.asyncio
    async def test_prompt_records_deliver_payload(self, adapter, tmp_path):
        from gateway.platforms.base import ExecApprovalPrompt

        prompt = ExecApprovalPrompt(
            chat_id="635267",
            session_key="agent:main:zulip:stream:635267",
            text="Approval required",
            actions=FULL_ACTIONS,
            command="ls -la",
            description="test command",
            smart_denied=False,
            metadata={"thread_id": "deploy"},
        )

        result = await adapter._send_exec_approval_prompt(prompt)

        assert result.success is True
        events = _named(tmp_path, "deliver_payload")
        assert events, "the approval prompt is an outbound send too"
        assert all(e["details"]["chat_id"] == "635267" for e in events)

    @pytest.mark.asyncio
    async def test_a_refused_prompt_records_deliver_failed(self, adapter, tmp_path):
        from gateway.platforms.base import ExecApprovalPrompt

        adapter.client.send_result = {"result": "error", "code": "NOPE"}
        prompt = ExecApprovalPrompt(
            chat_id="635267",
            session_key="agent:main:zulip:stream:635267",
            text="Approval required",
            actions=FULL_ACTIONS,
            command="ls -la",
            description="test command",
            smart_denied=False,
            metadata={"thread_id": "deploy"},
        )

        await adapter._send_exec_approval_prompt(prompt)

        events = _named(tmp_path, "deliver_failed")
        assert events
        assert all(e["details"]["error"] == "api_error" for e in events)


class TestTraceSend:
    @pytest.mark.asyncio
    async def test_trace_post_records_deliver_payload(
        self, adapter, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "1")
        monkeypatch.setenv("ZULIP_TRACE_COALESCE_MS", "0")
        adapter._trace_cfg = TraceConfig.from_env()

        event = SimpleNamespace(
            source=SimpleNamespace(chat_id="573423"),
            metadata={"thread_id": "deploy"},
            message_id="55",
        )
        await adapter.on_processing_start(event)
        await asyncio.sleep(0.15)

        events = _named(tmp_path, "deliver_payload")
        assert events, "the trace message is an outbound send too"
        assert events[0]["details"]["chat_id"] == "573423"
        assert events[0]["details"]["topic"] == "deploy"


class TestTurnPairing:
    @staticmethod
    def _event():
        return SimpleNamespace(
            source=SimpleNamespace(chat_id="573423"),
            metadata={"thread_id": "deploy"},
            message_id="55",
        )

    @pytest.mark.asyncio
    async def test_an_idle_run_is_distinguishable_from_one_never_dispatched(
        self, adapter, tmp_path
    ):
        event = self._event()

        await adapter.on_processing_start(event)
        await adapter.on_processing_complete(event, SUCCESS)

        assert [e["event"] for e in _events(tmp_path)] == [
            "dispatch_turn",
            "deliver_empty",
        ]

    @pytest.mark.asyncio
    async def test_a_send_suppresses_deliver_empty(self, adapter, tmp_path):
        event = self._event()

        await adapter.on_processing_start(event)
        await adapter.send("573423", "answer", metadata={"thread_id": "deploy"})
        await adapter.on_processing_complete(event, SUCCESS)

        names = [e["event"] for e in _events(tmp_path)]
        assert "deliver_payload" in names
        assert "deliver_empty" not in names


class TestSkippedDelivery:
    @pytest.mark.asyncio
    async def test_a_secret_refusal_records_a_machine_readable_reason(
        self, adapter, tmp_path
    ):
        secret = "k" * 32

        result = await adapter.send("dm:42", f"the key is {secret}")

        assert result.success is False
        events = _named(tmp_path, "deliver_skipped")
        assert len(events) == 1
        assert events[0]["details"]["reason"] == "secret_leak_blocked"


class TestNoBodyOrCredentialIsWritten:
    @pytest.mark.asyncio
    async def test_the_message_body_never_reaches_the_log(self, adapter, tmp_path):
        await adapter.send(
            "573423",
            "SENTINEL_BODY_TEXT_XYZ",
            metadata={"thread_id": "deploy"},
        )

        assert "SENTINEL_BODY_TEXT_XYZ" not in _artifact_text(tmp_path)

    @pytest.mark.asyncio
    async def test_a_refused_credential_value_never_reaches_the_log(
        self, adapter, tmp_path
    ):
        secret = "k" * 32

        await adapter.send("dm:42", f"the key is {secret}")

        payload = _artifact_text(tmp_path)
        assert secret not in payload
        assert "secret_leak_blocked" in payload
