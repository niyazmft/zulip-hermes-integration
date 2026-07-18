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
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", False)

        from zulip.adapter import ZulipAdapter
        with pytest.raises(ImportError, match="zulip package not installed"):
            ZulipAdapter(mock_platform_config)
