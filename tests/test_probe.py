"""Tests for zulip.probe — health probe and SSRF protection."""

import asyncio
from unittest.mock import patch, MagicMock

import pytest

from zulip.probe import (
    probe_zulip,
    _normalize_base_url,
    _is_internal_host,
)


class TestNormalizeBaseUrl:
    def test_accepts_https(self):
        assert _normalize_base_url("https://example.zulipchat.com") == "https://example.zulipchat.com"

    def test_accepts_http(self):
        assert _normalize_base_url("http://internal.local") == "http://internal.local"

    def test_removes_trailing_slash(self):
        assert _normalize_base_url("https://example.com/") == "https://example.com"

    def test_rejects_file_scheme(self):
        assert _normalize_base_url("file:///etc/passwd") is None

    def test_rejects_gopher(self):
        assert _normalize_base_url("gopher://example.com") is None

    def test_rejects_localhost(self):
        assert _normalize_base_url("http://localhost:8080") is None

    def test_rejects_localhost_localdomain(self):
        assert _normalize_base_url("https://localhost.localdomain") is None


class TestIsInternalHost:
    def test_rejects_private_ranges(self):
        assert _is_internal_host("127.0.0.1") is True
        assert _is_internal_host("10.0.0.1") is True
        assert _is_internal_host("192.168.1.1") is True
        assert _is_internal_host("172.16.0.1") is True
        assert _is_internal_host("172.31.255.255") is True
        assert _is_internal_host("169.254.1.1") is True
        assert _is_internal_host("0.0.0.0") is True

    def test_accepts_public_ips(self):
        assert _is_internal_host("8.8.8.8") is False
        assert _is_internal_host("1.2.3.4") is False
        assert _is_internal_host("example.com") is False

    def test_rejects_aws_metadata(self):
        assert _is_internal_host("169.254.169.254") is True


class TestProbeZulip:
    @pytest.mark.asyncio
    async def test_probe_success(self):
        mock_response = MagicMock()
        mock_response.read.return_value = (
            '{"result": "success", "user_id": 42, "email": "bot@z.com", "full_name": "Bot"}'
            .encode()
        )

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = await probe_zulip("https://z.com", "bot@z.com", "key123")

        assert result["ok"] is True
        assert result["bot"]["id"] == "42"
        assert result["bot"]["email"] == "bot@z.com"
        assert result["bot"]["full_name"] == "Bot"

    @pytest.mark.asyncio
    async def test_probe_auth_failure(self):
        from urllib.error import HTTPError

        with patch("urllib.request.urlopen", side_effect=HTTPError(
            "https://z.com/api/v1/users/me", 401, "Unauthorized", {}, None
        )):
            result = await probe_zulip("https://z.com", "bad", "bad")

        assert result["ok"] is False
        assert "401" in result["error"]

    @pytest.mark.asyncio
    async def test_probe_timeout(self):
        import asyncio

        async def slow_call(*args, **kwargs):
            await asyncio.sleep(100)
            return MagicMock()

        loop = asyncio.get_event_loop()
        with patch.object(loop, "run_in_executor", side_effect=slow_call):
            result = await probe_zulip("https://z.com", "bot@z.com", "key", timeout=0.1)

        assert result["ok"] is False
        assert "timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_probe_rejects_internal_url(self):
        result = await probe_zulip("http://127.0.0.1", "bot@z.com", "key")
        assert result["ok"] is False
        assert "internal" in result["error"].lower() or "invalid" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_probe_rejects_localhost(self):
        result = await probe_zulip("http://localhost", "bot@z.com", "key")
        assert result["ok"] is False
