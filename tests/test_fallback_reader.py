"""Tests for zulip.fallback_reader — auto-send fallback for OSS models."""

import json
import os
import tempfile
from pathlib import Path

import pytest


class TestReadLatestAssistantTexts:
    """Tests for read_latest_assistant_texts()."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_sessions_dir(self):
        from zulip.fallback_reader import read_latest_assistant_texts

        result = await read_latest_assistant_texts(
            data_dir="/nonexistent/path",
            agent_id="main",
            session_key="agent:main:zulip:direct:user@test.com",
            start_time="2026-01-01T00:00:00",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_trajectory_files(self, tmp_path):
        from zulip.fallback_reader import read_latest_assistant_texts

        # Create sessions dir but no trajectory files
        sessions_dir = tmp_path / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)

        result = await read_latest_assistant_texts(
            data_dir=str(tmp_path),
            agent_id="main",
            session_key="agent:main:zulip:direct:user@test.com",
            start_time="2026-01-01T00:00:00",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_matching_artifacts(self, tmp_path):
        from zulip.fallback_reader import read_latest_assistant_texts

        sessions_dir = tmp_path / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create a trajectory file with no trace.artifacts
        traj_file = sessions_dir / "session_1.trajectory.jsonl"
        traj_file.write_text(
            json.dumps({"type": "user_message", "content": "hello"}) + "\n"
        )

        result = await read_latest_assistant_texts(
            data_dir=str(tmp_path),
            agent_id="main",
            session_key="agent:main:zulip:direct:user@test.com",
            start_time="2026-01-01T00:00:00",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_texts_when_matching_artifact_found(self, tmp_path):
        from zulip.fallback_reader import read_latest_assistant_texts

        sessions_dir = tmp_path / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)

        traj_file = sessions_dir / "session_1.trajectory.jsonl"
        traj_file.write_text(
            json.dumps({
                "type": "trace.artifacts",
                "sessionKey": "agent:main:zulip:direct:user@test.com",
                "ts": "2026-07-01T12:00:00",
                "data": {
                    "assistantTexts": ["Hello! How can I help you today?"],
                },
            }) + "\n"
        )

        result = await read_latest_assistant_texts(
            data_dir=str(tmp_path),
            agent_id="main",
            session_key="agent:main:zulip:direct:user@test.com",
            start_time="2026-01-01T00:00:00",
        )
        assert result == ["Hello! How can I help you today?"]

    @pytest.mark.asyncio
    async def test_filters_by_session_key_prefix(self, tmp_path):
        from zulip.fallback_reader import read_latest_assistant_texts

        sessions_dir = tmp_path / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)

        traj_file = sessions_dir / "session_1.trajectory.jsonl"
        traj_file.write_text(
            json.dumps({
                "type": "trace.artifacts",
                "sessionKey": "agent:main:zulip:direct:user@test.com:thread:topic1",
                "ts": "2026-07-01T12:00:00",
                "data": {
                    "assistantTexts": ["Reply in thread"],
                },
            }) + "\n"
        )

        # Match with base session key (should match prefix)
        result = await read_latest_assistant_texts(
            data_dir=str(tmp_path),
            agent_id="main",
            session_key="agent:main:zulip:direct:user@test.com",
            start_time="2026-01-01T00:00:00",
        )
        assert result == ["Reply in thread"]

    @pytest.mark.asyncio
    async def test_filters_by_start_time(self, tmp_path):
        from zulip.fallback_reader import read_latest_assistant_texts

        sessions_dir = tmp_path / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)

        traj_file = sessions_dir / "session_1.trajectory.jsonl"
        traj_file.write_text(
            json.dumps({
                "type": "trace.artifacts",
                "sessionKey": "agent:main:zulip:direct:user@test.com",
                "ts": "2026-01-01T00:00:00",  # Before start_time
                "data": {
                    "assistantTexts": ["Stale reply"],
                },
            }) + "\n"
        )

        result = await read_latest_assistant_texts(
            data_dir=str(tmp_path),
            agent_id="main",
            session_key="agent:main:zulip:direct:user@test.com",
            start_time="2026-07-01T00:00:00",  # After the event
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_texts_list(self, tmp_path):
        from zulip.fallback_reader import read_latest_assistant_texts

        sessions_dir = tmp_path / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)

        traj_file = sessions_dir / "session_1.trajectory.jsonl"
        traj_file.write_text(
            json.dumps({
                "type": "trace.artifacts",
                "sessionKey": "agent:main:zulip:direct:user@test.com",
                "ts": "2026-07-01T12:00:00",
                "data": {
                    "assistantTexts": [],
                },
            }) + "\n"
        )

        result = await read_latest_assistant_texts(
            data_dir=str(tmp_path),
            agent_id="main",
            session_key="agent:main:zulip:direct:user@test.com",
            start_time="2026-01-01T00:00:00",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_handles_corrupted_json_gracefully(self, tmp_path):
        from zulip.fallback_reader import read_latest_assistant_texts

        sessions_dir = tmp_path / "agents" / "main" / "sessions"
        sessions_dir.mkdir(parents=True)

        traj_file = sessions_dir / "session_1.trajectory.jsonl"
        traj_file.write_text("not valid json\n")

        result = await read_latest_assistant_texts(
            data_dir=str(tmp_path),
            agent_id="main",
            session_key="agent:main:zulip:direct:user@test.com",
            start_time="2026-01-01T00:00:00",
        )
        assert result is None
