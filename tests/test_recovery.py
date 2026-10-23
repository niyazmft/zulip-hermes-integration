"""Tests for zulip.recovery — interrupted message recovery after restart."""

import asyncio
import logging

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# Mirrors the message-related surface of ``zulip.Client`` (python-zulip-api
# 0.9.x). ``spec_set`` makes any other attribute access raise AttributeError,
# exactly like the real SDK does — a plain ``MagicMock()`` silently invents
# methods, which is how ``get_private_messages`` went unnoticed.
_SDK_CLIENT_METHODS = [
    "get_messages",
    "get_raw_message",
    "send_message",
    "update_message",
    "update_message_flags",
    "add_reaction",
    "remove_reaction",
]


def _sdk_like_client(messages):
    client = MagicMock(spec_set=_SDK_CLIENT_METHODS)
    client.get_messages.return_value = {"result": "success", "messages": messages}
    return client


async def _real_sdk_call(fn, *args, timeout, **kwargs):
    """Same shape as ``ZulipAdapter._sdk_call``: run the sync SDK fn in a thread."""
    return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)


class TestRecoverInterruptedMessages:
    """Tests for recover_interrupted_messages()."""

    @pytest.mark.asyncio
    async def test_fetches_dms_via_sdk_get_messages(self, caplog):
        """Regression: recovery must use ``Client.get_messages`` (GET /messages).

        Before the fix it called ``client.get_private_messages``, which does
        not exist on the zulip SDK, so every gateway start logged
        ``zulip recovery: failed [error='Client' object has no attribute
        'get_private_messages']`` and recovery silently never ran.
        """
        from zulip.recovery import (
            RECOVERY_MENTION_NARROW,
            RECOVERY_NARROW,
            RECOVERY_SCAN_LIMIT,
            recover_interrupted_messages,
        )

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
        client = _sdk_like_client(messages)
        handle_message = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="zulip.recovery"):
            count = await recover_interrupted_messages(
                client=client,
                bot_email="bot@test.com",
                bot_user_id="1",
                reaction_start="eyes",
                reaction_success="check_mark",
                reaction_error="warning",
                handle_message=handle_message,
                sdk_call=_real_sdk_call,
                send_timeout=30,
            )

        assert count == 1
        handle_message.assert_awaited_once()
        assert "zulip recovery: failed" not in caplog.text
        # Two scans since #220: DMs first (byte-identical to before), then the
        # addressed-stream scan. The same message is returned by both here, and
        # the id dedupe is why it is still dispatched exactly once.
        calls = client.get_messages.call_args_list
        assert len(calls) == 2
        assert calls[0].args[0] == {
            "anchor": "newest",
            "num_before": RECOVERY_SCAN_LIMIT,
            "num_after": 0,
            "narrow": RECOVERY_NARROW,
        }
        assert calls[1].args[0]["narrow"] == RECOVERY_MENTION_NARROW

    def test_recovery_narrow_targets_direct_messages(self):
        from zulip.recovery import RECOVERY_NARROW, RECOVERY_SCAN_LIMIT

        assert RECOVERY_NARROW == [{"operator": "is", "operand": "dm"}]
        assert 1 <= RECOVERY_SCAN_LIMIT <= 1000  # Zulip caps num_before at 1000

    @pytest.mark.asyncio
    async def test_sdk_call_receives_message_filters(self):
        """The filter dict is passed positionally, matching ``get_messages(message_filters)``."""
        from zulip.recovery import recover_interrupted_messages

        client = _sdk_like_client([])
        sdk_call = AsyncMock(return_value={"result": "success", "messages": []})

        await recover_interrupted_messages(
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

        assert sdk_call.await_count == 2, "one DM scan and one mention scan (#220)"
        first = sdk_call.await_args_list[0]
        assert first.args[0] is client.get_messages
        assert first.args[1]["narrow"] == [{"operator": "is", "operand": "dm"}]
        assert first.args[1]["num_after"] == 0
        assert first.kwargs == {"timeout": 30}
        second = sdk_call.await_args_list[1]
        assert second.args[1]["narrow"] == [{"operator": "is", "operand": "mentioned"}]
        assert second.kwargs == {"timeout": 30}

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


def _eligible(msg_id, **overrides):
    msg = {
        "id": msg_id,
        "sender_email": "user@test.com",
        "sender_id": 2,
        "reactions": [
            {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
        ],
    }
    msg.update(overrides)
    return msg


async def _run(messages, handle_message, *, dedupe=None, max_dispatch=5):
    from zulip.recovery import recover_interrupted_messages

    client = MagicMock(spec_set=_SDK_CLIENT_METHODS)
    sdk_call = AsyncMock(return_value={"result": "success", "messages": messages})
    return await recover_interrupted_messages(
        client=client,
        bot_email="bot@test.com",
        bot_user_id="1",
        reaction_start="eyes",
        reaction_success="check_mark",
        reaction_error="warning",
        handle_message=handle_message,
        sdk_call=sdk_call,
        send_timeout=30,
        dedupe=dedupe,
        max_dispatch=max_dispatch,
    )


class TestTheRecoveryContract:
    """#227: three guarantees that were assumed, not asserted.

    Recovery re-dispatches through the normal inbound path, which re-enters the
    reaction lifecycle and bypasses the poll loop's dedupe gate.
    """

    @pytest.mark.asyncio
    async def test_a_recovered_message_marks_the_start_reaction_as_already_placed(self):
        from zulip.recovery import recover_interrupted_messages

        seen = {}

        async def handle(msg):
            seen.update(msg)

        count = await _run([_eligible(1)], handle)

        assert count == 1
        assert seen.get("_recovery_skip_start") is True, (
            "inbound would add a second start reaction the server silently absorbs"
        )

    @pytest.mark.asyncio
    async def test_inbound_does_not_re_add_the_start_reaction(self, mock_platform_config, monkeypatch):
        """The other half: the inbound path must honour the marker."""
        import zulip.zulip_client as zc
        from unittest.mock import AsyncMock

        calls = {"added": []}
        monkeypatch.setattr(zc, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    pass

                def add_reaction(self, request):
                    calls["added"].append(request)
                    return {"result": "success"}

                def remove_reaction(self, request):
                    return {"result": "success"}

                def set_typing_status(self, request):
                    return {"result": "success"}

                def update_message_flags(self, request):
                    return {"result": "success"}

                def get_messages(self, request):
                    return {"result": "success", "messages": []}

        monkeypatch.setattr(zc, "zulip", MockZulipModule())
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.setenv("ZULIP_REQUIRE_MENTION", "false")

        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)
        adapter.bot_full_name = "Test Bot"
        adapter.handle_message = AsyncMock()

        await adapter._handle_message(
            {
                "id": 11,
                "type": "stream",
                "stream_id": 5,
                "subject": "general",
                "display_recipient": "test",
                "content": "recovered turn",
                "sender_email": "user@zulip.com",
                "sender_full_name": "User",
                "sender_id": 42,
                "flags": [],
                "_recovery_skip_start": True,
            }
        )

        starts = [c for c in calls["added"] if c.get("emoji_name") == "eyes"]
        assert starts == [], "a recovered message was given a second start reaction"
        # The ✅ placed at dispatch is expected -- it is the run's own
        # acknowledgement, not a re-entry into the start lifecycle.
        assert [c for c in calls["added"] if c.get("emoji_name") == "check_mark"]
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_recovery_places_no_outcome_reaction(self):
        """A pass that adds ✅/⚠️ would corrupt the signal it selects on."""
        from zulip.recovery import recover_interrupted_messages

        client = MagicMock(spec_set=_SDK_CLIENT_METHODS)
        sdk_call = AsyncMock(return_value={"result": "success", "messages": [_eligible(1)]})

        await recover_interrupted_messages(
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

        client.add_reaction.assert_not_called()
        client.remove_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_message_the_poll_loop_already_claimed_is_not_dispatched(self, caplog):
        """No duplicate dispatch: recovery must claim before it answers."""
        import logging

        class _Claimed:
            def __init__(self):
                self.keys = []

            def check(self, key):
                self.keys.append(key)
                return True  # already seen (by the poll loop, or an earlier pass)

        dedupe = _Claimed()
        handle = AsyncMock()

        with caplog.at_level(logging.INFO, logger="zulip.recovery"):
            count = await _run([_eligible(1)], handle, dedupe=dedupe)

        assert count == 0
        handle.assert_not_awaited()
        assert dedupe.keys == ["1"], "recovery must claim the poll loop's own key"
        assert any("reason=duplicate" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_recovery_claims_the_id_before_dispatching(self):
        order = []

        class _Store:
            def check(self, key):
                order.append(("claim", key))
                return False

        async def handle(msg):
            order.append(("dispatch", str(msg["id"])))

        count = await _run([_eligible(7)], handle, dedupe=_Store())

        assert count == 1
        assert order == [("claim", "7"), ("dispatch", "7")]

    @pytest.mark.asyncio
    async def test_the_cap_bounds_one_pass(self, caplog):
        import logging

        handle = AsyncMock()

        with caplog.at_level(logging.INFO, logger="zulip.recovery"):
            count = await _run([_eligible(1), _eligible(2), _eligible(3)], handle, max_dispatch=1)

        assert count == 1
        assert handle.await_count == 1
        assert any("reason=cap reached" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_eligibility_skips_say_why(self, caplog):
        import logging

        end_reacted = _eligible(
            1,
            reactions=[
                {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
                {"emoji_name": "check_mark", "user": {"email": "bot@test.com", "id": 1}},
            ],
        )
        answered = _eligible(2)
        # A bot reply after the message means it was already answered.
        bot_reply = {"id": 3, "sender_email": "bot@test.com", "sender_id": 1}

        handle = AsyncMock()
        with caplog.at_level(logging.INFO, logger="zulip.recovery"):
            count = await _run([end_reacted, answered, bot_reply], handle)

        assert count == 0
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "reason=already end-reacted" in text
        assert "reason=already answered" in text


def _stream_mention(msg_id, **overrides):
    msg = {
        "id": msg_id,
        "type": "stream",
        "stream_id": 5,
        "subject": "deploy help",
        "display_recipient": "test",
        "sender_email": "user@test.com",
        "sender_id": 2,
        "reactions": [
            {"emoji_name": "eyes", "user": {"email": "bot@test.com", "id": 1}},
        ],
    }
    msg.update(overrides)
    return msg


def _two_scan_sdk(dms, mentions):
    """``sdk_call`` that answers each narrow with its own message set."""

    async def _call(fn, filters, *, timeout):
        if filters.get("narrow") == [{"operator": "is", "operand": "mentioned"}]:
            return {"result": "success", "messages": list(mentions)}
        return {"result": "success", "messages": list(dms)}

    return _call


def _two_scan_sdk_with_rejected_mentions(dms):
    async def _call(fn, filters, *, timeout):
        if filters.get("narrow") == [{"operator": "is", "operand": "mentioned"}]:
            return {"result": "error", "msg": "unknown operator"}
        return {"result": "success", "messages": list(dms)}

    return _call


async def _run_two_scan(dms, mentions, handle, *, max_dispatch=5, sdk=None):
    from zulip.recovery import recover_interrupted_messages

    client = MagicMock(spec_set=_SDK_CLIENT_METHODS)
    return await recover_interrupted_messages(
        client=client,
        bot_email="bot@test.com",
        bot_user_id="1",
        reaction_start="eyes",
        reaction_success="check_mark",
        reaction_error="warning",
        handle_message=handle,
        sdk_call=sdk or _two_scan_sdk(dms, mentions),
        send_timeout=30,
        max_dispatch=max_dispatch,
    )


class TestStreamCatchUp:
    """#220: a missed @mention is caught up, in the topic the user wrote in."""

    @pytest.mark.asyncio
    async def test_a_stream_mention_with_a_stale_eyes_is_recovered_into_its_topic(self):
        seen = {}

        async def handle(msg):
            seen.update(msg)

        count = await _run_two_scan([], [_stream_mention(9)], handle)

        assert count == 1
        assert seen["type"] == "stream"
        assert seen["stream_id"] == 5
        assert seen["subject"] == "deploy help", "the topic must survive the round trip"
        assert seen.get("_recovery_skip_start") is True
        assert "_recovery_session_key" not in seen, (
            "a DM-shaped session key would be a second, drifting routing derivation"
        )

    @pytest.mark.asyncio
    async def test_a_stream_mention_already_answered_is_not_recovered(self):
        handle = AsyncMock()
        bot_reply = {"id": 11, "type": "stream", "sender_email": "bot@test.com", "sender_id": 1}

        count = await _run_two_scan([], [_stream_mention(9), bot_reply], handle)

        assert count == 0
        handle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_dm_still_gets_its_dm_session_key(self, caplog):
        seen = {}

        async def handle(msg):
            seen.update(msg)

        with caplog.at_level(logging.INFO, logger="zulip.recovery"):
            count = await _run_two_scan([_eligible(1)], [], handle)

        assert count == 1
        assert seen["_recovery_session_key"].startswith("agent:main:zulip:direct:")
        assert "dm scan found 1 message" in caplog.text

    @pytest.mark.asyncio
    async def test_the_cap_holds_across_both_scans(self):
        handle = AsyncMock()

        count = await _run_two_scan(
            [_eligible(1)], [_stream_mention(2), _stream_mention(3)], handle, max_dispatch=1
        )

        assert count == 1
        assert handle.await_count == 1

    @pytest.mark.asyncio
    async def test_a_rejected_mention_scan_does_not_lose_the_dm_scan(self, caplog):
        handle = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="zulip.recovery"):
            count = await _run_two_scan(
                [_eligible(1)], [], handle, sdk=_two_scan_sdk_with_rejected_mentions([_eligible(1)])
            )

        assert count == 1, "the DM half must survive a server without is:mentioned"
        assert "mention scan unavailable" in caplog.text

    @pytest.mark.asyncio
    async def test_streams_can_be_left_out_entirely(self):
        from zulip.recovery import recover_interrupted_messages

        handle = AsyncMock()
        client = MagicMock(spec_set=_SDK_CLIENT_METHODS)

        count = await recover_interrupted_messages(
            client=client,
            bot_email="bot@test.com",
            bot_user_id="1",
            reaction_start="eyes",
            reaction_success="check_mark",
            reaction_error="warning",
            handle_message=handle,
            sdk_call=_two_scan_sdk([_eligible(1)], [_stream_mention(2)]),
            send_timeout=30,
            include_streams=False,
        )

        assert count == 1
        assert handle.await_args.args[0]["id"] == 1
