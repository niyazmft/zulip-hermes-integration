"""Bot workspace — safe file generation and cleanup for AI agents.

Provides a sandboxed directory where bots can write files (reports, CSVs,
JSON dumps, etc.) and later send them as Zulip uploads. Files are isolated
per bot instance and auto-cleaned on a TTL to prevent disk bloat.

Usage
-----
    from zulip.workspace import BotWorkspace

    ws = BotWorkspace()
    path = ws.save_text("report.csv", "id,value\n1,42\n")
    await adapter.send(chat_id, "Here is your report:", media_files=[path])
"""

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 3600  # 1 hour


class BotWorkspace:
    """Sandboxed workspace for bot-generated files.

    All files live under a single directory (default: under /tmp).
    Old files are pruned on every save to keep disk usage bounded.
    """

    def __init__(self, root: Optional[str] = None, ttl: int = DEFAULT_TTL_SECONDS):
        if root:
            self.root = Path(root).expanduser().resolve()
        else:
            self.root = Path(tempfile.gettempdir()) / "hermes_bot_workspace"
        self.ttl = ttl
        self.root.mkdir(parents=True, exist_ok=True)

    def _prune_old(self) -> int:
        """Delete files older than TTL. Returns count deleted."""
        cutoff = time.time() - self.ttl
        deleted = 0
        for p in self.root.iterdir():
            if p.is_file() and p.stat().st_mtime < cutoff:
                try:
                    p.unlink()
                    deleted += 1
                except OSError as e:
                    logger.warning("workspace prune failed [file=%s]: %s", p, e)
        return deleted

    def _safe_path(self, filename: str) -> Path:
        """Resolve a filename under root, rejecting path traversal and symlinks."""
        target = self.root / filename
        # Check for symlinks BEFORE resolving — prevent reading outside workspace
        if target.is_symlink():
            raise ValueError(f"Symlink rejected: {filename}")
        target = target.resolve()
        if not str(target).startswith(str(self.root)):
            raise ValueError(f"Path traversal rejected: {filename}")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def save_text(self, filename: str, content: str) -> str:
        """Write text to workspace. Returns absolute path."""
        self._prune_old()
        path = self._safe_path(filename)
        path.write_text(content, encoding="utf-8")
        logger.debug("workspace saved text [file=%s]", path)
        return str(path)

    def save_bytes(self, filename: str, content: bytes) -> str:
        """Write raw bytes to workspace. Returns absolute path."""
        self._prune_old()
        path = self._safe_path(filename)
        path.write_bytes(content)
        logger.debug("workspace saved bytes [file=%s]", path)
        return str(path)

    def save_json(self, filename: str, data: Any) -> str:
        """Serialize JSON to workspace. Returns absolute path."""
        return self.save_text(filename, json.dumps(data, indent=2, ensure_ascii=False))

    def read_text(self, filename: str) -> str:
        """Read text from workspace."""
        path = self._safe_path(filename)
        return path.read_text(encoding="utf-8")

    def read_bytes(self, filename: str) -> bytes:
        """Read raw bytes from workspace."""
        path = self._safe_path(filename)
        return path.read_bytes()

    def list_files(self) -> list[str]:
        """List all files in workspace (names only, relative to root)."""
        return sorted(
            str(p.relative_to(self.root))
            for p in self.root.rglob("*")
            if p.is_file()
        )

    def clear(self) -> int:
        """Delete every file under root. Returns count deleted."""
        deleted = 0
        for p in self.root.rglob("*"):
            if p.is_file():
                try:
                    p.unlink()
                    deleted += 1
                except OSError:
                    pass
            elif p.is_dir() and p != self.root:
                try:
                    shutil.rmtree(p)
                except OSError:
                    pass
        logger.info("workspace cleared [deleted=%d root=%s]", deleted, self.root)
        return deleted
