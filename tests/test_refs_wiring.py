"""Send-path wiring for actionable refs (issue #150).

``render_refs`` already degrades safely; these tests are about *where* it is
called: before truncation and chunking, on both the live adapter send path and
the standalone/cron send path, so a ``[[zulip_ref: …]]`` marker can never be
split across two messages. Rendering is best-effort and must never fail or
change a reply that has no markers.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zulip import adapter as adapter_module
import zulip.refs as refs_module
import zulip.zulip_client as zulip_client_module
from zulip.adapter import _standalone_send
from zulip.refs import clear_ref_cache


MARKER = "[[zulip_ref: https://github.com/octo/hello/pull/128]]"
LINK = "[octo/hello#128](https://github.com/octo/hello/pull/128)"

_STANDALONE_ENV = {
    "ZULIP_SITE": "https://zulip.example.test",
    "ZULIP_EMAIL": "bot@example.test",
    "ZULIP_API_KEY": "k" * 32,
}


class _RecordingClient:
    def __init__(self, **kwargs):
        self.sent = []

    def send_message(self, request):
        self.sent.append(dict(request))
        return {"result": "success", "id": 700 + len(self.sent)}

    def update_message_flags(self, request):
        return {"result": "success"}


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch):
    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

    class MockZulipModule:
        Client = _RecordingClient

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    adapter_module._clear_caches()
    clear_ref_cache()
    yield
    adapter_module._clear_caches()
    clear_ref_cache()


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))

    from zulip.adapter import ZulipAdapter

    return ZulipAdapter(mock_platform_config)


class TestLiveSendPath:
    @pytest.mark.asyncio
    async def test_render_is_called_with_the_unchunked_reply(self, adapter, monkeypatch):
        rendered = AsyncMock(return_value=LINK)
        monkeypatch.setattr(refs_module, "render_refs", rendered)

        result = await adapter.send("dm:42", f"Opened a PR: {MARKER}")

        assert result.success is True
        rendered.assert_awaited_once()
        assert rendered.await_args[0][0] == f"Opened a PR: {MARKER}"
        # The mock returns only the link; the point here is that rendering ran
        # on the full reply before the send, not on a chunk.
        assert adapter.client.sent[0]["content"] == LINK

    @pytest.mark.asyncio
    async def test_a_marker_is_never_split_across_chunks(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_TEXT_CHUNK_LIMIT", "60")
        rendered = AsyncMock(return_value=LINK)
        monkeypatch.setattr(refs_module, "render_refs", rendered)

        result = await adapter.send("dm:42", "pad " * 12 + MARKER)

        assert result.success is True
        chunks = [c["content"] for c in adapter.client.sent]
        assert any(LINK in chunk for chunk in chunks), "the rendered link must survive whole"
        assert all("zulip_ref" not in chunk for chunk in chunks), (
            "no chunk may carry a half-marker"
        )

    @pytest.mark.asyncio
    async def test_rendering_is_best_effort(self, adapter, monkeypatch):
        rendered = AsyncMock(side_effect=RuntimeError("github down"))
        monkeypatch.setattr(refs_module, "render_refs", rendered)

        result = await adapter.send("dm:42", f"Opened a PR: {MARKER}")

        assert result.success is True, "a rendering failure must never fail the send"
        assert MARKER in adapter.client.sent[0]["content"], (
            "the original reply must be sent untouched when rendering fails"
        )

    @pytest.mark.asyncio
    async def test_a_reply_without_markers_is_unchanged(self, adapter):
        result = await adapter.send("573423", "plain reply, no markers here")

        assert result.success is True
        assert adapter.client.sent[0]["content"] == "plain reply, no markers here"


class TestStandaloneSendPath:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        adapter_module._clear_caches()
        monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
        for key in _STANDALONE_ENV:
            monkeypatch.delenv(key, raising=False)
        yield
        adapter_module._clear_caches()

    @pytest.mark.asyncio
    async def test_standalone_renders_before_sending(self, monkeypatch):
        for key, value in _STANDALONE_ENV.items():
            monkeypatch.setenv(key, value)
        rendered = AsyncMock(return_value=LINK)
        monkeypatch.setattr(refs_module, "render_refs", rendered)

        client = MagicMock(spec_set=["send_message"])
        client.send_message.return_value = {"result": "success", "id": 1}
        with patch.object(zulip_client_module, "get_cached_client", return_value=client):
            result = await _standalone_send(
                SimpleNamespace(extra={}), "20", f"Opened a PR: {MARKER}"
            )

        assert result["success"] is True
        rendered.assert_awaited_once()
        assert rendered.await_args[0][0] == f"Opened a PR: {MARKER}"
        content = client.send_message.call_args[0][0]["content"]
        assert LINK in content
        assert "zulip_ref" not in content

    @pytest.mark.asyncio
    async def test_standalone_rendering_is_best_effort(self, monkeypatch):
        for key, value in _STANDALONE_ENV.items():
            monkeypatch.setenv(key, value)
        rendered = AsyncMock(side_effect=RuntimeError("github down"))
        monkeypatch.setattr(refs_module, "render_refs", rendered)

        client = MagicMock(spec_set=["send_message"])
        client.send_message.return_value = {"result": "success", "id": 1}
        with patch.object(zulip_client_module, "get_cached_client", return_value=client):
            result = await _standalone_send(
                SimpleNamespace(extra={}), "20", f"Opened a PR: {MARKER}"
            )

        assert result["success"] is True
        assert MARKER in client.send_message.call_args[0][0]["content"]
