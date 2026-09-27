"""Tests for HTTPS enforcement on ZULIP_SITE (Issue #137).

Zulip sends the bot API key as HTTP Basic on **every** request, so a plain-http
realm puts that credential on the wire in cleartext. https is therefore the
default requirement, and `ZULIP_ALLOW_INSECURE_HTTP` is the explicit opt-in for
a self-hosted realm on a trusted network.

The opt-in has to relax two checks **together**: a LAN Zulip *is* a private
address, so relaxing only the scheme would still refuse the exact case the
opt-in exists for.
"""

import pytest

from zulip.probe import (
    INSECURE_HTTP_ENV,
    _normalize_base_url,
    _validate_media_url,
    allow_insecure_http_enabled,
    base_url_error,
    probe_zulip,
)

OPT_IN = "ZULIP_ALLOW_INSECURE_HTTP"


@pytest.fixture(autouse=True)
def _no_opt_in(monkeypatch):
    """Every test starts with the opt-in unset unless it sets it itself."""
    monkeypatch.delenv(OPT_IN, raising=False)


class TestDefaultRequiresHttps:
    def test_public_http_is_refused(self):
        assert _normalize_base_url("http://chat.example.com") is None

    def test_https_is_unchanged(self):
        assert _normalize_base_url("https://chat.example.com") == "https://chat.example.com"
        assert _normalize_base_url("https://chat.example.com/") == "https://chat.example.com"

    def test_private_host_is_still_refused_over_https(self):
        assert _normalize_base_url("https://192.168.1.10") is None

    def test_non_http_schemes_are_still_refused(self):
        assert _normalize_base_url("ftp://chat.example.com") is None


class TestOptIn:
    def test_allows_public_http(self, monkeypatch):
        monkeypatch.setenv(OPT_IN, "1")
        assert _normalize_base_url("http://chat.example.com") == "http://chat.example.com"

    def test_allows_a_private_address(self, monkeypatch):
        """The case the opt-in exists for: a LAN Zulip on a trusted network."""
        monkeypatch.setenv(OPT_IN, "1")
        assert (
            _normalize_base_url("http://192.168.1.10:9991")
            == "http://192.168.1.10:9991"
        )

    def test_allows_the_zulip_dev_server(self, monkeypatch):
        monkeypatch.setenv(OPT_IN, "1")
        assert _normalize_base_url("http://localhost:9991") == "http://localhost:9991"

    def test_opt_in_is_read_at_call_time_not_captured(self, monkeypatch):
        """Regression guard for the sibling's field bug.

        Their ``allowInsecureHttp`` was silently discarded on later calls
        because the base URL was re-normalized without passing the option, so a
        realm that validated at setup then failed at runtime. Normalizing twice
        must give the same answer — which it does by reading the environment
        where the decision is made instead of threading a flag by hand.
        """
        monkeypatch.setenv(OPT_IN, "1")
        first = _normalize_base_url("http://localhost:9991")
        second = _normalize_base_url("http://localhost:9991")
        assert first == second == "http://localhost:9991"

    def test_inbound_media_urls_are_not_relaxed_by_the_opt_in(self, monkeypatch):
        """A media URL host can come from a message, so it must stay strict.

        The opt-in is about the operator's own server; it must not widen what
        an inbound message can make the plugin fetch.
        """
        monkeypatch.setenv(OPT_IN, "1")
        assert _validate_media_url("http://192.168.1.10/file.png") is False
        assert _validate_media_url("http://localhost/file.png") is False
        assert _validate_media_url("https://example.com/file.png") is True


class TestAllowInsecureHttpEnabled:
    def test_default_is_off(self):
        assert allow_insecure_http_enabled({}) is False

    @pytest.mark.parametrize("raw", ["1", "true", "YES", "on", " true "])
    def test_truthy(self, raw):
        assert allow_insecure_http_enabled({OPT_IN: raw}) is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
    def test_falsy(self, raw):
        assert allow_insecure_http_enabled({OPT_IN: raw}) is False


class TestBaseUrlError:
    def test_names_the_opt_in_when_that_is_the_reason(self):
        message = base_url_error("http://chat.example.com")
        assert INSECURE_HTTP_ENV in message
        assert "cleartext" in message

    def test_names_the_opt_in_for_a_refused_private_host(self):
        assert INSECURE_HTTP_ENV in base_url_error("https://192.168.1.10")

    def test_does_not_mention_the_opt_in_for_a_genuinely_broken_url(self):
        """Naming the switch when it would not help only misleads."""
        message = base_url_error("ftp://chat.example.com")
        assert INSECURE_HTTP_ENV not in message


class TestProbeErrorNamesTheFlag:
    @pytest.mark.asyncio
    async def test_probe_reports_the_opt_in_for_an_http_realm(self):
        result = await probe_zulip("http://chat.example.com", "bot@example.com", "key")
        assert result["ok"] is False
        assert INSECURE_HTTP_ENV in result["error"]

    @pytest.mark.asyncio
    async def test_probe_does_not_contact_the_network_when_refusing(self, monkeypatch):
        """A refused URL must fail before any request is attempted."""
        called = []
        monkeypatch.setattr(
            "urllib.request.urlopen", lambda *a, **k: called.append(1)
        )
        result = await probe_zulip("http://chat.example.com", "bot@example.com", "key")
        assert result["ok"] is False
        assert called == []


class TestAdapterSiteEnforcement:
    @pytest.fixture
    def _sdk(self, monkeypatch, tmp_path):
        import zulip.adapter as adapter_module

        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    pass

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        monkeypatch.setenv("ZULIP_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_API_KEY", "k" * 32)
        # Keep the adapter's state (audit log, queue, dedupe) out of ~/.hermes.
        monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))

    def test_adapter_refuses_an_http_site_by_default(self, mock_platform_config, monkeypatch, _sdk):
        monkeypatch.setenv("ZULIP_SITE", "http://chat.example.com")
        from zulip.adapter import ZulipAdapter

        with pytest.raises(ValueError) as excinfo:
            ZulipAdapter(mock_platform_config)
        assert INSECURE_HTTP_ENV in str(excinfo.value)

    def test_adapter_accepts_a_private_site_with_the_opt_in(self, mock_platform_config, monkeypatch, _sdk):
        monkeypatch.setenv("ZULIP_ALLOW_INSECURE_HTTP", "1")
        monkeypatch.setenv("ZULIP_SITE", "http://localhost:9991")
        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)
        assert adapter.site == "http://localhost:9991"

    def test_adapter_still_rejects_internal_hosts_over_https(self, mock_platform_config, monkeypatch, _sdk):
        monkeypatch.setenv("ZULIP_SITE", "https://192.168.1.10")
        from zulip.adapter import ZulipAdapter

        with pytest.raises(ValueError):
            ZulipAdapter(mock_platform_config)
