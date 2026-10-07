"""Static guard: optional gateway imports must degrade, not crash (issue #141).

#132: ``ExecApprovalPrompt`` was imported unconditionally, so the whole adapter
raised ``ImportError`` on a gateway that predates the symbol — the plugin
became *unloadable*, not merely button-less. ``tests/stubs/`` can never catch
that: the stub exports whatever we wrote there, so it always matches.

This test parses ``zulip/*.py`` and asserts that every import of a gateway
symbol we know can be absent — and every call of a version-gated plugin API —
sits inside a ``try:`` with a fallback handler. It needs no host and no
network, so it lives in the normal unit lane as the cheap regression guard.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "zulip"

# Gateway symbols a real host may not export. An unconditional import of any of
# these is the #132 class of bug: it takes the whole plugin down instead of
# disabling one feature. Some are imported (ExecApprovalPrompt,
# ProcessingOutcome, get_session_env); the rest are named so a future import of
# them is caught by the same rule.
_VERSION_GATED_GATEWAY_SYMBOLS = {
    "ExecApprovalPrompt",
    "ProcessingOutcome",
    "get_session_env",
    "_send_exec_approval_prompt",
    "supports_exec_approval_buttons",
    "register_hook",
    "register_tool",
    # Arrived with host 0.21.4 (gateway/run_turn_runner_approval_settle.py): the
    # gateway's own notice for an approval window that elapsed. Its absence is a
    # *capability* boundary (hosts below 0.21.4 refuse an unanswered approval
    # silently, so the plugin posts its own line under the fail-closed policy),
    # and an unguarded import would take the plugin down on those hosts.
    "register_timeout_notice",
}

# The optional imports that actually exist today. Asserted present so the guard
# cannot pass vacuously if the import is moved or deleted.
_MUST_BE_GUARDED_IMPORTS = {
    "ExecApprovalPrompt",
    "ProcessingOutcome",
    "get_session_env",
    "register_timeout_notice",
}

# Version-gated plugin-side APIs called on the registration context (#139/#160).
_CALL_GUARDED_GATEWAY_APIS = {"register_hook", "register_tool"}

# A handler that catches these makes an import/call failure non-fatal.
_IMPORT_GUARD_EXCEPTIONS = {"ImportError"}
_CALL_GUARD_EXCEPTIONS = {"ImportError", "Exception"}


class _GuardedIndex(ast.NodeVisitor):
    """Maps every node inside a ``try:`` body to the ``Try`` nodes guarding it."""

    def __init__(self) -> None:
        self.guarded: dict[int, list[ast.Try]] = {}

    def visit_Try(self, node: ast.Try) -> None:
        for child in node.body:
            for sub in ast.walk(child):
                self.guarded.setdefault(id(sub), []).append(node)
        self.generic_visit(node)


def _exception_names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        names: set[str] = set()
        for element in node.elts:
            names |= _exception_names(element)
        return names
    return set()


def _is_guarded(node: ast.AST, index: _GuardedIndex, accepted: set[str]) -> bool:
    for try_node in index.guarded.get(id(node), []):
        for handler in try_node.handlers:
            if _exception_names(handler.type) & accepted:
                return True
    return False


def _source_files() -> list[Path]:
    return sorted(PLUGIN_DIR.glob("*.py"))


def _scan_imports() -> dict[str, list[tuple[Path, ast.ImportFrom, bool]]]:
    found: dict[str, list[tuple[Path, ast.ImportFrom, bool]]] = {}
    for path in _source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        index = _GuardedIndex()
        index.visit(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith("gateway")
            ):
                continue
            for alias in node.names:
                if alias.name not in _VERSION_GATED_GATEWAY_SYMBOLS:
                    continue
                found.setdefault(alias.name, []).append(
                    (path, node, _is_guarded(node, index, _IMPORT_GUARD_EXCEPTIONS))
                )
    return found


def _scan_api_calls() -> dict[str, list[tuple[Path, ast.Call, bool]]]:
    found: dict[str, list[tuple[Path, ast.Call, bool]]] = {}
    for path in _source_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        index = _GuardedIndex()
        index.visit(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _CALL_GUARDED_GATEWAY_APIS
            ):
                continue
            found.setdefault(node.func.attr, []).append(
                (path, node, _is_guarded(node, index, _CALL_GUARD_EXCEPTIONS))
            )
    return found


def test_version_gated_gateway_imports_are_guarded():
    unguarded = [
        f"{name} imported unguarded in {path.relative_to(REPO_ROOT)}:{node.lineno}"
        for name, entries in _scan_imports().items()
        for path, node, guarded in entries
        if not guarded
    ]
    assert not unguarded, (
        "Optional/version-gated gateway imports must sit inside "
        "`try: ... except ImportError:` with a fallback, so an older host "
        "disables the feature instead of failing to load the plugin (#132).\n"
        + "\n".join(unguarded)
    )


def test_known_gated_imports_are_present():
    found = set(_scan_imports())
    missing = _MUST_BE_GUARDED_IMPORTS - found
    assert not missing, (
        "Expected these optional gateway imports to be present (and guarded): "
        + ", ".join(sorted(missing))
        + ". If one was moved or removed, update tests/test_guarded_imports.py "
        "so the #132 guard cannot pass vacuously."
    )


def test_version_gated_gateway_api_calls_are_guarded():
    found = _scan_api_calls()
    unguarded = [
        f"{name} called unguarded in {path.relative_to(REPO_ROOT)}:{node.lineno}"
        for name, entries in found.items()
        for path, node, guarded in entries
        if not guarded
    ]
    assert not unguarded, (
        "Version-gated plugin APIs must be called inside `try: ... except "
        "Exception:` so a host without them keeps loading.\n" + "\n".join(unguarded)
    )
    missing = _CALL_GUARDED_GATEWAY_APIS - set(found)
    assert not missing, (
        "Expected these version-gated plugin APIs to be called: "
        + ", ".join(sorted(missing))
        + ". Update tests/test_guarded_imports.py if their call sites moved."
    )
