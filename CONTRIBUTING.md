# Contributing

Thanks for taking the time to contribute. This document covers development setup, the checks
CI will run on your PR, and the handful of repo-specific rules that are easy to trip over.

## Table of Contents

- [Development Setup](#development-setup)
- [Project Layout](#project-layout)
- [Rules That Bite](#rules-that-bite)
- [Testing](#testing)
- [Documentation Claims Are Tested](#documentation-claims-are-tested)
- [Commit Messages](#commit-messages)
- [Pull Request Process](#pull-request-process)
- [Release Process](#release-process)

---

## Development Setup

1. **Fork and clone**:

   ```bash
   git clone https://github.com/YOUR_USERNAME/zulip-hermes-integration.git
   cd zulip-hermes-integration
   ```

2. **Install dependencies** (Python 3.10+; CI runs the suite on 3.12):

   ```bash
   python3 -m pip install -r requirements.txt
   ```

3. **Install the pre-push hook**:

   ```bash
   bash scripts/setup-hooks.sh
   ```

   This points `core.hooksPath` at `.githooks`. The hook regenerates `checksums.txt` and runs
   `py_compile`, the `plugin.yaml` parse and the full test suite before any push, mirroring
   the fast CI job. To bypass it in an emergency, push with `--no-verify` (not recommended).

4. **Verify**:

   ```bash
   bash .githooks/pre-push
   ```

### What CI runs, and how to run it yourself

| Local command | CI |
|---|---|
| `bash .githooks/pre-push` | the fast `zulip-bridge` job: `checksums.txt` freshness, `py_compile`, `plugin.yaml`, pytest |
| `pip-audit -r requirements.txt` | the fast job's dependency audit |
| `gitleaks dir .` (or `git .`) | the fast job's secret scan; tuned by `.gitleaks.toml` |
| `python3 scripts/check_compat.py` | the `compat` matrix legs — see [Testing](#testing) |
| `python3 scripts/parity_check.py` | **nothing** — the parity workflow was removed (#229); this is manual |

The fast job is the required status check. It runs for documentation-only PRs too, because
[the doc claims are tested](#documentation-claims-are-tested). Only the `compat` legs are
skipped for docs-only changes.

---

## Project Layout

```
zulip/            the plugin package — one module per concern, all shipped via PLUGIN_FILES
  adapter.py        the gateway adapter: registration, event dispatch, hooks
  routing.py        topic/session routing and reply-topic resolution
  outbound.py       message/typing/media egress
  inbound.py        inbound dispatch and the traffic-policy path
  policy.py         chatmode, mention gating, group/DM allowlists
  security:         secret_guard.py, media.py, approvals.py, audit_logger.py
  observability:    activity_trace.py, tracing.py, history.py, logger.py
  persistence:      conversations.py, dedupe_store.py, inbound_queue.py, session_queue.py
  updater.py, version.py, plugin.yaml   self-update + manifest
tests/            pytest modules, one per concern; tests/stubs/ mirrors the Hermes gateway surface
scripts/          check_compat.py (real-host gate), parity_check.py, release_notes.py
docs/             RELEASING.md, PARITY.md, parity-matrix.yaml
```

---

## Rules That Bite

Three of these have caused production incidents; each is guarded by a test, so CI will tell
you — but knowing them saves a round trip.

1. **A new module under `zulip/` must be added to `PLUGIN_FILES`** in `zulip/version.py`.
   The self-updater only *replaces* files named in that list, so a module missing from it is
   absent after every `zulip update` while `adapter.py` imports it — the plugin then fails to
   import and the bot is down for every user (#177; caught by
   `tests/test_version.py::test_every_package_module_is_shipped`).
2. **A new environment variable must be declared in `zulip/plugin.yaml`.** Reads and the
   manifest are compared by `tests/test_manifest_parity.py` (#147).
3. **Any change under `zulip/` changes `checksums.txt`.** The pre-push hook regenerates it;
   CI regenerates it independently under `LC_ALL=C` and diffs. A stale entry makes `zulip
   update` abort with a checksum mismatch for every user, so commit the regenerated file.

---

## Testing

```bash
python3 -m pytest tests/ -q            # the full suite
python3 -m pytest tests/test_policy.py -v
```

- Unit tests use `tests/stubs/`, which mirrors the gateway surface. **A stub cannot notice
  that a real gateway lacks a symbol or changed a contract** — that is what
  `scripts/check_compat.py` is for, and why it runs against real hosts in CI.
- To run the compatibility gate locally against a specific host version, install the host
  editable (a plain direct reference fails: Hermes's build backend refuses to build a wheel
  from a source tree) and point the gate at it:

  ```bash
  python3 -m venv /tmp/host && /tmp/host/bin/pip install -e \
    "git+https://github.com/NousResearch/hermes-agent@v2026.9.24#egg=hermes-agent"
  cd /path/to/zulip-hermes-integration && /tmp/host/bin/python scripts/check_compat.py
  ```

  Exit `0` = compatible, `1` = a required symbol/contract is missing (stderr names it) or the
  host is below `__min_hermes__`, `2` = no host importable. CI pins three legs: `0.18.2` (the
  declared floor, from PyPI), `v2026.9.14` (host 0.21.3) and `v2026.9.24` (host 0.21.5).
- `tests/` runs against the Python the gateway uses, in a stubbed environment. There is no
  tier-2 fake-server job here (the sibling has one); outbound payloads are asserted against
  the recorded client calls in `tests/test_media_outbound.py`, `tests/test_outbound_think.py`
  and `tests/test_refs.py`.

---

## Documentation Claims Are Tested

README.md, AGENTS.md and SECURITY.md make claims that are asserted in the suite —
`tests/test_security_docs.py`, `tests/test_readme_docs.py`, `tests/test_compat_floor.py` and
`tests/test_ci_workflow.py`. If you change configuration, defaults, the supported Hermes
range, the README badges or the workflow shape, expect those tests to fail and **update the
document to match the code — never the test to match the document.** Softening a claim to
land a change is the failure mode these tests exist to prevent.

---

## Commit Messages

Conventional-commit prefixes, as used throughout this repo's history:

```
feat: add ZULIP_STREAM_OVERRIDES for per-stream policy
fix(compat): the declared floor was right, the gate was wrong (#230)
docs: correct README claims that the code does not support
ci: adopt the sibling's CI optimizations
refactor(zulip): decompose the adapter god-object
test: pin the stale-reaction recovery contract
chore: release v1.10.1
```

Explain the *why* in the body. Where a change fixes a class of bug, say which guard now
catches it.

---

## Pull Request Process

1. **Branch** from `main`: `fix/…`, `feat/…`, `docs/…`, `ci/…` or `chore/…`.
2. **One PR per issue.** Each PR body says `Fixes #<issue>` and, for epic work, "Part of
   epic #N" — never one PR covering several children.
3. **Fill in the template.** The checklist mirrors what CI enforces.
4. **CI must pass.** `main` is governed by a ruleset: PR required, **squash merge only**
   (linear history), and the `zulip-bridge` job must pass.
5. **If `main` has moved**, the required check is strict about being up to date, so update
   your branch before merging:

   ```bash
   gh pr update-branch <number> --rebase
   ```

   (A rebase, not a merge commit — linear history is enforced.)

Review may ask for a guard test alongside a fix. That is the house style: a bug that reached
a user usually gets a test that would have caught it.

---

## Release Process

Releases are cut from `main` by the maintainer; see [docs/RELEASING.md](docs/RELEASING.md) for
the procedure, the contributor-credit policy and the release-notes tooling. Versioning is
semantic (`__version__` in `zulip/version.py`), and a release must regenerate `checksums.txt`
— pushing to `main` alone does not ship anything to installed plugins.
