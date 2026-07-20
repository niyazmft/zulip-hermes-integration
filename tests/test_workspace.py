"""Tests for zulip.workspace — bot file generation and cleanup."""

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from zulip.workspace import BotWorkspace, DEFAULT_TTL_SECONDS


class TestBotWorkspace:
    def test_init_creates_directory(self, tmp_path):
        ws = BotWorkspace(root=str(tmp_path / "bot_ws"))
        assert ws.root.exists()
        assert ws.root.is_dir()

    def test_save_text_and_read(self, tmp_path):
        ws = BotWorkspace(root=str(tmp_path))
        path = ws.save_text("report.csv", "id,value\n1,42\n")
        assert Path(path).exists()
        assert Path(path).read_text() == "id,value\n1,42\n"
        assert ws.read_text("report.csv") == "id,value\n1,42\n"

    def test_save_bytes(self, tmp_path):
        ws = BotWorkspace(root=str(tmp_path))
        path = ws.save_bytes("data.bin", b"\x00\x01\x02")
        assert Path(path).read_bytes() == b"\x00\x01\x02"
        assert ws.read_bytes("data.bin") == b"\x00\x01\x02"

    def test_save_json(self, tmp_path):
        ws = BotWorkspace(root=str(tmp_path))
        path = ws.save_json("config.json", {"key": "value", "nested": {"a": 1}})
        data = json.loads(Path(path).read_text())
        assert data["key"] == "value"
        assert data["nested"]["a"] == 1

    def test_list_files(self, tmp_path):
        ws = BotWorkspace(root=str(tmp_path))
        ws.save_text("a.txt", "a")
        ws.save_text("sub/b.txt", "b")
        files = ws.list_files()
        assert "a.txt" in files
        assert "sub/b.txt" in files

    def test_path_traversal_rejected(self, tmp_path):
        ws = BotWorkspace(root=str(tmp_path))
        with pytest.raises(ValueError, match="Path traversal"):
            ws.save_text("../../etc/passwd", "x")

    def test_prune_old_files(self, tmp_path):
        ws = BotWorkspace(root=str(tmp_path), ttl=1)
        path = ws.save_text("old.txt", "old")
        time.sleep(1.1)
        ws.save_text("new.txt", "new")  # triggers prune
        assert not Path(path).exists()
        assert (ws.root / "new.txt").exists()

    def test_clear_removes_all(self, tmp_path):
        ws = BotWorkspace(root=str(tmp_path))
        ws.save_text("a.txt", "a")
        ws.save_text("b.txt", "b")
        deleted = ws.clear()
        assert deleted == 2
        assert ws.list_files() == []

    def test_default_root_under_tmp(self):
        ws = BotWorkspace()
        tmp_resolved = Path(tempfile.gettempdir()).resolve()
        assert ws.root.resolve().is_relative_to(tmp_resolved)

    def test_safe_delete_only_tmp(self, tmp_path):
        from zulip.adapter import _safe_delete_temp_file
        f = tmp_path / "test.txt"
        f.write_text("x")
        _safe_delete_temp_file(str(f))
        assert not f.exists()

    def test_safe_delete_skips_non_tmp(self, tmp_path):
        from zulip.adapter import _safe_delete_temp_file
        # Create file outside tmp
        non_tmp = Path.home() / ".hermes_test_delete_me.txt"
        non_tmp.write_text("x")
        _safe_delete_temp_file(str(non_tmp))
        assert non_tmp.exists()
        non_tmp.unlink(missing_ok=True)
