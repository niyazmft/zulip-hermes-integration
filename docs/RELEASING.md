# Releasing

Semantic versioning, tags are `vX.Y.Z`. Every release is published from `main`
with a **short, reader-first body**: what a person needs on the page, and the
exhaustive record behind expandable sections
([the shape openclaw/openclaw uses](https://github.com/openclaw/openclaw/releases)).

| On the page | Source | Credits contributors? |
|-------------|--------|----------------------|
| Title, one-paragraph summary, `**Highlights**` (one line per user-facing change), scale line | `CHANGELOG.md` via `scripts/release_notes.py` | No — describes changes |
| `<details>` **Curated release notes** (`Added`, `Fixed`, `Docs`, `Internal`) | `CHANGELOG.md` via `scripts/release_notes.py` | No — describes changes |
| `<details>` **What's Changed** (`* <title> by @user in <pr>`) | GitHub's generate-release-notes API, configured by `.github/release.yml` | **Yes** |
| `**Thanks**` line and the `Full changelog` links (rendered · raw · compare) | derived by `scripts/release_notes.py` | Yes — from the generated list |

Nothing is dropped: the full prose and the full PR list are both on the page,
just collapsed. The point of the layout is that a reader learns what changed
without expanding anything.

## Contributor credit policy

This is the rule; it exists because hand-maintained credit drifted (v1.9.0 and
v1.9.2 both omitted the maintainer's own PRs).

- **The generated layer is the credit record.** `## What's Changed` lists
  every merged PR author in `vPREV...vX.Y.Z`, maintainer included
  (`* <title> by @user in <pr url>`), and `## New Contributors` lists everyone
  making their first contribution. Nothing to maintain.
- **`scripts/release_notes.py` fetches that layer with `gh api`** and renders
  its authors as the on-page `**Thanks**` line. If the fetch fails the script
  exits non-zero rather than publishing a page that looks complete but has lost
  the credit record. Use `--no-generated` only for a dry run; it prints an
  explicit "credit is missing" line in the body.
- **GitHub's avatar strip** above Assets is built from `@mentions` in the body,
  so the collapsed list still feeds it.
- **The release PR excludes itself** via the `release` label
  (`.github/release.yml`); otherwise `chore: release vX.Y.Z` shows up in its own
  notes.
- **`CHANGELOG.md` keeps a `### Contributors` section as the in-repo record.**
  When you write one, it must list *every* PR author in the range — the
  maintainer included — because the script strips this subsection before it
  reaches the release body (the collapsed generated list and the `Thanks` line
  already credit those people).

## Authoring the entry so the page reads well

The highlights are **derived from the bolded lead of each `Added` and `Fixed`
bullet**, so that convention is load-bearing:

```markdown
- **Only the bot owner may decide an exec approval.** Explanation, evidence and
  the trailing refs stay in the collapsed section. ([#228](…), [#262](…))
```

The bolded part must stand alone as the sentence a user wants to read — it
becomes the one-line highlight, with the refs appended. Write it as a claim
("A failed turn no longer ends in silence"), not a label ("Delivery audit").
`tests/test_release_notes.py` fails if the newest entry has no `Added`/`Fixed`
bullets or a bullet has no bolded lead, so a release cannot silently publish a
page with no usable summary.

A release that wants a different, shorter set can add a `### Highlights`
subsection; those bullets are used verbatim instead of the derived ones.

## Procedure

1. **Land the changes on `main`.** All PRs merged; `main` is green.

2. **Bump the version** in `zulip/plugin.yaml` (`version: X.Y.Z`).

3. **Add the CHANGELOG entry** — newest first, Keep a Changelog headings:
   `Added`, `Fixed`, `Docs`, `Internal`, optional `Highlights`, optional
   `Contributors`. Open the entry with a short paragraph saying why this release
   exists; that paragraph leads the release page.

   ```markdown
   ## [1.9.3] - 2026-10-01

   One paragraph on why this release exists.

   ### Added
   - **A standalone claim.** What changed and why; the detail lives here, off the
     release page. ([#135](https://github.com/niyazmft/zulip-hermes-integration/pull/135))

   ### Contributors
   - [@niyazmft](https://github.com/niyazmft) — [#135](https://github.com/niyazmft/zulip-hermes-integration/pull/135)
   ```

4. **Regenerate `checksums.txt`** — required, CI fails on a stale manifest and
   `zulip update` aborts for every user:

   ```bash
   bash .githooks/pre-push          # regenerates checksums + runs CI checks
   ```

   The pre-push hook runs the same checks as CI (pytest, `py_compile`, YAML
   validation, checksums) and writes `checksums.txt` in place.

5. **Open the release PR** (`chore: release vX.Y.Z`), wait for CI, then
   **apply the `release` label** — this is what keeps the PR out of its own
   release notes. Merge it.

6. **Tag and push** (annotated):

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```

7. **Build the release body and publish it.** The script reads the CHANGELOG
   entry, derives the highlights, and fetches the generated PR list itself:

   ```bash
   python3 scripts/release_notes.py X.Y.Z > /tmp/notes.md
   gh release create vX.Y.Z --verify-tag \
     --title "vX.Y.Z" \
     --notes-file /tmp/notes.md
   ```

   Do **not** pass `--generate-notes`: that would append an uncollapsed
   `## What's Changed` after the file and undo the layout. Read `/tmp/notes.md`
   before publishing — the highlights are only as good as the entry they came
   from.

   Useful flags: `--generated FILE` to reuse an already-fetched generated body
   (keeps the script offline), `--no-generated` for a dry run,
   `--with-contributors` to keep `### Contributors` in the collapsed notes, and
   `--previous-tag TAG` / `--repo OWNER/NAME` to override the auto-detected
   compare target and repository.

8. **Verify the published release page** shows, in order:
   `## vX.Y.Z`, the summary paragraph, `**Highlights**`,
   `**N pull requests · M contributors**`, the collapsed **Curated release
   notes** and **What's Changed** sections, the `**Thanks**` line (with your
   handle on your own PRs), and the three `Full changelog` links.

## Notes

- `.github/release.yml` excludes the `release` / `ignore-for-release` labels and
  `dependabot` from generated notes. Both labels must exist in the repo
  (`gh label list` to check; `gh label create release` if missing).
- Labels are sparse on PRs today, so most generated entries land under
  `Other Changes`. Label PRs `enhancement` / `bug` / `documentation` /
  `ci-cd` if you want the generated list grouped.
- **Backfilling a published release** — for a reformat, or only when the body is
  genuinely wrong:

  ```bash
  python3 scripts/release_notes.py X.Y.Z > /tmp/notes.md
  gh release edit vX.Y.Z --notes-file /tmp/notes.md
  ```

  That replaces the body, so read `/tmp/notes.md` first.
