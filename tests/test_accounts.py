"""Tests for multi-account config resolver (Issue #48)."""

import os
import pytest

from zulip.accounts import AccountResolver, ZulipAccount


class TestSingleAccount:
    def test_single_from_env(self, monkeypatch):
        monkeypatch.setenv("ZULIP_EMAIL", "bot@company.com")
        monkeypatch.setenv("ZULIP_API_KEY", "key123")
        monkeypatch.setenv("ZULIP_SITE", "https://company.zulipchat.com")
        resolver = AccountResolver()
        accounts = resolver.resolve()
        assert len(accounts) == 1
        assert accounts[0].name == "default"
        assert accounts[0].email == "bot@company.com"
        assert accounts[0].api_key == "key123"
        assert accounts[0].site == "https://company.zulipchat.com"

    def test_single_from_extra(self):
        resolver = AccountResolver(extra={
            "email": "bot@other.com",
            "api_key": "key456",
            "site": "https://other.zulipchat.com",
        })
        accounts = resolver.resolve()
        assert len(accounts) == 1
        assert accounts[0].email == "bot@other.com"

    def test_env_overrides_extra(self, monkeypatch):
        monkeypatch.setenv("ZULIP_EMAIL", "env@company.com")
        resolver = AccountResolver(extra={"email": "extra@company.com"})
        accounts = resolver.resolve()
        assert accounts[0].email == "env@company.com"


class TestMultiAccount:
    def test_multi_accounts(self):
        resolver = AccountResolver(extra={
            "accounts": {
                "default": {
                    "email": "bot@company.com",
                    "api_key": "key1",
                    "site": "https://company.zulipchat.com",
                },
                "support": {
                    "email": "support@other.com",
                    "api_key": "key2",
                    "site": "https://other.zulipchat.com",
                    "streams": ["#support"],
                    "dm_policy": "allowlist",
                    "allow_from": ["admin@other.com"],
                },
            }
        })
        accounts = resolver.resolve()
        assert len(accounts) == 2

        support = [a for a in accounts if a.name == "support"][0]
        assert support.email == "support@other.com"
        assert support.streams == ["#support"]
        assert support.dm_policy == "allowlist"
        assert support.allow_from == ["admin@other.com"]

    def test_multi_empty_falls_back(self):
        resolver = AccountResolver(extra={"accounts": {}})
        accounts = resolver.resolve()
        # Falls back to single account from env (which is empty)
        assert len(accounts) == 1
        assert accounts[0].email == ""


class TestAccountStreamsParsing:
    def test_parse_list(self):
        resolver = AccountResolver()
        result = resolver._parse_streams(["#a", "#b"])
        assert result == ["#a", "#b"]

    def test_parse_string(self):
        resolver = AccountResolver()
        result = resolver._parse_streams("#a, #b")
        assert result == ["#a", "#b"]

    def test_parse_none(self):
        resolver = AccountResolver()
        assert resolver._parse_streams(None) is None


class TestAccountAllowlistParsing:
    def test_parse_list(self):
        resolver = AccountResolver()
        result = resolver._parse_allowlist(["A@test.com", "B@test.com "])
        assert result == ["a@test.com", "b@test.com"]

    def test_parse_string(self):
        resolver = AccountResolver()
        result = resolver._parse_allowlist("A@test.com, B@test.com")
        assert result == ["a@test.com", "b@test.com"]

    def test_parse_none(self):
        resolver = AccountResolver()
        assert resolver._parse_allowlist(None) is None
