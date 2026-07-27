"""Tests for Zulip SDK network timeouts (Issue #62).

Verifies that:
- Timeout values are read from environment variables
- SDK calls are wrapped with asyncio.wait_for
- Timeouts produce proper log warnings
"""

import asyncio
import os
from unittest.mock import MagicMock, patch
import pytest

from zulip.adapter import _resolve_timeouts, ZulipAdapter


class TestResolveTimeouts:
    """Test timeout config parsing from environment."""

    def test_defaults_when_no_env_vars(self):
        """All defaults returned when env is empty."""
        with patch.dict(os.environ, {}, clear=True):
            connect, read, send = _resolve_timeouts()
        assert connect == 30.0
        assert read == 60.0
        assert send == 90.0

    def test_custom_values_from_env(self):
        """Custom values parsed correctly."""
        with patch.dict(
            os.environ,
            {
                "ZULIP_CONNECT_TIMEOUT": "10",
                "ZULIP_READ_TIMEOUT": "45",
                "ZULIP_SEND_TIMEOUT": "120",
            },
            clear=True,
        ):
            connect, read, send = _resolve_timeouts()
        assert connect == 10.0
        assert read == 45.0
        assert send == 120.0

    def test_float_values_accepted(self):
        """Float values like 7.5 are parsed."""
        with patch.dict(
            os.environ,
            {"ZULIP_CONNECT_TIMEOUT": "7.5"},
            clear=True,
        ):
            connect, _read, _send = _resolve_timeouts()
        assert connect == 7.5

    def test_invalid_values_fall_back_to_default(self):
        """Non-numeric values fall back to defaults."""
        with patch.dict(
            os.environ,
            {"ZULIP_CONNECT_TIMEOUT": "not_a_number"},
            clear=True,
        ):
            connect, _read, _send = _resolve_timeouts()
        assert connect == 30.0

    def test_whitespace_trimmed(self):
        """Leading/trailing whitespace is trimmed."""
        with patch.dict(
            os.environ,
            {"ZULIP_SEND_TIMEOUT": "  15  "},
            clear=True,
        ):
            _connect, _read, send = _resolve_timeouts()
        assert send == 15.0


class TestSdkCallTimeout:
    """Test that _sdk_call enforces timeouts."""

    @pytest.mark.asyncio
    async def test_sdk_call_succeeds_within_timeout(self, caplog):
        """Normal completion when function returns in time."""
        config = MagicMock()
        config.extra = {}

        with patch.dict(
            os.environ,
            {
                "ZULIP_API_KEY": "test-key",
                "ZULIP_EMAIL": "bot@test.com",
                "ZULIP_SITE": "https://test.zulipchat.com",
            },
            clear=True,
        ):
            with patch("zulip.adapter._import_zulip_sdk") as mock_sdk:
                client = MagicMock()
                mock_sdk.return_value = MagicMock(Client=lambda **kw: client)
                adapter = ZulipAdapter(config)

        def fast_fn():
            return {"result": "success"}

        result = await adapter._sdk_call(fast_fn, timeout=1.0)
        assert result == {"result": "success"}

    @pytest.mark.asyncio
    async def test_sdk_call_raises_timeout_error(self, caplog):
        """asyncio.TimeoutError raised when function exceeds timeout."""
        config = MagicMock()
        config.extra = {}

        with patch.dict(
            os.environ,
            {
                "ZULIP_API_KEY": "test-key",
                "ZULIP_EMAIL": "bot@test.com",
                "ZULIP_SITE": "https://test.zulipchat.com",
            },
            clear=True,
        ):
            with patch("zulip.adapter._import_zulip_sdk") as mock_sdk:
                client = MagicMock()
                mock_sdk.return_value = MagicMock(Client=lambda **kw: client)
                adapter = ZulipAdapter(config)

        def slow_fn():
            import time

            time.sleep(10)
            return {"result": "success"}

        with pytest.raises(asyncio.TimeoutError):
            await adapter._sdk_call(slow_fn, timeout=0.01)

        # Warning log emitted
        assert any(
            "SDK call timed out" in rec.message for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_sdk_call_logs_warning_on_timeout(self, caplog):
        """Timeout produces a warning log with function name."""
        config = MagicMock()
        config.extra = {}

        with patch.dict(
            os.environ,
            {
                "ZULIP_API_KEY": "test-key",
                "ZULIP_EMAIL": "bot@test.com",
                "ZULIP_SITE": "https://test.zulipchat.com",
            },
            clear=True,
        ):
            with patch("zulip.adapter._import_zulip_sdk") as mock_sdk:
                client = MagicMock()
                mock_sdk.return_value = MagicMock(Client=lambda **kw: client)
                adapter = ZulipAdapter(config)

        def my_slow_function():
            import time

            time.sleep(10)

        with pytest.raises(asyncio.TimeoutError):
            await adapter._sdk_call(my_slow_function, timeout=0.01)

        log_msg = " ".join(r.message for r in caplog.records)
        assert "my_slow_function" in log_msg
