"""Tests for zulip.reactions — emoji reaction status indicators."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from zulip.reactions import (
    ReactionConfig,
    add_reaction,
    remove_reaction,
    ReactionLifecycle,
    DEFAULT_START,
    DEFAULT_SUCCESS,
    DEFAULT_ERROR,
)


class TestReactionConfig:
    def test_defaults(self):
        cfg = ReactionConfig()
        assert cfg.enabled is True
        assert cfg.clear_on_finish is True
        assert cfg.on_start == DEFAULT_START
        assert cfg.on_success == DEFAULT_SUCCESS
        assert cfg.on_error == DEFAULT_ERROR

    def test_custom_values(self):
        cfg = ReactionConfig(
            enabled=False,
            clear_on_finish=False,
            on_start=":clock:",
            on_success=":rocket:",
            on_error=":fire:",
        )
        assert cfg.enabled is False
        assert cfg.clear_on_finish is False
        assert cfg.on_start == "clock"
        assert cfg.on_success == "rocket"
        assert cfg.on_error == "fire"

    def test_from_env_defaults(self, monkeypatch):
        monkeypatch.delenv("ZULIP_REACTIONS_ENABLED", raising=False)
        monkeypatch.delenv("ZULIP_REACTION_CLEAR_ON_FINISH", raising=False)
        monkeypatch.delenv("ZULIP_REACTION_START", raising=False)
        monkeypatch.delenv("ZULIP_REACTION_SUCCESS", raising=False)
        monkeypatch.delenv("ZULIP_REACTION_ERROR", raising=False)
        cfg = ReactionConfig.from_env()
        assert cfg.enabled is True
        assert cfg.on_start == DEFAULT_START

    def test_from_env_custom(self, monkeypatch):
        monkeypatch.setenv("ZULIP_REACTIONS_ENABLED", "false")
        monkeypatch.setenv("ZULIP_REACTION_CLEAR_ON_FINISH", "0")
        monkeypatch.setenv("ZULIP_REACTION_START", "hourglass")
        monkeypatch.setenv("ZULIP_REACTION_SUCCESS", "party_popper")
        monkeypatch.setenv("ZULIP_REACTION_ERROR", "x")
        cfg = ReactionConfig.from_env()
        assert cfg.enabled is False
        assert cfg.clear_on_finish is False
        assert cfg.on_start == "hourglass"
        assert cfg.on_success == "party_popper"
        assert cfg.on_error == "x"


class TestAddReaction:
    @pytest.mark.asyncio
    async def test_add_success(self):
        client = AsyncMock()
        await add_reaction(client, "123", "eyes", enabled=True)
        client.add_reaction.assert_awaited_once_with(
            {"message_id": "123", "emoji_name": "eyes"}
        )

    @pytest.mark.asyncio
    async def test_add_disabled(self):
        client = AsyncMock()
        await add_reaction(client, "123", "eyes", enabled=False)
        client.add_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_empty_emoji(self):
        client = AsyncMock()
        await add_reaction(client, "123", "", enabled=True)
        client.add_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_failure_logged(self, caplog):
        client = AsyncMock()
        client.add_reaction.side_effect = RuntimeError("api down")
        await add_reaction(client, "123", "eyes", enabled=True)
        assert "api down" in caplog.text


class TestRemoveReaction:
    @pytest.mark.asyncio
    async def test_remove_success(self):
        client = AsyncMock()
        await remove_reaction(client, "123", "eyes", enabled=True)
        client.remove_reaction.assert_awaited_once_with(
            {"message_id": "123", "emoji_name": "eyes"}
        )

    @pytest.mark.asyncio
    async def test_remove_disabled(self):
        client = AsyncMock()
        await remove_reaction(client, "123", "eyes", enabled=False)
        client.remove_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_remove_failure_logged(self, caplog):
        client = AsyncMock()
        client.remove_reaction.side_effect = RuntimeError("no reaction")
        await remove_reaction(client, "123", "eyes", enabled=True)
        assert "no reaction" in caplog.text


class TestReactionLifecycle:
    @pytest.mark.asyncio
    async def test_start_adds_eyes(self):
        client = AsyncMock()
        lifecycle = ReactionLifecycle(client, "msg_1", ReactionConfig())
        await lifecycle.start()
        client.add_reaction.assert_awaited_once_with(
            {"message_id": "msg_1", "emoji_name": DEFAULT_START}
        )

    @pytest.mark.asyncio
    async def test_success_clears_start_and_adds_check(self):
        client = AsyncMock()
        lifecycle = ReactionLifecycle(client, "msg_1", ReactionConfig())
        await lifecycle.success()
        client.remove_reaction.assert_awaited_with(
            {"message_id": "msg_1", "emoji_name": DEFAULT_START}
        )
        client.add_reaction.assert_awaited_with(
            {"message_id": "msg_1", "emoji_name": DEFAULT_SUCCESS}
        )

    @pytest.mark.asyncio
    async def test_error_clears_start_and_adds_warning(self):
        client = AsyncMock()
        lifecycle = ReactionLifecycle(client, "msg_1", ReactionConfig())
        await lifecycle.error()
        client.remove_reaction.assert_awaited_with(
            {"message_id": "msg_1", "emoji_name": DEFAULT_START}
        )
        client.add_reaction.assert_awaited_with(
            {"message_id": "msg_1", "emoji_name": DEFAULT_ERROR}
        )

    @pytest.mark.asyncio
    async def test_success_no_clear(self):
        cfg = ReactionConfig(clear_on_finish=False)
        client = AsyncMock()
        lifecycle = ReactionLifecycle(client, "msg_1", cfg)
        await lifecycle.success()
        client.remove_reaction.assert_not_called()
        client.add_reaction.assert_awaited_once_with(
            {"message_id": "msg_1", "emoji_name": DEFAULT_SUCCESS}
        )
