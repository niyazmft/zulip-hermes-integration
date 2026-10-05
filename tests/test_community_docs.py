"""Guards for the community/documentation file set.

GitHub's community profile is only as good as the files that exist, and this repo
had README/SECURITY/CHANGELOG/AGENTS but no CONTRIBUTING, SUPPORT, code of
conduct, issue templates or CODEOWNERS — the sibling adapter had all of them.

These tests also assert that CONTRIBUTING.md describes *this* repository. It was
adapted from the TypeScript sibling, and the failure mode of that kind of
adaptation is a document full of `pnpm` and `openclaw` that reads plausibly and
is wrong; so the copy-paste failure is asserted against directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# The community-profile set. README.md, LICENSE and SECURITY.md already existed.
# NOTE the uppercase PULL_REQUEST_TEMPLATE.md: that is the committed name, and it
# only looks interchangeable on macOS, where the filesystem is case-insensitive.
EXPECTED_FILES = [
    "CONTRIBUTING.md",
    "SUPPORT.md",
    "CODE_OF_CONDUCT.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "AGENTS.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/CODEOWNERS",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/question.yml",
]

# Words that only make sense in the OpenClaw sibling. If one shows up in this
# repo's contributor docs, the adaptation was a copy-paste.
SIBLING_ONLY = ["pnpm", "npm run", "openclaw plugins", "typecheck", "node --test"]

VALID_BODY_TYPES = {"markdown", "textarea", "input", "dropdown", "checkboxes"}


def _tracked_files() -> set[str]:
    """Paths tracked in the git index, which is case-sensitive everywhere.

    `Path.is_file()` is not a sufficient check: on macOS it returns True for a
    path whose case differs from the committed one, so a wrong-case path passes
    locally and fails on Linux CI. That is exactly how this test first failed.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):  # not a git checkout
        return set()
    return {entry for entry in result.stdout.split("\0") if entry}


def _exists(rel: str) -> bool:
    tracked = _tracked_files()
    if tracked:
        return rel in tracked
    return (REPO_ROOT / rel).is_file()


def _read(rel: str) -> str:
    path = REPO_ROOT / rel
    assert path.is_file(), f"{rel} must exist"
    return path.read_text(encoding="utf-8")


def test_community_profile_files_exist_and_are_committed():
    """Present *and tracked*: a file in the working tree that was never added is
    invisible to GitHub and missing from every CI checkout."""
    missing = [rel for rel in EXPECTED_FILES if not _exists(rel)]
    assert not missing, f"missing (or untracked) community/documentation files: {missing}"


def test_contributing_describes_this_repo_not_the_sibling():
    text = _read("CONTRIBUTING.md")
    for word in SIBLING_ONLY:
        assert word not in text, (
            f"CONTRIBUTING.md mentions {word!r}, which belongs to the TypeScript "
            "sibling — this looks like an unadapted copy"
        )
    # ...and it must describe the things that are actually true here.
    for required in ("requirements.txt", "pytest", ".githooks/pre-push", "checksums.txt"):
        assert required in text, f"CONTRIBUTING.md must mention {required!r}"


def test_contributing_documents_the_three_shipping_rules():
    """Each of these has caused an incident; they belong in the contributor docs."""
    text = _read("CONTRIBUTING.md")
    assert "PLUGIN_FILES" in text, "the updater-manifest rule must be documented (#177)"
    assert "plugin.yaml" in text, "the env-var declaration rule must be documented (#147)"
    assert "checksums.txt" in text


def test_support_and_contributing_links_resolve():
    """A support doc that links nowhere is how the sibling's 'docs/audit/' aged."""
    for rel in ("SUPPORT.md", "CONTRIBUTING.md"):
        text = _read(rel)
        for target in ("README.md", "SECURITY.md", "CONTRIBUTING.md", "docs/RELEASING.md"):
            for line in text.splitlines():
                if f"({target})" in line and not (REPO_ROOT / target).exists():
                    raise AssertionError(f"{rel} links to missing {target}")


def test_issue_templates_are_well_formed():
    """A malformed issue form fails silently on GitHub, with no CI to notice."""
    for rel in EXPECTED_FILES:
        if "ISSUE_TEMPLATE" not in rel:
            continue
        data = yaml.safe_load(_read(rel))
        for key in ("name", "description", "body"):
            assert key in data, f"{rel} is missing {key!r}"
        assert isinstance(data["body"], list) and data["body"], f"{rel} has an empty body"
        for index, item in enumerate(data["body"]):
            kind = item.get("type")
            assert kind in VALID_BODY_TYPES, f"{rel} body[{index}] has type {kind!r}"
            if kind != "markdown":
                assert item.get("id"), f"{rel} body[{index}] ({kind}) needs an id"
            if kind in {"textarea", "input", "dropdown"}:
                label = item.get("attributes", {}).get("label")
                assert label, f"{rel} body[{index}] ({kind}) needs attributes.label"


def test_codeowners_names_a_real_owner():
    text = _read(".github/CODEOWNERS").splitlines()
    entries = [line for line in text if line.strip() and not line.startswith("#")]
    assert entries, "CODEOWNERS must contain at least one rule"
    for line in entries:
        parts = line.split()
        assert len(parts) == 2, f"unexpected CODEOWNERS line: {line!r}"
        assert parts[1].startswith("@"), f"owner must be a user/team: {line!r}"
