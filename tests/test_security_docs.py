"""Drift guards for SECURITY.md.

SECURITY.md makes claims about this codebase. These tests keep the
code-checkable ones honest, so the file cannot silently diverge from the
implementation it describes (the concern raised in issue #142).
"""

import re
from pathlib import Path

from zulip.version import __version__, __min_hermes__

REPO_ROOT = Path(__file__).resolve().parents[1]
SECURITY_MD = REPO_ROOT / "SECURITY.md"
ZULIP_DIR = REPO_ROOT / "zulip"


def _read_security_md() -> str:
    assert SECURITY_MD.is_file(), "SECURITY.md must exist at the repo root"
    return SECURITY_MD.read_text(encoding="utf-8")


def test_security_md_exists():
    assert SECURITY_MD.is_file()


def test_documented_versions_match_version_module():
    """The supported-versions table must quote the real constants."""
    text = _read_security_md()
    assert __version__ in text, "SECURITY.md must state the current plugin version"
    assert __min_hermes__ in text, "SECURITY.md must state __min_hermes__"


def test_exec_approval_version_gate_documented():
    """The 0.21.3 gate and plain-text fallback are a documented invariant."""
    text = _read_security_md()
    assert "0.21.3" in text
    assert "plain-text" in text


def test_documented_audited_events_are_actually_emitted():
    """Every event the doc lists as emitted must appear at a real call site."""
    text = _read_security_md()
    emitted = [
        "rate_limit_exceeded",
        "policy_block",
        "secret_leak_blocked",
        "media_upload_blocked",
        "activity_trace_recovered",
    ]
    source = "\n".join(p.read_text(encoding="utf-8") for p in ZULIP_DIR.glob("*.py"))
    for event in emitted:
        assert f'"{event}"' in source, f"{event} is not emitted anywhere in zulip/"
        assert event in text, f"{event} is not documented in SECURITY.md"


def test_documented_unemitted_helpers_have_no_call_site():
    """SECURITY.md names these as defined-but-never-emitted gaps.

    If one is wired up, this test fails on purpose: the doc must be corrected
    in the same change rather than left overstating coverage.
    """
    defined = {"log_monitor_start", "log_monitor_stop", "log_auth_failure"}
    callers: list[str] = []
    for path in ZULIP_DIR.glob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "def " in line:
                continue
            for name in defined:
                if re.search(rf"\b{name}\b", line):
                    callers.append(f"{path.name}:{lineno}: {line.strip()}")
    assert callers == [], (
        "SECURITY.md says these audit helpers are never emitted; a call site "
        f"now exists, so update the doc: {callers}"
    )


def test_redact_secrets_documented_as_not_wired():
    """SECURITY.md flags redact_secrets() as defined but not wired in.

    If the trace path starts using it, the non-guarantee must be revisited.
    """
    for path in ZULIP_DIR.glob("*.py"):
        if path.name == "secret_guard.py":
            continue
        assert "redact_secrets" not in path.read_text(encoding="utf-8"), (
            f"redact_secrets is now referenced by {path.name}; update SECURITY.md's "
            "activity-trace non-guarantee"
        )
