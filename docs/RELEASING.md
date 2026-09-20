# Releasing

Semantic versioning, tags are `vX.Y.Z`. Every release is published from `main`
with **two layers** in the release body:

| Layer | Source | Credits contributors? |
|-------|--------|----------------------|
| Curated prose (`## Added`, `## Fixed`, `## Docs`, `## Internal`) | `CHANGELOG.md` via `scripts/release_notes.py` | No — describes changes |
| Generated notes (`## What's Changed`, `## New Contributors`, Full Changelog link) | GitHub's generate-release-notes API, configured by `.github/release.yml` | **Yes** |

## Contributor credit policy

This is the rule; it exists because hand-maintained credit drifted (v1.9.0 and
v1.9.2 both omitted the maintainer's own PRs).

- **The generated layer is the credit record.** `## What's Changed` lists
  every merged PR author in `vPREV...vX.Y.Z`, maintainer included
  (`* <title> by @user in <pr url>`), and `## New Contributors` lists everyone
  making their first contribution. Nothing to maintain.
- **GitHub's avatar strip** above Assets is built from `@mentions` in the body —
  no separate "Contributors" markdown section is needed for it.
- **The release PR excludes itself** via the `release` label
  (`.github/release.yml`); otherwise `chore: release vX.Y.Z` shows up in its own
  notes.
- **`CHANGELOG.md` keeps a `### Contributors` section as the in-repo record.**
  When you write one, it must list *every* PR author in the range — the
  maintainer included — because the script strips this subsection before it
  reaches the release body (double-crediting would otherwise occur).

## Procedure

1. **Land the changes on `main`.** All PRs merged; `main` is green.

2. **Bump the version** in `zulip/plugin.yaml` (`version: X.Y.Z`).

3. **Add the CHANGELOG entry** — newest first, Keep a Changelog headings:
   `Added`, `Fixed`, `Docs`, `Internal`, optional `Contributors`:

   ```markdown
   ## [1.9.3] - 2026-10-01

   ### Added
   - **Thing**: what changed, why, and the PR link. ([#135](https://github.com/niyazmft/zulip-hermes-integration/pull/135))

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

7. **Create the release** — curated prose first, generated notes appended by
   `gh` (`--notes-file` sets the body; `--generate-notes` appends after it):

   ```bash
   python3 scripts/release_notes.py X.Y.Z > /tmp/notes.md
   gh release create vX.Y.Z --verify-tag \
     --title "vX.Y.Z — <one-line summary>" \
     --notes-file /tmp/notes.md --generate-notes
   ```

8. **Verify the published release page** shows, in order:
   the curated headings, `## What's Changed` (with your handle on your own PRs),
   `## New Contributors` when applicable, and the `**Full Changelog**` compare
   link.

## Notes

- `.github/release.yml` excludes the `release` / `ignore-for-release` labels and
  `dependabot` from generated notes. Both labels must exist in the repo
  (`gh label list` to check; `gh label create release` if missing).
- Labels are sparse on PRs today, so most generated entries land under
  `Other Changes`. Label PRs `enhancement` / `bug` / `documentation` /
  `ci-cd` if you want the generated list grouped.
- **Backfilling a published release** (only when credit is genuinely wrong):

  ```bash
  python3 scripts/release_notes.py X.Y.Z > /tmp/notes.md
  gh release edit vX.Y.Z --notes-file /tmp/notes.md
  ```

  That replaces the body, so append the generated section too if you want it
  kept — see `gh release view vX.Y.Z`.
