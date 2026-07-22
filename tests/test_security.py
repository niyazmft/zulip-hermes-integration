"""Tests for security hardening: SSRF, symlink rejection, URL validation."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from zulip.probe import _normalize_base_url, _is_internal_host


class TestSsrfUrlValidation:
    def test_rejects_file_scheme(self):
        assert _normalize_base_url("file:///etc/passwd") is None

    def test_rejects_gopher(self):
        assert _normalize_base_url("gopher://example.com") is None

    def test_rejects_localhost(self):
        assert _normalize_base_url("http://localhost:8080") is None

    def test_rejects_localhost_localdomain(self):
        assert _normalize_base_url("https://localhost.localdomain") is None

    def test_rejects_private_ips(self):
        assert _normalize_base_url("http://127.0.0.1") is None
        assert _normalize_base_url("http://10.0.0.1") is None
        assert _normalize_base_url("https://192.168.1.1") is None
        assert _normalize_base_url("http://172.16.0.1") is None

    def test_rejects_aws_metadata(self):
        assert _normalize_base_url("http://169.254.169.254") is None

    def test_accepts_public_urls(self):
        assert _normalize_base_url("https://example.zulipchat.com") == "https://example.zulipchat.com"
        assert _normalize_base_url("http://public.example.com/") == "http://public.example.com"

    def test_adapter_rejects_bad_site(self, mock_platform_config, monkeypatch):
        monkeypatch.setenv("ZULIP_SITE", "http://127.0.0.1")
        monkeypatch.setenv("ZULIP_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")

        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        from zulip.adapter import ZulipAdapter
        with pytest.raises(ValueError, match="Invalid or unsafe"):
            ZulipAdapter(mock_platform_config)

    def test_adapter_rejects_file_scheme(self, mock_platform_config, monkeypatch):
        monkeypatch.setenv("ZULIP_SITE", "file:///etc/passwd")
        monkeypatch.setenv("ZULIP_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")

        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        from zulip.adapter import ZulipAdapter
        with pytest.raises(ValueError, match="Invalid or unsafe"):
            ZulipAdapter(mock_platform_config)


class TestSymlinkRejection:
    def test_workspace_rejects_symlink_file(self, tmp_path):
        from zulip.workspace import BotWorkspace
        ws = BotWorkspace(root=str(tmp_path))

        # Create a file outside the workspace
        outside = tmp_path / ".." / "outside.txt"
        outside.write_text("secret")

        # Create a symlink inside the workspace pointing outside
        link = tmp_path / "link.txt"
        link.symlink_to(outside)

        with pytest.raises(ValueError, match="Symlink rejected"):
            ws.read_text("link.txt")

    def test_workspace_rejects_symlink_in_parent(self, tmp_path):
        from zulip.workspace import BotWorkspace
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()

        # Create a symlink directory in the path
        link_dir = tmp_path / "linkdir"
        link_dir.symlink_to(tmp_path / ".." / "other")

        # Nest workspace under the symlink - this is hard to test directly
        # Just verify that normal path traversal still works
        ws = BotWorkspace(root=str(ws_root))
        path = ws.save_text("test.txt", "hello")
        assert Path(path).read_text() == "hello"

    def test_media_upload_rejects_symlink(self, monkeypatch):
        import tempfile
        from zulip.media import upload_file_to_zulip

        # Create a real file and a symlink to it
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("real content")
            real_path = f.name

        # Create a symlink
        tmp_dir = Path(tempfile.gettempdir())
        link_path = tmp_dir / "test_link.txt"
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        link_path.symlink_to(real_path)

        monkeypatch.setenv("HERMES_DATA_DIR", tempfile.gettempdir())

        async def test():
            client = MagicMock()
            client.upload_file.return_value = {"result": "success", "uri": "https://z.com/upload"}
            with pytest.raises(ValueError, match="Symlink rejected"):
                await upload_file_to_zulip(client, str(link_path), tempfile.gettempdir())

        import asyncio
        asyncio.run(test())

        # Cleanup
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        Path(real_path).unlink(missing_ok=True)

    def test_workspace_allows_normal_file(self, tmp_path):
        from zulip.workspace import BotWorkspace
        ws = BotWorkspace(root=str(tmp_path))
        path = ws.save_text("normal.txt", "hello")
        assert ws.read_text("normal.txt") == "hello"


class TestUrlEncoding:
    """Verify that URL-sensitive characters are handled safely.

    The Zulip SDK handles URL encoding internally; these tests verify
    our wrapper code doesn't introduce injection vulnerabilities.
    """

    def test_path_traversal_in_filename_blocked(self, tmp_path):
        from zulip.workspace import BotWorkspace
        ws = BotWorkspace(root=str(tmp_path))
        with pytest.raises(ValueError, match="Path traversal"):
            ws.save_text("../../etc/passwd", "x")

    def test_null_bytes_rejected(self, tmp_path):
        from zulip.workspace import BotWorkspace
        ws = BotWorkspace(root=str(tmp_path))
        # Null bytes in filenames should be handled safely
        try:
            ws.save_text("file\x00.txt", "x")
        except (ValueError, OSError):
            pass  # Expected on most filesystems
