"""Guards for the release-notes builder.

The release page is the most-read document this repo publishes and the only one
no test used to look at. It had grown into a wall of prose: a reader had to get
through several paragraphs before learning what changed. These tests pin the
shape that fixed that — a short, scannable top and the exhaustive record behind
expandable sections — and pin the CHANGELOG authoring convention the highlights
are derived from, so a release cannot silently publish a release page with no
usable summary.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import release_notes  # noqa: E402

CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# The on-page part — everything a reader sees without expanding a disclosure.
# Keep it short enough to be scanned in one screen; the detail is one click away.
MAX_VISIBLE_LINES = 30

# A highlight is a themed line that says what changed, not a bare label. This is
# the floor that stops one-word-per-change summaries coming back.
MIN_HIGHLIGHT_CHARS = 80

FIXTURE = """\
# Changelog

## [2.0.0] - 2026-10-08

Why this release exists, in one paragraph.

### Highlights

- **Explicit highlight wins.** ([#1](https://example.com/pull/1))

### Added

- **A bolded lead is a highlight.** The rest of the explanation, which is long
  and detailed and stays in the collapsed section. ([#10](https://example.com/pull/10))

### Fixed

- **A fix is also a highlight.** Detail. ([#11](https://example.com/pull/11), [#12](https://example.com/pull/12))

### Docs

- Not a highlight, because docs are not what a user scans for. ([#13](https://example.com/pull/13))

### Contributors

- [@someone](https://github.com/someone) — [#10](https://example.com/pull/10)

## [1.0.0] - 2026-01-01

### Added

- **Old.** Old.
"""

GENERATED = """\
## What's Changed
* feat: a thing by @someone in https://example.com/pull/10
* fix: another thing by @someone in https://example.com/pull/11
* fix: a third by @other in https://example.com/pull/12


**Full Changelog**: https://example.com/compare/v1.0.0...v2.0.0
"""


@pytest.fixture
def fixture_changelog(tmp_path, monkeypatch):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(FIXTURE, encoding="utf-8")
    monkeypatch.setattr(release_notes, "CHANGELOG", path)
    return path


def build(**overrides):
    kwargs = dict(
        version="2.0.0",
        repo="owner/name",
        prev="v1.0.0",
        generated=GENERATED,
        generated_missing=False,
        with_contributors=False,
    )
    kwargs.update(overrides)
    return release_notes.build_body(**kwargs)


def visible_lines(body: str) -> list[str]:
    """The body with every `<details>` block removed — what a reader actually sees."""
    without = re.sub(r"<details>.*?</details>", "", body, flags=re.S)
    return [line for line in without.splitlines() if line.strip()]


def visible_text(body: str) -> str:
    return "\n".join(visible_lines(body))


class TestParsing:
    def test_find_entry_matches_the_heading_not_prose(self):
        text = "## [1.0.0] - x\n\n### Added\n\n- **Told.** 1.0.0 is old.\n"
        assert release_notes.find_entry(text, "1.0.0") is not None
        assert release_notes.find_entry(text, "0.0.1") is None

    def test_split_entry_separates_intro_and_sections(self, fixture_changelog):
        entry = release_notes.find_entry(fixture_changelog.read_text(), "2.0.0")
        intro, sections = release_notes.split_entry(entry[1])
        assert intro == "Why this release exists, in one paragraph."
        assert [heading for heading, _ in sections] == [
            "Highlights",
            "Added",
            "Fixed",
            "Docs",
            "Contributors",
        ]

    def test_highlight_lines_come_from_the_highlights_block(self):
        assert release_notes.highlight_lines("- **A.** b.", "1.0.0") == ["- **A.** b."]

    def test_missing_highlights_block_is_an_error_not_a_terse_page(self, capsys):
        """Highlights are written; a release with none must not publish labels instead."""
        with pytest.raises(SystemExit) as exc:
            release_notes.highlight_lines(None, "1.0.0")
        assert exc.value.code == 1
        assert "no '### Highlights' block" in capsys.readouterr().err

    def test_empty_highlights_block_is_an_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            release_notes.highlight_lines("\n", "1.0.0")
        assert exc.value.code == 1
        assert "has no '- ' bullets" in capsys.readouterr().err

    def test_github_slug_matches_the_published_anchor(self):
        """The compare-page anchor is computed, so pin the algorithm on a real heading."""
        assert release_notes.github_slug("[1.12.0] - 2026-10-07") == "1120---2026-10-07"

    def test_version_key_orders_numerically(self):
        assert release_notes.version_key("v1.10.0") > release_notes.version_key("v1.9.9")

    def test_previous_tag_is_the_highest_one_below(self):
        assert release_notes.previous_tag("1.12.0") == "v1.11.0"

    def test_previous_tag_of_the_first_release_is_none(self):
        assert release_notes.previous_tag("1.0.0") is None


class TestScaleAndCredit:
    def test_scale_line_counts_prs_and_unique_authors(self):
        assert release_notes.scale_line(GENERATED) == "**3 pull requests · 2 contributors**"

    def test_scale_line_singularises(self):
        one = "* feat: x by @a in https://example.com/pull/1\n"
        assert release_notes.scale_line(one) == "**1 pull request · 1 contributor**"

    def test_empty_generated_has_no_scale(self):
        assert release_notes.scale_line("") == ""


class TestBodyShape:
    def test_body_leads_with_version_summary_and_highlights(self, fixture_changelog):
        body = build()
        head = body.splitlines()
        assert head[0] == "## v2.0.0"
        assert "Why this release exists" in body
        assert "**Highlights**" in body
        # Highlights come before any collapsed detail.
        assert body.index("**Highlights**") < body.index("<details>")

    def test_highlights_are_used_verbatim(self, fixture_changelog):
        body = build()
        assert "- **Explicit highlight wins.** ([#1](https://example.com/pull/1))" in visible_text(body)
        # Derived labels from the detailed sections must not leak onto the page.
        assert "- **A bolded lead is a highlight.**" not in visible_text(body)
        assert "- **A fix is also a highlight.**" not in visible_text(body)

    def test_detail_is_collapsed_in_expandable_sections(self, fixture_changelog):
        body = build()
        assert body.count("<details>") == 2
        assert body.count("</details>") == 2
        assert "Curated release notes" in body
        assert "What's Changed" in body
        # The long explanation lives inside a disclosure, not on the page.
        assert "long\nand detailed" not in "\n".join(visible_lines(body))

    def test_visible_page_stays_short(self, fixture_changelog):
        body = build()
        assert len(visible_lines(body)) <= MAX_VISIBLE_LINES, (
            "the release page must stay scannable; move detail into a <details> block"
        )

    def test_full_changelog_links_people_agents_and_compare(self, fixture_changelog):
        body = build()
        assert "blob/main/CHANGELOG.md#200---2026-10-08" in body
        assert "raw.githubusercontent.com/owner/name/main/CHANGELOG.md" in body
        assert "compare/v1.0.0...v2.0.0" in body

    def test_contributors_subsection_is_dropped_from_the_body(self, fixture_changelog):
        """The generated list already credits every author; listing twice is noise."""
        body = build()
        assert "### Contributors" not in body

    def test_with_contributors_keeps_the_in_repo_record(self, fixture_changelog):
        body = build(with_contributors=True)
        assert "### Contributors" in body

    def test_no_standalone_thanks_line(self, fixture_changelog):
        """The collapsed list already credits every author as `by @user`.

        A second name list on the page is duplication, and the collapsed
        @mentions still feed GitHub's avatar strip.
        """
        body = build()
        assert "**Thanks**" not in body
        assert "by @someone" in body

    def test_missing_generated_notes_are_stated_not_hidden(self, fixture_changelog):
        """A page that quietly dropped its credit record would still look complete."""
        body = build(generated="", generated_missing=True)
        assert "credit is missing" in body

    def test_unknown_version_exits_non_zero(self, fixture_changelog, capsys):
        with pytest.raises(SystemExit) as exc:
            build(version="9.9.9")
        assert exc.value.code == 1
        assert "no '## [9.9.9]' section" in capsys.readouterr().err


class TestChangelogConvention:
    """Highlights are written, so the newest entry must carry them."""

    def _newest_entry(self) -> tuple[str, list[tuple[str, str]]]:
        text = CHANGELOG.read_text(encoding="utf-8")
        match = next(release_notes.ENTRY_RE.finditer(text))
        version = match.group("version").strip()
        return version, release_notes.split_entry(match.group("body"))[1]

    def test_newest_entry_has_a_usable_summary_and_highlights(self):
        version, sections = self._newest_entry()
        headings = [heading for heading, _ in sections]
        assert headings, f"the {version} entry has no sections"
        explicit = next((b for h, b in sections if h == "Highlights"), None)
        assert explicit is not None, (
            f"the {version} entry has no '### Highlights' block, so the release "
            "page builder would refuse to run"
        )
        assert release_notes.bullets(explicit), (
            f"the {version} '### Highlights' block has no '- ' bullets, so the release "
            "page would publish no highlights"
        )

    def test_every_highlight_says_what_changed(self):
        """A highlight is a description, not a one-word label.

        The failure this guards is the page that listed ``**Rate Limiting**`` and
        nothing else: a reader learns a topic, not a change.
        """
        version, sections = self._newest_entry()
        explicit = next(b for h, b in sections if h == "Highlights")
        for line in release_notes.bullets(explicit):
            assert len(line) >= MIN_HIGHLIGHT_CHARS, (
                f"{version} highlight is too short to say what changed "
                f"({len(line)} chars, want >= {MIN_HIGHLIGHT_CHARS}): {line!r}"
            )

    def test_backfilled_entries_carry_highlights_too(self):
        """The four releases whose pages are regenerated from the CHANGELOG."""
        text = CHANGELOG.read_text(encoding="utf-8")
        for version in ("1.12.0", "1.11.0", "1.10.1", "1.10.0"):
            match = release_notes.find_entry(text, version)
            assert match is not None, f"CHANGELOG.md is missing [{version}]"
            sections = release_notes.split_entry(match[1])[1]
            explicit = next((b for h, b in sections if h == "Highlights"), None)
            assert explicit and release_notes.bullets(explicit), (
                f"[{version}] has no '### Highlights' block, so its release page "
                "cannot be rebuilt"
            )

    def test_current_version_builds_a_real_body_from_the_real_changelog(self):
        from zulip.version import __version__

        body = release_notes.build_body(
            __version__,
            repo="owner/name",
            prev=None,
            generated=GENERATED,
            generated_missing=False,
            with_contributors=False,
        )
        assert body.startswith(f"## v{__version__}")
        assert "**Highlights**" in body
        assert "<details>" in body
