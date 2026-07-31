"""Mention detection and acknowledgement ordering in stream gating.

Covers two bugs that made `oncall` mode unusable:

1. Mentions were matched against the email local-part (`@soju-bot`), which is
   not what Zulip writes. Real mentions use display-name markup (`@**Soju**`),
   so the bot never saw itself mentioned and silently dropped everything.
2. The start reaction and typing indicator fired before the gate, so a dropped
   message was left showing "typing..." forever.
"""

from unittest.mock import AsyncMock

import pytest

from zulip.text_utils import create_mention_regex


class TestMentionRegex:
    def test_plain_username_still_matches(self):
        # Hand-typed and pre-existing behaviour; must keep working.
        rx = create_mention_regex("soju-bot")
        assert rx.search("hey @soju-bot are you there")

    def test_display_name_mention_matches(self):
        rx = create_mention_regex("soju-bot", "Soju")
        assert rx.search("@**Soju** how much are pies")

    def test_silent_mention_matches(self):
        rx = create_mention_regex("soju-bot", "Soju")
        assert rx.search("@_**Soju** quietly")

    def test_mention_with_user_id_matches(self):
        rx = create_mention_regex("soju-bot", "Soju")
        assert rx.search("@**Soju|373** disambiguated")

    def test_multiword_display_name(self):
        rx = create_mention_regex("mail-ops-bot", "Mail Ops")
        assert rx.search("@**Mail Ops** please look")

    def test_post_strip_form_matches(self):
        # The regression that mattered. strip_html_to_text() runs before
        # gating and reduces "@**Soju**" to "@Soju", so this plain form is what
        # the gate actually sees in production. A markup-only pattern misses it
        # and the bot silently ignores every mention.
        from zulip.text_utils import strip_html_to_text
        rx = create_mention_regex("soju-bot", "Soju")
        stripped = strip_html_to_text("@**Soju** how much are pies")
        assert stripped == "@Soju how much are pies"
        assert rx.search(stripped)

    def test_unrelated_mention_does_not_match(self):
        rx = create_mention_regex("soju-bot", "Soju")
        assert not rx.search("@**Someone Else** hello")
        assert not rx.search("just talking about soju, no mention")

    def test_without_full_name_display_mention_is_missed(self):
        # Documents exactly why the display name must be threaded through:
        # this is the old behaviour, and it is why oncall was unreachable.
        rx = create_mention_regex("soju-bot")
        assert not rx.search("@**Soju** hello")


class TestGatingUsesZulipFlag:
    @pytest.fixture
    def adapter(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, email=None, api_key=None, site=None):
                    pass

                # The adapter looks these up before calling _sdk_call, so they
                # must exist or the attribute error is swallowed by the
                # best-effort try/except and the call is never observable.
                def set_typing_status(self, *a, **k):
                    pass

                def add_reaction(self, *a, **k):
                    pass

                def remove_reaction(self, *a, **k):
                    pass

                def update_message_flags(self, *a, **k):
                    pass

                def send_message(self, *a, **k):
                    pass

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter
        a = ZulipAdapter(mock_platform_config)
        a.email = "soju-bot@zulip.com"
        a.bot_full_name = "Soju"
        a.handle_message = AsyncMock()
        a._sdk_call = AsyncMock(return_value={"result": "success", "id": 1})
        return a

    def _msg(self, content, flags=None):
        m = {
            "id": 1,
            "type": "stream",
            "stream_id": 30,
            "subject": "general chat",
            "display_recipient": "general",
            "content": content,
            "sender_email": "user@zulip.com",
            "sender_full_name": "User",
            "sender_id": 42,
        }
        if flags is not None:
            m["flags"] = flags
        return m

    @pytest.mark.asyncio
    async def test_zulip_mentioned_flag_is_trusted(self, adapter, monkeypatch):
        # The flag is authoritative even when the text carries markup this
        # adapter would otherwise have to parse.
        monkeypatch.setenv("ZULIP_CHATMODE", "oncall")
        monkeypatch.delenv("ZULIP_STREAM_OVERRIDES", raising=False)
        await adapter._handle_message(
            self._msg("@**Soju** how much are pies", flags=["mentioned"])
        )
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_display_name_markup_matches_without_flag(self, adapter, monkeypatch):
        # Fallback path: no flags on the event, but the markup is still ours.
        monkeypatch.setenv("ZULIP_CHATMODE", "oncall")
        monkeypatch.delenv("ZULIP_STREAM_OVERRIDES", raising=False)
        await adapter._handle_message(self._msg("@**Soju** hello"))
        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_unmentioned_message_is_dropped(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_CHATMODE", "oncall")
        monkeypatch.delenv("ZULIP_STREAM_OVERRIDES", raising=False)
        await adapter._handle_message(self._msg("just chatting", flags=[]))
        adapter.handle_message.assert_not_called()


class TestAcknowledgementOrdering:
    """A dropped message must not be left showing typing or a reaction."""

    @pytest.fixture
    def adapter(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, email=None, api_key=None, site=None):
                    pass

                # The adapter looks these up before calling _sdk_call, so they
                # must exist or the attribute error is swallowed by the
                # best-effort try/except and the call is never observable.
                def set_typing_status(self, *a, **k):
                    pass

                def add_reaction(self, *a, **k):
                    pass

                def remove_reaction(self, *a, **k):
                    pass

                def update_message_flags(self, *a, **k):
                    pass

                def send_message(self, *a, **k):
                    pass

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter
        a = ZulipAdapter(mock_platform_config)
        a.email = "soju-bot@zulip.com"
        a.bot_full_name = "Soju"
        a.handle_message = AsyncMock()
        a._sdk_call = AsyncMock(return_value={"result": "success", "id": 1})
        return a

    def _msg(self, content, flags=None):
        return {
            "id": 1, "type": "stream", "stream_id": 30, "subject": "t",
            "display_recipient": "general", "content": content,
            "sender_email": "u@z.com", "sender_full_name": "U", "sender_id": 42,
            "flags": flags if flags is not None else [],
        }

    def _typing_calls(self, adapter):
        return [
            c for c in adapter._sdk_call.await_args_list
            if getattr(c.args[0], "__name__", "") == "set_typing_status"
            or "set_typing_status" in str(c.args[0])
        ]

    @pytest.mark.asyncio
    async def test_dropped_message_sends_no_typing_indicator(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_CHATMODE", "oncall")
        monkeypatch.delenv("ZULIP_STREAM_OVERRIDES", raising=False)
        await adapter._handle_message(self._msg("no mention here"))
        adapter.handle_message.assert_not_called()
        assert self._typing_calls(adapter) == [], (
            "a dropped message must not leave a typing indicator running"
        )

    @pytest.mark.asyncio
    async def test_accepted_message_does_send_typing(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.delenv("ZULIP_STREAM_OVERRIDES", raising=False)
        await adapter._handle_message(self._msg("hello"))
        adapter.handle_message.assert_called_once()
        assert self._typing_calls(adapter), (
            "an accepted message should still show a typing indicator"
        )


class TestFlagNormalisation:
    """Zulip delivers flags on the event, not the message.

    This is the core of the mention fix: losing them here loses the
    authoritative "mentioned" signal, which is what made oncall unreachable.
    """

    def test_event_flags_are_moved_onto_the_message(self):
        from zulip.adapter import _message_with_flags
        event = {"message": {"id": 1, "content": "hi"}, "flags": ["mentioned"]}
        assert _message_with_flags(event)["flags"] == ["mentioned"]

    def test_absent_flags_become_an_empty_list(self):
        from zulip.adapter import _message_with_flags
        event = {"message": {"id": 1}}
        assert _message_with_flags(event)["flags"] == []

    def test_null_flags_become_an_empty_list(self):
        from zulip.adapter import _message_with_flags
        event = {"message": {"id": 1}, "flags": None}
        assert _message_with_flags(event)["flags"] == []

    def test_existing_message_flags_win(self):
        # The REST API already puts flags on the message; do not clobber them.
        from zulip.adapter import _message_with_flags
        event = {"message": {"id": 1, "flags": ["read"]}, "flags": ["mentioned"]}
        assert _message_with_flags(event)["flags"] == ["read"]

    def test_missing_message_does_not_raise(self):
        from zulip.adapter import _message_with_flags
        assert _message_with_flags({"flags": ["mentioned"]})["flags"] == ["mentioned"]


class TestOtherDropPathsAreSilent:
    """Every drop path must run before the acknowledgement block.

    The trigger gate is covered above; these are the two the PR description
    claimed were covered but were not.
    """

    @pytest.fixture
    def adapter(self, mock_platform_config, monkeypatch):
        import zulip.adapter as adapter_module
        monkeypatch.setattr(adapter_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, email=None, api_key=None, site=None):
                    pass

                def set_typing_status(self, *a, **k):
                    pass

                def add_reaction(self, *a, **k):
                    pass

                def remove_reaction(self, *a, **k):
                    pass

                def update_message_flags(self, *a, **k):
                    pass

                def send_message(self, *a, **k):
                    pass

        monkeypatch.setattr(adapter_module, "zulip", MockZulipModule())
        from zulip.adapter import ZulipAdapter
        a = ZulipAdapter(mock_platform_config)
        a.email = "soju-bot@zulip.com"
        a.bot_full_name = "Soju"
        a.handle_message = AsyncMock()
        a._sdk_call = AsyncMock(return_value={"result": "success", "id": 1})
        return a

    def _msg(self):
        return {
            "id": 1, "type": "stream", "stream_id": 30, "subject": "t",
            "display_recipient": "general", "content": "hello",
            "sender_email": "u@z.com", "sender_full_name": "U", "sender_id": 42,
            "flags": [],
        }

    def _typing_calls(self, adapter):
        return [
            c for c in adapter._sdk_call.await_args_list
            if "set_typing_status" in str(c.args[0])
        ]

    @pytest.mark.asyncio
    async def test_stream_filter_drop_is_silent(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.delenv("ZULIP_STREAM_OVERRIDES", raising=False)
        adapter._streams_filter = {"some-other-stream"}
        await adapter._handle_message(self._msg())
        adapter.handle_message.assert_not_called()
        assert self._typing_calls(adapter) == []

    @pytest.mark.asyncio
    async def test_group_policy_drop_is_silent(self, adapter, monkeypatch):
        monkeypatch.setenv("ZULIP_CHATMODE", "onmessage")
        monkeypatch.delenv("ZULIP_STREAM_OVERRIDES", raising=False)
        monkeypatch.setattr(adapter._policy, "can_group_message", lambda email: False)
        monkeypatch.setattr(adapter._policy, "group_mode", "disabled", raising=False)
        await adapter._handle_message(self._msg())
        adapter.handle_message.assert_not_called()
        assert self._typing_calls(adapter) == []
