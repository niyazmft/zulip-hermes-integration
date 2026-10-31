"""Guards for the *documentation surface* — which file owns what, not what it says.

The configuration surface used to exist four times: read by ``zulip/*.py``,
declared in ``zulip/plugin.yaml``, described in ``README.md``, and re-described
in ``.env.example``. Three of those were hand-maintained and none was tied to
another, so they drifted: the README documented ``/model`` as passing straight
through to Hermes after the plugin started rendering it, and
``ZULIP_QUEUE_MAX_ACTIVE_SECONDS`` was read by the adapter while being declared
nowhere at all.

The fix was structural rather than editorial — one annotated reference
(``.env.example``, generated-from-manifest in spirit, hand-written in practice),
the README reduced to the decisions that matter plus a pointer, and
``AGENTS.md`` kept exhaustive because it is a runtime contract rather than an
introduction. These tests hold that shape. They deliberately check *membership
and linkage*, never prose: a guard that asserts sentences is a guard that gets
softened the first time someone rewords a paragraph.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from zulip import commands as plugin_commands
from zulip import engagement

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
AGENTS = REPO_ROOT / "AGENTS.md"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
MANIFEST = REPO_ROOT / "zulip" / "plugin.yaml"
DOCS_DIR = REPO_ROOT / "docs"

_ENV_NAME = re.compile(r"\b(?:ZULIP|HERMES)_[A-Z0-9_]+\b")

#: Knobs a person tunes once and forgets: timeouts, chunking, size caps, and the
#: pacing of the trace and history harvesters. They belong in ``.env.example``,
#: which lists every knob with its default, and must not reappear as a table row
#: in the README — that table is the copy that drifted.
PLUMBING_KNOBS = frozenset(
    {
        "ZULIP_CONNECT_TIMEOUT",
        "ZULIP_READ_TIMEOUT",
        "ZULIP_SEND_TIMEOUT",
        "ZULIP_TEXT_CHUNK_LIMIT",
        "ZULIP_CHUNK_MODE",
        "ZULIP_MAX_MESSAGE_LENGTH",
        "ZULIP_MEDIA_MAX_MB",
        "ZULIP_BLOCK_STREAMING",
        "ZULIP_TYPING_DELAY_SECONDS",
        "ZULIP_TRACE_COALESCE_MS",
        "ZULIP_TRACE_MAX_RATE",
        "ZULIP_TRACE_MAX_CONTENT",
        "ZULIP_TRACE_BOARD_TTL",
        "ZULIP_TRACE_TOOL_MATCHER",
        "ZULIP_HISTORY_MAX_MESSAGES",
        "ZULIP_HISTORY_WINDOW_HOURS",
        "ZULIP_HISTORY_MAX_CHARS",
        "ZULIP_QUEUE_MAX_ACTIVE_SECONDS",
    }
)

#: Commands the plugin answers but does not register in ``zulip/commands.py``,
#: mapped to the module that owns them. The owner is asserted, so this mapping
#: cannot rot into a claim about a command that no longer exists.
UNREGISTERED_PLUGIN_COMMANDS = {
    "/pick-model": "zulip/adapter.py",
}


def _read(path: Path) -> str:
    assert path.is_file(), f"{path.name} must exist"
    return path.read_text(encoding="utf-8")


def _declared_knobs() -> set[str]:
    manifest = yaml.safe_load(_read(MANIFEST))
    return {
        entry["name"]
        for section in ("requires_env", "optional_env")
        for entry in manifest.get(section) or []
    }


def test_every_declared_knob_is_documented_in_the_env_example():
    """The manifest declares a knob; ``.env.example`` must tell a person about it.

    This is the tie the four copies never had. ``plugin.yaml`` is the manifest
    Hermes drives setup, ``doctor`` and upgrade prompting from, so a knob that is
    declared but absent from the annotated reference is one an operator can only
    discover by reading the source.
    """
    documented = _read(ENV_EXAMPLE)
    missing = sorted(name for name in _declared_knobs() if name not in documented)
    assert not missing, (
        "declared in zulip/plugin.yaml but absent from .env.example: "
        + ", ".join(missing)
    )


def test_the_env_example_documents_nothing_undeclared():
    """The other direction: the reference must not promise a knob that does not exist."""
    declared = _declared_knobs()
    stray = sorted(set(_ENV_NAME.findall(_read(ENV_EXAMPLE))) - declared)
    assert not stray, (
        ".env.example names settings the manifest does not declare: " + ", ".join(stray)
    )


def test_the_readme_does_not_carry_a_second_reference_table():
    """A per-knob default/example table in the README is the copy that drifted."""
    rows = set(
        re.findall(r"^\| `((?:ZULIP|HERMES)_[A-Z0-9_]+)`", _read(README), re.MULTILINE)
    )
    reacquired = sorted(rows & PLUMBING_KNOBS)
    assert not reacquired, (
        "the README grew a reference table for knob(s) .env.example already "
        "documents: " + ", ".join(reacquired)
    )


def test_the_readme_points_at_the_two_authoritative_surfaces():
    """Subtraction is only safe while the pointer survives.

    Deleting the long table is correct *because* the README names where the real
    answer lives. If this fails, the configuration surface is unreachable from the
    front page and the deletion became a regression.
    """
    readme = _read(README)
    assert "(.env.example)" in readme, "the README must link the annotated reference"
    assert "config.sh --advanced" in readme, (
        "the README must name the command that prints the effective value and its source"
    )


def test_every_doc_is_indexed_from_the_readme():
    """A doc with no inbound link is a doc nobody reads.

    ``docs/STABLE-TOPIC-SESSIONS.md`` was in exactly that state: it existed, the
    README described its subject in a table row, and no file in the repo linked it.
    """
    readme = _read(README)
    unlinked = sorted(
        f"docs/{path.name}" for path in DOCS_DIR.glob("*.md") if path.name not in readme
    )
    assert not unlinked, "docs not linked from README.md: " + ", ".join(unlinked)


def test_every_plugin_command_is_named_in_the_agents_md():
    """``AGENTS.md`` is a runtime contract: a command it omits is one an agent may mishandle.

    The failure this prevents is concrete — a picker click arrives as a ``zform``
    token, and an agent told only that "anything else starting with ``/`` is a user
    question" would answer a button press as chat.
    """
    agents = _read(AGENTS)
    named = {f"/{name}" for name in plugin_commands.plugin_command_names()}
    named |= {stop for stop in engagement._STOP_COMMANDS if stop.startswith("/")}
    named |= set(UNREGISTERED_PLUGIN_COMMANDS)

    missing = sorted(command for command in named if command not in agents)
    assert not missing, "commands the plugin owns but AGENTS.md never names: " + ", ".join(
        missing
    )


def test_the_unregistered_command_list_still_has_a_owner():
    """Every entry above must still be handled where it claims to be."""
    for command, owner in UNREGISTERED_PLUGIN_COMMANDS.items():
        source = _read(REPO_ROOT / owner)
        assert f'"{command}"' in source, (
            f"{owner} no longer handles {command}; update "
            "UNREGISTERED_PLUGIN_COMMANDS in tests/test_doc_surface.py"
        )
