"""Inbound uploads must reach the agent as files, not as dead links (#271).

``extract_upload_urls`` and ``download_upload`` were written for #8 and then
never called: ``inbound.py`` built every event as text, with no ``media_urls``.
A user's screenshot arrived as ``[image.jpeg](/user_uploads/...)`` and the
gateway resolved the server path under the profile folder, so the vision tool
answered "media file not found".

These tests drive the real inbound chain and inspect the ``MessageEvent`` the
gateway receives, which is the only thing the agent ever sees.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# Streams only dispatch unmentioned traffic with the mention requirement off.
_DISPATCH_ALL = {"ZULIP_CHATMODE": "onmessage", "ZULIP_REQUIRE_MENTION": "false"}


class _FakeClient:
    def __init__(self, **kwargs):
        self.sent: list[dict] = []
        self.get_messages_calls: list[dict] = []
        self.get_messages_result = {"result": "success", "messages": []}

    def send_message(self, request):
        self.sent.append(request)
        return {"result": "success", "id": len(self.sent) + 1000}

    def update_message_flags(self, request):
        return {"result": "success"}

    def add_reaction(self, request):
        return {"result": "success"}

    def remove_reaction(self, request):
        return {"result": "success"}

    def set_typing_status(self, request):
        return {"result": "success"}

    def get_messages(self, request):
        self.get_messages_calls.append(request)
        return self.get_messages_result


@pytest.fixture
def make_adapter(mock_platform_config, monkeypatch):
    def _make(**env):
        import zulip.zulip_client as zulip_client_module

        monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)

        class MockZulipModule:
            class Client:
                def __init__(self, **kwargs):
                    self._client = _FakeClient(**kwargs)

                def __getattr__(self, name):
                    return getattr(self._client, name)

        monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        from zulip.adapter import ZulipAdapter

        adapter = ZulipAdapter(mock_platform_config)
        adapter.email = "bot@test.zulipchat.com"
        adapter.site = "https://test.zulipchat.com"
        adapter.api_key = "fake-key"
        adapter.bot_full_name = "Test Bot"
        adapter.handle_message = AsyncMock()
        return adapter

    return _make


def _stream_msg(content, *, msg_id=1, topic="general", stream_id=1):
    return {
        "id": msg_id,
        "type": "stream",
        "stream_id": stream_id,
        "subject": topic,
        "display_recipient": "test",
        "content": content,
        "sender_email": "user@zulip.com",
        "sender_full_name": "User",
        "sender_id": 42,
        "flags": [],
    }


def _dispatched(adapter):
    return adapter.handle_message.call_args[0][0]


def _patch_fetch(monkeypatch, *, data=b"png-bytes", content_type="image/png", filename="shot.png"):
    calls = []

    async def fake_fetch(url, auth_header, max_bytes, base_url):
        calls.append(
            {
                "url": url,
                "auth_header": auth_header,
                "max_bytes": max_bytes,
                "base_url": base_url,
            }
        )
        return {"data": data, "content_type": content_type, "filename": filename}

    monkeypatch.setattr("zulip.media.fetch_upload", fake_fetch)
    return calls


def _patch_cache(monkeypatch, *, path="/cache/images/img_1.png", media_type=None, kind="image"):
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("zulip.media.asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr(
        "zulip.media.cache_inbound_upload",
        lambda data, filename, mime: {
            "path": path,
            "media_type": media_type or mime,
            "kind": kind,
        },
    )


class TestAnUploadReachesTheEvent:
    @pytest.mark.asyncio
    async def test_markdown_upload_becomes_media_on_the_event(
        self, make_adapter, monkeypatch
    ):
        adapter = make_adapter(**_DISPATCH_ALL)
        calls = _patch_fetch(monkeypatch)
        _patch_cache(monkeypatch)

        await adapter._handle_message(
            _stream_msg("[shot.png](/user_uploads/2/ab/c1/shot.png) what is this?")
        )

        event = _dispatched(adapter)
        assert event.media_urls == ["/cache/images/img_1.png"]
        assert event.media_types == ["image/png"]
        assert event.media_text_inlined == [False]
        assert event.message_type is not None
        assert event.message_type.name == "PHOTO"
        assert calls[0]["url"] == "https://test.zulipchat.com/user_uploads/2/ab/c1/shot.png"
        assert calls[0]["base_url"] == "https://test.zulipchat.com"
        assert calls[0]["auth_header"], "the download must be authenticated"

    @pytest.mark.asyncio
    async def test_a_document_upload_is_a_document_turn(self, make_adapter, monkeypatch):
        adapter = make_adapter(**_DISPATCH_ALL)
        _patch_fetch(monkeypatch, data=b"%PDF", content_type="application/pdf", filename="a.pdf")
        _patch_cache(monkeypatch, path="/cache/documents/doc_1.pdf", kind="document")

        await adapter._handle_message(
            _stream_msg("[a.pdf](/user_uploads/1/a/b/a.pdf) summarise")
        )

        event = _dispatched(adapter)
        assert event.media_types == ["application/pdf"]
        assert event.message_type.name == "DOCUMENT"

    @pytest.mark.asyncio
    async def test_the_live_text_survives_the_attachment(self, make_adapter, monkeypatch):
        adapter = make_adapter(**_DISPATCH_ALL)
        _patch_fetch(monkeypatch)
        _patch_cache(monkeypatch)

        await adapter._handle_message(
            _stream_msg("[shot.png](/user_uploads/2/ab/c1/shot.png) what is this?")
        )

        assert "what is this?" in _dispatched(adapter).text

    @pytest.mark.asyncio
    async def test_no_upload_means_no_fetch_and_a_plain_turn(
        self, make_adapter, monkeypatch
    ):
        adapter = make_adapter(**_DISPATCH_ALL)
        calls = _patch_fetch(monkeypatch)

        await adapter._handle_message(_stream_msg("just a question"))

        event = _dispatched(adapter)
        assert calls == []
        assert event.media_urls == []
        assert event.message_type.name == "TEXT"


class TestARefusedUploadIsStated:
    @pytest.mark.asyncio
    async def test_a_refusal_is_one_line_in_the_turn(self, make_adapter, monkeypatch):
        from zulip.media import MediaRefused

        adapter = make_adapter(**_DISPATCH_ALL)

        async def refuse(*a, **k):
            raise MediaRefused("not an upload on this Zulip site")

        monkeypatch.setattr("zulip.media.fetch_upload", refuse)

        await adapter._handle_message(
            _stream_msg("[x](/user_uploads/1/a/b.png) what is this?")
        )

        event = _dispatched(adapter)
        assert event.media_urls == []
        assert "what is this?" in event.text
        assert "could not be read" in event.text
        assert "not an upload on this Zulip site" in event.text

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_does_not_take_the_turn_down(
        self, make_adapter, monkeypatch
    ):
        adapter = make_adapter(**_DISPATCH_ALL)

        async def boom(*a, **k):
            raise RuntimeError("socket closed")

        monkeypatch.setattr("zulip.media.fetch_upload", boom)

        await adapter._handle_message(
            _stream_msg("[x](/user_uploads/1/a/b.png) still there?")
        )

        event = _dispatched(adapter)
        assert event.media_urls == []
        assert "still there?" in event.text
        assert "could not be read" in event.text


class TestQuotedHistoryIsNotDownloaded:
    @pytest.mark.asyncio
    async def test_an_upload_named_only_in_quoted_history_is_not_fetched(
        self, make_adapter, monkeypatch
    ):
        """Only the live message's uploads may be fetched.

        The quoted-history block is prepended to the agent-facing body, so
        extracting after it would download every attachment ever mentioned in
        the topic -- an unbounded, attacker-supplyable set of requests.
        """
        adapter = make_adapter(**_DISPATCH_ALL)
        calls = _patch_fetch(monkeypatch)
        _patch_cache(monkeypatch)

        async def quoted(*a, **k):
            return "[Topic history]\nEarlier: [old](/user_uploads/9/z/z/old.png)\n\n"

        monkeypatch.setattr(adapter, "_quoted_topic_history", quoted)

        await adapter._handle_message(
            _stream_msg("[new](/user_uploads/2/ab/c1/new.png) current?")
        )

        assert [c["url"] for c in calls] == [
            "https://test.zulipchat.com/user_uploads/2/ab/c1/new.png"
        ]
        assert _dispatched(adapter).media_urls == ["/cache/images/img_1.png"]
