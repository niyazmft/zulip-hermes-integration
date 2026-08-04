"""Smoke tests for ZulipAdapter import and basic instantiation."""

import pytest


class TestAdapterImport:
    def test_adapter_imports(self):
        from zulip.adapter import ZulipAdapter
        assert ZulipAdapter is not None

    def test_register_function_imports(self):
        from zulip.adapter import register
        assert callable(register)

    def test_init_imports(self):
        from zulip import register
        assert callable(register)


class TestAdapterInstantiation:
    def test_adapter_can_be_instantiated(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        # Patch ZULIP_AVAILABLE so the adapter doesn't bail out
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)
        # Patch the zulip module (official SDK) to return our mock client
        from tests.conftest import MockZulipClient

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = MockZulipClient(**kwargs)
                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())

        from zulip.adapter import ZulipAdapter
        adapter = ZulipAdapter(mock_platform_config)
        assert adapter.api_key == "fake-key"
        assert adapter.email == "bot@test.zulipchat.com"
        assert adapter.site == "https://test.zulipchat.com"

    def test_adapter_missing_zulip_raises(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "_import_zulip_sdk", lambda: None)

        from zulip.adapter import ZulipAdapter
        with pytest.raises(ImportError, match="zulip package not installed"):
            ZulipAdapter(mock_platform_config)


class TestInputValidation:
    """Tests for _validate_message_id, _validate_stream_id, _validate_user_id."""

    def test_validate_message_id_valid(self):
        from zulip.adapter import ZulipAdapter
        assert ZulipAdapter._validate_message_id(123) == 123
        assert ZulipAdapter._validate_message_id("456") == 456

    def test_validate_message_id_rejects_zero(self):
        from zulip.adapter import ZulipAdapter
        with pytest.raises(ValueError, match="message_id must be positive"):
            ZulipAdapter._validate_message_id(0)

    def test_validate_message_id_rejects_negative(self):
        from zulip.adapter import ZulipAdapter
        with pytest.raises(ValueError, match="message_id must be positive"):
            ZulipAdapter._validate_message_id(-1)

    def test_validate_message_id_rejects_none(self):
        from zulip.adapter import ZulipAdapter
        with pytest.raises(ValueError, match="message_id is required"):
            ZulipAdapter._validate_message_id(None)

    def test_validate_message_id_rejects_non_numeric(self):
        from zulip.adapter import ZulipAdapter
        with pytest.raises(ValueError, match="Invalid message_id"):
            ZulipAdapter._validate_message_id("abc/../def")

    def test_validate_stream_id_valid(self):
        from zulip.adapter import ZulipAdapter
        assert ZulipAdapter._validate_stream_id(42) == 42
        assert ZulipAdapter._validate_stream_id("99") == 99

    def test_validate_stream_id_rejects_zero(self):
        from zulip.adapter import ZulipAdapter
        with pytest.raises(ValueError, match="stream_id must be positive"):
            ZulipAdapter._validate_stream_id(0)

    def test_validate_user_id_valid(self):
        from zulip.adapter import ZulipAdapter
        assert ZulipAdapter._validate_user_id(7) == 7
        assert ZulipAdapter._validate_user_id("8") == 8

    def test_validate_user_id_rejects_zero(self):
        from zulip.adapter import ZulipAdapter
        with pytest.raises(ValueError, match="user_id must be positive"):
            ZulipAdapter._validate_user_id(0)


class TestSdkCallWithRetry:
    """Tests for _sdk_call_with_retry."""

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)
        from tests.conftest import MockZulipClient

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = MockZulipClient(**kwargs)
                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)

        # _sdk_call wraps in asyncio.to_thread, so the fn must be sync
        def mock_fn():
            return "success"

        result = await adapter._sdk_call_with_retry(mock_fn, timeout=30)
        assert result == "success"

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)
        from tests.conftest import MockZulipClient

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = MockZulipClient(**kwargs)
                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)

        call_count = 0

        def mock_fn():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("429 Too Many Requests")
            return "success"

        result = await adapter._sdk_call_with_retry(mock_fn, timeout=30, max_retries=3)
        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)
        from tests.conftest import MockZulipClient

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = MockZulipClient(**kwargs)
                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)

        def mock_fn():
            raise RuntimeError("503 Service Unavailable")

        with pytest.raises(RuntimeError, match="503 Service Unavailable"):
            await adapter._sdk_call_with_retry(mock_fn, timeout=30, max_retries=2)

    @pytest.mark.asyncio
    async def test_does_not_retry_timeout(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        import asyncio
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)
        from tests.conftest import MockZulipClient

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = MockZulipClient(**kwargs)
                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)

        call_count = 0

        def mock_fn():
            nonlocal call_count
            call_count += 1
            raise asyncio.TimeoutError()

        with pytest.raises(asyncio.TimeoutError):
            await adapter._sdk_call_with_retry(mock_fn, timeout=30)
        assert call_count == 1  # Should not retry on timeout


class TestNewApiMethods:
    """Tests for new Zulip API methods on ZulipAdapter."""

    @pytest.fixture
    def adapter(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)
        from tests.conftest import MockZulipClient

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = MockZulipClient(**kwargs)
                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter
        return ZulipAdapter(mock_platform_config)

    @pytest.mark.asyncio
    async def test_fetch_messages(self, adapter):
        # Mock the SDK call to return messages
        adapter.client.get_messages = MagicMock(return_value={
            "result": "success",
            "messages": [{"id": 1, "content": "hello"}],
        })

        messages = await adapter.fetch_messages("general", limit=10)
        assert len(messages) == 1
        assert messages[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_search_messages(self, adapter):
        adapter.client.get_messages = MagicMock(return_value={
            "result": "success",
            "messages": [{"id": 2, "content": "result"}],
        })

        messages = await adapter.search_messages("query", stream="general")
        assert len(messages) == 1

    @pytest.mark.asyncio
    async def test_list_streams(self, adapter):
        adapter.client.get_streams = MagicMock(return_value={
            "result": "success",
            "streams": [{"name": "general"}],
        })

        streams = await adapter.list_streams()
        assert len(streams) == 1
        assert streams[0]["name"] == "general"

    @pytest.mark.asyncio
    async def test_subscribe_stream(self, adapter):
        adapter.client.add_subscriptions = MagicMock(return_value={"result": "success"})

        result = await adapter.subscribe_stream("general")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_message(self, adapter):
        adapter.client.delete_message = MagicMock(return_value={"result": "success"})

        result = await adapter.delete_message(123)
        assert result is True

    @pytest.mark.asyncio
    async def test_get_user_presence(self, adapter):
        adapter.client.get_user_presence = MagicMock(return_value={
            "result": "success",
            "presence": {"status": "active"},
        })

        presence = await adapter.get_user_presence("user@test.com")
        assert presence == {"status": "active"}

    @pytest.mark.asyncio
    async def test_fetch_messages_handles_error(self, adapter):
        adapter.client.get_messages = MagicMock(side_effect=RuntimeError("API error"))

        messages = await adapter.fetch_messages("general")
        assert messages == []


from unittest.mock import MagicMock
