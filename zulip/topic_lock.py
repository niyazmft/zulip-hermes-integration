"""Same-host coordination for bot turns in a Zulip topic."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # Safe to import on platforms where the feature is off.
    fcntl = None


class TopicLock:
    def __init__(self, directory: str, site: str) -> None:
        self.directory = Path(directory)
        if fcntl is None:
            raise ValueError("ZULIP_TOPIC_LOCK_DIR requires OS support for flock")
        if not self.directory.is_absolute() or not self.directory.is_dir():
            raise ValueError("ZULIP_TOPIC_LOCK_DIR must be an existing absolute directory")
        parsed = urlsplit(site)
        port = parsed.port or {"https": 443, "http": 80}.get(parsed.scheme)
        self.realm = (parsed.scheme, parsed.hostname, port, parsed.path.rstrip("/"))

    @asynccontextmanager
    async def hold(self, stream_id: str, topic: str):
        identity = json.dumps((self.realm, str(stream_id), topic.casefold()))
        key = hashlib.sha256(identity.encode()).hexdigest()
        # Never unlink these files: waiters may already hold the old inode open.
        fd = os.open(self.directory / key, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.1)
            yield
        finally:
            # Closing releases the lock on cancellation; the OS also closes it on SIGKILL.
            os.close(fd)
