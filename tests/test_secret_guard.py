"""Tests for the outbound secret guard (Issue #136).

The guard exists because a path allowlist on uploads cannot cover
read-and-type exfiltration: an agent that can read the host config can simply
paste the credential into chat, and nothing is uploaded. These tests pin both
halves of that: the collection heuristic, and the two refuse-to-transmit choke
points.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zulip.secret_guard import (
    MAX_WALK_DEPTH,
    MIN_SECRET_LENGTH,
    block_secret_leaks_enabled,
    collect_known_secrets,
    describe_leaked_secrets,
    find_leaked_secrets,
    is_credential_key,
    redact_secrets,
)

SECRET = "sk-live-0123456789abcdef"  # 24 chars: comfortably over the minimum
SHORT = "hunter2"  # 7 chars: below the minimum, must never be treated as one


def names(secrets):
    return sorted(s.name for s in secrets)


def values(secrets):
    return sorted(s.value for s in secrets)


class TestCredentialKeyHeuristic:
    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "apiKey",
            "API-KEY",
            "apikey",
            "token",
            "access_token",
            "secret",
            "client_secret",
            "password",
            "passwd",
            "credential",
            "credentials",
        ],
    )
    def test_credential_shaped_keys(self, key):
        assert is_credential_key(key) is True

    @pytest.mark.parametrize(
        "key", ["email", "site", "chat_id", "stream", "timeout", "prefix", "topic"]
    )
    def test_ordinary_keys(self, key):
        assert is_credential_key(key) is False


class TestCollectKnownSecrets:
    def test_walks_nested_config_and_names_the_source(self):
        cfg = {"channels": {"zulip": {"apiKey": SECRET, "site": "https://x.test"}}}
        found = collect_known_secrets(cfg, env={})
        assert values(found) == [SECRET]
        # The source path is what gets audited — never the value.
        assert names(found) == ["channels.zulip.apiKey"]

    def test_ignores_non_credential_keys(self):
        cfg = {"email": "bot@example.test", "site": "https://example.test"}
        assert collect_known_secrets(cfg, env={}) == []

    def test_ignores_short_values(self):
        assert collect_known_secrets({"api_key": SHORT}, env={}) == []
        assert len(SHORT) < MIN_SECRET_LENGTH

    def test_ignores_non_string_values(self):
        cfg = {"api_key": 1234567890123456, "token": None, "secret": ["x" * 40]}
        assert collect_known_secrets(cfg, env={}) == []

    def test_same_value_reported_once_under_its_first_name(self):
        cfg = {"a": {"api_key": SECRET}, "b": {"token": SECRET}}
        found = collect_known_secrets(cfg, env={})
        assert values(found) == [SECRET]
        assert names(found) == ["a.api_key"]

    def test_walks_lists_with_index_paths(self):
        cfg = {"accounts": [{"api_key": SECRET}]}
        assert names(collect_known_secrets(cfg, env={})) == ["accounts.0.api_key"]

    def test_depth_limit_is_respected(self):
        node: dict = {"api_key": SECRET}
        for _ in range(MAX_WALK_DEPTH + 2):
            node = {"secret": node}
        assert collect_known_secrets(node, env={}) == []

    def test_reads_credentials_from_the_environment(self):
        found = collect_known_secrets(
            {}, env={"ZULIP_API_KEY": SECRET, "PATH": "/usr/bin"}
        )
        assert values(found) == [SECRET]
        assert names(found) == ["env.ZULIP_API_KEY"]

    def test_explicit_extras_are_collected(self):
        found = collect_known_secrets({}, extra=[("zulip.api_key", SECRET)], env={})
        assert names(found) == ["zulip.api_key"]

    def test_value_is_trimmed(self):
        found = collect_known_secrets({}, extra=[("zulip.api_key", f"  {SECRET}  ")], env={})
        assert values(found) == [SECRET]


class TestFindLeakedSecrets:
    def test_finds_a_verbatim_value_inside_prose(self):
        secrets = collect_known_secrets({}, extra=[("zulip.api_key", SECRET)], env={})
        hits = find_leaked_secrets(f"Here is the key: {SECRET} — enjoy", secrets)
        assert [h.name for h in hits] == ["zulip.api_key"]

    def test_no_match_for_clean_text(self):
        secrets = collect_known_secrets({}, extra=[("zulip.api_key", SECRET)], env={})
        assert find_leaked_secrets("just a normal reply", secrets) == []

    def test_empty_inputs(self):
        assert find_leaked_secrets("", []) == []
        assert find_leaked_secrets("text", []) == []
        assert find_leaked_secrets("", collect_known_secrets({}, extra=[("k", SECRET)], env={})) == []


class TestDescribeLeakedSecrets:
    def test_names_the_source_and_never_the_value(self):
        secrets = collect_known_secrets(
            {"channels": {"zulip": {"apiKey": SECRET}}}, env={}
        )
        summary = describe_leaked_secrets(find_leaked_secrets(SECRET, secrets))
        assert "channels.zulip.apiKey" in summary
        assert SECRET not in summary
        assert "1 credential value(s)" in summary


class TestRedactSecrets:
    def test_replaces_every_occurrence(self):
        secrets = collect_known_secrets({}, extra=[("zulip.api_key", SECRET)], env={})
        text, count = redact_secrets(f"{SECRET} and again {SECRET}", secrets)
        assert count == 1
        assert SECRET not in text
        assert text.count("[redacted]") == 2

    def test_longest_value_first_leaves_no_fragment(self):
        """A credential containing another one must not leave the tail behind."""
        long_secret = SECRET + "-suffix"
        secrets = collect_known_secrets(
            {}, extra=[("a.long", long_secret), ("b.short", SECRET)], env={}
        )
        text, _ = redact_secrets(long_secret, secrets)
        assert text == "[redacted]"

    def test_clean_text_is_untouched(self):
        secrets = collect_known_secrets({}, extra=[("api_key", SECRET)], env={})
        text, count = redact_secrets("nothing to see", secrets)
        assert text == "nothing to see"
        assert count == 0

    def test_empty_text(self):
        assert redact_secrets("", collect_known_secrets({}, extra=[("k", SECRET)], env={})) == ("", 0)


class TestBlockSecretLeaksEnabled:
    def test_enabled_by_default(self):
        assert block_secret_leaks_enabled({}) is True

    @pytest.mark.parametrize("raw", ["", "  "])
    def test_empty_value_means_default(self, raw):
        assert block_secret_leaks_enabled({"ZULIP_BLOCK_SECRET_LEAKS": raw}) is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " off "])
    def test_explicit_off(self, raw):
        assert block_secret_leaks_enabled({"ZULIP_BLOCK_SECRET_LEAKS": raw}) is False

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on"])
    def test_explicit_on(self, raw):
        assert block_secret_leaks_enabled({"ZULIP_BLOCK_SECRET_LEAKS": raw}) is True


# --- integration: the two outbound choke points ---------------------------

_ENV = {
    "ZULIP_SITE": "https://zulip.example.test",
    "ZULIP_EMAIL": "bot@example.test",
    "ZULIP_API_KEY": SECRET,
}


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    import zulip.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)
    monkeypatch.setenv("ZULIP_API_KEY", SECRET)
    monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
    monkeypatch.setenv("ZULIP_SITE", "https://test.zulipchat.com")
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                self._calls = []

            def send_message(self, request):
                self._calls.append(request)
                return {"result": "success", "id": len(self._calls) + 100}

    monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(mock_platform_config)
    a._audit_logger = MagicMock()
    a._audit_logger.log_event = AsyncMock()
    # Delivery-outcome events (#145) are recorded on the same logger; make every
    # helper awaitable so the send path can call them without a real file write.
    a._audit_logger.log_deliver_payload = AsyncMock()
    a._audit_logger.log_deliver_skipped = AsyncMock()
    a._audit_logger.log_deliver_empty = AsyncMock()
    a._audit_logger.log_deliver_failed = AsyncMock()
    a._audit_logger.log_dispatch_turn = AsyncMock()
    return a


class TestSendRefusesCredentials:
    @pytest.mark.asyncio
    async def test_message_containing_a_credential_is_refused(self, adapter):
        result = await adapter.send("dm:42", f"the key is {SECRET}")
        assert result.success is False
        assert adapter.client._calls == [], "nothing may reach Zulip"

    @pytest.mark.asyncio
    async def test_ordinary_message_still_sends(self, adapter):
        result = await adapter.send("dm:42", "a perfectly ordinary reply")
        assert result.success is True
        assert len(adapter.client._calls) == 1

    @pytest.mark.asyncio
    async def test_audit_event_names_sources_and_never_the_value(self, adapter):
        await adapter.send("dm:42", f"key: {SECRET}")

        args = adapter._audit_logger.log_event.await_args
        assert args.args[0] == "secret_leak_blocked"
        details = args.args[1]
        assert details["direction"] == "outbound"
        assert details["count"] == 1
        assert details["sources"], "the audit must name where it came from"
        # The message describing a leak must not become one.
        assert SECRET not in json.dumps(details)

    @pytest.mark.asyncio
    async def test_no_audit_event_for_a_clean_send(self, adapter):
        await adapter.send("dm:42", "nothing sensitive")
        adapter._audit_logger.log_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_opt_out_allows_the_message_through(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_BLOCK_SECRET_LEAKS", "0")
        result = await adapter.send("dm:42", f"key: {SECRET}")
        assert result.success is True
        assert len(adapter.client._calls) == 1

    @pytest.mark.asyncio
    async def test_refusal_happens_before_media_upload(self, adapter, monkeypatch):
        """A refused message must not leave a stray upload behind."""
        uploaded = AsyncMock(side_effect=AssertionError("upload must not run"))
        monkeypatch.setattr("zulip.adapter.upload_file_to_zulip", uploaded)

        result = await adapter.send(
            "dm:42", f"key: {SECRET}", media_files=["/tmp/report.pdf"]
        )

        assert result.success is False
        uploaded.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_short_innocuous_text_is_not_blocked(self, adapter, monkeypatch):
        """A short credential-shaped value must not match ordinary prose."""
        adapter._known_secrets_cache = collect_known_secrets(
            {}, extra=[("zulip.api_key", SHORT)], env={}
        )
        result = await adapter.send("dm:42", "my hunter2 is not a secret here")
        assert result.success is True


class TestStandaloneSendRefusesCredentials:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch, tmp_path):
        import zulip.adapter as adapter_module

        adapter_module._clear_caches()
        monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
        for key in list(_ENV) + ["ZULIP_BLOCK_SECRET_LEAKS"]:
            monkeypatch.delenv(key, raising=False)
        yield
        adapter_module._clear_caches()

    @pytest.mark.asyncio
    async def test_standalone_delivery_is_refused(self, monkeypatch):
        import zulip.adapter as adapter_module
        from zulip.adapter import _standalone_send

        for key, value in _ENV.items():
            monkeypatch.setenv(key, value)

        client = MagicMock()
        client.send_message.return_value = {"result": "success", "id": 1}
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(
                SimpleNamespace(extra={}), "dm:8", f"here: {SECRET}"
            )

        assert "error" in result and "credential" in result["error"]
        client.send_message.assert_not_called()
        assert SECRET not in result["error"], "the refusal must not echo the value"

    @pytest.mark.asyncio
    async def test_standalone_delivery_allowed_when_guard_disabled(self, monkeypatch):
        import zulip.adapter as adapter_module
        from zulip.adapter import _standalone_send

        for key, value in _ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("ZULIP_BLOCK_SECRET_LEAKS", "0")

        client = MagicMock()
        client.send_message.return_value = {"result": "success", "id": 1}
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(
                SimpleNamespace(extra={}), "dm:8", f"here: {SECRET}"
            )

        assert result.get("success") is True
        client.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_standalone_clean_message_still_sends(self, monkeypatch):
        import zulip.adapter as adapter_module
        from zulip.adapter import _standalone_send

        for key, value in _ENV.items():
            monkeypatch.setenv(key, value)

        client = MagicMock()
        client.send_message.return_value = {"result": "success", "id": 1}
        with patch.object(adapter_module, "_get_cached_client", return_value=client):
            result = await _standalone_send(
                SimpleNamespace(extra={}), "dm:8", "cron job finished"
            )

        assert result.get("success") is True
        client.send_message.assert_called_once()
