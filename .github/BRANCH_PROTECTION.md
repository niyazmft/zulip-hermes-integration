# Branch Protection & Ruleset

This repository uses a GitHub **Ruleset** (not legacy branch protection) named **"Production Safeguard"**.

## Active Rules on `main`

| Rule | Detail |
|------|--------|
| 🚫 **Branch deletion** | Cannot delete `main` |
| 📏 **Linear history** | No merge commits allowed |
| ✅ **Status checks** | Required job: `zulip-bridge` — **strict** (branches must be up-to-date) |
| 🔀 **Pull requests** | Required, 0 approvals, dismiss stale reviews on push, **squash merge only**, threads must be resolved |
| 🚫 **Non-fast-forward** | No force push |
| 🧹 **Auto-delete branches** | Head branches deleted automatically after PR merge |

## What This Means for Contributors

1. **All changes must go through PRs** — direct push to `main` is blocked.
2. **Squash merge only** — use "Squash and merge" when merging PRs.
3. **CI must pass** — the `zulip-bridge` job in `.github/workflows/ci.yml` must succeed.
4. **Up-to-date branches** — rebase your PR on latest `main` before merging.
5. **Conversation resolution** — all review threads must be marked resolved.
6. **Auto-cleanup** — your feature branch is deleted automatically after merge.

## For Repo Admins

- **Ruleset:** Settings → Rules → Rulesets → "Production Safeguard"
- **Auto-delete branches:** Settings → General → "Automatically delete head branches" (currently ✅ enabled)

Do not disable without team agreement.
