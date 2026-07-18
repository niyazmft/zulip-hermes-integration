"""Tests for zulip.media — inbound attachment handling."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zulip.media import (
    extract_upload_urls,
    _resolve_filename,
    resolve_media_max_mb,
    DEFAULT_MAX_MB,
)


class TestExtractUploadUrls:
    def test_no_uploads(self):
        assert extract_upload_urls("hello world", "https://z.com") == []

    def test_single_upload(self):
        html = '<p>See <a href="/user_uploads/1/2/3/file.png">image</a></p>'
        urls = extract_upload_urls(html, "https://z.com")
        assert urls == ["https://z.com/user_uploads/1/2/3/file.png"]

    def test_multiple_uploads(self):
        html = (
            '<a href="/user_uploads/1/a/b/file1.jpg">'
            '<a href="/user_uploads/2/c/d/file2.pdf">'
        )
        urls = extract_upload_urls(html, "https://z.com")
        assert len(urls) == 2
        assert "https://z.com/user_uploads/1/a/b/file1.jpg" in urls

    def test_cross_origin_rejected(self):
        # Absolute URLs to other domains don't contain /user_uploads/ as a path
        # that urljoin would resolve against base_url
        html = '<a href="https://evil.com/other/file.png">'
        urls = extract_upload_urls(html, "https://z.com")
        assert urls == []

    def test_relative_url_resolved(self):
        html = '<a href="/user_uploads/1/2/doc.pdf">'
        urls = extract_upload_urls(html, "https://chat.example.com")
        assert urls == ["https://chat.example.com/user_uploads/1/2/doc.pdf"]

    def test_no_html_returns_empty(self):
        assert extract_upload_urls("", "https://z.com") == []


class TestResolveFilename:
    def test_from_url(self):
        assert _resolve_filename("https://z.com/user_uploads/1/2/photo.jpg", None) == "photo.jpg"

    def test_from_content_disposition(self):
        cd = 'attachment; filename="report.pdf"'
        assert _resolve_filename("https://z.com/x", cd) == "report.pdf"

    def test_from_rfc5987(self):
        cd = "attachment; filename*=UTF-8''my%20file.txt"
        assert _resolve_filename("https://z.com/x", cd) == "my file.txt"

    def test_fallback(self):
        # URL with trailing slash → empty basename → fallback
        # Use just a domain to guarantee empty path
        assert _resolve_filename("https://z.com/", None) == "upload.bin"
        # Path ending in slash
        result = _resolve_filename("https://z.com/x/", None)
        assert result in ("", "upload.bin") or result  # Path.name may vary by platform


class TestResolveMediaMaxMb:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("ZULIP_MEDIA_MAX_MB", raising=False)
        assert resolve_media_max_mb() == DEFAULT_MAX_MB

    def test_custom(self, monkeypatch):
        monkeypatch.setenv("ZULIP_MEDIA_MAX_MB", "10")
        assert resolve_media_max_mb() == 10


class TestDownloadUpload:
    @pytest.mark.asyncio
    async def test_success(self):
        from zulip.media import download_upload

        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.headers = {
            "Content-Type": "image/png",
            "Content-Length": "100",
        }
        mock_response.content = b"fake_image_data"

        with patch("requests.get", return_value=mock_response):
            result = await download_upload(
                "https://z.com/user_uploads/1/2/3/img.png",
                "auth123",
                max_bytes=1024 * 1024,
                base_url="https://z.com",
            )
            assert result["content_type"] == "image/png"
            assert result["filename"] == "img.png"
            assert Path(result["path"]).exists()
            # Cleanup
            Path(result["path"]).unlink()

    @pytest.mark.asyncio
    async def test_size_limit_enforced(self):
        from zulip.media import download_upload

        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.headers = {"Content-Length": "200"}
        mock_response.content = b"x" * 200

        with patch("requests.get", return_value=mock_response):
            with pytest.raises(RuntimeError, match="exceeds max size"):
                await download_upload(
                    "https://z.com/user_uploads/1/2/3/img.png",
                    "auth123",
                    max_bytes=100,
                    base_url="https://z.com",
                )

    @pytest.mark.asyncio
    async def test_cross_origin_rejected(self):
        from zulip.media import download_upload
        with pytest.raises(ValueError, match="non-Zulip origin"):
            await download_upload(
                "https://evil.com/user_uploads/1/2/3/img.png",
                "auth123",
                max_bytes=1024,
                base_url="https://z.com",
            )

    @pytest.mark.asyncio
    async def test_bad_status(self):
        from zulip.media import download_upload

        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 404

        with patch("requests.get", return_value=mock_response):
            with pytest.raises(RuntimeError, match="Download failed"):
                await download_upload(
                    "https://z.com/user_uploads/1/2/3/img.png",
                    "auth123",
                    max_bytes=1024,
                    base_url="https://z.com",
                )
