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
[the doc claims are tested](#documentation-claims-are-tested). The `compat` matrix is shaped so
it can be a required check as well (issue #241): it always starts and always reports, and only
its *work* is skipped for docs-only changes, because a required check that is skipped at the
job level never reports and would leave such a PR permanently unmergeable.

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

Every entry below is a defect **class**, not a one-off. Most have bitten this repo at least
once and list their instances, so a repeat is visible as a repeat; an entry with none was
found by inspection — an unguarded hand-maintained value — before it bit. Two statuses:

- **guarded** — a test or CI check fails if you get it wrong. CI will tell you; knowing the
  rule just saves you a round trip.
- **open** — nothing catches it yet, so CI will *not* tell you. These are the expensive ones.

An entry stays `open` after its latest instance is fixed, because fixing an instance is not
fixing the class. Rows are added as fixes land, not in a separate retrospective.

1. **A new module under `zulip/` must be added to `PLUGIN_FILES`** in `zulip/version.py`.
   The self-updater only *replaces* files named in that list, so a module missing from it is
   absent after every `zulip update` while `adapter.py` imports it — the plugin then fails to
   import and the bot is down for every user.
   *Instances:* #100, #177, #204. The third is the one that hurt: the updater was reading the
   *installed* list, so a tree could report the new version while missing the modules that
   version needed, and could not repair itself. #205 made the updater read the file list out
   of the release being installed, so the installed manifest is no longer the source of truth.
   *Status:* **guarded** — `tests/test_version.py::test_every_package_module_is_shipped`.
2. **A new environment variable must be declared in `zulip/plugin.yaml`.** Reads and the
   manifest are compared by the suite, so an undeclared read fails CI.
   *Instances:* #147.
   *Status:* **guarded** — `tests/test_manifest_parity.py`.
3. **Any change under `zulip/` changes `checksums.txt`.** The pre-push hook regenerates it;
   CI regenerates it independently under `LC_ALL=C` and diffs. A stale entry makes `zulip
   update` abort with a checksum mismatch for every user, so commit the regenerated file.
   *Status:* **guarded** — `.githooks/pre-push`, plus the CI step added in #133.
4. **A gate that does not run, or that asserts the wrong contract, is worse than no gate.**
   Both halves have happened: the real-host `compat` job was not actually running (#231), and
   once it ran it reported a false failure because the *gate* was wrong and the declared floor
   was right (#233, from #230).
   *Status:* **open** — #242 makes the gate report on every PR so it can be a required check
   (issue #241) and `tests/test_ci_workflow.py` pins the workflow *shape*, but nothing asserts
   that a deliberately broken host is caught. Read the gate's own output before believing it.
5. **Do not assume a host surface; check the installed artifact.** The plugin collided with
   Hermes slash commands twice — `/version` → `!version` (#43), then `/help`, `/status` and
   `/model` shadowing the gateway that owns them (#190/#191) — and called `client.get_user`,
   which the installed SDK does not expose, silently dropping every reaction trigger
   (#196/#197). Older hosts also lack the exec-approval hook entirely (#131/#132).
   *Status:* **open** — `scripts/check_compat.py` checks host *symbols* against real hosts and
   `tests/test_guarded_imports.py` checks optional imports, but nothing checks host
   *namespaces*, which is the variant that has bitten twice.
6. **A failure path that reports success.** A turn that delivered nothing was
   indistinguishable from one that did (#145/#186), an incomplete install reported "Already
   up to date" (#205), `update.sh` claimed a restart that never happened (#207), and a run the
   gateway reported as FAILURE kept the ✅ its own dispatch had placed — leaving the room with
   no answer and no hint that anything had gone wrong (#219).
   *Status:* **guarded** — `tests/test_silent_run_notice.py` (a failed, cancelled or silent run
   is marked and says so in the room), `tests/test_delivery_audit_wiring.py`,
   `tests/test_audit_delivery.py`. Report what happened, never what was supposed to happen.
7. **Documentation states behaviour the code does not implement.** See
   [Documentation Claims Are Tested](#documentation-claims-are-tested) — the fix is a test,
   not a rewrite.
   *Instances:* #199, #202, #207, #234.
   *Status:* **guarded** — `tests/test_readme_docs.py`, `tests/test_security_docs.py`,
   `tests/test_community_docs.py`, `tests/test_compat_floor.py`, `tests/test_ci_workflow.py`.
8. **The version must agree everywhere it is stated.** `zulip/version.py::__version__` is the
   source of truth; `zulip/plugin.yaml` repeats it for the gateway, and `SECURITY.md` and
   `CHANGELOG.md` repeat it for readers. Nothing reads one from another, so any of them can
   drift silently — and `checksums.txt` keeps covering the mismatch, because it hashes a
   file's bytes and not what they mean.
   *Instances:* none yet. Found by inspection while cutting v1.11.0, when `SECURITY.md` — the
   one of the four that *was* guarded — failed the release PR, and the other two would have
   shipped stale with CI green.
   *Status:* **guarded** — `tests/test_version.py::test_plugin_yaml_version_matches_version_module`
   and `test_changelog_records_this_version`, with `SECURITY.md` covered by
   `tests/test_security_docs.py::test_documented_versions_match_version_module`.

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
