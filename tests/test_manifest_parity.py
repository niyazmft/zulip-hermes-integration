"""Manifest ↔ code env parity (issue #141, folded from #147).

Every ``ZULIP_*`` / ``HERMES_*`` name the plugin reads from the environment
must be declared in ``zulip/plugin.yaml``. Hermes drives setup prompting,
``doctor`` checks, ``plugin_validate`` and upgrade-time
``_offer_new_optional_env_vars`` from those declarations, so a variable read
by the code but absent from the manifest is invisible to all of them.

A variable that is deliberately internal (a gateway-owned path, an
experimental switch) is listed in :data:`INTERNAL_ENV` below with a reason
rather than declared. The manifest is maintained by hand alongside the code,
so this test is the tie between them: drift now fails CI instead of shipping.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "zulip"
MANIFEST_PATH = PLUGIN_DIR / "plugin.yaml"

_ENV_NAME = re.compile(r"^(?:ZULIP|HERMES)_[A-Z0-9_]+$")

#: Callables that resolve a setting by name.  The parity check must follow
#: every path a name can be read through, or rewiring a read onto a new
#: accessor silently drops it from the check and the manifest drifts again --
#: the exact failure this module exists to prevent.
#:   * ``os.getenv`` / ``os.environ`` / a mapping's ``.get``  -- the original reads
#:   * ``runtime_scope.get_setting``   -- profile-scoped accessor (issue #156)
#:   * ``runtime_scope.effective_value`` / ``resolve_setting`` -- the preset gate
#:     (epic #211, child #213), which resolvers now read through
#:   * ``settings._preset_value``  -- that module's local wrapper over the gate
_SETTING_ACCESSORS = frozenset(
    {
        "getenv",
        "get",
        "get_setting",
        "effective_value",
        "resolve_setting",
        "_preset_value",
    }
)

# Read by the code but intentionally absent from the manifest: not an
# operator-facing setting. Each entry must actually be read (see
# test_internal_allowlist_entries_are_read), so the allowlist cannot rot.
INTERNAL_ENV = {
    # The gateway owns this directory and passes it down; it is not part of the
    # Zulip setup surface (e.g. ~/.hermes).
    "HERMES_DATA_DIR": "gateway-owned data dir",
    # Internal streaming escape hatch, deliberately not advertised in setup.
    "ZULIP_BLOCK_STREAMING": "internal experimental switch",
}

# The eight variables folded in from #147 that were read by the code but
# declared nowhere in the manifest.
_FOLDED_IN_147 = {
    "HERMES_MEDIA_ALLOW_DIRS",
    "ZULIP_ALLOW_ALL_USERS",
    "ZULIP_DM_POLICY",
    "ZULIP_DM_SESSION_TURN_LIMIT",
    "ZULIP_HOME_CHANNEL",
    "ZULIP_MAX_MESSAGE_LENGTH",
    "ZULIP_MAX_MESSAGES_PER_MINUTE",
    "ZULIP_TYPING_DELAY_SECONDS",
}


def _module_string_constants(tree: ast.AST) -> dict[str, str]:
    """Module-level ``NAME = "ZULIP_..."`` assignments.

    Lets ``env.get(INSECURE_HTTP_ENV)`` resolve to the name it reads.
    """
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if not (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and _ENV_NAME.match(value.value)
        ):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _resolve_name(arg: ast.AST, constants: dict[str, str]) -> str | None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return constants.get(arg.id)
    return None


def _env_names_from_call(node: ast.Call, constants: dict[str, str]) -> list[str]:
    """Names read by ``os.getenv(...)`` / ``<mapping>.get(...)`` calls.

    Also recognises ``get_setting("ZULIP_...")`` (issue #156's profile-scoped
    accessor) and the preset gate's ``effective_value`` / ``resolve_setting`` /
    ``_preset_value`` (epic #211), so rewiring a read through ``runtime_scope``
    -- which child #213 did to every behaviour resolver -- does not silently
    drop it from this parity check.
    """
    func = node.func
    is_accessor = (
        isinstance(func, ast.Name)
        and func.id in _SETTING_ACCESSORS
    ) or (
        isinstance(func, ast.Attribute)
        and func.attr in _SETTING_ACCESSORS
    )
    if not is_accessor or not node.args:
        return []
    name = _resolve_name(node.args[0], constants)
    return [name] if name and _ENV_NAME.match(name) else []


def _env_names_from_subscript(node: ast.Subscript, constants: dict[str, str]) -> list[str]:
    """Names read by ``os.environ["ZULIP_..."]``."""
    value = node.value
    if not (isinstance(value, ast.Attribute) and value.attr == "environ"):
        return []
    name = _resolve_name(node.slice, constants)
    return [name] if name and _ENV_NAME.match(name) else []


def _env_reads(path: Path) -> dict[str, int]:
    """First line each env var is read on, for one source file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    constants = _module_string_constants(tree)
    reads: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            names = _env_names_from_call(node, constants)
        elif isinstance(node, ast.Subscript):
            names = _env_names_from_subscript(node, constants)
        else:
            continue
        for name in names:
            reads.setdefault(name, node.lineno)
    return reads


def _code_env_reads() -> dict[str, tuple[str, int]]:
    """Every env name read by ``zulip/*.py`` → (repo-relative path, line)."""
    reads: dict[str, tuple[str, int]] = {}
    for path in sorted(PLUGIN_DIR.glob("*.py")):
        rel = str(path.relative_to(REPO_ROOT))
        for name, lineno in _env_reads(path).items():
            reads.setdefault(name, (rel, lineno))
    return reads


def _declared_env_names() -> set[str]:
    manifest = yaml.safe_load(MANIFEST_PATH.read_text())
    declared: set[str] = set()
    for section in ("requires_env", "optional_env"):
        for entry in manifest.get(section) or []:
            declared.add(entry["name"])
    return declared


def _format_undeclared(undeclared: dict[str, tuple[str, int]]) -> str:
    lines = [
        "Env var(s) read by zulip/*.py but not declared in zulip/plugin.yaml:",
    ]
    for name in sorted(undeclared):
        path, lineno = undeclared[name]
        lines.append(f"  {name} — read in {path}:{lineno}")
    lines.append(
        "Declare each in zulip/plugin.yaml (requires_env or optional_env) so "
        "Hermes setup/doctor/validate can see it, or add it to INTERNAL_ENV in "
        "tests/test_manifest_parity.py with a reason."
    )
    return "\n".join(lines)


def test_every_read_env_var_is_declared_or_allowlisted():
    declared = _declared_env_names()
    undeclared = {
        name: where
        for name, where in _code_env_reads().items()
        if name not in declared and name not in INTERNAL_ENV
    }
    assert not undeclared, _format_undeclared(undeclared)


def test_folded_in_147_env_vars_are_declared():
    """The eight vars from the #147 fold-in must be in zulip/plugin.yaml."""
    missing = _FOLDED_IN_147 - _declared_env_names()
    assert not missing, (
        "Declare these in zulip/plugin.yaml (optional_env): "
        + ", ".join(sorted(missing))
    )


def test_internal_allowlist_entries_are_read():
    """Every INTERNAL_ENV entry must still be read, or it is stale."""
    reads = _code_env_reads()
    stale = sorted(name for name in INTERNAL_ENV if name not in reads)
    assert not stale, (
        "INTERNAL_ENV lists env vars no longer read by zulip/*.py; remove "
        "them from tests/test_manifest_parity.py: " + ", ".join(stale)
    )


def test_reads_are_detected():
    """Guard against a parser regression silently seeing nothing."""
    reads = _code_env_reads()
    for name in (
        "ZULIP_API_KEY",
        "ZULIP_DM_POLICY",
        "ZULIP_MAX_MESSAGE_LENGTH",
        "HERMES_MEDIA_ALLOW_DIRS",
        "HERMES_DATA_DIR",
    ):
        assert name in reads, f"expected {name} to be detected as read"


def test_preset_gate_reads_are_detected():
    """A read that only exists through the preset gate is still a read (#213).

    These knobs were moved off ``get_setting`` in child #213. If the scanner
    stops following ``effective_value`` / ``_preset_value``, they disappear
    from :func:`test_every_read_env_var_is_declared_or_allowlisted` -- which
    passes vacuously -- instead of failing here.
    """
    reads = _code_env_reads()
    for name in (
        "ZULIP_DM_POLICY",
        "ZULIP_GROUP_POLICY",
        "ZULIP_HISTORY_MODE",
        "ZULIP_SOFT_GATE",
        "ZULIP_OBSERVE_GROUP",
        "ZULIP_SESSION_QUEUE",
        "ZULIP_TOPIC_SESSIONS",
        "ZULIP_REQUIRE_MENTION",
        "ZULIP_CHATMODE",
        "ZULIP_ACTIVITY_TRACE",
    ):
        assert name in reads, (
            f"{name} is read through the preset gate but the parity scanner no "
            "longer sees it; add the accessor to _SETTING_ACCESSORS"
        )
