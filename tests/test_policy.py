"""Tests for DM policy engine (Issue #48)."""

import os
import time
from unittest.mock import patch

import pytest

from zulip.policy import (
    PolicyEngine,
    PairingCode,
    POLICY_OPEN,
    POLICY_ALLOWLIST,
    POLICY_PAIRING,
    POLICY_DISABLED,
)


class TestPolicyOpen:
    def test_open_allows_anyone(self):
        p = PolicyEngine()
        assert p.can_dm("anyone@example.com") is True
        assert p.can_dm("another@example.com") is True


class TestPolicyDisabled:
    def test_disabled_blocks_everyone(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "disabled")
        p = PolicyEngine()
        assert p.mode == "disabled"
        assert p.can_dm("anyone@example.com") is False


class TestPolicyAllowlist:
    def test_allowlist_allows_configured(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "allowlist")
        monkeypatch.setenv("ZULIP_ALLOWED_USERS", "alice@test.com, bob@test.com")
        p = PolicyEngine()
        assert p.can_dm("alice@test.com") is True
        assert p.can_dm("ALICE@test.com") is True  # case-insensitive
        assert p.can_dm("bob@test.com") is True
        assert p.can_dm("charlie@test.com") is False

    def test_allowlist_empty_blocks_all(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "allowlist")
        monkeypatch.setenv("ZULIP_ALLOWED_USERS", "")
        p = PolicyEngine()
        assert p.can_dm("anyone@example.com") is False


class TestPolicyPairing:
    def test_pairing_blocks_unknown(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        p = PolicyEngine()
        allowed, code = p.check_dm("newuser@example.com")
        assert allowed is False
        assert code is not None
        assert len(code) == 6

    def test_pairing_allows_approved(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        p = PolicyEngine()
        p.approve_email("approved@example.com")
        assert p.can_dm("approved@example.com") is True

    def test_pairing_returns_same_code(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        p = PolicyEngine()
        _, code1 = p.check_dm("user@example.com")
        _, code2 = p.check_dm("user@example.com")
        assert code1 == code2

    def test_pairing_code_expires(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        p = PolicyEngine(pairing_ttl=0)  # immediate expiry
        _, code = p.check_dm("user@example.com")
        time.sleep(0.01)
        allowed, code2 = p.check_dm("user@example.com")
        # After expiry, should generate a NEW code
        assert allowed is False
        assert code2 is not None

    def test_pairing_approve_adds_to_allowlist(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        p = PolicyEngine()
        p.approve_email("user@example.com")
        assert "user@example.com" in p.allowlist

    def test_pairing_revoke_removes(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        p = PolicyEngine()
        p.approve_email("user@example.com")
        assert p.can_dm("user@example.com") is True
        p.revoke_email("user@example.com")
        assert p.can_dm("user@example.com") is False


class TestPolicyStatus:
    def test_status_open(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "open")
        p = PolicyEngine()
        assert p.get_status("anyone@example.com") == "open"

    def test_status_disabled(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "disabled")
        p = PolicyEngine()
        assert p.get_status("anyone@example.com") == "disabled"

    def test_status_approved(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        p = PolicyEngine()
        p.approve_email("user@example.com")
        assert p.get_status("user@example.com") == "approved"

    def test_status_pending(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        p = PolicyEngine()
        _, code = p.check_dm("user@example.com")
        assert p.get_status("user@example.com") == f"pending ({code})"

    def test_status_unauthorized(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "allowlist")
        p = PolicyEngine()
        assert p.get_status("unknown@example.com") == "unauthorized"
