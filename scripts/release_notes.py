#!/usr/bin/env python3
"""Print the curated release notes for a version, ready to prepend to GitHub's
automatically generated notes.

Usage:
    python3 scripts/release_notes.py 1.9.3 > /tmp/notes.md
    gh release create v1.9.3 --verify-tag \\
        --title "v1.9.3 — <one-line summary>" \\
        --notes-file /tmp/notes.md --generate-notes

`gh` prepends the file contents to the notes returned by GitHub's
"generate release notes" API, so the published release reads:

    ## Added / ## Fixed / ## Docs / ## Internal   <- CHANGELOG.md, curated
    ## What's Changed                             <- every merged PR author
    ## New Contributors                           <- first-timers only
    **Full Changelog**: vPREV...vX.Y.Z

Because the generated layer already credits every PR author in the range
(maintainer included), this script drops the hand-maintained "### Contributors"
subsection to avoid listing the same people twice. Pass --with-contributors to
keep it.

See docs/RELEASING.md for the full release procedure.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

VERSION_RE = re.compile(r"^## \[([^\]]+)\]", re.M)


def find_section(text: str, version: str) -> str | None:
    """Return the body of the `## [<version>]` entry, or None if absent."""
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)",
        text,
        re.S | re.M,
    )
    return match.group(1) if match else None


def drop_contributors(body: str) -> str:
    """Remove the `### Contributors` subsection (heading through end of entry)."""
    return re.sub(r"^### Contributors\b.*?(?=^### |\Z)", "", body, flags=re.S | re.M)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="version as it appears in CHANGELOG.md, e.g. 1.9.3")
    parser.add_argument(
        "--with-contributors",
        action="store_true",
        help="keep the ### Contributors subsection instead of dropping it",
    )
    args = parser.parse_args()

    version = args.version.lstrip("v")
    text = CHANGELOG.read_text(encoding="utf-8")

    body = find_section(text, version)
    if body is None:
        available = [v for v in VERSION_RE.findall(text) if v.lower() != "unreleased"]
        print(f"error: no '## [{version}]' section in {CHANGELOG.name}", file=sys.stderr)
        print("available: " + ", ".join(dict.fromkeys(available)), file=sys.stderr)
        return 1

    if not args.with_contributors:
        body = drop_contributors(body)

    # Promote `### Added` -> `## Added`: the release body is a top-level
    # document where GitHub's own sections ("What's Changed") are also H2.
    body = re.sub(r"^### ", "## ", body, flags=re.M)

    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body:
        print(f"error: '## [{version}]' section is empty", file=sys.stderr)
        return 1

    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
