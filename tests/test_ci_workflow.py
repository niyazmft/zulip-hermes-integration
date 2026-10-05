"""Invariants for `.github/workflows/ci.yml`.

Each of these encodes something that was either a real incident or a deliberate
trade-off, so a future edit that drops it has to argue with a failing test:

* the Node 20 deprecation — every action must stay on its Node 24 major;
* N queued runs for one PR — `concurrency` must cancel superseded runs;
* a wedged job holding a runner for the 360-minute default;
* docs-only PRs paying for up to three real host installs (the `code` filter),
  without the *required* check ever being skipped — if `zulip-bridge` were gated
  on the filter, a docs-only PR would produce no required check at all.

The filter's own behaviour is covered end-to-end by the matrix in the PR that
added it; what is asserted here is the wiring that makes it true.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# First major of each action that declares `runs.using: node24` (read from each
# tag's action.yml). Anything below these re-introduces the Node 20 deprecation
# warning on every run.
NODE24_MINIMUMS = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
    "actions/cache": 5,
    "gitleaks/gitleaks-action": 3,
}

REQUIRED_CHECK_JOB = "zulip-bridge"
EXPENSIVE_JOB = "compat"


def _workflow() -> dict:
    assert WORKFLOW.is_file(), ".github/workflows/ci.yml must exist"
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def _uses(workflow: dict) -> list[str]:
    return [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in _steps(job)
        if "uses" in step
    ]


def test_every_action_stays_on_a_node24_major():
    for ref in _uses(_workflow()):
        name, _, tag = ref.partition("@")
        if name not in NODE24_MINIMUMS:
            continue
        match = re.fullmatch(r"v(\d+)(?:\.\d+)*", tag)
        assert match, f"{ref} must be pinned to a major tag like @v5, got {tag!r}"
        major = int(match.group(1))
        assert major >= NODE24_MINIMUMS[name], (
            f"{ref} targets Node 20; {name} needs v{NODE24_MINIMUMS[name]} or later "
            "to run on Node 24"
        )


def test_superseded_runs_are_cancelled():
    concurrency = _workflow()["concurrency"]
    assert concurrency["cancel-in-progress"] is True
    assert "github.ref" in concurrency["group"]


def test_every_job_has_a_timeout():
    for name, job in _workflow()["jobs"].items():
        assert job.get("timeout-minutes"), (
            f"job {name!r} has no timeout-minutes; the default is 360 minutes, "
            "so a wedged job holds a runner for six hours"
        )


def test_the_expensive_job_is_skipped_for_docs_only_changes():
    workflow = _workflow()
    assert workflow["jobs"][EXPENSIVE_JOB]["if"] == (
        "needs.zulip-bridge.outputs.code == 'true'"
    )
    # ...and the flag it keys off must actually be produced.
    fast = workflow["jobs"][REQUIRED_CHECK_JOB]
    assert fast["outputs"]["code"] == "${{ steps.filter.outputs.code }}"
    filter_step = next(s for s in _steps(fast) if s.get("id") == "filter")
    assert '"$GITHUB_OUTPUT"' in filter_step["run"]
    assert "code=false" in filter_step["run"] and "code=true" in filter_step["run"]


def test_the_required_check_is_never_gated_on_the_filter():
    """A docs-only PR must still produce the `zulip-bridge` check.

    The doc claims are tested in that job (README/AGENTS/SECURITY contents), so
    it must run for docs changes too. Gating it on the filter would make a
    docs-only PR produce no required status at all.
    """
    fast = _workflow()["jobs"][REQUIRED_CHECK_JOB]
    assert "if" not in fast, (
        f"{REQUIRED_CHECK_JOB} must not be conditional: it is the required check"
    )


def test_the_filter_can_see_the_base_commit():
    """A shallow clone has no PR base, which silently forces code=true."""
    fast = _workflow()["jobs"][REQUIRED_CHECK_JOB]
    checkout = next(s for s in _steps(fast) if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0


def test_secret_scanning_and_dependency_audit_stay_in_the_fast_job():
    fast = _workflow()["jobs"][REQUIRED_CHECK_JOB]
    joined = yaml.safe_dump(fast)
    assert "gitleaks/gitleaks-action" in joined, "the secret scan must run in CI"
    assert "pip-audit" in joined, "the dependency audit must run in CI"
