"""Drift guards for the declared Hermes floor (#230).

``zulip/version.py::__min_hermes__`` is a claim about the *oldest* host the
plugin supports, and it is copied into user-facing docs. These tests keep the
copies honest, and — more importantly — keep the gate's *classification* of the
pre-0.21.3 typing helpers as capabilities rather than requirements. Treating
them as requirements is what made a true floor look false: 0.18.2 was rejected
by the harness while the docs claimed it was supported.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

from zulip.version import __min_hermes__

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_gate():
    """Import ``scripts/check_compat.py`` without adding scripts/ to sys.path.

    The gate is deliberately self-contained (stdlib only at module scope), so it
    is safe to load inside the unit suite.
    """
    spec = importlib.util.spec_from_file_location(
        "check_compat_under_test", REPO_ROOT / "scripts" / "check_compat.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(name: str) -> str:
    path = REPO_ROOT / name
    assert path.is_file(), f"{name} must exist at the repo root"
    return path.read_text(encoding="utf-8")


def test_agents_md_gateway_matrix_states_the_floor():
    """The row that names ``__min_hermes__`` must quote the real floor value.

    Asserting only that both strings appear in the file is not enough: the value
    also appears in the surrounding prose, so a stale table would still pass.
    """
    text = _read("AGENTS.md")
    marker = "## 🔌 Gateway Compatibility"
    assert marker in text, "AGENTS.md must document gateway compatibility"
    section = text.split(marker, 1)[1].split("\n## ", 1)[0]

    floor_rows = [line for line in section.splitlines() if "`__min_hermes__`" in line]
    assert floor_rows, "the matrix must carry a row naming __min_hermes__"
    assert any(__min_hermes__ in row for row in floor_rows), (
        "the row that names __min_hermes__ must quote the floor value itself; "
        f"expected {__min_hermes__} in that row"
    )


def test_readme_states_the_floor_in_prose_and_in_the_badge():
    text = _read("README.md")
    assert f"`>= {__min_hermes__}`" in text, "README prerequisites must state the floor"
    assert f"%3E%3D{__min_hermes__}" in text, "the Hermes badge must state the floor"


def test_typing_helpers_are_capabilities_not_requirements():
    """#230: their absence degrades typing, so it must never fail the gate."""
    gate = _load_gate()
    assert set(gate.GATED_BASE_METHODS) == {
        "_stop_typing_with_metadata",
        "_accepts_kwarg",
    }
    assert not hasattr(gate, "REQUIRED_BASE_METHODS"), (
        "the metadata-aware stop-typing helpers are capabilities, not "
        "requirements — re-adding them to the fatal path makes the declared "
        f"{__min_hermes__} floor look false again (#230)"
    )
    # The fatal path must not consult them either, under any helper name.
    source = inspect.getsource(gate.main)
    assert "typing_helpers" not in source


def test_floor_comparison_is_numeric_not_lexical():
    """``0.18.10`` is *above* ``0.18.2``; a string compare would say otherwise."""
    gate = _load_gate()
    floor = gate._parse_version(__min_hermes__)
    assert floor is not None
    assert gate._parse_version("0.18.10") > floor
    assert gate._parse_version("0.9.0") < floor
    # An unparseable host version is not evidence of an unsupported host.
    assert gate._parse_version("unknown") is None


def test_below_floor_hosts_are_rejected():
    """The floor is enforced, not just documented."""
    gate = _load_gate()
    below = "0.18.0" if __min_hermes__ != "0.18.0" else "0.17.9"
    gate._host_version = lambda: below
    failures = gate._check_host_meets_floor()
    assert failures, f"host {below} must be rejected as below {__min_hermes__}"
    assert "below the declared floor" in failures[0]

    gate._host_version = lambda: __min_hermes__
    assert gate._check_host_meets_floor() == []

    gate._host_version = lambda: "unknown"
    assert gate._check_host_meets_floor() == [], (
        "an unknown host version must not be treated as below the floor"
    )
