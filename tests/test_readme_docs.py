"""Guards for the README's test-count badge.

The badge read ``tests-1168 passing`` while the suite ran 1471 — a stale number
in the first thing a visitor sees, and one that only degrades as tests are added.
It is published from the suite itself now (the ``badges`` job in
``.github/workflows/ci.yml`` writes ``tests.json`` on the ``badges`` branch), so
these tests keep it from regressing to a frozen number and keep the README and
the workflow agreeing on where that data lives.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import yaml

from zulip.version import __repo__

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BADGE_BRANCH = "badges"
BADGE_FILE = "tests.json"


def _readme() -> str:
    assert README.is_file(), "README.md must exist at the repo root"
    return README.read_text(encoding="utf-8")


def _workflow() -> dict:
    assert WORKFLOW.is_file(), ".github/workflows/ci.yml must exist"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_test_count_is_not_a_hardcoded_number():
    text = _readme()
    frozen = re.findall(r"img\.shields\.io/badge/tests-[0-9]+", text)
    assert not frozen, (
        f"the test count must not be hardcoded: {frozen} "
        "(it drifted to 1168 while the suite ran 1471)"
    )
    assert "img.shields.io/endpoint" in text, (
        "the test badge must read its number from the published badge data"
    )


def test_readme_badge_url_matches_the_canonical_repo_and_path():
    """A repo rename must not leave the badge pointing at a 404."""
    match = re.search(r"img\.shields\.io/endpoint\?url=([^)\s]+)", _readme())
    assert match, "no shields.io endpoint badge found in README"
    assert unquote(match.group(1)) == (
        f"https://raw.githubusercontent.com/{__repo__}/{BADGE_BRANCH}/{BADGE_FILE}"
    )


def test_badge_job_publishes_the_file_the_readme_reads():
    job = _workflow()["jobs"]["badges"]
    # Write access is opt-in: the repo default for GITHUB_TOKEN is read-only.
    assert job["permissions"]["contents"] == "write"
    # Main only — a fork PR's token cannot push, so running it there would just
    # turn contributors' checks red.
    assert "refs/heads/main" in job["if"]
    assert "pull_request" in job["if"]
    assert job["needs"] == "zulip-bridge"
    publish = "\n".join(step.get("run", "") for step in job["steps"])
    assert f"origin {BADGE_BRANCH}" in publish, "the job must push the badge branch"
    assert BADGE_FILE in publish, f"the job must write {BADGE_FILE}"


def test_the_published_count_comes_from_the_run_not_from_hand():
    """The fast job must extract the count from its own pytest output."""
    fast = _workflow()["jobs"]["zulip-bridge"]
    assert fast["outputs"]["tests_passed"] == "${{ steps.tests.outputs.count }}"
    step = next(s for s in fast["steps"] if s.get("id") == "tests")
    assert "set -o pipefail" in step["run"], (
        "without pipefail the step's status is tee's, not pytest's"
    )
    assert "pytest" in step["run"] and "passed" in step["run"]
