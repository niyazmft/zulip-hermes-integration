"""Tests for zulip.media — inbound attachment handling."""

from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile

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


class TestUploadFileToZulip:
    """Tests for upload_file_to_zulip — outbound file upload with security."""

    @pytest.mark.asyncio
    async def test_upload_success(self, tmp_path):
        from zulip.media import upload_file_to_zulip

        # Create a real file
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")

        mock_client = MagicMock()
        mock_client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/test.txt",
        }
        mock_client.base_url = "https://zulip.example.com"

        url = await upload_file_to_zulip(mock_client, str(test_file), str(tmp_path))
        assert url == "https://zulip.example.com/user_uploads/1/test.txt"

    @pytest.mark.asyncio
    async def test_upload_success_strips_api_suffix_from_base_url(self, tmp_path):
        """Regression: python-zulip-api's real Client.base_url always ends in
        "/api/" (Client.__init__ unconditionally appends it), while the
        upload endpoint's `uri` is server-root-relative. Naively
        concatenating the two produced "https://host/api//user_uploads/..."
        — a double slash that 404s. The previous test used a bare
        "https://zulip.example.com" base_url with no "/api" suffix at all,
        so it never actually exercised this path."""
        from zulip.media import upload_file_to_zulip

        test_file = tmp_path / "test.png"
        test_file.write_bytes(b"\x89PNG")

        mock_client = MagicMock()
        mock_client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/test.png",
        }
        # Matches the real SDK exactly: rstrip("/") then += "/api/".
        mock_client.base_url = "https://zulip.example.com/api/"

        url = await upload_file_to_zulip(mock_client, str(test_file), str(tmp_path))
        assert url == "https://zulip.example.com/user_uploads/1/test.png"
        assert "/api/" not in url
        assert "//user_uploads" not in url

    @pytest.mark.asyncio
    async def test_upload_success_strips_api_suffix_without_trailing_slash(self, tmp_path):
        """Same as above but base_url ends in bare "/api" (no trailing
        slash) — the other form seen across zulip SDK versions/mocks."""
        from zulip.media import upload_file_to_zulip

        test_file = tmp_path / "test.png"
        test_file.write_bytes(b"\x89PNG")

        mock_client = MagicMock()
        mock_client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/test.png",
        }
        mock_client.base_url = "https://zulip.example.com/api"

        url = await upload_file_to_zulip(mock_client, str(test_file), str(tmp_path))
        assert url == "https://zulip.example.com/user_uploads/1/test.png"

    @pytest.mark.asyncio
    async def test_rejects_symlink(self, tmp_path):
        from zulip.media import upload_file_to_zulip

        # Create a real file outside allowed dir. Deliberately not named
        # "secret.txt": a credential-shaped name is refused by the upload
        # denylist (Issue #138) before the symlink check runs, so the test would
        # pass for the wrong reason.
        outside = tmp_path / ".." / "payload.txt"
        outside.write_text("payload")

        # Create a symlink inside allowed dir pointing outside
        link = tmp_path / "link.txt"
        link.symlink_to(outside.resolve())

        mock_client = MagicMock()

        with pytest.raises(ValueError, match="Symlink rejected"):
            await upload_file_to_zulip(mock_client, str(link), str(tmp_path))

    @pytest.mark.asyncio
    async def test_rejects_path_outside_allowed(self, tmp_path, monkeypatch):
        from zulip.media import upload_file_to_zulip

        # Create a data_dir that is NOT under system temp
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Create a file under a different non-temp directory
        # Use a path that's clearly outside both tmp and data_dir
        import tempfile
        # Temporarily change tempdir to something else so the file isn't under it
        original_temp = tempfile.tempdir
        tempfile.tempdir = str(tmp_path / "other_temp")

        outside = tmp_path / "outside.txt"
        outside.write_text("outside")

        mock_client = MagicMock()
        mock_client.upload_file.return_value = {"result": "error"}

        with pytest.raises(ValueError, match="unauthorized path"):
            await upload_file_to_zulip(mock_client, str(outside), str(data_dir))
        # Verify upload was never called
        mock_client.upload_file.assert_not_called()

        # Restore tempdir
        tempfile.tempdir = original_temp

    @pytest.mark.asyncio
    async def test_allow_dirs_env_permits_extra_root(self, tmp_path, monkeypatch):
        """HERMES_MEDIA_ALLOW_DIRS mirrors gateway.platforms.base's operator
        allowlist env var of the same name — a dir listed there must be
        accepted even when it's outside both system temp and HERMES_DATA_DIR
        (e.g. browser-harness's screenshot cache under ~/.config)."""
        from zulip.media import upload_file_to_zulip

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        extra_root = tmp_path / "browser-harness" / "tmp"
        extra_root.mkdir(parents=True)
        screenshot = extra_root / "shot.png"
        screenshot.write_bytes(b"\x89PNG")

        monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(extra_root))

        mock_client = MagicMock()
        mock_client.upload_file.return_value = {"result": "success", "uri": "/user_uploads/1/shot.png"}
        mock_client.base_url = "https://z.com"

        url = await upload_file_to_zulip(mock_client, str(screenshot), str(data_dir))
        assert url == "https://z.com/user_uploads/1/shot.png"
        mock_client.upload_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_allow_dirs_env_does_not_widen_beyond_listed_root(self, tmp_path, monkeypatch):
        """A sibling directory not itself listed in HERMES_MEDIA_ALLOW_DIRS
        must still be rejected — the allowlist is exact-root, not a prefix
        guess across the whole parent tree."""
        from zulip.media import upload_file_to_zulip

        # Isolate from the real system temp dir (same technique as
        # test_rejects_path_outside_allowed above) so this sibling path
        # isn't accidentally covered by the always-allowed tmp_dir root.
        original_temp = tempfile.tempdir
        tempfile.tempdir = str(tmp_path / "other_temp")

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        allowed_root = tmp_path / "allowed"
        allowed_root.mkdir()
        sibling = tmp_path / "allowed-but-not-really"
        sibling.mkdir()
        outside = sibling / "payload.txt"
        outside.write_text("nope")

        monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(allowed_root))

        mock_client = MagicMock()
        try:
            with pytest.raises(ValueError, match="unauthorized path"):
                await upload_file_to_zulip(mock_client, str(outside), str(data_dir))
            mock_client.upload_file.assert_not_called()
        finally:
            tempfile.tempdir = original_temp

    @pytest.mark.asyncio
    async def test_rejects_nonexistent_file(self, tmp_path):
        from zulip.media import upload_file_to_zulip

        mock_client = MagicMock()

        with pytest.raises(ValueError, match="File not found"):
            await upload_file_to_zulip(mock_client, str(tmp_path / "nonexistent.txt"), str(tmp_path))

    @pytest.mark.asyncio
    async def test_upload_failure_raises(self, tmp_path):
        from zulip.media import upload_file_to_zulip

        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")

        mock_client = MagicMock()
        mock_client.upload_file.return_value = {"result": "error", "msg": "upload failed"}

        with pytest.raises(RuntimeError, match="Upload failed"):
            await upload_file_to_zulip(mock_client, str(test_file), str(tmp_path))

    @pytest.mark.asyncio
    async def test_missing_uri_raises(self, tmp_path):
        from zulip.media import upload_file_to_zulip

        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")

        mock_client = MagicMock()
        mock_client.upload_file.return_value = {"result": "success"}  # No uri

        with pytest.raises(RuntimeError, match="missing uri"):
            await upload_file_to_zulip(mock_client, str(test_file), str(tmp_path))

    @pytest.mark.asyncio
    async def test_relative_path_resolves_against_workspace(self, tmp_path, monkeypatch):
        """A bare relative filename (e.g. 'haiku.txt' from the agent workspace)
        must resolve against the bot workspace root, mirroring the sibling
        OpenClaw plugin's relative-attachment resolution (#268). Previously
        path.resolve() tied it to the process CWD and it was silently dropped."""
        from zulip.media import upload_file_to_zulip

        # Isolate the workspace root under tmp_path so it's inside the
        # always-allowed system temp dir.
        workspace = tmp_path / "hermes_bot_workspace"
        workspace.mkdir(parents=True)
        (workspace / "haiku.txt").write_text("a haiku")

        # Point tempfile.gettempdir() at tmp_path so the workspace root
        # (tmp_path/hermes_bot_workspace) is under the allowed tmp root.
        original_temp = tempfile.tempdir
        tempfile.tempdir = str(tmp_path)
        try:
            mock_client = MagicMock()
            mock_client.upload_file.return_value = {
                "result": "success",
                "uri": "/user_uploads/1/haiku.txt",
            }
            mock_client.base_url = "https://z.com"

            url = await upload_file_to_zulip(mock_client, "haiku.txt", str(tmp_path / "data"))
            assert url == "https://z.com/user_uploads/1/haiku.txt"
            mock_client.upload_file.assert_called_once()
        finally:
            tempfile.tempdir = original_temp

    @pytest.mark.asyncio
    async def test_relative_path_resolves_against_data_dir(self, tmp_path, monkeypatch):
        """If the file isn't in the workspace, a relative name should resolve
        against the data dir next."""
        from zulip.media import upload_file_to_zulip

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "report.csv").write_text("id,value\n1,42")

        original_temp = tempfile.tempdir
        tempfile.tempdir = str(tmp_path / "other_temp")
        try:
            mock_client = MagicMock()
            mock_client.upload_file.return_value = {
                "result": "success",
                "uri": "/user_uploads/1/report.csv",
            }
            mock_client.base_url = "https://z.com"

            url = await upload_file_to_zulip(mock_client, "report.csv", str(data_dir))
            assert url == "https://z.com/user_uploads/1/report.csv"
            mock_client.upload_file.assert_called_once()
        finally:
            tempfile.tempdir = original_temp

    @pytest.mark.asyncio
    async def test_relative_path_traversal_still_rejected(self, tmp_path, monkeypatch):
        """Relative traversal that escapes the sandbox must remain refused
        even with candidate resolution — a path resolving outside every
        allowed root (tmp, data dir, workspace) must not upload."""
        from zulip.media import upload_file_to_zulip

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)

        # Isolate tempdir so the workspace root is under tmp_path, and create
        # the workspace dir so candidate resolution can traverse through it.
        original_temp = tempfile.tempdir
        tempfile.tempdir = str(tmp_path / "other_temp")
        workspace = tmp_path / "other_temp" / "hermes_bot_workspace"
        workspace.mkdir(parents=True)
        # A real file OUTSIDE the isolated tempdir (and outside data_dir),
        # reachable only by climbing out of the workspace. Named benignly on
        # purpose — a credential-shaped name would be caught by the upload
        # denylist (Issue #138) first, and this test is about path traversal.
        outside_file = tmp_path / "payload.txt"
        outside_file.write_text("payload")
        try:
            # From the workspace, ../../payload.txt resolves to
            # tmp_path/payload.txt, which is outside every allowed root → must
            # be refused for that reason.
            mock_client = MagicMock()
            mock_client.upload_file.return_value = {"result": "error"}

            with pytest.raises(ValueError, match="unauthorized path"):
                await upload_file_to_zulip(mock_client, "../../payload.txt", str(data_dir))
            mock_client.upload_file.assert_not_called()
        finally:
            tempfile.tempdir = original_temp


class TestMarkdownAndHtmlExtraction:
    """#271: the content is Markdown, and the old pattern ate the closing token.

    ``UPLOAD_PATH_RE`` was written for ``apply_markdown`` HTML, but the plugin
    never asks for that, so ``message["content"]`` is raw Markdown. On
    ``[shot.png](/user_uploads/2/ab/c1/shot.png)`` it kept the ``)`` and the
    download 404'd. It also could not tell a protocol-relative link from a
    same-site one.
    """

    BASE = "https://chat.example.com"

    def test_markdown_link_does_not_keep_the_closing_paren(self):
        content = "look [shot.png](/user_uploads/2/ab/c1/shot.png) please"
        assert extract_upload_urls(content, self.BASE) == [
            "https://chat.example.com/user_uploads/2/ab/c1/shot.png"
        ]

    def test_markdown_link_with_title(self):
        content = '[report](/user_uploads/1/a/b/report.pdf "the report")'
        assert extract_upload_urls(content, self.BASE) == [
            "https://chat.example.com/user_uploads/1/a/b/report.pdf"
        ]

    def test_html_href_still_works(self):
        content = '<p><a href="/user_uploads/1/2/3/file.png">image</a></p>'
        assert extract_upload_urls(content, self.BASE) == [
            "https://chat.example.com/user_uploads/1/2/3/file.png"
        ]

    def test_bare_path_in_text(self):
        content = "screenshot at /user_uploads/2/ab/c1/shot.png done"
        assert extract_upload_urls(content, self.BASE) == [
            "https://chat.example.com/user_uploads/2/ab/c1/shot.png"
        ]

    def test_absolute_url_on_this_site(self):
        content = "see https://chat.example.com/user_uploads/9/x/y/z.png"
        assert extract_upload_urls(content, self.BASE) == [
            "https://chat.example.com/user_uploads/9/x/y/z.png"
        ]

    def test_protocol_relative_other_site_is_rejected(self):
        """`//other.example/user_uploads/...` is another site, not ours."""
        content = "see //other.example/user_uploads/1/a/b.png"
        assert extract_upload_urls(content, self.BASE) == []

    def test_absolute_url_on_another_site_is_rejected(self):
        content = "see https://evil.example/user_uploads/1/a/b.png"
        assert extract_upload_urls(content, self.BASE) == []

    def test_deduplicates_and_stays_stable(self):
        content = (
            "[a](/user_uploads/1/a/first.png) and again "
            "/user_uploads/1/a/first.png and [b](/user_uploads/2/b/second.pdf)"
        )
        urls = extract_upload_urls(content, self.BASE)
        assert urls == [
            "https://chat.example.com/user_uploads/1/a/first.png",
            "https://chat.example.com/user_uploads/2/b/second.pdf",
        ]

    def test_non_upload_links_are_ignored(self):
        content = "[docs](https://chat.example.com/help) and [x](/user_uploads/1/a/b.png)"
        assert extract_upload_urls(content, self.BASE) == [
            "https://chat.example.com/user_uploads/1/a/b.png"
        ]


class TestFetchUpload:
    """Fetching an upload: same-site only, no redirects, size-capped."""

    BASE = "https://z.com"

    def _response(self, *, status=200, body=b"data", headers=None):
        response = MagicMock()
        response.status_code = status
        response.ok = 200 <= status < 300
        response.headers = headers if headers is not None else {"Content-Type": "image/png"}
        response.content = body
        return response

    @pytest.mark.asyncio
    async def test_success_returns_bytes_type_and_name(self):
        from zulip.media import fetch_upload

        with patch("requests.get", return_value=self._response()) as get:
            result = await fetch_upload(
                "https://z.com/user_uploads/1/2/3/shot.png",
                "auth123",
                max_bytes=1024,
                base_url=self.BASE,
            )

        assert result["data"] == b"data"
        assert result["content_type"] == "image/png"
        assert result["filename"] == "shot.png"
        assert get.call_args.kwargs["allow_redirects"] is False
        assert get.call_args.kwargs["headers"]["Authorization"] == "Basic auth123"

    @pytest.mark.asyncio
    async def test_other_origin_is_refused_without_a_request(self):
        from zulip.media import MediaRefused, fetch_upload

        with patch("requests.get") as get:
            with pytest.raises(MediaRefused, match="not an upload on this Zulip site"):
                await fetch_upload(
                    "https://evil.com/user_uploads/1/2/3/x.png",
                    "auth123",
                    max_bytes=1024,
                    base_url=self.BASE,
                )
        get.assert_not_called()

    @pytest.mark.asyncio
    async def test_other_scheme_is_refused(self):
        from zulip.media import MediaRefused, fetch_upload

        with patch("requests.get") as get:
            with pytest.raises(MediaRefused):
                await fetch_upload(
                    "http://z.com/user_uploads/1/2/3/x.png",
                    "auth123",
                    max_bytes=1024,
                    base_url=self.BASE,
                )
        get.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_redirect_is_refused_not_followed(self):
        from zulip.media import MediaRefused, fetch_upload

        response = self._response(status=302, headers={"Location": "https://evil.com/x"})
        with patch("requests.get", return_value=response):
            with pytest.raises(MediaRefused, match="redirect"):
                await fetch_upload(
                    "https://z.com/user_uploads/1/2/3/x.png",
                    "auth123",
                    max_bytes=1024,
                    base_url=self.BASE,
                )

    @pytest.mark.asyncio
    async def test_size_limit_from_content_length(self):
        from zulip.media import MediaRefused, fetch_upload

        response = self._response(headers={"Content-Length": "5000"})
        with patch("requests.get", return_value=response):
            with pytest.raises(MediaRefused, match="larger than"):
                await fetch_upload(
                    "https://z.com/user_uploads/1/2/3/x.png",
                    "auth123",
                    max_bytes=1024,
                    base_url=self.BASE,
                )

    @pytest.mark.asyncio
    async def test_size_limit_from_body_when_the_header_lies(self):
        from zulip.media import MediaRefused, fetch_upload

        response = self._response(body=b"x" * 2000, headers={})
        with patch("requests.get", return_value=response):
            with pytest.raises(MediaRefused, match="larger than"):
                await fetch_upload(
                    "https://z.com/user_uploads/1/2/3/x.png",
                    "auth123",
                    max_bytes=1024,
                    base_url=self.BASE,
                )

    @pytest.mark.asyncio
    async def test_bad_status_is_refused(self):
        from zulip.media import MediaRefused, fetch_upload

        with patch("requests.get", return_value=self._response(status=404)):
            with pytest.raises(MediaRefused, match="404"):
                await fetch_upload(
                    "https://z.com/user_uploads/1/2/3/x.png",
                    "auth123",
                    max_bytes=1024,
                    base_url=self.BASE,
                )


class TestCacheInboundUpload:
    """The host's own media cache is used when it exists (#271)."""

    def test_host_cache_is_preferred(self, monkeypatch):
        from zulip import media

        monkeypatch.setattr(
            media,
            "_host_cache_media_bytes",
            lambda data, filename, mime: {
                "path": "/home/h/.hermes/cache/images/img_1.png",
                "media_type": "image/png",
                "kind": "image",
            },
        )

        cached = media.cache_inbound_upload(b"bytes", "shot.png", "image/png")
        assert cached == {
            "path": "/home/h/.hermes/cache/images/img_1.png",
            "media_type": "image/png",
            "kind": "image",
        }

    def test_falls_back_to_a_tempfile_when_the_host_has_no_cache(self, monkeypatch):
        from zulip import media

        monkeypatch.setattr(media, "_host_cache_media_bytes", lambda *a, **k: None)
        cached = media.cache_inbound_upload(b"bytes", "shot.png", "image/png")
        try:
            assert Path(cached["path"]).read_bytes() == b"bytes"
            assert cached["media_type"] == "image/png"
            assert cached["kind"] == "image"
        finally:
            Path(cached["path"]).unlink()

    def test_a_document_is_classified_as_a_document(self, monkeypatch):
        from zulip import media

        monkeypatch.setattr(media, "_host_cache_media_bytes", lambda *a, **k: None)
        cached = media.cache_inbound_upload(b"%PDF", "a.pdf", "application/pdf")
        try:
            assert cached["kind"] == "document"
        finally:
            Path(cached["path"]).unlink()

    def test_host_cache_that_raises_falls_back(self, monkeypatch):
        from zulip import media

        def boom(*a, **k):
            raise RuntimeError("host cache exploded")

        monkeypatch.setattr(media, "_host_cache_media_bytes", boom)
        cached = media.cache_inbound_upload(b"bytes", "shot.png", "image/png")
        try:
            assert Path(cached["path"]).exists()
        finally:
            Path(cached["path"]).unlink()
