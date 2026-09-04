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


class TestSendImageFileAndDocument:
    """send_image_file / send_document override the base class's
    "native send unavailable" stub with a real upload_file_to_zulip() call
    (see gateway.platforms.base.BasePlatformAdapter defaults, which the
    MEDIA:<path> delivery pipeline calls directly — issue: screenshots from
    browser_exec's capture_screenshot() were silently dropped because these
    two methods were never overridden here)."""

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
    async def test_send_image_file_uploads_and_embeds_inline(self, adapter, monkeypatch):
        monkeypatch.setenv("HERMES_DATA_DIR", tempfile.gettempdir())
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.write(b"\x89PNG")
        tmp.close()
        expected_name = Path(tmp.name).name

        with patch("zulip.adapter.upload_file_to_zulip", return_value="https://z.com/user_uploads/1/shot.png"):
            result = await adapter.send_image_file("dm:42", tmp.name, caption="Here you go")
        assert result.success is True
        call = adapter.client._calls[0]
        # Inline image markdown, not a plain link — Zulip renders ![]() as
        # an embedded image and [] () as a bare downloadable link.
        assert f"![{expected_name}](https://z.com/user_uploads/1/shot.png)" in call["content"]
        assert "Here you go" in call["content"]
        Path(tmp.name).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_send_document_uses_plain_link(self, adapter, monkeypatch):
        monkeypatch.setenv("HERMES_DATA_DIR", tempfile.gettempdir())
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.write(b"%PDF")
        tmp.close()
        expected_name = Path(tmp.name).name

        with patch("zulip.adapter.upload_file_to_zulip", return_value="https://z.com/user_uploads/1/report.pdf"):
            result = await adapter.send_document("dm:42", tmp.name)
        assert result.success is True
        call = adapter.client._calls[0]
        assert call["content"] == f"[{expected_name}](https://z.com/user_uploads/1/report.pdf)"
        assert not call["content"].startswith("!")
        Path(tmp.name).unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_send_image_file_upload_failure_sends_warning_not_stub_text(self, adapter, monkeypatch):
        monkeypatch.setenv("HERMES_DATA_DIR", tempfile.gettempdir())
        with patch("zulip.adapter.upload_file_to_zulip", side_effect=ValueError("unauthorized path")):
            result = await adapter.send_image_file("dm:42", "/etc/passwd")
        assert result.success is True
        assert "Couldn't deliver the image attachment" in adapter.client._calls[0]["content"]

    @pytest.mark.asyncio
    async def test_send_image_file_upload_failure_preserves_caption(self, adapter, monkeypatch):
        """The failure fallback keeps the caller's caption as a prefix so the
        context of what failed isn't lost."""
        monkeypatch.setenv("HERMES_DATA_DIR", tempfile.gettempdir())
        with patch("zulip.adapter.upload_file_to_zulip", side_effect=ValueError("unauthorized path")):
            result = await adapter.send_image_file("dm:42", "/etc/passwd", caption="Screenshot")
        assert result.success is True
        content = adapter.client._calls[0]["content"]
        assert content.startswith("Screenshot\n")
        assert "Couldn't deliver the image attachment" in content
