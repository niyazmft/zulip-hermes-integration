"""Tests for adapter.py outbound file upload integration."""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


class TestOutboundUpload:
    @pytest.fixture
    def adapter(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._calls = []
                def send_message(self, request):
                    msg_id = len(self._calls) + 100
                    self._calls.append(request)
                    return {"result": "success", "id": msg_id}

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter
        return ZulipAdapter(mock_platform_config)

    @pytest.mark.asyncio
    async def test_upload_file_and_send(self, adapter, monkeypatch):
        monkeypatch.setenv("HERMES_DATA_DIR", tempfile.gettempdir())
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.write("hello")
        tmp.close()

        with patch("zulip.adapter.upload_file_to_zulip", return_value="https://z.com/user_uploads/1/doc.pdf"):
            result = await adapter.send("dm:42", "See attached", media_files=[tmp.name])
        assert result.success is True
        call = adapter.client._calls[0]
        assert "See attached" in call["content"]
        assert "doc.pdf" in call["content"]
        Path(tmp.name).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_upload_only_no_text(self, adapter, monkeypatch):
        monkeypatch.setenv("HERMES_DATA_DIR", tempfile.gettempdir())
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        tmp.write("x")
        tmp.close()

        with patch("zulip.adapter.upload_file_to_zulip", return_value="https://z.com/user_uploads/1/doc.pdf"):
            result = await adapter.send("dm:42", "", media_files=[tmp.name])
        assert result.success is True
        call = adapter.client._calls[0]
        assert "doc.pdf" in call["content"]
        Path(tmp.name).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, adapter, monkeypatch):
        monkeypatch.setenv("HERMES_DATA_DIR", tempfile.gettempdir())
        with patch("zulip.adapter.upload_file_to_zulip", side_effect=ValueError("unauthorized path")):
            result = await adapter.send("dm:42", "hi", media_files=["/etc/passwd"])
        assert result.success is True
        assert adapter.client._calls[0]["content"] == "hi"
