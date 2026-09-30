"""Tests for delivery-outcome auditing (issue #145).

When a reply did not reach Zulip, nothing recorded *why* — the only signal was
console output, which does not surface on every host. These events turn an
undelivered reply into a lookup instead of a report, so three properties are
asserted here:

* the events land in the **real audit artifact**, not a mock;
* a skipped delivery always carries a machine-readable reason;
* neither a message body nor a credential finds its way into the log.

The file is read back from disk on purpose: the failure mode being guarded
against is a delivery path that *claims* to be audited while nothing was
written.
"""

import json
import logging

import pytest

from zulip.audit_logger import AuditLogger


def _read_events(tmp_path) -> list[dict]:
    """Every JSON line the audit logger wrote under ``tmp_path``."""
    logs = list(tmp_path.rglob("*.audit.log"))
    assert logs, "the audit logger wrote no file"
    return [json.loads(line) for line in logs[0].read_text().splitlines() if line]


def _events_named(tmp_path, name: str) -> list[dict]:
    return [e for e in _read_events(tmp_path) if e["event"] == name]


class TestDeliveryEventsReachTheRealFile:
    @pytest.mark.asyncio
    async def test_dispatch_turn_is_recorded(self, tmp_path):
        audit = AuditLogger(str(tmp_path), account_id="bot@example.com")

        await audit.log_dispatch_turn("dm:1032616", topic="api-review", message_id=99)

        events = _events_named(tmp_path, "dispatch_turn")
        assert len(events) == 1
        assert events[0]["account_id"] == "bot@example.com"
        assert events[0]["details"] == {
            "chat_id": "dm:1032616",
            "topic": "api-review",
            "message_id": 99,
        }

    @pytest.mark.asyncio
    async def test_deliver_payload_is_recorded(self, tmp_path):
        audit = AuditLogger(str(tmp_path))

        await audit.log_deliver_payload("573423", topic="deploy", message_id=1000)

        events = _events_named(tmp_path, "deliver_payload")
        assert len(events) == 1
        assert events[0]["details"]["chat_id"] == "573423"
        assert events[0]["details"]["topic"] == "deploy"
        assert events[0]["details"]["message_id"] == 1000

    @pytest.mark.asyncio
    async def test_deliver_empty_is_recorded(self, tmp_path):
        audit = AuditLogger(str(tmp_path))

        await audit.log_deliver_empty(chat_id="dm:1032616", topic="api-review")

        events = _events_named(tmp_path, "deliver_empty")
        assert len(events) == 1
        assert events[0]["details"]["chat_id"] == "dm:1032616"

    @pytest.mark.asyncio
    async def test_deliver_failed_is_recorded(self, tmp_path):
        audit = AuditLogger(str(tmp_path))

        await audit.log_deliver_failed(
            "send timed out", chat_id="573423", topic="deploy"
        )

        events = _events_named(tmp_path, "deliver_failed")
        assert len(events) == 1
        assert events[0]["details"]["error"] == "send timed out"

    @pytest.mark.asyncio
    async def test_an_idle_run_is_distinguishable_from_one_never_dispatched(
        self, tmp_path
    ):
        """Issue acceptance: "no reply" must not look like "never ran".

        A dispatched turn that produced nothing writes ``dispatch_turn`` plus
        ``deliver_empty``; a run that never started writes neither. Only the
        pair makes the two lookups different.
        """
        audit = AuditLogger(str(tmp_path))

        await audit.log_dispatch_turn("dm:1032616", topic="api-review")
        await audit.log_deliver_empty(chat_id="dm:1032616", topic="api-review")

        names = [e["event"] for e in _read_events(tmp_path)]
        assert names == ["dispatch_turn", "deliver_empty"]
        assert not _events_named(tmp_path, "deliver_skipped")
        assert not _events_named(tmp_path, "deliver_payload")


class TestSkippedCarriesAMachineReadableReason:
    @pytest.mark.asyncio
    async def test_the_reason_is_written(self, tmp_path):
        audit = AuditLogger(str(tmp_path))

        await audit.log_deliver_skipped(
            "no_trigger", chat_id="573423", topic="deploy"
        )

        events = _events_named(tmp_path, "deliver_skipped")
        assert len(events) == 1
        assert events[0]["details"]["reason"] == "no_trigger"
        assert events[0]["details"]["chat_id"] == "573423"
        assert events[0]["details"]["topic"] == "deploy"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reason", ["", "   ", None])
    async def test_a_skip_without_a_reason_is_refused(self, tmp_path, reason):
        """A reasonless skip is the silent drop this event exists to expose."""
        audit = AuditLogger(str(tmp_path))

        with pytest.raises(ValueError):
            await audit.log_deliver_skipped(reason, chat_id="573423")

        assert not list(tmp_path.rglob("*.audit.log"))


class TestNoBodyOrCredentialIsWritten:
    @pytest.mark.asyncio
    async def test_the_event_details_hold_identifiers_only(self, tmp_path):
        audit = AuditLogger(str(tmp_path))

        await audit.log_dispatch_turn("dm:1032616", topic="api-review", message_id=1)
        await audit.log_deliver_payload("dm:1032616", topic="api-review", message_id=2)
        await audit.log_deliver_skipped("policy_block", chat_id="dm:1032616")
        await audit.log_deliver_empty(chat_id="dm:1032616")
        await audit.log_deliver_failed("timeout", chat_id="dm:1032616")

        allowed = {"chat_id", "topic", "message_id", "reason", "error"}
        for event in _read_events(tmp_path):
            assert set(event["details"]) <= allowed

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "call",
        [
            lambda audit: audit.log_dispatch_turn("dm:1", content="secret body"),
            lambda audit: audit.log_deliver_payload("dm:1", content="secret body"),
            lambda audit: audit.log_deliver_empty("dm:1", content="secret body"),
            lambda audit: audit.log_deliver_failed(
                "boom", chat_id="dm:1", content="secret body"
            ),
            lambda audit: audit.log_deliver_skipped(
                "no_trigger", chat_id="dm:1", content="secret body"
            ),
        ],
    )
    async def test_a_message_body_cannot_be_passed_at_all(self, tmp_path, call):
        """There is no body parameter, so no body can reach the log."""
        audit = AuditLogger(str(tmp_path))

        with pytest.raises(TypeError):
            await call(audit)

        assert not list(tmp_path.rglob("*.audit.log"))

    @pytest.mark.asyncio
    async def test_the_artifact_holds_no_body_or_credential_text(self, tmp_path):
        """The event must not become the leak it is reporting."""
        secret = "sk-do-not-log-me-0123456789abcdef"
        body = "the answer that must never be audited"
        audit = AuditLogger(str(tmp_path), account_id="bot@example.com")

        await audit.log_dispatch_turn("dm:1032616", topic="api-review")
        await audit.log_deliver_skipped("no_trigger", chat_id="dm:1032616")
        await audit.log_deliver_empty(chat_id="dm:1032616")
        await audit.log_deliver_payload("dm:1032616", topic="api-review", message_id=7)
        await audit.log_deliver_failed("timeout", chat_id="dm:1032616")

        payload = "".join(
            path.read_text() for path in tmp_path.rglob("*.audit.log")
        )
        assert secret not in payload
        assert body not in payload


class TestAFailedWriteIsReportedNotSwallowed:
    @pytest.mark.asyncio
    async def test_a_real_write_failure_is_logged(self, tmp_path, caplog):
        """A blocked log directory must not silence the delivery event."""
        audit = AuditLogger(str(tmp_path))
        # Occupy the audit path with a file so the log dir cannot be created.
        (tmp_path / "audit").write_text("not a directory")

        with caplog.at_level(logging.WARNING, logger="zulip.audit_logger"):
            await audit.log_deliver_payload("573423", message_id=7)

        assert "audit log write failed" in caplog.text

    @pytest.mark.asyncio
    async def test_a_write_failure_never_breaks_the_delivery_path(
        self, tmp_path, monkeypatch, caplog
    ):
        audit = AuditLogger(str(tmp_path))

        async def boom(event_type, details=None):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(audit, "log_event", boom)

        with caplog.at_level(logging.WARNING, logger="zulip.audit_logger"):
            await audit.log_deliver_skipped("no_trigger", chat_id="573423")

        assert "delivery audit write failed" in caplog.text
        assert "deliver_skipped" in caplog.text
