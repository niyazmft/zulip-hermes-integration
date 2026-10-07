# Releasing

Semantic versioning, tags are `vX.Y.Z`. Every release is published from `main`
with a **short, reader-first body**: what a person needs on the page, and the
exhaustive record behind expandable sections
([the shape openclaw/openclaw uses](https://github.com/openclaw/openclaw/releases)).

| On the page | Source | Credits contributors? |
|-------------|--------|----------------------|
| Title, one-paragraph summary, `**Highlights**` (themed lines written in the entry), scale line | `CHANGELOG.md` via `scripts/release_notes.py` | No — describes changes; the scale line counts contributors |
| `<details>` **Curated release notes** (`Added`, `Fixed`, `Docs`, `Internal`) | `CHANGELOG.md` via `scripts/release_notes.py` | No — describes changes |
| `<details>` **What's Changed** (`* <title> by @user in <pr>`) | GitHub's generate-release-notes API, configured by `.github/release.yml` | **Yes** |
| `Full changelog` links (rendered · raw · compare) | derived by `scripts/release_notes.py` | No |

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
- **`scripts/release_notes.py` fetches that layer with `gh api`**. If the fetch
  fails the script exits non-zero rather than publishing a page that looks
  complete but has lost the credit record. Use `--no-generated` only for a dry
  run; it prints an explicit "credit is missing" line in the body.
- **There is no separate `Thanks` line.** The collapsed list already names every
  author as `by @user`, so a second name list would be duplication — and the
  collapsed `@mentions` are what GitHub's avatar strip above Assets is built
  from.
- **The release PR excludes itself** via the `release` label
  (`.github/release.yml`); otherwise `chore: release vX.Y.Z` shows up in its own
  notes.
- **`CHANGELOG.md` keeps a `### Contributors` section as the in-repo record.**
  When you write one, it must list *every* PR author in the range — the
  maintainer included — because the script strips this subsection before it
  reaches the release body (the collapsed generated list already credits those
  people).

## Authoring the entry so the page reads well

A release page is read by somebody deciding whether to care, so its highlights
are **written, not derived**: they group related work into themed lines and say
what each one does, in the shape
[openclaw uses](https://github.com/openclaw/openclaw/releases):

```markdown
### Highlights
- **Approvals are owned, and every decision is on the record:** `ZULIP_APPROVAL_AUTHORITY=owner`
  refuses a decision from anyone but the bot owner and does not count it, so the prompt stays
  open; every resolved approval is audited with its choice, decider and request id; and
  `ZULIP_APPROVAL_ON_TIMEOUT=deny` posts one refusal line where the host posts no timeout
  notice of its own. ([#228](…), [#262](…), [#222](…), [#261](…))
```

- **Theme first, then clauses.** A bolded theme, a colon, then what changed —
  comma-separated verb phrases joined with "and", the way a person would
  describe the release to a colleague. Several `Added`/`Fixed` bullets can fold
  into one highlight, and one can cover several PRs; the refs go at the end.
- **Group, don't enumerate.** Aim for a handful of lines, not one per changed
  bullet. `v1.10.0` has 21 detail bullets and 4 highlights.
- **Say what changed, not what the area is.** ``**Rate Limiting**`` is a label;
  "a per-sender sliding window caps a sender at 60 messages a minute" is a
  highlight. `tests/test_release_notes.py` enforces a floor of
  `MIN_HIGHLIGHT_CHARS = 80` on the newest entry's highlights for exactly this.
- **A missing `### Highlights` block is an error, not a quiet downgrade.**
  `scripts/release_notes.py` refuses to build the page, because the alternative
  is publishing a page that looks finished while saying nothing. The detail
  bullets keep their `- **Claim.** explanation` lead so the collapsed section
  stays scannable, but nothing is derived from them any more.

The four entries whose pages are regenerated from `CHANGELOG.md`
(`1.10.0`, `1.10.1`, `1.11.0`, `1.12.0`) carry a `### Highlights` block, and a
test keeps them that way.

## Procedure

1. **Land the changes on `main`.** All PRs merged; `main` is green.

2. **Bump the version** in `zulip/plugin.yaml` (`version: X.Y.Z`).

3. **Add the CHANGELOG entry** — newest first, Keep a Changelog headings:
   `Added`, `Fixed`, `Docs`, `Internal`, a required `Highlights`, optional
   `Contributors`. Open the entry with a short paragraph saying why this release
   exists, then write the highlights in the themed shape above — that paragraph
   and those lines are the release page. (`scripts/release_notes.py` fails
   without the `Highlights` block.)

   ```markdown
   ## [1.9.3] - 2026-10-01

   One paragraph on why this release exists.

   ### Highlights
   - **A theme, then what changed:** clause, clause, and clause. ([#135](…))

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
   notes** and **What's Changed** sections (your handle on your own PRs, which is
   the whole credit record), and the three `Full changelog` links.

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
