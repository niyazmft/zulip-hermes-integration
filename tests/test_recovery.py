"""Tests for zulip.recovery — interrupted message recovery after restart."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestRecoverInterruptedMessages:
    """Tests for recover_interrupted_messages()."""

    @pytest.mark.asyncio
    async def test_no_messages_returns_zero(self):
        from zulip.recovery import recover_interrupted_messages

        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": []})

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=AsyncMock(),
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_skips_bot_own_messages(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {"id": 1, "sender_email": "bot@test.com", "sender_id": 1, "reactions": []},
            {"id": 2, "sender_email": "user@test.com", "sender_id": 2, "reactions": []},
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0
        handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_messages_without_start_reaction(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {"id": 1, "sender_email": "user@test.com", "sender_id": 2, "reactions": []},
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0
        handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_messages_with_end_reaction(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {
                "id": 1,
                "sender_email": "user@test.com",
                "sender_id": 2,
                "reactions": [
                    {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
                    {"emoji_name": "check_mark", "user": {"email": "bot@test.com", "id": 1}},
                ],
            },
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0
        handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_messages_with_bot_response(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {
                "id": 1,
                "sender_email": "user@test.com",
                "sender_id": 2,
                "reactions": [
                    {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
                ],
            },
            {
                "id": 2,
                "sender_email": "bot@test.com",
                "sender_id": 1,
                "reactions": [],
            },
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0
        handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_recovers_interrupted_message(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {
                "id": 1,
                "sender_email": "user@test.com",
                "sender_id": 2,
                "reactions": [
                    {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
                ],
            },
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 1
        handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sets_recovery_session_key(self):
        from zulip.recovery import recover_interrupted_messages

        messages = [
            {
                "id": 1,
                "sender_email": "user@test.com",
                "sender_id": 2,
                "reactions": [
                    {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
                ],
            },
        ]
        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
        handle_message = AsyncMock()

        await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle_message,
            sdk_call=sdk_call,
            send_timeout=30,
        )

        # Verify the message was tagged with a recovery session key
        called_msg = handle_message.await_args[0][0]
        assert "_recovery_session_key" in called_msg
        assert "recovery" in called_msg["_recovery_session_key"]

    @pytest.mark.asyncio
    async def test_handles_api_failure_gracefully(self):
        from zulip.recovery import recover_interrupted_messages

        client = MagicMock()
        sdk_call = AsyncMock(return_value={"result": "error", "msg": "API error"})

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=AsyncMock(),
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0

    @pytest.mark.asyncio
    async def test_handles_exception_gracefully(self):
        from zulip.recovery import recover_interrupted_messages

        client = MagicMock()
        sdk_call = AsyncMock(side_effect=RuntimeError("connection lost"))

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=AsyncMock(),
            sdk_call=sdk_call,
            send_timeout=30,
        )
        assert count == 0
