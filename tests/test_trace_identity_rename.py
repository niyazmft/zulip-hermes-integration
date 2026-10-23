"""A trace's identity must survive a rename that happens mid-run.

A Zulip topic can be renamed while a run is in flight (R2/R3/R5/R8 in
``zulip.routing``). The run's session keeps the same *conversation id* — that is
the whole point of stable topic sessions — while the human topic *name* changes.

That makes any identity derived from the name unsafe for the lifetime of a run:
the key computed when the trace starts and the key computed when it finishes are
then different objects, so ``finish_trace`` looks up a key that does not exist,
leaves the board showing ``Working``, and leaves the durable record behind for
the next process restart to clean up (the very "board stuck in progress" failure
#161 exists to prevent).

These tests pin the name-independent invariant: whatever the host puts in the
event metadata (a conversation id, or the topic name), the run that started must
be the run that finishes, and its board must reach a terminal state.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

SUCCESS = SimpleNamespace(value="success")

# A real conversation id is shaped "c" + 12 hex chars (see routing._CONVERSATION_ID_RE).
CONVERSATION_ID = "c0123456789ab"


@pytest.fixture
def adapter(mock_platform_config, monkeypatch, tmp_path):
    import zulip.adapter as adapter_module
    import zulip.zulip_client as zulip_client_module

    monkeypatch.setattr(zulip_client_module, "ZULIP_AVAILABLE", True)
    monkeypatch.setenv("ZULIP_SITE", "https://test.zulipchat.com")
    monkeypatch.setenv("ZULIP_EMAIL", "bot@test.com")
    monkeypatch.setenv("ZULIP_API_KEY", "k" * 32)
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ZULIP_ACTIVITY_TRACE", "1")
    monkeypatch.setenv("ZULIP_TRACE_COALESCE_MS", "0")

    class MockZulipModule:
        class Client:
            def __init__(self, **kwargs):
                self.sent = []
                self.edited = []
                self._next_id = 500

            def send_message(self, request):
                self.sent.append(request)
                self._next_id += 1
                return {"result": "success", "id": self._next_id}

            def update_message(self, request):
                self.edited.append(request)
                return {"result": "success"}

            def set_typing_status(self, *a, **k):
                return {"result": "success"}

    monkeypatch.setattr(zulip_client_module, "zulip", MockZulipModule())
    # Adapters self-register in a module-level WeakSet that the hooks iterate;
    # a stale adapter from an earlier test can answer for this one. Isolate it.
    monkeypatch.setattr(
        adapter_module, "_LIVE_ADAPTERS", type(adapter_module._LIVE_ADAPTERS)()
    )
    adapter_module._clear_caches()

    from zulip.adapter import ZulipAdapter

    return ZulipAdapter(mock_platform_config)


def conversation_registry(name):
    """A registry whose conversation currently lives under ``name``.

    ``current_name`` mirrors TopicConversationRegistry: it maps a *conversation
    id* to its current topic name and returns None for anything else, so a topic
    name never resolves to itself through it.
    """
    registry = MagicMock()
    registry.current_name.side_effect = (
        lambda channel_id, conversation_id: (
            name["value"] if conversation_id == CONVERSATION_ID else None
        )
    )
    return registry


async def _settle():
    """Let the background trace-start task finish (it posts on a worker thread)."""
    await asyncio.sleep(0.15)


@pytest.mark.asyncio
async def test_a_rename_mid_run_does_not_strand_the_trace(adapter, monkeypatch):
    """The board must reach a terminal state even if the topic was renamed."""
    from zulip import tracing

    name = {"value": "api-review"}
    monkeypatch.setattr(
        adapter._routing, "_conversations", conversation_registry(name), raising=False
    )

    # Under stable topic sessions the event's thread is the conversation id.
    event = SimpleNamespace(
        source=SimpleNamespace(chat_id="573423"),
        metadata={"thread_id": CONVERSATION_ID},
    )
    await adapter.on_processing_start(event)
    await _settle()
    assert len(adapter.client.sent) == 1, "one status board per work item"

    trace = next(iter(adapter._traces.values()))
    trace.complete(trace.step("work"))

    # The topic is renamed while the run is still going. The conversation — and
    # therefore the run — is the same one.
    name["value"] = "api-review-v2"

    adapter._note_trace_reply("573423", {"thread_id": CONVERSATION_ID})
    await adapter.on_processing_complete(event, SUCCESS)

    assert adapter.client.edited, (
        "the terminal flush never happened: the finish key did not match the "
        "start key, so the board was left showing Working"
    )
    final = adapter.client.edited[-1]["content"]
    assert "**Done**" in final, final
    assert "replied in" in final, "the delivered reply must count after a rename"
    assert adapter._traces == {}, "the trace must not leak after finalization"
    assert adapter._trace_replied == set(), "reply accounting must not leak keys"
    assert tracing.load_trace_records(adapter) == {}, (
        "a finalized run must not leave a durable record for startup recovery"
    )
