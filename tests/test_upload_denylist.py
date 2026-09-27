"""Tests for the upload denylist (Issue #138).

The allowlist is a *root* check, and the data dir is an allowed root on purpose
(the bot workspace lives under it). That makes it structurally unable to refuse
the files that matter most: the audit log, the queue/dedupe state and the
persisted allowlist all sit **inside** an allowed root. Refusing them therefore
has to be a name-based denylist evaluated *before* the roots are consulted —
otherwise a prompt-injected agent attaches the bot's own credentials as a Zulip
file.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zulip.media import (
    SensitiveUploadRefused,
    deny_reason,
    upload_file_to_zulip,
)


def _client():
    client = MagicMock()
    client.upload_file.return_value = {
        "result": "success",
        "uri": "/user_uploads/1/x.bin",
    }
    client.base_url = "https://zulip.example.com/api/"
    return client


DENIED_NAMES = [
    # credential-shaped basenames
    ".env",
    "config.env",
    ".env.local",
    "credentials.json",
    "credentials.yaml",
    "api_key.txt",
    "api-key.txt",
    "apikey.json",
    "my_secret_notes.md",
    "access_token.txt",
    "passwd",
    "passwords.txt",
    # the audit log and its rotated siblings
    "bot.audit.log",
    "bot.audit.log.20260101",
    # our own persisted state, by the names the stores really use
    "zulip_allowlist.json",
    "zulip_dedupe_bot@example.com.json",
    "zulip_queue_bot@example.com.json",
]

DENIED_DIRS = [
    "credentials/report.csv",
    "sessions/2026-01-01.md",
    "transcripts/topic.txt",
]

class TestDenyReason:
    @pytest.mark.parametrize("name", DENIED_NAMES)
    def test_denied_names(self, name):
        from pathlib import Path

        assert deny_reason(Path("/data") / name) is not None

    @pytest.mark.parametrize("rel", DENIED_DIRS)
    def test_denied_directories(self, rel):
        from pathlib import Path

        assert deny_reason(Path("/data") / rel) is not None

    @pytest.mark.parametrize(
        "rel", ["report.csv", "screenshot.png", "notes.md", "data.json"]
    )
    def test_ordinary_paths_are_allowed(self, rel):
        from pathlib import Path

        assert deny_reason(Path("/data") / rel) is None

    def test_returns_the_rule_that_matched(self):
        from pathlib import Path

        assert "credential" in deny_reason(Path("/data/credentials.json"))
        assert "directory" in deny_reason(Path("/data/credentials/report.csv"))
        assert "audit" in deny_reason(Path("/data/bot.audit.log"))

    def test_substring_rule_is_conservative_by_design(self):
        """Documented false positive: the cost is asymmetric.

        ``tokenizer_benchmark.md`` is harmless, but the credential substrings are
        matched unanchored on purpose — a refused upload plus an audit line is a
        far better outcome than publishing a token, and the audit line names the
        rule so the operator can see why.
        """
        from pathlib import Path

        reason = deny_reason(Path("/data/tokenizer_benchmark.md"))
        assert reason is not None and "token" in reason


class TestDeniedUploads:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", DENIED_NAMES)
    async def test_refused_by_name(self, tmp_path, name):
        target = tmp_path / name
        target.write_text("sensitive")

        client = _client()
        with pytest.raises(SensitiveUploadRefused):
            await upload_file_to_zulip(client, str(target), str(tmp_path))

        client.upload_file.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rel", DENIED_DIRS)
    async def test_refused_by_directory(self, tmp_path, rel):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("sensitive")

        client = _client()
        with pytest.raises(SensitiveUploadRefused):
            await upload_file_to_zulip(client, str(target), str(tmp_path))

        client.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_refusal_is_still_a_value_error(self, tmp_path):
        """Existing callers catch ValueError, so the new type must keep it."""
        target = tmp_path / ".env"
        target.write_text("SECRET=1")

        with pytest.raises(ValueError):
            await upload_file_to_zulip(_client(), str(target), str(tmp_path))

    @pytest.mark.asyncio
    async def test_denylist_wins_over_the_operator_allow_dir(self, tmp_path, monkeypatch):
        """An operator allow dir must not be able to authorize a credential file.

        This is why the denylist is evaluated *before* the roots: the operator
        path exists as defense-in-depth, and a broad allow dir (a whole home
        directory, say) would otherwise re-open the hole.
        """
        home = tmp_path / "home"
        home.mkdir()
        target = home / "credentials.json"
        target.write_text("{}")
        monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(home))

        client = _client()
        with pytest.raises(SensitiveUploadRefused):
            await upload_file_to_zulip(client, str(target), str(tmp_path))

        client.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_operator_allow_dir_still_works_for_normal_files(
        self, tmp_path, monkeypatch
    ):
        """The denial must not regress the operator path itself."""
        home = tmp_path / "home"
        home.mkdir()
        target = home / "report.csv"
        target.write_text("id,value\n1,42\n")
        monkeypatch.setenv("HERMES_MEDIA_ALLOW_DIRS", str(home))

        url = await upload_file_to_zulip(_client(), str(target), str(tmp_path / "data"))
        assert url.endswith("/user_uploads/1/x.bin")


class TestAllowedUploads:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "name", ["report.csv", "screenshot.png", "notes.md", "data.json", "haiku.txt"]
    )
    async def test_ordinary_files_still_upload(self, tmp_path, name):
        target = tmp_path / name
        target.write_text("ordinary")

        client = _client()
        url = await upload_file_to_zulip(client, str(target), str(tmp_path))

        assert url == "https://zulip.example.com/user_uploads/1/x.bin"
        client.upload_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_delimited_env_segment_is_required(self, tmp_path):
        """`environment_notes.md` is prose, not a dotenv file."""
        target = tmp_path / "environment_notes.md"
        target.write_text("notes")

        client = _client()
        await upload_file_to_zulip(client, str(target), str(tmp_path))
        client.upload_file.assert_called_once()


class TestRefusalIsAudited:
    @pytest.mark.asyncio
    async def test_audit_event_records_the_rule_and_not_the_content(self, tmp_path):
        target = tmp_path / "credentials.json"
        target.write_text('{"api_key": "sk-do-not-log-me"}')

        client = _client()
        with patch("zulip.audit_logger.AuditLogger") as logger_cls:
            logger_cls.return_value.log_event = AsyncMock()
            with pytest.raises(SensitiveUploadRefused):
                await upload_file_to_zulip(
                    client, str(target), str(tmp_path), account_id="bot@example.com"
                )

        kwargs = logger_cls.call_args.kwargs
        assert kwargs["account_id"] == "bot@example.com"

        event, details = logger_cls.return_value.log_event.await_args.args
        assert event == "media_upload_blocked"
        assert details["direction"] == "outbound"
        assert "credential" in details["reason"].lower()
        # The event must not become the leak it is reporting.
        assert "sk-do-not-log-me" not in json.dumps(details)

    @pytest.mark.asyncio
    async def test_audit_event_really_lands_on_disk(self, tmp_path):
        """End-to-end and unmocked — the acceptance criterion is "is it audited?"

        Asserted against the real file on purpose: the mocked version of this
        check passed while the audit was in fact raising (a missing import) and
        being swallowed by a bare ``except: pass``, so a mock would have kept
        the bug invisible.
        """
        target = tmp_path / "credentials.json"
        target.write_text('{"api_key": "sk-do-not-log-me"}')

        with pytest.raises(SensitiveUploadRefused):
            await upload_file_to_zulip(
                _client(), str(target), str(tmp_path), account_id="bot@example.com"
            )

        logs = list(tmp_path.rglob("*.audit.log"))
        assert logs, "the refusal must write an audit log"
        payload = logs[0].read_text()
        assert "media_upload_blocked" in payload
        assert "bot@example.com" in logs[0].name
        assert "sk-do-not-log-me" not in payload

    @pytest.mark.asyncio
    async def test_a_failing_audit_does_not_change_the_outcome(self, tmp_path):
        """Auditing is best-effort: the refusal must stand either way."""
        target = tmp_path / ".env"
        target.write_text("SECRET=1")

        with patch("zulip.audit_logger.AuditLogger", side_effect=OSError("no disk")):
            with pytest.raises(SensitiveUploadRefused):
                await upload_file_to_zulip(_client(), str(target), str(tmp_path))

    @pytest.mark.asyncio
    async def test_no_audit_event_for_an_ordinary_upload(self, tmp_path):
        target = tmp_path / "report.csv"
        target.write_text("id,value\n")

        with patch("zulip.audit_logger.AuditLogger") as logger_cls:
            await upload_file_to_zulip(_client(), str(target), str(tmp_path))
        logger_cls.assert_not_called()
