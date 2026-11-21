"""The kernel lock survives overlapping turns and releases on process death."""

import asyncio
from pathlib import Path
import signal
import sys

import pytest

from zulip.topic_lock import TopicLock

pytest.importorskip("fcntl")

SITE = "https://chat.example.invalid"
LOCK_MODULE = str(Path(__file__).resolve().parents[1] / "zulip" / "topic_lock.py")
HOLDER = """
import asyncio
import runpy
import sys

TopicLock = runpy.run_path(sys.argv[1])["TopicLock"]

async def main():
    async with TopicLock(sys.argv[2], sys.argv[3]).hold("7", "Sprint"):
        print("locked", flush=True)
        await asyncio.Event().wait()

asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_topic_lock_is_process_shared_and_releases_on_kill(tmp_path):
    holder = await asyncio.create_subprocess_exec(
        sys.executable, "-c", HOLDER, LOCK_MODULE, str(tmp_path), SITE,
        stdout=asyncio.subprocess.PIPE,
    )
    waiter = None
    try:
        assert await asyncio.wait_for(holder.stdout.readline(), 3) == b"locked\n"
        lock = TopicLock(str(tmp_path), SITE)
        waiting = asyncio.Event()

        async def take_turn():
            waiting.set()
            async with lock.hold("7", "sprint"):
                return "ran"

        waiter = asyncio.create_task(take_turn())
        await waiting.wait()
        assert not waiter.done()
        # Different topics and channels have separate files.
        async with lock.hold("7", "Other"), lock.hold("8", "Sprint"):
            pass
        assert not waiter.done()
        holder.kill()
        assert await asyncio.wait_for(holder.wait(), 3) == -signal.SIGKILL
        assert await asyncio.wait_for(waiter, 3) == "ran"
    finally:
        if holder.returncode is None:
            holder.kill()
            await holder.wait()
        if waiter is not None and not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
