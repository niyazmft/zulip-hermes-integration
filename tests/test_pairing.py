"""Tests for the DM pairing approval path (issue #198).

``ZULIP_DM_POLICY=pairing`` used to issue a code that nothing could consume:
``PolicyEngine.approve_email`` existed but had no caller, and the codes lived
only in the gateway's memory, so nothing outside that process could see them.

Two things are therefore load-bearing here:

* pending requests survive a process boundary (persisted to the data file), so
  ``python3 -m zulip.pairing`` can act on them, and
* a running gateway notices an out-of-process change, so an approval does not
  need a restart.
"""

import json
import time
from pathlib import Path

import pytest

from zulip.policy import PolicyEngine
from zulip.pairing import main as pairing_main


@pytest.fixture
def data_dir(tmp_path: Path) -> str:
    return str(tmp_path)


@pytest.fixture
def pairing_engine(data_dir, monkeypatch):
    """A PolicyEngine in pairing mode, as the gateway would build it."""
    monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
    monkeypatch.delenv("ZULIP_ALLOWED_USERS", raising=False)
    return PolicyEngine(data_dir=data_dir)


def _issue(engine: PolicyEngine, email: str = "newbie@example.com") -> str:
    allowed, code = engine.check_dm(email)
    assert allowed is False
    assert code
    return code


class TestPendingRequestsPersist:
    def test_code_is_written_to_the_data_file(self, pairing_engine, data_dir):
        code = _issue(pairing_engine)
        raw = json.loads(Path(data_dir, "zulip_allowlist.json").read_text())
        assert [p["code"] for p in raw["pairing"]] == [code]
        assert raw["pairing"][0]["email"] == "newbie@example.com"

    def test_a_separate_process_can_see_the_request(self, pairing_engine, data_dir):
        """The CLI is a different process; only the file connects the two."""
        code = _issue(pairing_engine)
        other = PolicyEngine(data_dir=data_dir)
        assert [(c, e) for c, e, _ in other.list_pending()] == [
            (code, "newbie@example.com")
        ]

    def test_request_survives_a_gateway_restart(self, pairing_engine, data_dir):
        code = _issue(pairing_engine)
        restarted = PolicyEngine(data_dir=data_dir)
        assert restarted.approve_code(code) == "newbie@example.com"

    def test_list_pending_is_oldest_first(self, pairing_engine):
        first = _issue(pairing_engine, "a@example.com")
        second = _issue(pairing_engine, "b@example.com")
        assert [c for c, _, _ in pairing_engine.list_pending()] == [first, second]

    def test_malformed_entry_does_not_poison_startup(self, data_dir):
        path = Path(data_dir, "zulip_allowlist.json")
        path.write_text(
            json.dumps(
                {
                    "allowlist": ["ok@example.com"],
                    "pairing": [
                        {
                            "code": "GOOD1",
                            "email": "x@example.com",
                            "created_at": time.time(),
                        },
                        {"email": "missing-code@example.com"},
                        "not-a-dict",
                    ],
                }
            )
        )
        engine = PolicyEngine(data_dir=data_dir)
        assert [c for c, _, _ in engine.list_pending()] == ["GOOD1"]
        assert engine.can_dm("ok@example.com") is True


class TestApproval:
    def test_approve_by_code(self, pairing_engine):
        code = _issue(pairing_engine)
        assert pairing_engine.approve_code(code) == "newbie@example.com"
        assert pairing_engine.check_dm("newbie@example.com")[0] is True

    @pytest.mark.parametrize(
        "rendered",
        ["PAIR-{c}", "{c}", "pair-{c}", "pair{c}", "  PAIR-{c}  "],
    )
    def test_approve_accepts_the_forms_a_human_would_paste(
        self, pairing_engine, rendered
    ):
        code = _issue(pairing_engine)
        assert pairing_engine.approve_code(rendered.format(c=code)) == "newbie@example.com"

    def test_a_consumed_code_cannot_be_replayed(self, pairing_engine):
        code = _issue(pairing_engine)
        pairing_engine.approve_code(code)
        assert pairing_engine.approve_code(code) is None

    def test_replay_is_blocked_across_a_restart(self, pairing_engine, data_dir):
        code = _issue(pairing_engine)
        pairing_engine.approve_code(code)
        restarted = PolicyEngine(data_dir=data_dir)
        assert restarted.approve_code(code) is None

    def test_unknown_code_is_rejected(self, pairing_engine):
        assert pairing_engine.approve_code("ZZZZZZ") is None

    def test_expired_code_is_rejected(self, data_dir, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        # TTL 0: a freshly issued code is already stale, so it is pruned and
        # never becomes approvable.
        engine = PolicyEngine(data_dir=data_dir, pairing_ttl=0)
        code = _issue(engine)
        assert engine.list_pending() == []
        assert engine.approve_code(code) is None

    def test_expired_entry_on_disk_is_ignored(self, data_dir):
        """A code that aged out while the gateway was down stays dead."""
        path = Path(data_dir, "zulip_allowlist.json")
        path.write_text(
            json.dumps(
                {
                    "allowlist": [],
                    "pairing": [
                        {
                            "code": "STALE1",
                            "email": "old@example.com",
                            "created_at": time.time() - 90_000,  # > 24h
                        }
                    ],
                }
            )
        )
        engine = PolicyEngine(data_dir=data_dir)
        assert engine.list_pending() == []
        assert engine.approve_code("STALE1") is None

    def test_approve_by_email_still_works(self, pairing_engine):
        _issue(pairing_engine)
        assert pairing_engine.approve_email("newbie@example.com") is True
        assert pairing_engine.check_dm("newbie@example.com")[0] is True

    def test_approved_email_is_not_left_pending(self, pairing_engine):
        code = _issue(pairing_engine)
        pairing_engine.approve_code(code)
        assert pairing_engine.list_pending() == []


class TestRevocation:
    def test_revoke_removes_access(self, pairing_engine):
        code = _issue(pairing_engine)
        pairing_engine.approve_code(code)
        assert pairing_engine.revoke_email("newbie@example.com") is True
        assert pairing_engine.check_dm("newbie@example.com")[0] is False

    def test_revoke_unknown_email_reports_false(self, pairing_engine):
        assert pairing_engine.revoke_email("nobody@example.com") is False


class TestRunningGatewayNoticesExternalChanges:
    """The gateway is a long-lived process; the CLI is not."""

    def test_approval_in_another_process_is_picked_up(
        self, pairing_engine, data_dir
    ):
        code = _issue(pairing_engine)
        assert pairing_engine.check_dm("newbie@example.com")[0] is False

        cli_engine = PolicyEngine(data_dir=data_dir)
        cli_engine.approve_code(code)

        pairing_engine.refresh_if_changed()
        assert pairing_engine.check_dm("newbie@example.com")[0] is True

    def test_revocation_in_another_process_is_picked_up(
        self, pairing_engine, data_dir
    ):
        cli_engine = PolicyEngine(data_dir=data_dir)
        cli_engine.approve_email("newbie@example.com")
        pairing_engine.refresh_if_changed()
        assert pairing_engine.check_dm("newbie@example.com")[0] is True

        cli_engine.revoke_email("newbie@example.com")
        pairing_engine.refresh_if_changed()
        assert pairing_engine.check_dm("newbie@example.com")[0] is False

    def test_refresh_without_changes_is_harmless(self, pairing_engine):
        before = set(pairing_engine.allowlist)
        pairing_engine.refresh_if_changed()
        pairing_engine.refresh_if_changed()
        assert pairing_engine.allowlist == before

    def test_no_data_dir_is_a_noop(self, monkeypatch):
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        engine = PolicyEngine(data_dir=None)
        engine.refresh_if_changed()  # must not raise


class TestCli:
    def test_list_reports_pending(self, pairing_engine, data_dir, capsys, monkeypatch):
        code = _issue(pairing_engine)
        monkeypatch.setenv("ZULIP_DM_POLICY", "pairing")
        assert pairing_main(["--data-dir", data_dir, "list"]) == 0
        out = capsys.readouterr().out
        assert code in out and "newbie@example.com" in out

    def test_list_is_quiet_when_nothing_is_pending(self, data_dir, capsys):
        assert pairing_main(["--data-dir", data_dir, "list"]) == 0
        assert "No pending pairing requests." in capsys.readouterr().out

    def test_approve_by_code(self, pairing_engine, data_dir, capsys):
        code = _issue(pairing_engine)
        assert pairing_main(["--data-dir", data_dir, "approve", f"PAIR-{code}"]) == 0
        assert "Approved newbie@example.com." in capsys.readouterr().out

    def test_approve_by_email(self, data_dir, capsys):
        assert pairing_main(["--data-dir", data_dir, "approve", "someone@example.com"]) == 0
        assert "Approved someone@example.com." in capsys.readouterr().out

    def test_approve_unknown_code_fails_loudly(self, data_dir, capsys):
        assert pairing_main(["--data-dir", data_dir, "approve", "ZZZZZZ"]) == 1
        err = capsys.readouterr().err
        assert "expired" in err

    def test_revoke(self, pairing_engine, data_dir, capsys):
        code = _issue(pairing_engine)
        pairing_main(["--data-dir", data_dir, "approve", code])
        assert pairing_main(["--data-dir", data_dir, "revoke", "newbie@example.com"]) == 0
        assert "Revoked newbie@example.com." in capsys.readouterr().out

    def test_revoke_unknown_email_exits_nonzero(self, data_dir, capsys):
        assert pairing_main(["--data-dir", data_dir, "revoke", "nobody@example.com"]) == 1
        assert "not on the allowlist" in capsys.readouterr().err

    def test_cli_end_to_end_unblocks_the_user(self, pairing_engine, data_dir):
        code = _issue(pairing_engine)
        assert pairing_main(["--data-dir", data_dir, "approve", code]) == 0
        pairing_engine.refresh_if_changed()
        assert pairing_engine.check_dm("newbie@example.com")[0] is True
