"""Tests for in-channel reaction triggers (epic #149 / issues #163, #164)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from zulip.reaction_triggers import (
    ReactionTriggerConfig,
    build_reaction_trigger_message,
    find_unsubscribed_streams,
    is_eligible_target_message,
    match_reaction_trigger,
    normalize_emoji_name,
    reaction_dedupe_key,
)


class TestReactionTriggerConfig:
    def test_empty_is_disabled(self):
        cfg = ReactionTriggerConfig.from_mapping({})
        assert cfg.enabled is False
        assert cfg.triggers == {}

    def test_valid_triggers_enable(self):
        cfg = ReactionTriggerConfig.from_mapping({"+1": "Proceed with the step."})
        assert cfg.enabled is True
        assert cfg.triggers["+1"] == "Proceed with the step."

    def test_json_string(self):
        cfg = ReactionTriggerConfig.from_mapping('{"+1": "Go"}')
        assert cfg.enabled is True
        assert cfg.triggers == {"+1": "Go"}

    def test_invalid_json_disables(self, caplog):
        cfg = ReactionTriggerConfig.from_mapping("not json")
        assert cfg.enabled is False

    def test_ignores_non_string_values_and_empty(self):
        cfg = ReactionTriggerConfig.from_mapping(
            {"+1": "Go", "x": 5, "": "nothing", "y": "   "}
        )
        assert cfg.triggers == {"+1": "Go"}

    def test_normalizes_emoji_and_collapses_instruction(self):
        cfg = ReactionTriggerConfig.from_mapping({" :Thumbs_Up: ": "  do   it  "})
        assert cfg.triggers == {"thumbs_up": "do it"}
        assert cfg.enabled is True

    def test_instruction_capped(self):
        cfg = ReactionTriggerConfig.from_mapping({"+1": "x" * 900})
        assert len(cfg.triggers["+1"]) == 500

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("ZULIP_REACTION_TRIGGERS", '{"+1": "Go"}')
        monkeypatch.setenv("ZULIP_REACTION_TRIGGER_ANY_MESSAGE", "true")
        cfg = ReactionTriggerConfig.from_env()
        assert cfg.enabled is True
        assert cfg.any_message is True

    def test_normalize_emoji_name(self):
        assert normalize_emoji_name(":Check_Mark:") == "check_mark"
        assert normalize_emoji_name(None) == ""
        assert normalize_emoji_name("  ") == ""


class TestMatchReactionTrigger:
    def setup_method(self):
        self.cfg = ReactionTriggerConfig.from_mapping({"+1": "Proceed."})

    def test_matches_add(self):
        m = match_reaction_trigger(
            {"type": "reaction", "op": "add", "message_id": 5, "emoji_name": "+1", "user_id": 9},
            self.cfg,
        )
        assert m is not None
        assert m.emoji == "+1"
        assert m.instruction == "Proceed."
        assert m.message_id == "5"
        assert m.user_id == "9"

    def test_ignores_remove(self):
        assert (
            match_reaction_trigger(
                {"type": "reaction", "op": "remove", "message_id": 5, "emoji_name": "+1", "user_id": 9},
                self.cfg,
            )
            is None
        )

    def test_ignores_unconfigured_emoji(self):
        assert (
            match_reaction_trigger(
                {"type": "reaction", "op": "add", "message_id": 5, "emoji_name": "tada", "user_id": 9},
                self.cfg,
            )
            is None
        )

    def test_ignores_missing_ids(self):
        for event in (
            {"type": "reaction", "op": "add", "emoji_name": "+1", "user_id": 9},
            {"type": "reaction", "op": "add", "message_id": 5, "emoji_name": "+1"},
        ):
            assert match_reaction_trigger(event, self.cfg) is None

    def test_disabled_config_matches_nothing(self):
        assert (
            match_reaction_trigger(
                {"type": "reaction", "op": "add", "message_id": 5, "emoji_name": "+1", "user_id": 9},
                ReactionTriggerConfig(),
            )
            is None
        )


class TestEligibleTarget:
    def test_bot_by_id(self):
        msg = {"type": "stream", "sender_id": 42, "sender_email": "bot@x.com"}
        assert is_eligible_target_message(msg, any_message=False, bot_user_id="42")

    def test_bot_by_email(self):
        msg = {"type": "stream", "sender_id": 1, "sender_email": "bot@x.com"}
        assert is_eligible_target_message(msg, any_message=False, bot_email="BOT@x.com")

    def test_other_user_is_ineligible_by_default(self):
        msg = {"type": "stream", "sender_id": 1, "sender_email": "human@x.com"}
        assert not is_eligible_target_message(msg, any_message=False, bot_user_id="42")

    def test_any_message_relaxes(self):
        msg = {"type": "stream", "sender_id": 1, "sender_email": "human@x.com"}
        assert is_eligible_target_message(msg, any_message=True, bot_user_id="42")

    def test_dm_target_is_ineligible(self):
        msg = {"type": "private", "sender_id": 42}
        assert not is_eligible_target_message(msg, any_message=True, bot_user_id="42")


class TestBuildMessage:
    def test_sender_is_reacting_human(self):
        target = {"id": 5, "stream_id": 3, "timestamp": 100}
        m = match_reaction_trigger(
            {"type": "reaction", "op": "add", "message_id": 5, "emoji_name": "+1", "user_id": 9},
            ReactionTriggerConfig.from_mapping({"+1": "Proceed."}),
        )
        built = build_reaction_trigger_message(
            target, m, "engineering", "api-review", user_email="human@x.com", user_name="Human"
        )
        assert built["sender_email"] == "human@x.com"
        assert built["sender_full_name"] == "Human"
        assert built["type"] == "stream"
        assert built["display_recipient"] == "engineering"
        assert built["subject"] == "api-review"
        assert built["_reaction_trigger"] is True
        assert built["_reaction_emoji"] == "+1"
        assert built["_reaction_instruction"] == "Proceed."
        assert built["id"] == "5"

    def test_dedupe_key_is_stable(self):
        a = reaction_dedupe_key("5", "+1", "9")
        b = reaction_dedupe_key("5", "+1", "9")
        assert a == b
        assert a != reaction_dedupe_key("5", "+1", "10")


class TestUnsubscribedStreams:
    def test_wildcard_is_unverifiable(self):
        assert find_unsubscribed_streams(["*"], ["a", "b"]) == []

    def test_missing_streams_named(self):
        assert find_unsubscribed_streams(["a", "b", "c"], ["A", "b"]) == ["c"]

    def test_case_insensitive(self):
        assert find_unsubscribed_streams(["General"], ["general"]) == []


# ---------------------------------------------------------------------------
# Adapter-level behaviour
# ---------------------------------------------------------------------------


def _build_adapter(monkeypatch, tmp_path, triggers):
    """Construct a ZulipAdapter with a MagicMock client and given triggers."""
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

    class FakeConfig:
        extra = {
            "api_key": "fake-key",
            "email": "bot@test.zulipchat.com",
            "site": "https://test.zulipchat.com",
        }

    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(FakeConfig())
    a.client = MagicMock()
    a._reaction_trigger_cfg = ReactionTriggerConfig.from_mapping(triggers)
    a._extra_event_types = ["reaction"] if a._reaction_trigger_cfg.enabled else []
    a._audit_logger = MagicMock()
    a._audit_logger.log_event = AsyncMock()
    a._handle_message = AsyncMock()
    return a


def _reaction_event():
    return {
        "type": "reaction",
        "op": "add",
        "message_id": 5,
        "emoji_name": "+1",
        "user_id": 9,
    }


def _bot_message():
    return {
        "id": 5,
        "type": "stream",
        "stream_id": 3,
        "display_recipient": "engineering",
        "subject": "api-review",
        "sender_id": 42,
        "sender_email": "bot@test.zulipchat.com",
    }


class TestAdapterNeededEventTypes:
    def test_reaction_requested_only_with_triggers(self, monkeypatch, tmp_path):
        off = _build_adapter(monkeypatch, tmp_path, {})
        assert "reaction" not in off._needed_event_types()

        on = _build_adapter(monkeypatch, tmp_path, {"+1": "Go"})
        assert "reaction" in on._needed_event_types()


class TestHandleReactionEvent:
    @pytest.mark.asyncio
    async def test_dispatches_synthetic_turn(self, monkeypatch, tmp_path):
        a = _build_adapter(monkeypatch, tmp_path, {"+1": "Proceed."})
        a._bot_user_id = "42"
        a.client.get_raw_message.return_value = {
            "result": "success",
            "message": _bot_message(),
        }
        a.client.get_user_by_id.return_value = {
            "result": "success",
            "user": {"user_id": 9, "email": "human@x.com", "full_name": "Human"},
        }

        await a._handle_reaction_event(_reaction_event())

        a._handle_message.assert_awaited_once()
        sent = a._handle_message.await_args.args[0]
        assert sent["sender_email"] == "human@x.com"
        assert sent["subject"] == "api-review"
        assert sent["_reaction_trigger"] is True

    @pytest.mark.asyncio
    async def test_other_users_message_does_nothing_by_default(
        self, monkeypatch, tmp_path
    ):
        a = _build_adapter(monkeypatch, tmp_path, {"+1": "Proceed."})
        a._bot_user_id = "42"
        other = _bot_message()
        other["sender_id"] = 7
        other["sender_email"] = "someone-else@x.com"
        a.client.get_raw_message.return_value = {"result": "success", "message": other}
        a.client.get_user_by_id.return_value = {
            "result": "success",
            "user": {"user_id": 9, "email": "human@x.com"},
        }

        await a._handle_reaction_event(_reaction_event())
        a._handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_any_message_escape_hatch(self, monkeypatch, tmp_path):
        a = _build_adapter(monkeypatch, tmp_path, {"+1": "Proceed."})
        a._reaction_trigger_cfg.any_message = True
        other = _bot_message()
        other["sender_id"] = 7
        other["sender_email"] = "someone-else@x.com"
        a.client.get_raw_message.return_value = {"result": "success", "message": other}
        a.client.get_user_by_id.return_value = {
            "result": "success",
            "user": {"user_id": 9, "email": "human@x.com"},
        }

        await a._handle_reaction_event(_reaction_event())
        a._handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unresolved_email_drops_with_warning(
        self, monkeypatch, tmp_path, caplog
    ):
        a = _build_adapter(monkeypatch, tmp_path, {"+1": "Proceed."})
        a._bot_user_id = "42"
        a.client.get_user_by_id.return_value = {"result": "error", "msg": "no such user"}

        await a._handle_reaction_event(_reaction_event())

        a._handle_message.assert_not_awaited()
        assert "could not resolve" in caplog.text
        # The drop is auditable, not a silent policy rejection.
        events = [c.args[0] for c in a._audit_logger.log_event.await_args_list]
        assert "reaction_trigger_dropped" in events

    @pytest.mark.asyncio
    async def test_repeated_events_fire_once(self, monkeypatch, tmp_path):
        a = _build_adapter(monkeypatch, tmp_path, {"+1": "Proceed."})
        a._bot_user_id = "42"
        a.client.get_raw_message.return_value = {
            "result": "success",
            "message": _bot_message(),
        }
        a.client.get_user_by_id.return_value = {
            "result": "success",
            "user": {"user_id": 9, "email": "human@x.com"},
        }

        await a._handle_reaction_event(_reaction_event())
        await a._handle_reaction_event(_reaction_event())
        assert a._handle_message.await_count == 1

    @pytest.mark.asyncio
    async def test_dedupe_survives_restart(self, monkeypatch, tmp_path):
        a = _build_adapter(monkeypatch, tmp_path, {"+1": "Proceed."})
        a._bot_user_id = "42"
        a.client.get_raw_message.return_value = {
            "result": "success",
            "message": _bot_message(),
        }
        a.client.get_user_by_id.return_value = {
            "result": "success",
            "user": {"user_id": 9, "email": "human@x.com"},
        }
        await a._handle_reaction_event(_reaction_event())
        a._dedupe.save()

        # A fresh adapter reading the same data dir must not re-fire.
        b = _build_adapter(monkeypatch, tmp_path, {"+1": "Proceed."})
        b._bot_user_id = "42"
        b.client.get_raw_message.return_value = {
            "result": "success",
            "message": _bot_message(),
        }
        b.client.get_user_by_id.return_value = {
            "result": "success",
            "user": {"user_id": 9, "email": "human@x.com"},
        }
        await b._handle_reaction_event(_reaction_event())
        b._handle_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unmonitored_stream_ignored(self, monkeypatch, tmp_path):
        a = _build_adapter(monkeypatch, tmp_path, {"+1": "Proceed."})
        a._streams_filter = {"other-stream"}
        a._bot_user_id = "42"
        a.client.get_raw_message.return_value = {
            "result": "success",
            "message": _bot_message(),
        }
        a.client.get_user_by_id.return_value = {
            "result": "success",
            "user": {"user_id": 9, "email": "human@x.com"},
        }
        await a._handle_reaction_event(_reaction_event())
        a._handle_message.assert_not_awaited()


class TestSubscriptionCheck:
    @pytest.mark.asyncio
    async def test_warns_when_monitored_stream_unsubscribed(
        self, monkeypatch, tmp_path, caplog
    ):
        a = _build_adapter(monkeypatch, tmp_path, {"+1": "Proceed."})
        a._streams_filter = {"engineering", "general"}
        a.client.get_subscriptions.return_value = {
            "result": "success",
            "subscriptions": [{"name": "engineering"}],
        }
        await a._check_reaction_trigger_subscriptions()
        assert "general" in caplog.text

    @pytest.mark.asyncio
    async def test_wildcard_reports_subscribed_list(
        self, monkeypatch, tmp_path, caplog
    ):
        import logging

        a = _build_adapter(monkeypatch, tmp_path, {"+1": "Proceed."})
        a._streams_filter = None
        a.client.get_subscriptions.return_value = {
            "result": "success",
            "subscriptions": [{"name": "engineering"}],
        }
        with caplog.at_level(logging.INFO, logger="zulip.adapter"):
            await a._check_reaction_trigger_subscriptions()
        assert "engineering" in caplog.text

    @pytest.mark.asyncio
    async def test_no_check_when_disabled(self, monkeypatch, tmp_path):
        a = _build_adapter(monkeypatch, tmp_path, {})
        a.client.get_subscriptions.side_effect = AssertionError("must not be called")
        await a._check_reaction_trigger_subscriptions()
