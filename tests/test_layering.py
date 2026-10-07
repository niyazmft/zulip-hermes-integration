"""Import-direction guard for the ``zulip`` package (blueprint B.7).

The refactor moved ~3,300 lines out of a single 5,356-line ``adapter.py`` into
twelve focused modules. Locality is only durable if the dependency *direction*
is enforced: without a guard, any module may quietly import any other and the
package silently re-integrates into the god-object it was extracted from.

Two invariants are asserted:

1. **``adapter`` is a sink.** Only ``zulip/__init__.py`` — the plugin
   entrypoint, which has to expose ``register`` — may import ``zulip.adapter``.
   A feature module importing the adapter is the first step of the cycle that
   collapses the layering back into one class.

2. **The declared layering holds.** Every module is assigned a depth; a module
   may only import strictly lower depths. ``LAYERS`` was derived from the
   post-refactor graph by longest-path depth, and the levels are therefore
   *observed*, not aspirational.

If this test fails, the fix is almost always to import the module that
*defines* what you need rather than reaching upward — the rule the refactor
used throughout (the owning module is the one that DEFINES a name, not the one
that CALLS it). If the layering genuinely must change, update ``LAYERS`` in the
same commit so the change is visible in review.
"""

from __future__ import annotations

import ast
import pathlib

PKG_DIR = pathlib.Path(__file__).resolve().parent.parent / "zulip"

# Level 0 = depends on nothing else in the package. Each level may import only
# strictly lower levels. Derived from the graph by longest-path depth.
LAYERS: dict[int, tuple[str, ...]] = {
    0: (
        "conversations", "dedupe_store", "display_names", "engagement",
        "fallback_reader", "logger", "queue_manager", "rate_limiter",
        "reaction_triggers", "recovery", "refs", "runtime_scope",
        "session_queue", "text_utils", "updater", "version", "workspace",
        "zulip_client",
    ),
    1: (
        "activity_trace", "admin_actions", "approvals", "audit_logger",
        "commands", "history", "policy", "probe", "reactions", "secret_guard",
        "settings",
    ),
    2: ("approval_outcomes", "connection", "inbound", "media", "pairing", "platform_api"),
    3: ("cli", "outbound"),
    4: ("inbound_queue", "routing"),
    5: ("tracing",),
    6: ("adapter",),
    7: ("__init__",),
}


def _package_modules() -> list[str]:
    """Every module in the shipped package, by stem."""
    return sorted(path.stem for path in PKG_DIR.glob("*.py"))


def _declared_levels() -> dict[str, int]:
    """Flatten ``LAYERS`` into ``module -> depth``, guarding against typos."""
    levels: dict[str, int] = {}
    for level, modules in LAYERS.items():
        for module in modules:
            assert module not in levels, f"{module} is declared at two levels"
            levels[module] = level
    return levels


def _intra_package_deps(module: str) -> set[str]:
    """Package modules imported by ``module`` (both ``from .x`` and ``from . import x``)."""
    source = (PKG_DIR / f"{module}.py").read_text(encoding="utf-8")
    available = set(_package_modules())
    deps: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            names = (
                [node.module.split(".")[0]]
                if node.module
                else [alias.name.split(".")[0] for alias in node.names]
            )
            deps.update(name for name in names if name in available)
        elif isinstance(node, ast.Import):
            deps.update(
                alias.name.split(".")[1]
                for alias in node.names
                if alias.name.startswith("zulip.")
                and alias.name.split(".")[1] in available
            )
    deps.discard(module)
    return deps


def test_every_package_module_has_a_declared_layer() -> None:
    """A new module must be classified, not silently exempt."""
    missing = sorted(set(_package_modules()) - set(_declared_levels()))
    assert not missing, (
        f"module(s) missing from LAYERS: {missing}. Assign each a depth in "
        f"{__file__} consistent with what it imports."
    )


def test_no_module_imports_upward_or_sideways() -> None:
    """Every intra-package import must point strictly downward."""
    levels = _declared_levels()
    violations = [
        f"{module}(L{level}) -> {dep}(L{levels[dep]})"
        for module, level in sorted(levels.items())
        for dep in sorted(_intra_package_deps(module))
        if dep in levels and levels[dep] >= level
    ]
    assert not violations, (
        "upward/same-level imports break the layering: "
        + "; ".join(violations)
        + ". Import the module that DEFINES what you need instead."
    )


def test_adapter_is_a_sink_reachable_only_from_init() -> None:
    """Only the plugin entrypoint may import the composition root."""
    importers = sorted(
        module for module in _package_modules() if "adapter" in _intra_package_deps(module)
    )
    assert importers == ["__init__"], (
        f"expected only __init__ to import zulip.adapter, got {importers}. "
        "Importing the adapter from a feature module re-creates the cycle the "
        "refactor removed."
    )


def test_init_exposes_the_host_entrypoint() -> None:
    """The host loads ``register`` from the package root; that must not move."""
    assert "adapter" in _intra_package_deps("__init__")
    init_source = (PKG_DIR / "__init__.py").read_text(encoding="utf-8")
    assert "register" in init_source
