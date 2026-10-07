#!/usr/bin/env python3
"""Build a short, reader-first release body for a version.

The release page is the first thing a user sees, so it leads with what a
person needs and hides the exhaustive record behind expandable sections — the
shape openclaw/openclaw uses:

    ## v1.12.0                                  <- version, top level
    <one-paragraph summary from the entry>      <- why this release exists
    **Highlights**                              <- one line per user-facing change
    **7 pull requests · 1 contributor**         <- scale
    <details> Curated notes  (Added/Fixed/…)    <- full prose, collapsed
    <details> What's Changed (every PR)         <- mechanical credit, collapsed
    **Full changelog:** rendered · raw · compare <- pointers, including the
                                                   plain-Markdown file for agents

Usage:
    python3 scripts/release_notes.py 1.12.0 > /tmp/notes.md
    gh release create v1.12.0 --verify-tag --title "v1.12.0" \\
        --notes-file /tmp/notes.md

The collapsed "What's Changed" list is GitHub's own generate-release-notes
output, fetched with `gh` so `.github/release.yml` still owns the exclusions and
the categories. Credit is therefore unchanged: that list is still the record of
every merged PR author in the range, maintainer included. Fetch it once and pass
it in to keep the script offline:

    gh api repos/OWNER/REPO/releases/generate-notes \\
        -f tag_name=v1.12.0 -f previous_tag_name=v1.11.0 --jq .body > /tmp/gen.md
    python3 scripts/release_notes.py 1.12.0 --generated /tmp/gen.md

`--no-generated` skips the network fetch entirely (for dry runs and tests); the
body is then short of the mechanical list and says so.

The `### Contributors` subsection is an in-repo record that the generated list
already covers, so it is dropped unless --with-contributors is given.

See docs/RELEASING.md for the full release procedure.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

ENTRY_RE = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?P<rest>[^\n]*)\n(?P<body>.*?)(?=^## |\Z)",
    re.S | re.M,
)
SECTION_SPLIT_RE = re.compile(r"^### (.+?)\s*$", re.M)
BOLD_LEAD_RE = re.compile(r"^- \*\*(?P<lead>.+?)\*\*", re.S)
REF_RE = re.compile(r"\[#\d+\]\([^)]+\)")
AUTHOR_RE = re.compile(r"\bby @([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)")
PR_LINE_RE = re.compile(r"^\* .+/pull/\d+\s*$", re.M)

# Sections whose bolded leads become the on-page highlights, in order. Docs and
# Internal are real work but not what a user scans for.
HIGHLIGHT_SECTIONS = ("Added", "Fixed")

REPO_URL = "https://github.com/{repo}"


def find_entry(text: str, version: str) -> tuple[str, str] | None:
    """Return (trailing heading text, body) for `## [<version>]`, or None."""
    for match in ENTRY_RE.finditer(text):
        if match.group("version").strip() == version:
            return match.group("rest"), match.group("body")
    return None


def split_entry(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Split an entry body into its intro prose and its `### Section` blocks."""
    parts = SECTION_SPLIT_RE.split(body)
    intro = parts[0].strip()
    sections = [
        (parts[i].strip(), parts[i + 1].strip())
        for i in range(1, len(parts) - 1, 2)
    ]
    return intro, sections


def bullets(section_body: str) -> list[str]:
    return [line.rstrip() for line in section_body.splitlines() if line.startswith("- ")]


def highlight_line(bullet: str) -> str:
    """One-line highlight from a CHANGELOG bullet.

    The house style opens every bullet with a bolded, standalone claim
    (`- **Thing.** explanation…`), which is already the sentence a reader wants;
    the explanation and the trailing refs stay in the collapsed section.
    """
    match = BOLD_LEAD_RE.match(bullet)
    lead = f"**{match.group('lead').strip()}**" if match else bullet[2:].strip()
    refs: list[str] = []
    for ref in REF_RE.findall(bullet):
        if ref not in refs:
            refs.append(ref)
    return f"- {lead} ({', '.join(refs)})" if refs else f"- {lead}"


def derive_highlights(sections: list[tuple[str, str]], explicit: str | None) -> list[str]:
    """An explicit `### Highlights` block wins; otherwise derive from Added/Fixed."""
    if explicit is not None:
        return bullets(explicit)
    lines: list[str] = []
    for heading, section_body in sections:
        if heading in HIGHLIGHT_SECTIONS:
            lines.extend(highlight_line(b) for b in bullets(section_body))
    return lines


def scale_line(generated: str) -> str:
    """`**N pull requests · M contributors**` from the generated notes, or ""."""
    prs = len(PR_LINE_RE.findall(generated))
    authors: list[str] = []
    for handle in AUTHOR_RE.findall(generated):
        if handle not in authors:
            authors.append(handle)
    if not prs and not authors:
        return ""
    bits = []
    if prs:
        bits.append(f"{prs} pull request{'s' if prs != 1 else ''}")
    if authors:
        bits.append(f"{len(authors)} contributor{'s' if len(authors) != 1 else ''}")
    return "**" + " · ".join(bits) + "**"


def thanks_line(generated: str) -> str:
    """`**Thanks** @a, @b` from the generated notes, or "" when there are none."""
    authors: list[str] = []
    for handle in AUTHOR_RE.findall(generated):
        if handle not in authors:
            authors.append(handle)
    if not authors:
        return ""
    links = ", ".join(f"[@{h}](https://github.com/{h})" for h in authors)
    return f"**Thanks** {links}"


def github_slug(text: str) -> str:
    """GitHub's heading anchor algorithm: drop punctuation, spaces to hyphens."""
    slug = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return slug.replace(" ", "-")


def version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", tag)) or (0,)


def previous_tag(version: str) -> str | None:
    """Highest local tag below `version`, so the compare link needs no network."""
    try:
        out = subprocess.run(
            ["git", "tag", "--list", "v*"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    target = version_key(version)
    candidates = [
        tag.strip()
        for tag in out.splitlines()
        if tag.strip() and version_key(tag.strip()) < target
    ]
    return max(candidates, key=version_key) if candidates else None


def detect_repo() -> str | None:
    """`owner/name` from the origin remote, without calling the network."""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(r"github\.com[:/](?P<repo>[^/\s]+/[^/\s]+?)(?:\.git)?$", out)
    return match.group("repo") if match else None


def fetch_generated(repo: str, tag: str, prev: str | None) -> str:
    """GitHub's generate-release-notes output, or exit non-zero.

    A missing generated list is a failure, not a silent downgrade: it is the
    credit record, and a release page that quietly dropped it would still look
    complete.
    """
    cmd = ["gh", "api", f"repos/{repo}/releases/generate-notes", "-f", f"tag_name={tag}"]
    if prev:
        cmd += ["-f", f"previous_tag_name={prev}"]
    cmd += ["--jq", ".body"]
    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        print("error: `gh` not found; pass --generated FILE or --no-generated", file=sys.stderr)
        return ""
    except subprocess.CalledProcessError as exc:
        print(f"error: generate-notes failed for {repo} {tag}:", file=sys.stderr)
        print(exc.stderr.strip() or exc.stdout.strip(), file=sys.stderr)
        return ""
    if not result.stdout.strip():
        print(f"error: generate-notes returned an empty body for {tag}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def build_body(
    version: str,
    *,
    repo: str | None,
    prev: str | None,
    generated: str,
    generated_missing: bool,
    with_contributors: bool,
) -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    entry = find_entry(text, version)
    if entry is None:
        available = [m.group("version") for m in ENTRY_RE.finditer(text)]
        print(f"error: no '## [{version}]' section in {CHANGELOG.name}", file=sys.stderr)
        print("available: " + ", ".join(dict.fromkeys(available)), file=sys.stderr)
        raise SystemExit(1)

    heading_rest, body = entry
    intro, sections = split_entry(body)
    explicit = next((b for h, b in sections if h == "Highlights"), None)
    highlights = derive_highlights(sections, explicit)

    lines: list[str] = [f"## v{version}", ""]
    if intro:
        lines += [intro, ""]

    if highlights:
        lines += ["**Highlights**", ""]
        lines += highlights
        lines += [""]

    scale = scale_line(generated)
    if scale:
        lines += [scale, ""]

    curated = [
        (heading, section_body)
        for heading, section_body in sections
        if heading != "Highlights" and (with_contributors or heading != "Contributors")
    ]
    if curated:
        lines += [
            "<details>",
            "<summary>Curated release notes — Added, Fixed, Docs, Internal</summary>",
            "",
        ]
        for heading, section_body in curated:
            lines += [f"### {heading}", "", section_body, ""]
        lines += ["</details>", ""]

    if generated:
        lines += [
            "<details>",
            "<summary>What's Changed — every pull request, by author</summary>",
            "",
            generated,
            "",
            "</details>",
            "",
        ]
    elif generated_missing:
        lines += [
            "_The generated pull-request list was not fetched; credit is missing "
            "from this page._",
            "",
        ]

    thanks = thanks_line(generated) if not with_contributors else ""
    if thanks:
        lines += [thanks, ""]

    if repo:
        rendered = f"{REPO_URL.format(repo=repo)}/blob/main/CHANGELOG.md"
        raw = f"https://raw.githubusercontent.com/{repo}/main/CHANGELOG.md"
        anchor = github_slug(f"[{version}]{heading_rest}")
        links = [
            f"[CHANGELOG.md v{version}]({rendered}#{anchor}) — formatted for people",
            f"[raw Markdown]({raw}) — for AI agents and tools",
        ]
        if prev:
            links.append(
                f"[{prev}...v{version}]"
                f"({REPO_URL.format(repo=repo)}/compare/{prev}...v{version})"
            )
        lines += ["**Full changelog:** " + " · ".join(links), ""]

    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", help="version as it appears in CHANGELOG.md, e.g. 1.12.0")
    parser.add_argument(
        "--generated",
        metavar="FILE",
        help="read generate-release-notes output from FILE instead of calling gh",
    )
    parser.add_argument(
        "--no-generated",
        action="store_true",
        help="do not fetch the generated PR list; mark the body as missing credit",
    )
    parser.add_argument(
        "--repo",
        help="owner/name for changelog + compare links (default: the origin remote)",
    )
    parser.add_argument(
        "--previous-tag",
        help="tag to compare against (default: the highest local tag below the version)",
    )
    parser.add_argument(
        "--with-contributors",
        action="store_true",
        help="keep ### Contributors in the collapsed notes instead of dropping it",
    )
    args = parser.parse_args()

    version = args.version.lstrip("v")
    repo = args.repo or detect_repo()
    prev = args.previous_tag or previous_tag(version)

    generated = ""
    generated_missing = False
    if args.generated:
        path = Path(args.generated)
        if not path.is_file():
            print(f"error: --generated {path} does not exist", file=sys.stderr)
            return 1
        generated = path.read_text(encoding="utf-8").strip()
    elif args.no_generated:
        generated_missing = True
    elif prev is None:
        # The first release has no range to credit, and generate-notes has no
        # previous tag to diff against; skipping is correct, not a downgrade.
        print("note: no previous tag; skipping the generated PR list", file=sys.stderr)
    else:
        if not repo:
            print(
                "error: cannot detect the repository (no origin remote); pass --repo",
                file=sys.stderr,
            )
            return 1
        generated = fetch_generated(repo, f"v{version}", prev)
        if not generated:
            return 1

    sys.stdout.write(
        build_body(
            version,
            repo=repo,
            prev=prev,
            generated=generated,
            generated_missing=generated_missing,
            with_contributors=args.with_contributors,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
