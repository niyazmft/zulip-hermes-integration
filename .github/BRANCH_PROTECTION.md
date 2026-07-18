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

## What This Means for Contributors

1. **All changes must go through PRs** — direct push to `main` is blocked.
2. **Squash merge only** — use "Squash and merge" when merging PRs.
3. **CI must pass** — the `zulip-bridge` job in `.github/workflows/ci.yml` must succeed.
4. **Up-to-date branches** — rebase your PR on latest `main` before merging.
5. **Conversation resolution** — all review threads must be marked resolved.

## For Repo Admins

If you need to update the ruleset, go to:  
**Settings → Rules → Rulesets → "Production Safeguard"**

Do not disable without team agreement.
