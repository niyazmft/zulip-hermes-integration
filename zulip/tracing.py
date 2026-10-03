"""Activity-trace lifecycle and its durable records (epic #139).

One bot-owned status message per work item, edited in place: posted when the
gateway reports a run started (:func:`start_trace`), fed by the agent's own tool
calls (mode A, :func:`record_tool_step`) and progress notes (mode B,
:func:`record_progress_step`), and finalized with the outcome the gateway
reports (:func:`finish_trace`). :func:`recover_interrupted_traces` closes out a
trace whose process died before it could finalize, so a topic can never be left
showing ``Working`` forever (issue #161).

Layering: L3 — depends on L0–L2 (``activity_trace``, ``outbound``,
``zulip_client``, ``logger``) and the host's ``gateway.session_context``.

The adapter is passed in as an opaque collaborator and its state (``_traces``,
``_trace_cfg``, ``client``, ``_sdk_call``, ``_send_timeout``, ``_audit_logger``,
``_routed_topic``, ``_topic_cache``, ``_session_key_for_event``, ...) is read at
call time, so an instance patch of any of those still takes effect. ``adapter``
is deliberately never imported here — that would be a cycle, and the adapter's
own methods are thin delegates into this module.

``activity_trace.TraceConfig`` and ``outbound.metadata_topic`` are reached
through their owning modules rather than imported as names, so patching
``zulip.activity_trace`` / ``zulip.outbound`` keeps working.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from . import activity_trace
from . import outbound
from . import routing
from .logger import mask_pii
from .zulip_client import parse_target

# Per-task session context published by the gateway (epic #139 / #159). Guarded
# like every other host symbol: on a gateway without it, tool steps simply
# cannot be attributed and are dropped rather than guessed at. Owned here, not
# in ``adapter``, so a ``setattr`` aimed at this module is the one that counts.
try:
    from gateway.session_context import get_session_env as _host_get_session_env
except ImportError:  # pragma: no cover - gateways without session context
    _host_get_session_env = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def trace_config() -> activity_trace.TraceConfig:
    """Resolve the trace configuration from the environment.

    Read through ``activity_trace`` rather than a local binding so a patch of
    ``zulip.activity_trace.TraceConfig`` still takes effect.
    """
    return activity_trace.TraceConfig.from_env()


def trace_key(chat_id: str, metadata: Any) -> str:
    """Session key for a trace: the chat *plus* the topic it lands in.

    Topic sessions share a chat_id across topics, so keying on chat_id alone
    would let two concurrent topics in one stream edit each other's trace.
    """
    return f"{chat_id}\x00{outbound.metadata_topic(metadata) or ''}"


def trace_payload(
    adapter: Any, chat_id: str, metadata: Any, content: str
) -> Optional[dict]:
    """Send payload for a trace message, using the *reply* routing.

    Deliberately the same resolution the reply uses (``metadata_topic`` and
    ``parse_target``). Issue #143 was a second, drifting topic resolution, and a
    trace that lands in the wrong topic is worse than no trace.
    """
    try:
        target = parse_target(chat_id)
    except (TypeError, ValueError):
        return None
    if target["type"] == "dm":
        return {"type": "private", "to": target["user_ids"], "content": content}
    # Same resolution the reply path uses (_routed_topic): with stable
    # topic sessions metadata.topic is a conversation id, and a raw
    # send would materialize a ghost topic named like the id (#143 lesson:
    # no second, drifting resolution).
    topic = (
        adapter._routed_topic(target["stream_id"], metadata)
        or adapter._routing._topic_cache.get(chat_id, "general")
    )
    return {
        "type": "stream",
        "to": target["stream_id"],
        "topic": topic,
        "content": content,
    }


def note_trace_reply(adapter: Any, chat_id: str, metadata: Any) -> None:
    """Record that this work item actually produced a reply."""
    if adapter._traces:
        adapter._trace_replied.add(trace_key(chat_id, metadata))


def _session_aliases(adapter: Any, event: Any) -> list[str]:
    """Aliases that identify this event's run, as seen from the adapter.

    Owned by ``zulip.routing`` (session-key derivation); this is the trace
    module's call site, not a second definition.
    """
    return routing.session_aliases(adapter, event)


def _session_aliases_from_context() -> list[str]:
    """The same aliases, read from the *current* task's session context.

    Tool callbacks run outside the adapter, so this is how they recover
    which run they belong to. The hook payload's ``session_id`` is the
    agent-side durable id and is deliberately **not** used: it is derived
    differently from the adapter session key, so matching on it would be a
    guess.
    """
    if _host_get_session_env is None:
        return []
    try:
        key = _host_get_session_env("HERMES_SESSION_KEY") or ""
        chat_id = _host_get_session_env("HERMES_SESSION_CHAT_ID") or ""
        thread = _host_get_session_env("HERMES_SESSION_THREAD_ID") or ""
    except Exception:
        return []
    aliases: list[str] = []
    if key:
        aliases.append(f"key:{key}")
    if chat_id:
        aliases.append(f"route:{chat_id}\x00{thread}")
    return aliases


def trace_for_current_session(adapter: Any) -> Optional[activity_trace.ActivityTrace]:
    """The trace for the task we are running inside, or None.

    Shared by the tool hook (mode A) and the progress tool (mode B) so both
    attribute through exactly **one** code path — two would drift.
    """
    if not adapter._trace_cfg.enabled or not adapter._trace_sessions:
        return None
    for alias in _session_aliases_from_context():
        key = adapter._trace_sessions.get(alias)
        if key is not None:
            return adapter._traces.get(key)
    return None


def record_progress_step(adapter: Any, note: str) -> bool:
    """Add an agent-authored step (mode B). True when it was shown."""
    trace = trace_for_current_session(adapter)
    if trace is None:
        return False
    step = trace.step(str(note))
    trace.complete(step, ok=True)
    return step is not None


def record_tool_step(
    adapter: Any,
    *,
    tool_name: str = "",
    status: Optional[str] = None,
    duration_ms: int = 0,
    error_type: Optional[str] = None,
    **_: Any,
) -> None:
    """Attach a finished tool call to its work item's trace.

    A step whose run cannot be identified is **dropped**, never guessed into
    some other topic: a trace that narrates another conversation's work is
    worse than a trace with a gap.
    """
    # ZULIP_TRACE_TOOL_MATCHER (#176): a tool-heavy turn otherwise renders a
    # board far too long to read. The engine's coalescing bounds the API
    # cost, not the readability.
    if not adapter._trace_cfg.allows_tool(tool_name):
        return

    trace = trace_for_current_session(adapter)
    if trace is None:
        return

    ok = (status or "ok") != "error"
    detail = ""
    if ok and duration_ms:
        detail = f"{duration_ms} ms"
    elif not ok:
        detail = error_type or "failed"
    step = trace.step(str(tool_name or "tool"))
    trace.complete(step, ok=ok, detail=detail)


# ── durable trace records (issue #161) ──────────────────────────────────────


def trace_records_path(adapter: Any) -> Path:
    safe = "".join(
        ch if (ch.isalnum() or ch in "._-") else "_"
        for ch in (adapter.email or "default")
    )
    return Path(adapter._data_dir) / f"zulip_traces_{safe}.json"


def load_trace_records(adapter: Any) -> dict:
    try:
        with open(trace_records_path(adapter), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("zulip: unreadable trace record file, starting fresh: %s", e)
        return {}


def save_trace_records(adapter: Any, records: dict) -> None:
    path = trace_records_path(adapter)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(records, fh)
        os.replace(tmp, path)
    except Exception as e:
        logger.debug("zulip: could not persist trace records: %s", e)


def persist_trace_record(
    adapter: Any, key: str, chat_id: str, metadata: Any, message_id: int
) -> None:
    records = load_trace_records(adapter)
    records[key] = {
        "message_id": int(message_id),
        "chat_id": str(chat_id),
        "topic": outbound.metadata_topic(metadata) or "",
        "started_at": time.time(),
    }
    save_trace_records(adapter, records)


def drop_trace_record(adapter: Any, key: str) -> None:
    records = load_trace_records(adapter)
    if key in records:
        records.pop(key, None)
        save_trace_records(adapter, records)


async def recover_interrupted_traces(adapter: Any) -> int:
    """Close out traces whose run died with the previous process.

    A trace can only be finalized by the process that created it, so an
    abrupt restart (deploy, crash, OOM) would leave the topic showing
    ``Working`` forever — the one case where "no trace is left permanently
    in progress" fails. Those records are collapsed here to a terminal
    cancelled board. (Issue #161)
    """
    records = load_trace_records(adapter)
    if not records:
        return 0

    content = "**Cancelled**\n\nrun interrupted by a gateway restart"
    recovered = 0
    for key, record in list(records.items()):
        message_id = record.get("message_id") if isinstance(record, dict) else None
        if not isinstance(message_id, int):
            # Unusable record: drop it rather than retry it every start.
            records.pop(key, None)
            continue

        ok = False
        try:
            result = await adapter._sdk_call(
                adapter.client.update_message,
                {"message_id": message_id, "content": content},
                timeout=adapter._send_timeout,
            )
            ok = isinstance(result, dict) and result.get("result") == "success"
        except Exception as e:
            logger.warning(
                "zulip: could not close interrupted trace [key=%s]: %s", key, e
            )

        if not ok:
            # Typical cause: the realm's message-edit limit has passed.
            # Log it and STILL clear the record, otherwise every start
            # retries an edit the server will keep refusing.
            logger.warning(
                "zulip: interrupted trace left unfinalized"
                " [key=%s message=%s]",
                key,
                message_id,
            )

        records.pop(key, None)
        recovered += 1
        try:
            await adapter._audit_logger.log_event(
                "activity_trace_recovered",
                {
                    "chat_id": mask_pii(str(record.get("chat_id", ""))),
                    "topic": str(record.get("topic", "")),
                    "message_id": message_id,
                    "finalized": ok,
                },
            )
        except Exception:
            pass  # auditing never blocks recovery

    save_trace_records(adapter, records)
    if recovered:
        logger.info("zulip: closed %d interrupted trace(s) after restart", recovered)
    return recovered


def start_trace(adapter: Any, event: Any) -> None:
    """Begin a trace for a work item. Never raises, never blocks the run."""
    if not adapter._trace_cfg.enabled:
        return
    source = getattr(event, "source", None)
    chat_id = str(getattr(source, "chat_id", "") or "")
    metadata = getattr(event, "metadata", None)
    if not chat_id:
        return

    key = trace_key(chat_id, metadata)
    if key in adapter._traces:
        return  # already tracing this work item

    title = "Working"
    if isinstance(metadata, dict) and metadata.get("trace_title"):
        title = str(metadata["trace_title"])

    async def post(content: str) -> Optional[int]:
        payload = trace_payload(adapter, chat_id, metadata, content)
        if payload is None:
            return None
        # A trace message is an outbound send like any other, so it records
        # the same delivery outcome (issue #145).
        topic = outbound.metadata_topic(metadata)
        try:
            result = await adapter._sdk_call(
                adapter.client.send_message, payload, timeout=adapter._send_timeout
            )
        except Exception:
            await adapter._audit_logger.log_deliver_failed(
                "send_exception", chat_id=chat_id, topic=topic
            )
            raise
        if not isinstance(result, dict) or result.get("result") != "success":
            await adapter._audit_logger.log_deliver_failed(
                "api_error", chat_id=chat_id, topic=topic
            )
            return None
        try:
            message_id = int(result.get("id"))
        except (TypeError, ValueError):
            return None
        await adapter._audit_logger.log_deliver_payload(
            chat_id=chat_id, topic=topic, message_id=str(message_id)
        )
        return message_id

    async def edit(message_id: int, content: str) -> bool:
        result = await adapter._sdk_call(
            adapter.client.update_message,
            {"message_id": message_id, "content": content},
            timeout=adapter._send_timeout,
        )
        return isinstance(result, dict) and result.get("result") == "success"

    trace = activity_trace.ActivityTrace(post, edit, adapter._trace_cfg, title=title)
    adapter._traces[key] = trace
    adapter._trace_started[key] = time.monotonic()
    for alias in _session_aliases(adapter, event):
        adapter._trace_sessions[alias] = key
    # Posted in the background: that is one API round-trip and the agent run
    # must not wait on it. ``finish_trace`` awaits this task before
    # finalizing, so a fast turn cannot leave a stale "Working" board.
    try:
        adapter._trace_start_tasks[key] = asyncio.get_running_loop().create_task(
            start_trace_and_persist(adapter, trace, key, chat_id, metadata)
        )
    except RuntimeError:  # no running loop (sync caller): the trace is inert
        pass


async def start_trace_and_persist(
    adapter: Any, trace: Any, key: str, chat_id: str, metadata: Any
) -> None:
    """Post the trace, then remember it so a later start can close it out.

    The record is written only once the post succeeded: with no message id
    there is nothing for recovery to finalize. (Issue #161)
    """
    ok = await trace.start()
    if ok and trace.message_id is not None:
        persist_trace_record(adapter, key, chat_id, metadata, trace.message_id)


async def finish_trace(adapter: Any, event: Any, outcome: Any) -> None:
    """Finalize a work item's trace. Never raises into the gateway loop."""
    if not adapter._trace_cfg.enabled:
        return
    source = getattr(event, "source", None)
    chat_id = str(getattr(source, "chat_id", "") or "")
    metadata = getattr(event, "metadata", None)
    key = trace_key(chat_id, metadata)
    trace = adapter._traces.pop(key, None)
    if trace is None:
        return

    task = adapter._trace_start_tasks.pop(key, None)
    if task is not None:
        try:
            await task
        except Exception:
            pass  # a failed post already dropped the trace

    started = adapter._trace_started.pop(key, None)
    for alias, mapped in list(adapter._trace_sessions.items()):
        if mapped == key:
            adapter._trace_sessions.pop(alias, None)
    elapsed = max(0.0, time.monotonic() - started) if started else 0.0
    replied = key in adapter._trace_replied
    adapter._trace_replied.discard(key)

    status = getattr(outcome, "value", outcome)
    try:
        if status == "cancelled":
            note = f"cancelled after {elapsed:.0f}s"
            if not replied:
                note += " — no reply sent"
            await trace.finish(note=note)
        elif status == "failure":
            note = f"run failed after {elapsed:.0f}s"
            if not replied:
                note += " — no reply sent"
            await trace.fail(note)
        elif replied:
            await trace.finish(note=f"replied in {elapsed:.0f}s")
        else:
            # The case that used to be invisible: the run produced nothing.
            await trace.finish(note=f"run finished in {elapsed:.0f}s — no reply sent")
    except Exception as e:
        logger.warning("activity trace finalize failed (dropped): %s", e)
    finally:
        # Finalized here, so a later start must not try again — even when the
        # final edit failed, or recovery would retry it forever. (#161)
        drop_trace_record(adapter, key)
