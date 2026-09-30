#!/usr/bin/env python3
"""Parity matrix validator and drift reporter (issue #140).

Loads ``docs/parity-matrix.yaml``, validates it against the schema the parity
workflow depends on, and prints the sister-only / hermes-only delta.

Exit code is non-zero **only** for a schema error. Drift (a non-empty delta) is
reported but never fails the build: the matrix exists to make drift *visible*,
not to block a merge on a documentation file. The scheduled workflow in
``.github/workflows/parity.yml`` surfaces the delta on every run; a human decides
whether it is accepted or a parity item.

Usage::

    python3 scripts/parity_check.py                 # validate + print delta
    python3 scripts/parity_check.py --json          # machine-readable report
    python3 scripts/parity_check.py --matrix PATH   # validate another file
    python3 scripts/parity_check.py --self-test     # focused checker tests
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

# The three states a capability or config key may be in. Keyed off merge state
# on `main`, never releases — see docs/PARITY.md.
STATUS_BOTH = "both"
STATUS_SISTER_ONLY = "sister-only"
STATUS_HERMES_ONLY = "hermes-only"
STATUS_VALUES = frozenset({STATUS_BOTH, STATUS_SISTER_ONLY, STATUS_HERMES_ONLY})

REQUIRED_TOP_LEVEL = (
    "spec_version",
    "keyed_off",
    "statuses",
    "capabilities",
    "config",
)

DEFAULT_MATRIX_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "parity-matrix.yaml"
)

ENV_PREFIX = "ZULIP_"


def derive_sibling_key(env: str) -> str:
    """Derive a sibling camelCase key from a ``ZULIP_*`` env var name.

    The rule (docs/PARITY.md §5.2): strip the ``ZULIP_`` prefix, lowercase, split
    on ``_``, then join as lowerCamelCase. Pairs the rule cannot reproduce
    (``allowFrom``, the nested ``reactions.*`` group) are carried explicitly with
    ``derivable: false`` instead of being forced.
    """
    name = env[len(ENV_PREFIX):] if env.startswith(ENV_PREFIX) else env
    parts = [part for part in name.lower().split("_") if part]
    if not parts:
        return ""
    return parts[0] + "".join(part.capitalize() for part in parts[1:])


def load_matrix(path: Path) -> Any:
    """Parse the matrix YAML, raising ``ValueError`` on unreadable input."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read matrix {path}: {exc}") from exc
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc


def _is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_capability(
    cap: Any, index: int, seen_ids: set[str], errors: list[str]
) -> None:
    where = f"capabilities[{index}]"
    if not isinstance(cap, dict):
        errors.append(f"{where}: must be a mapping")
        return

    cap_id = cap.get("id")
    if not _is_non_empty_str(cap_id):
        errors.append(f"{where}: 'id' must be a non-empty string")
    elif cap_id in seen_ids:
        errors.append(f"{where}: duplicate capability id {cap_id!r}")
    else:
        seen_ids.add(cap_id)

    if not _is_non_empty_str(cap.get("title")):
        errors.append(f"{where}: 'title' must be a non-empty string")

    if cap.get("status") not in STATUS_VALUES:
        errors.append(
            f"{where}: 'status' must be one of "
            f"{sorted(STATUS_VALUES)} (got {cap.get('status')!r})"
        )

    refs = cap.get("refs", {})
    if not isinstance(refs, dict):
        errors.append(f"{where}: 'refs' must be a mapping when present")
    else:
        for key, value in refs.items():
            if not _is_non_empty_str(value):
                errors.append(f"{where}.refs.{key}: must be a non-empty string")


def _validate_config(
    entry: Any, index: int, seen_env: set[str], errors: list[str]
) -> None:
    where = f"config[{index}]"
    if not isinstance(entry, dict):
        errors.append(f"{where}: must be a mapping")
        return

    if entry.get("status") not in STATUS_VALUES:
        errors.append(
            f"{where}: 'status' must be one of "
            f"{sorted(STATUS_VALUES)} (got {entry.get('status')!r})"
        )

    if not isinstance(entry.get("derivable"), bool):
        errors.append(f"{where}: 'derivable' must be a boolean")

    env = entry.get("env")
    sibling_key = entry.get("sibling_key")

    if env is not None:
        if not _is_non_empty_str(env) or not env.startswith(ENV_PREFIX):
            errors.append(
                f"{where}: 'env' must be a {ENV_PREFIX}* string or null "
                f"(got {env!r})"
            )
        elif env in seen_env:
            errors.append(f"{where}: duplicate env key {env!r}")
        else:
            seen_env.add(env)

    if sibling_key is not None and not _is_non_empty_str(sibling_key):
        errors.append(f"{where}: 'sibling_key' must be a non-empty string or null")

    if env is None and sibling_key is None:
        errors.append(f"{where}: needs at least one of 'env' / 'sibling_key'")

    if entry.get("derivable") is True and env and sibling_key:
        expected = derive_sibling_key(env)
        if expected != sibling_key:
            errors.append(
                f"{where}: derivable is true but {env} -> {expected!r} "
                f"!= sibling_key {sibling_key!r}"
            )


def validate_matrix(data: Any) -> list[str]:
    """Return a list of schema errors; empty means the matrix is valid."""
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["matrix must be a top-level mapping"]

    for key in REQUIRED_TOP_LEVEL:
        if key not in data:
            errors.append(f"missing required top-level key: {key}")

    if not _is_non_empty_str(data.get("spec_version")):
        errors.append("spec_version must be a non-empty string")

    if not _is_non_empty_str(data.get("keyed_off")):
        errors.append("keyed_off must be a non-empty string")

    statuses = data.get("statuses")
    if not isinstance(statuses, list):
        errors.append("statuses must be a list")
    elif set(statuses) != STATUS_VALUES:
        errors.append(
            f"statuses must be exactly {sorted(STATUS_VALUES)} "
            f"(got {statuses!r})"
        )

    capabilities = data.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        errors.append("capabilities must be a non-empty list")
    else:
        seen_ids: set[str] = set()
        for index, cap in enumerate(capabilities):
            _validate_capability(cap, index, seen_ids, errors)

    config = data.get("config")
    if not isinstance(config, list) or not config:
        errors.append("config must be a non-empty list")
    else:
        seen_env: set[str] = set()
        for index, entry in enumerate(config):
            _validate_config(entry, index, seen_env, errors)

    return errors


def _label(entry: dict[str, Any]) -> str:
    return str(entry.get("id") or entry.get("env") or entry.get("sibling_key"))


def build_report(data: dict[str, Any]) -> dict[str, Any]:
    """Summarize the matrix and list the drift in each direction."""
    capabilities = data.get("capabilities", [])
    config = data.get("config", [])

    def pick(items: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
        return [
            {
                "id": _label(item),
                "title": item.get("title"),
                "env": item.get("env"),
                "sibling_key": item.get("sibling_key"),
                "refs": item.get("refs", {}),
            }
            for item in items
            if isinstance(item, dict) and item.get("status") == status
        ]

    def counts(items: list[dict[str, Any]]) -> dict[str, int]:
        return {
            status: sum(
                1
                for item in items
                if isinstance(item, dict) and item.get("status") == status
            )
            for status in (STATUS_BOTH, STATUS_SISTER_ONLY, STATUS_HERMES_ONLY)
        }

    return {
        "spec_version": data.get("spec_version"),
        "keyed_off": data.get("keyed_off"),
        "capabilities": counts(capabilities),
        "config": counts(config),
        "sister_only": {
            "capabilities": pick(capabilities, STATUS_SISTER_ONLY),
            "config": pick(config, STATUS_SISTER_ONLY),
        },
        "hermes_only": {
            "capabilities": pick(capabilities, STATUS_HERMES_ONLY),
            "config": pick(config, STATUS_HERMES_ONLY),
        },
    }


def _format_entry(entry: dict[str, Any]) -> str:
    key = entry.get("env") or entry.get("sibling_key") or entry.get("id")
    refs = entry.get("refs") or {}
    ref = refs.get("hermes") or refs.get("sister") or ""
    suffix = f"  ({ref})" if ref else ""
    return f"  - {key}{suffix}"


def _counts_line(label: str, counts: dict[str, int]) -> str:
    return (
        f"  {label}: {sum(counts.values())}  "
        f"(both {counts[STATUS_BOTH]}, "
        f"sister-only {counts[STATUS_SISTER_ONLY]}, "
        f"hermes-only {counts[STATUS_HERMES_ONLY]})"
    )


def format_report(report: dict[str, Any]) -> str:
    """Render the human-readable report."""
    lines: list[str] = []
    lines.append(
        f"Parity matrix v{report['spec_version']} "
        f"(keyed off {report['keyed_off']})"
    )
    lines.append(_counts_line("capabilities", report["capabilities"]))
    lines.append(_counts_line("config keys ", report["config"]))

    sister = [*report["sister_only"]["capabilities"], *report["sister_only"]["config"]]
    hermes = [*report["hermes_only"]["capabilities"], *report["hermes_only"]["config"]]

    lines.append("")
    lines.append(
        "Sister-only delta (merged in openclaw-zulip-bridge, missing here): "
        f"{len(sister)}"
    )
    lines.extend(_format_entry(entry) for entry in sister)

    lines.append("")
    lines.append(
        "Hermes-only delta (merged here, not verified in the sibling): "
        f"{len(hermes)}"
    )
    lines.extend(_format_entry(entry) for entry in hermes)

    return "\n".join(lines)


def _self_test() -> int:
    """Focused tests for the checker, without a separate test file.

    The parity workflow runs this before the real validation so a broken
    checker cannot silently pass the matrix.
    """
    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        if not condition:
            failures.append(name)

    check("derive apiKey", derive_sibling_key("ZULIP_API_KEY") == "apiKey")
    check("derive mediaMaxMb", derive_sibling_key("ZULIP_MEDIA_MAX_MB") == "mediaMaxMb")
    check(
        "derive allowInsecureHttp",
        derive_sibling_key("ZULIP_ALLOW_INSECURE_HTTP") == "allowInsecureHttp",
    )

    valid = {
        "spec_version": "0.0.1",
        "keyed_off": "merge-state-on-main",
        "statuses": [STATUS_BOTH, STATUS_SISTER_ONLY, STATUS_HERMES_ONLY],
        "capabilities": [
            {
                "id": "x",
                "title": "X",
                "status": STATUS_BOTH,
                "refs": {"hermes": "#1", "sister": "src/x.ts"},
            }
        ],
        "config": [
            {"env": "ZULIP_API_KEY", "sibling_key": "apiKey", "derivable": True, "status": STATUS_BOTH}
        ],
    }
    check("valid matrix passes", validate_matrix(valid) == [])

    broken = json.loads(json.dumps(valid))
    broken["capabilities"][0]["status"] = "maybe"
    check("bad status is rejected", bool(validate_matrix(broken)))

    broken = json.loads(json.dumps(valid))
    broken["config"][0]["derivable"] = True
    broken["config"][0]["sibling_key"] = "wrongKey"
    check("derivation mismatch is rejected", bool(validate_matrix(broken)))

    broken = json.loads(json.dumps(valid))
    broken["capabilities"][0]["id"] = "y"
    broken["capabilities"].append(dict(broken["capabilities"][0]))
    check("duplicate id is rejected", bool(validate_matrix(broken)))

    check("default matrix exists", DEFAULT_MATRIX_PATH.is_file())
    if DEFAULT_MATRIX_PATH.is_file():
        real = load_matrix(DEFAULT_MATRIX_PATH)
        errors = validate_matrix(real)
        check("real matrix validates", errors == [])
        if errors:
            failures.extend(errors)

    if failures:
        sys.stderr.write("self-test FAILED:\n")
        for failure in failures:
            sys.stderr.write(f"  - {failure}\n")
        return 1
    sys.stdout.write("self-test OK\n")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--matrix",
        type=Path,
        default=DEFAULT_MATRIX_PATH,
        help="path to parity-matrix.yaml (default: docs/parity-matrix.yaml)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the report as JSON"
    )
    parser.add_argument(
        "--self-test", action="store_true", help="run focused checker tests and exit"
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    try:
        data = load_matrix(args.matrix)
    except ValueError as exc:
        sys.stderr.write(f"[schema] {exc}\n")
        return 1

    errors = validate_matrix(data)
    if errors:
        sys.stderr.write(f"[schema] {args.matrix} is invalid:\n")
        for error in errors:
            sys.stderr.write(f"  - {error}\n")
        return 1

    report = build_report(data)
    if args.json:
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        sys.stdout.write(format_report(report) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
