"""Fallback reader for auto-send when agents don't use structured tool calls.

When the agent ends its turn without invoking the messaging tool (common with
local OSS models like Qwen, Gemma, Llama), this module reads the assistant
text from the session's trajectory file and returns it for sending through
the channel.
"""

import json
import logging
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)


async def read_latest_assistant_texts(
    data_dir: str,
    agent_id: str,
    session_key: str,
    start_time: str,
    log_func: Optional[Callable[[str], None]] = None,
) -> Optional[list[str]]:
    """Read the latest assistant texts from a session's trajectory file.

    Scans trajectory files in the agent's sessions directory for
    ``trace.artifacts`` events with ``assistantTexts`` that occurred
    after ``start_time``.

    Returns the list of assistant texts, or None if no matching artifacts
    were found.
    """
    sessions_dir = Path(data_dir) / "agents" / agent_id / "sessions"
    if not sessions_dir.exists():
        log_func and log_func(f"[fallback-reader] sessions dir not found: {sessions_dir}")
        return None

    try:
        import asyncio
        entries = await asyncio.to_thread(
            lambda: [f for f in sessions_dir.iterdir() if f.name.endswith(".trajectory.jsonl")]
        )
    except OSError as e:
        log_func and log_func(f"[fallback-reader] readdir failed: {e}")
        return None

    if not entries:
        log_func and log_func(f"[fallback-reader] no trajectory files in {sessions_dir}")
        return None

    start_time_ms = 0.0
    if start_time:
        try:
            from datetime import datetime
            start_time_ms = datetime.fromisoformat(start_time).timestamp() * 1000
        except (ValueError, TypeError):
            pass

    for entry in entries:
        try:
            import asyncio
            content = await asyncio.to_thread(lambda: entry.read_text("utf-8"))
        except OSError:
            continue

        lines = content.split("\n")
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if not line or "trace.artifacts" not in line:
                continue

            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue

            if event.get("type") != "trace.artifacts":
                continue

            # Check session key match
            event_key = event.get("sessionKey", "")
            if event_key != session_key and not event_key.startswith(session_key + ":"):
                continue

            # Check start time
            if start_time_ms > 0:
                event_ts = event.get("ts", 0)
                if isinstance(event_ts, str):
                    try:
                        from datetime import datetime
                        event_ts = datetime.fromisoformat(event_ts).timestamp() * 1000
                    except (ValueError, TypeError):
                        event_ts = 0
                if event_ts < start_time_ms:
                    continue

            texts = event.get("data", {}).get("assistantTexts")
            if isinstance(texts, list) and len(texts) > 0:
                result = [t for t in texts if isinstance(t, str) and len(t) > 0]
                if result:
                    log_func and log_func(
                        f"[fallback-reader] MATCH in {entry.name} texts={len(result)}"
                    )
                    return result

    log_func and log_func("[fallback-reader] no match after scanning all files")
    return None
