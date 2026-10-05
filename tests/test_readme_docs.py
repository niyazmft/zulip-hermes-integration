"""Guards for the README's badges.

The Tests badge has been wrong in two shapes before this one. First a hand-written
shields.io counter — `tests-1168 passing` while the suite ran 1471. Then a count
published from CI onto a `badges` branch: truthful, but it needed a
write-permission job and a branch of its own to serve a number nobody acted on,
and it read stale the first time a run could not start at all (a GitHub Actions
incident left the publish job queued, so the badge sat on a seeded value while
`main` had moved on).

It is the workflow's own status badge now: always true, nothing to publish, no
branch to keep. These tests hold that, and keep a count from coming back in any
of the three shapes.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from zulip.version import __repo__

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _readme() -> str:
    assert README.is_file(), "README.md must exist at the repo root"
    return README.read_text(encoding="utf-8")


def _workflow() -> dict:
    assert WORKFLOW.is_file(), ".github/workflows/ci.yml must exist"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_tests_badge_is_the_workflow_status_badge():
    """Built from ``__repo__``, so a rename cannot leave a 404 behind."""
    text = _readme()
    expected = f"github.com/{__repo__}/actions/workflows/ci.yml/badge.svg?branch=main"
    assert expected in text, f"the Tests badge must be the ci.yml status badge, got: {expected}"
    # ...and it links to the runs, not to a bare image.
    assert f"](https://github.com/{__repo__}/actions/workflows/ci.yml)" in text


def test_no_test_count_is_written_down_anywhere():
    """Three shapes have been wrong here: a static counter, an inline comment,
    and a published JSON endpoint."""
    text = _readme()
    static = re.findall(r"img\.shields\.io/badge/tests-[\d,]+", text)
    assert not static, f"a hardcoded count badge came back: {static}"
    inline = re.findall(r"[\d,]{3,}\s+tests?\b", text)
    assert not inline, f"a test count was written into the README: {inline}"
    published = re.findall(r"img\.shields\.io/endpoint", text)
    assert not published, "the README must not depend on a published badge file"


def test_the_badge_branch_machinery_is_gone():
    """A `badges` branch plus a `contents: write` job cannot stay accurate when
    Actions is degraded, and it exists only to serve a number."""
    workflow = _workflow()
    assert "badges" not in workflow["jobs"], "the badge-publishing job came back"
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "tests.json" not in text, "the published-badge file came back"
    assert "outputs" not in workflow["jobs"]["zulip-bridge"] or set(
        workflow["jobs"]["zulip-bridge"]["outputs"]
    ) == {"code"}, (
        "the fast job should export only the docs-only flag (the test-count "
        "output belongs to the removed badge machinery)"
    )
