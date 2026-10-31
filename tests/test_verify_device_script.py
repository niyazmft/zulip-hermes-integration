"""Guards for ``scripts/verify_device.sh``.

The script exists because hand-run device verification produced four confident
false passes in one afternoon:

* ``... | tail -n; echo $?`` reported *tail's* status, so a failed gate looked green
* the gate imported the **installed** plugin instead of the staged working tree
* a container-flattened install reported host ``0.0.0`` from ``importlib.metadata``
  while ``hermes --version`` said ``0.22.5``, so the floor check rejected a
  supported host
* a string replace in the script itself silently no-op'd on an indent mismatch

None of those were plugin defects. They were the verification lying. These tests
hold the properties that make the result trustworthy; they deliberately do not
assert its output, which needs a device.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_device.sh"


def _text() -> str:
    assert SCRIPT.is_file(), "scripts/verify_device.sh must exist"
    return SCRIPT.read_text(encoding="utf-8")


def test_it_is_executable_and_parses():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111, "a script nobody can run is a README"
    completed = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr


def test_help_names_both_transports_and_exits_zero():
    completed = subprocess.run(
        ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0
    for expected in ("docker:<container>", "ssh:<host>", "--matrix", "READ-ONLY"):
        assert expected in completed.stdout, f"--help does not mention {expected!r}"


def test_it_never_deploys():
    """Verification and deployment must stay separate acts.

    A script that syncs the working tree while claiming to verify it destroys the
    only fixture that is behind on purpose. The check is for *invocations*, not the
    words: the header mentions `rsync` to say it is deliberately not used, and a
    guard that trips on its own explanation is a guard nobody keeps.
    """
    text = _text()
    forbidden = re.findall(r"(?m)^\s*(?:rsync|cp)\s+\S", text)
    assert not forbidden, f"verify_device.sh must not deploy, found: {forbidden}"
    assert "docker cp" not in text, "verify_device.sh must not copy files into a device"


def test_it_stages_into_a_unique_directory():
    """A fixed scratch path left behind by a root-owned run wedges the next one.

    That happened during development: the extraction ran as root and the cleanup as
    the runtime user, so every later run failed with "Permission denied" and looked
    like a plugin problem.
    """
    text = _text()
    assert "WORK_BASE" in text, "the scratch directory must be derived from a base"
    assert 'run.$$' in text, "each run needs its own scratch directory"
    assert "trap" in text, "the scratch directory must be cleaned up on exit"


def test_the_gate_exit_status_is_captured_directly():
    """Never through a pipe: that is the first false pass in the list above."""
    text = _text()
    gate = text.split("check_gate()", 1)[1].split("\ncheck_", 1)[0]
    assert "check_compat.py' > " in gate or "check_compat.py' >" in gate, (
        "the gate must be redirected to a log file, not piped to a formatter"
    )
    assert "rc=$?" in gate, "the gate's exit status must be captured immediately"


def test_it_cross_checks_the_host_version():
    """A gate that grades the wrong host is worse than no gate."""
    text = _text()
    assert "check_crosscheck" in text
    assert "hermes --version" in text
    for placeholder in ("0.0.0", "unknown"):
        assert placeholder in text, (
            "the placeholder host versions must be named, or the cross-check "
            "cannot reject them"
        )
