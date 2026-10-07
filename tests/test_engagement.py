"""Tests for sticky topic engagement (issues #165, #166).

Covers the store, the config, the mention-to-start gate, idle TTL expiry, key
isolation across topics/streams, the "DMs are never affected" guarantee, the
explicit stop command and the in-topic expiry notice.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from zulip.engagement import (
    MODE_OFF,
    MODE_STICKY_TOPIC,
    SCOPE_TOPIC,
    SCOPE_USER,
    EngagementConfig,
    TopicEngagementStore,
    is_bot_address,
    is_bot_sender,
    is_end_session_message,
    is_stop_listening_message,
)


# --------------------------------------------------------------------------
# Store / config unit tests
# --------------------------------------------------------------------------


class TestEngagementConfig:
    def test_off_by_default(self, monkeypatch):
        for name in (
            "ZULIP_ENGAGEMENT_MODE",
            "ZULIP_ENGAGEMENT_SCOPE",
            "ZULIP_ENGAGEMENT_TTL_MINUTES",
            "ZULIP_ENGAGEMENT_EXPIRY_NOTICE",
            "ZULIP_ENGAGEMENT_EXPIRY_SCAN_SECONDS",
        ):
            monkeypatch.delenv(name, raising=False)
        cfg = EngagementConfig.from_env()
        assert cfg.mode == MODE_OFF
        assert cfg.scope == SCOPE_USER
        assert cfg.ttl_seconds == 45 * 60
        assert cfg.expiry_notice is True
        assert cfg.expiry_scan_seconds == 30

    def test_reads_overrides(self, monkeypatch):
        monkeypatch.setenv("ZULIP_ENGAGEMENT_MODE", "sticky_topic")
        monkeypatch.setenv("ZULIP_ENGAGEMENT_SCOPE", "topic")
        monkeypatch.setenv("ZULIP_ENGAGEMENT_TTL_MINUTES", "10")
        monkeypatch.setenv("ZULIP_ENGAGEMENT_EXPIRY_NOTICE", "false")
        monkeypatch.setenv("ZULIP_ENGAGEMENT_EXPIRY_SCAN_SECONDS", "15")
        cfg = EngagementConfig.from_env()
        assert cfg.mode == MODE_STICKY_TOPIC
        assert cfg.scope == SCOPE_TOPIC
        assert cfg.ttl_seconds == 600
        assert cfg.expiry_notice is False
        assert cfg.expiry_scan_seconds == 15

    def test_invalid_mode_falls_back_to_off_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("ZULIP_ENGAGEMENT_MODE", "nonsense")
        with caplog.at_level("WARNING"):
            cfg = EngagementConfig.from_env()
        assert cfg.mode == MODE_OFF
        assert "ZULIP_ENGAGEMENT_MODE" in caplog.text

    def test_invalid_scope_falls_back_to_user_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("ZULIP_ENGAGEMENT_MODE", "sticky_topic")
        monkeypatch.setenv("ZULIP_ENGAGEMENT_SCOPE", "everyone")
        with caplog.at_level("WARNING"):
            cfg = EngagementConfig.from_env()
        assert cfg.scope == SCOPE_USER
        assert "ZULIP_ENGAGEMENT_SCOPE" in caplog.text

    def test_invalid_ttl_falls_back(self, monkeypatch):
        monkeypatch.setenv("ZULIP_ENGAGEMENT_TTL_MINUTES", "-3")
        assert EngagementConfig.from_env().ttl_seconds == 45 * 60


class TestKeyIsolation:
    def _store(self, scope):
        return TopicEngagementStore(EngagementConfig(mode=MODE_STICKY_TOPIC, scope=scope))

    def test_user_scope_no_cross_topic_or_stream_leak(self):
        store = self._store(SCOPE_USER)
        store.mark_engaged(1, "topic-a", "u@zulip.com")
        assert store.is_engaged(1, "topic-a", "u@zulip.com") is True
        assert store.is_engaged(1, "topic-b", "u@zulip.com") is False
        assert store.is_engaged(2, "topic-a", "u@zulip.com") is False
        assert store.is_engaged(1, "topic-a", "other@zulip.com") is False

    def test_topic_scope_no_cross_topic_or_stream_leak(self):
        store = self._store(SCOPE_TOPIC)
        store.mark_engaged(1, "topic-a", "u@zulip.com")
        assert store.is_engaged(1, "topic-a", "other@zulip.com") is True
        assert store.is_engaged(1, "topic-b", "other@zulip.com") is False
        assert store.is_engaged(2, "topic-a", "other@zulip.com") is False

    def test_topics_are_case_sensitive(self):
        store = self._store(SCOPE_TOPIC)
        store.mark_engaged(1, "General", "u@zulip.com")
        assert store.is_engaged(1, "General", "u@zulip.com") is True
        assert store.is_engaged(1, "general", "u@zulip.com") is False

    def test_topic_and_user_key_shapes(self):
        store = self._store(SCOPE_USER)
        assert store._topic_key(1, "t") == "1\0t"
        assert store._user_key(1, "t", "U@Zulip.com") == "1\0t\0u@zulip.com"

    def test_mode_off_never_engages(self):
        store = TopicEngagementStore(EngagementConfig(mode=MODE_OFF))
        store.mark_engaged(1, "t", "u@zulip.com")
        assert store.is_engaged(1, "t", "u@zulip.com") is False
        assert store.active_count() == 0


class TestTtlExpiry:
    def _store(self):
        return TopicEngagementStore(
            EngagementConfig(mode=MODE_STICKY_TOPIC, scope=SCOPE_USER, ttl_seconds=100)
        )

    def test_idle_ttl_expires(self):
        store = self._store()
        store.mark_engaged(1, "t", "u@zulip.com", now=1000.0)
        assert store.is_engaged(1, "t", "u@zulip.com", now=1099.0) is True
        assert store.is_engaged(1, "t", "u@zulip.com", now=1101.0) is False

    def test_message_refreshes_ttl(self):
        store = self._store()
        store.mark_engaged(1, "t", "u@zulip.com", now=1000.0)
        store.touch(1, "t", "u@zulip.com", now=1099.0)
        assert store.is_engaged(1, "t", "u@zulip.com", now=1190.0) is True

    def test_pop_expired_removes_and_returns(self):
        store = self._store()
        store.mark_engaged(1, "t", "u@zulip.com", now=1000.0)
        expired = store.pop_expired(now=2000.0)
        assert [e.topic for e in expired] == ["t"]
        assert store.active_count(now=2000.0) == 0


class TestPhraseHelpers:
    @pytest.mark.parametrize(
        "content",
        ["stop listening", "Stop Listening", "/unlisten", "/stop-listening",
         "/stop_listening", "unlisten", "please stop listening"],
    )
    def test_stop_phrases_match(self, content):
        assert is_stop_listening_message(content) is True

    @pytest.mark.parametrize("content", ["stop", "/stop", "", "listening", "don't stop"])
    def test_bare_stop_is_not_claimed(self, content):
        assert is_stop_listening_message(content) is False

    @pytest.mark.parametrize("content", ["/new", "/reset", "new", "reset", "/reset now"])
    def test_end_session_phrases(self, content):
        assert is_end_session_message(content) is True

    @pytest.mark.parametrize("content", ["renew", "/newer", "resetting", ""])
    def test_non_end_session_phrases(self, content):
        assert is_end_session_message(content) is False


# --------------------------------------------------------------------------
# Adapter integration tests
# --------------------------------------------------------------------------


def _build(mock_platform_config, monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    import zulip.zulip_client as zulip_client_module

    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

    class MockZulipModule:
        class Client:
            def __init__(self, email=None, api_key=None, site=None):
                pass

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    from zulip.adapter import ZulipAdapter

    a = ZulipAdapter(mock_platform_config)
    a.email = "bot@zulip.com"
    a.handle_message = AsyncMock()
    # Capture outbound sends without touching the real SDK.
    a.client = MagicMock()
    return a


def _stream_msg(
    content, *, stream_id=1, topic="general", sender="user@zulip.com", sender_id=42
):
    return {
        "id": 1,
        "type": "stream",
        "stream_id": stream_id,
        "subject": topic,
        "display_recipient": "test",
        "content": content,
        "sender_email": sender,
        "sender_full_name": sender.split("@")[0],
        "sender_id": sender_id,
    }


def _dm(content, *, sender="user@zulip.com"):
    return {
        "id": 2,
        "type": "private",
        "content": content,
        "sender_email": sender,
        "sender_full_name": sender.split("@")[0],
        "sender_id": 42,
    }


@pytest.fixture(autouse=True)
def _hermes_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))


class TestOffByDefault:
    @pytest.mark.asyncio
    async def test_follow_up_without_mention_is_dropped(
        self, mock_platform_config, monkeypatch
    ):
        # Engagement unset -> off.
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
        )
        assert a._engagement_cfg.mode == MODE_OFF
        assert a._engagement_task is None

        await a._handle_message(_stream_msg("@bot hello"))
        assert a.handle_message.call_count == 1

        await a._handle_message(_stream_msg("a follow-up without a mention"))
        assert a.handle_message.call_count == 1  # dropped: no engagement
        assert a._engagement_store.active_count() == 0


class TestMentionToStart:
    @pytest.mark.asyncio
    async def test_user_scope_engages_only_the_opener(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="user",
        )

        await a._handle_message(_stream_msg("@bot start"))
        assert a.handle_message.call_count == 1

        # Same user, same topic, no mention -> answered.
        await a._handle_message(_stream_msg("and another thing"))
        assert a.handle_message.call_count == 2

        # Different user, same topic -> still needs a mention (user scope).
        await a._handle_message(_stream_msg("me too", sender="other@zulip.com"))
        assert a.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_topic_scope_engages_everyone(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="topic",
        )

        await a._handle_message(_stream_msg("@bot start"))
        assert a.handle_message.call_count == 1

        await a._handle_message(_stream_msg("me too", sender="other@zulip.com"))
        assert a.handle_message.call_count == 2

        # A different topic is not engaged.
        await a._handle_message(_stream_msg("other topic", topic="elsewhere"))
        assert a.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_ttl_expiry_stops_answering(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="user",
        )
        await a._handle_message(_stream_msg("@bot start"))
        assert a.handle_message.call_count == 1

        # Backdate the entry past the TTL.
        entry = next(iter(a._engagement_store._entries.values()))
        entry.last_active -= a._engagement_cfg.ttl_seconds + 1

        await a._handle_message(_stream_msg("too late"))
        assert a.handle_message.call_count == 1


class TestDmsUnaffected:
    @pytest.mark.asyncio
    async def test_dm_never_gated_by_engagement(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="user",
        )
        # No mention, but a DM is always answered and never touches the store.
        await a._handle_message(_dm("hello privately"))
        assert a.handle_message.call_count == 1
        assert a._engagement_store.active_count() == 0


class TestStopCommand:
    @pytest.mark.asyncio
    async def test_stop_clears_and_is_acknowledged(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="user",
        )
        await a._handle_message(_stream_msg("@bot start"))
        assert a.handle_message.call_count == 1
        assert a._engagement_store.is_engaged(1, "general", "user@zulip.com") is True

        # Stop is consumed: acknowledged in-topic, no agent turn.
        await a._handle_message(_stream_msg("stop listening"))
        assert a.handle_message.call_count == 1  # no dispatch
        assert a._engagement_store.is_engaged(1, "general", "user@zulip.com") is False

        sent = [c.args[0] for c in a.client.send_message.call_args_list]
        acks = [s for s in sent if "stop listening" in s.get("content", "")]
        assert acks, "expected an in-topic acknowledgement"
        assert acks[0]["type"] == "stream"
        assert acks[0]["topic"] == "general"

        # A follow-up now needs a fresh mention.
        await a._handle_message(_stream_msg("still there?"))
        assert a.handle_message.call_count == 1

    @pytest.mark.asyncio
    async def test_end_session_clears_engagement(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="user",
        )
        await a._handle_message(_stream_msg("@bot start"))
        assert a._engagement_store.is_engaged(1, "general", "user@zulip.com") is True

        # /reset clears but still falls through to the agent (session reset).
        await a._handle_message(_stream_msg("/reset"))
        assert a._engagement_store.is_engaged(1, "general", "user@zulip.com") is False


class TestExpiryNotice:
    @pytest.mark.asyncio
    async def test_expiry_posts_in_topic_notice(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="user",
            ZULIP_ENGAGEMENT_EXPIRY_NOTICE="true",
        )
        # An engagement that is already past its TTL.
        a._engagement_store.mark_engaged(5, "topic-x", "u@zulip.com")
        entry = next(iter(a._engagement_store._entries.values()))
        entry.last_active -= a._engagement_cfg.ttl_seconds + 1

        a._engagement_cfg.expiry_scan_seconds = 0.01
        a._listening = True
        task = asyncio.create_task(a._engagement_expiry_loop())
        await asyncio.sleep(0.05)
        a._listening = False
        await task

        sent = [c.args[0] for c in a.client.send_message.call_args_list]
        notices = [s for s in sent if "Engagement expired" in s.get("content", "")]
        assert notices, "expected an in-topic expiry notice"
        assert notices[0]["type"] == "stream"
        assert notices[0]["to"] == 5
        assert notices[0]["topic"] == "topic-x"

    @pytest.mark.asyncio
    async def test_expiry_notice_can_be_disabled(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="user",
            ZULIP_ENGAGEMENT_EXPIRY_NOTICE="false",
        )
        a._engagement_store.mark_engaged(5, "topic-x", "u@zulip.com")
        entry = next(iter(a._engagement_store._entries.values()))
        entry.last_active -= a._engagement_cfg.ttl_seconds + 1

        a._engagement_cfg.expiry_scan_seconds = 0.01
        a._listening = True
        task = asyncio.create_task(a._engagement_expiry_loop())
        await asyncio.sleep(0.05)
        a._listening = False
        await task

        sent = [c.args[0] for c in a.client.send_message.call_args_list]
        assert not [s for s in sent if "Engagement expired" in s.get("content", "")]
        assert a._engagement_store.active_count() == 0


# --------------------------------------------------------------------------
# Bot senders never engage (#221)
# --------------------------------------------------------------------------


class TestBotSenderClassifier:
    """The pure sender check that keeps bot traffic out of engagement (#221)."""

    @pytest.mark.parametrize(
        "email",
        [
            "helper-bot@org.zulipchat.com",
            "notification-bot@zulip.com",  # cross-realm system bot
            "Helper-BOT@Org.Zulipchat.com",  # case-insensitive
            "review-bot@self-hosted.example.org",  # not a zulipchat.com realm
        ],
    )
    def test_bot_address_convention(self, email):
        assert is_bot_address(email) is True

    @pytest.mark.parametrize(
        "email",
        [
            "dana@org.zulipchat.com",  # a human on a *zulipchat.com* realm
            "bot@example.com",  # the convention is `-bot`, not `bot`
            "bottom@example.com",  # bare substring must not match
            "",
            "no-at-sign",
            "-bot",
        ],
    )
    def test_human_addresses_are_not_bots(self, email):
        assert is_bot_address(email) is False

    def test_own_identity_is_a_bot_by_email_or_id(self):
        assert is_bot_sender("bot@zulip.com", bot_email="bot@zulip.com") is True
        assert is_bot_sender("BOT@Zulip.com", bot_email="bot@zulip.com") is True
        assert is_bot_sender("someone@example.com", sender_id=7, bot_user_id="7") is True

    def test_human_sender_is_not_a_bot(self):
        assert (
            is_bot_sender(
                "user@zulip.com",
                sender_id=42,
                bot_email="bot@zulip.com",
                bot_user_id="7",
            )
            is False
        )

    def test_classifier_works_without_a_configured_identity(self):
        # The address convention is self-contained: no bot registry, no
        # lookup, no config, so the guard cannot be silently un-armed.
        assert is_bot_sender("helper-bot@org.zulipchat.com") is True
        assert is_bot_sender("user@zulip.com") is False


class TestBotSendersNeverEngage:
    """A bot message may not satisfy an engagement nor refresh its TTL (#221)."""

    @pytest.mark.asyncio
    async def test_bot_message_is_not_accepted_and_does_not_extend_ttl(
        self, mock_platform_config, monkeypatch, caplog
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="topic",
        )
        await a._handle_message(_stream_msg("@bot start"))
        assert a.handle_message.call_count == 1

        entry = next(iter(a._engagement_store._entries.values()))
        last_active_before = entry.last_active

        with caplog.at_level("DEBUG"):
            await a._handle_message(
                _stream_msg(
                    "status report",
                    sender="helper-bot@org.zulipchat.com",
                    sender_id=99,
                )
            )

        # Not answered, and the TTL was not refreshed by the bot's traffic.
        assert a.handle_message.call_count == 1
        assert entry.last_active == last_active_before
        # The skip is diagnosable, with the sender masked.
        assert "engagement skip" in caplog.text
        assert "helper-bot@org.zulipchat.com" not in caplog.text
        assert "h***@org.zulipchat.com" in caplog.text

        # The human's window is exactly as it was: a follow-up still engages.
        await a._handle_message(_stream_msg("and another thing"))
        assert a.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_bot_mention_does_not_open_an_engagement(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="user",
        )
        bot = "helper-bot@org.zulipchat.com"

        # A bot that *names* the bot still dispatches: mention-gating is a
        # separate, pre-existing gate this issue does not change.
        await a._handle_message(_stream_msg("@bot hello", sender=bot, sender_id=99))
        assert a.handle_message.call_count == 1

        # But it opens no window: otherwise the next unmentioned message
        # from that bot would be admitted, which is the loop.
        assert a._engagement_store.active_count() == 0
        await a._handle_message(_stream_msg("still here", sender=bot, sender_id=99))
        assert a.handle_message.call_count == 1

    @pytest.mark.asyncio
    async def test_own_message_is_never_accepted_or_refreshed(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="topic",
        )
        a._bot_user_id = "4242"
        await a._handle_message(_stream_msg("@bot start"))
        assert a.handle_message.call_count == 1

        entry = next(iter(a._engagement_store._entries.values()))
        last_active_before = entry.last_active

        # Our own send echoed back by the event queue.
        await a._handle_message(
            _stream_msg("my own reply", sender="bot@zulip.com", sender_id=4242)
        )
        assert a.handle_message.call_count == 1
        assert entry.last_active == last_active_before
        assert a._engagement_store.active_count() == 1

    @pytest.mark.asyncio
    async def test_bot_message_in_a_non_engaged_topic_changes_nothing(
        self, mock_platform_config, monkeypatch
    ):
        a = _build(
            mock_platform_config,
            monkeypatch,
            ZULIP_CHATMODE="oncall",
            ZULIP_REQUIRE_MENTION="true",
            ZULIP_ENGAGEMENT_MODE="sticky_topic",
            ZULIP_ENGAGEMENT_SCOPE="topic",
        )
        await a._handle_message(
            _stream_msg(
                "fyi", sender="helper-bot@org.zulipchat.com", sender_id=99
            )
        )
        assert a.handle_message.call_count == 0
        assert a._engagement_store.active_count() == 0
