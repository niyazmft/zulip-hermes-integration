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


class TestGroupPolicy:
    """Group (stream) policy tests (Issue #66)."""

    def test_group_defaults_to_open(self):
        """Default group policy is open."""
        p = PolicyEngine()
        assert p.group_mode == "open"
        assert p.can_group_message("anyone@example.com") is True

    def test_group_disabled_blocks_all(self, monkeypatch):
        monkeypatch.setenv("ZULIP_GROUP_POLICY", "disabled")
        p = PolicyEngine()
        assert p.group_mode == "disabled"
        assert p.can_group_message("anyone@example.com") is False

    def test_group_allowlist_allows_configured(self, monkeypatch):
        monkeypatch.setenv("ZULIP_GROUP_POLICY", "allowlist")
        monkeypatch.setenv("ZULIP_GROUP_ALLOW_FROM", "alice@example.com, bob@example.com")
        p = PolicyEngine()
        assert p.can_group_message("alice@example.com") is True
        assert p.can_group_message("bob@example.com") is True
        assert p.can_group_message("charlie@example.com") is False

    def test_group_allowlist_empty_blocks_all(self, monkeypatch):
        monkeypatch.setenv("ZULIP_GROUP_POLICY", "allowlist")
        monkeypatch.setenv("ZULIP_GROUP_ALLOW_FROM", "")
        p = PolicyEngine()
        assert p.can_group_message("anyone@example.com") is False

    def test_group_lowercased(self, monkeypatch):
        monkeypatch.setenv("ZULIP_GROUP_POLICY", "allowlist")
        monkeypatch.setenv("ZULIP_GROUP_ALLOW_FROM", "Alice@Example.COM")
        p = PolicyEngine()
        assert p.can_group_message("alice@example.com") is True

    def test_dm_policy_unchanged_by_group_policy(self, monkeypatch):
        """DM policy and group policy are independent."""
        monkeypatch.setenv("ZULIP_DM_POLICY", "open")
        monkeypatch.setenv("ZULIP_GROUP_POLICY", "disabled")
        p = PolicyEngine()
        assert p.mode == "open"
        assert p.group_mode == "disabled"
        # DM still allowed even though group is disabled
        assert p.can_dm("anyone@example.com") is True
        assert p.can_group_message("anyone@example.com") is False

    def test_group_pairing_not_supported(self, monkeypatch):
        """Pairing mode falls back to open for group policy."""
        monkeypatch.setenv("ZULIP_GROUP_POLICY", "pairing")
        p = PolicyEngine()
        assert p.group_mode == "open"


class TestPolicyDiskPersistence:
    """Tests for disk-based allowlist persistence."""

    def test_save_and_load_persists_allowlist(self, tmp_path):
        p = PolicyEngine(data_dir=str(tmp_path))
        p.approve_email("alice@test.com")
        p.approve_email("bob@test.com")

        # Create a new engine pointing to the same dir
        p2 = PolicyEngine(data_dir=str(tmp_path))
        assert "alice@test.com" in p2.allowlist
        assert "bob@test.com" in p2.allowlist

    def test_save_and_load_persists_group_allowlist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ZULIP_GROUP_POLICY", "allowlist")
        p = PolicyEngine(data_dir=str(tmp_path))
        # Group allowlist is env-based, but we can test that it's loaded
        assert p.group_allowlist == set()

    def test_persistence_file_created(self, tmp_path):
        p = PolicyEngine(data_dir=str(tmp_path))
        p.approve_email("test@test.com")

        persist_file = tmp_path / "zulip_allowlist.json"
        assert persist_file.exists()

        import json
        data = json.loads(persist_file.read_text())
        assert "test@test.com" in data["allowlist"]

    def test_no_data_dir_does_not_persist(self):
        p = PolicyEngine()  # No data_dir
        p.approve_email("test@test.com")
        # Should not crash — just skip persistence
        assert "test@test.com" in p.allowlist

    def test_load_from_nonexistent_file_does_not_crash(self, tmp_path):
        # Non-existent file should not raise
        p = PolicyEngine(data_dir=str(tmp_path / "nonexistent"))
        assert p.allowlist == set()

    def test_revoke_persists_to_disk(self, tmp_path):
        p = PolicyEngine(data_dir=str(tmp_path))
        p.approve_email("alice@test.com")
        p.revoke_email("alice@test.com")

        p2 = PolicyEngine(data_dir=str(tmp_path))
        assert "alice@test.com" not in p2.allowlist

    def test_env_allowlist_merged_with_disk(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ZULIP_ALLOWED_USERS", "env@test.com")
        p = PolicyEngine(data_dir=str(tmp_path))
        p.approve_email("disk@test.com")

        p2 = PolicyEngine(data_dir=str(tmp_path))
        assert "env@test.com" in p2.allowlist  # from env
        assert "disk@test.com" in p2.allowlist  # from disk
