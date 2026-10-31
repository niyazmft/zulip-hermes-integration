# Device Matrix

Two devices are kept on purpose, in **different states**, because the interesting
question is not "does it work on the latest everything" — it is "does it still
work for the person who upgraded one half and not the other".

| Cell | Hermes | Plugin | Covered by |
|------|--------|--------|------------|
| **new / new** | 0.22.5 | 1.15.0 | `lab-hermes` |
| **old / old** | 0.20.0 | 1.13.0 | `8x` |
| old / new | 0.20.0 | latest | `8x`, after a plugin-only update (the common end-user state) |
| new / old | 0.22.5 | older | `lab-hermes` with a pinned `__version__`, rare |

A mismatch is a **supported state**, not a bug. The plugin's declared floor is
`__min_hermes__ = 0.18.2`, and below 0.21.3 the exec-approval buttons are simply
absent — the plugin degrades to plain text rather than failing.

## Why 8x stays behind

8x is the only fixture that exercises the degraded paths: 0.20.0 has no
`_send_exec_approval_prompt`, no `ExecApprovalPrompt`, no `supports_exec_approval_buttons`,
no `_accepts_kwarg`, and no `MessageEvent.media_text_inlined` (so inbound files cannot be
opened by the agent). Keeping it old is what proves those are treated as
*capabilities* rather than *requirements* — the failure that once made a true floor
look false (#230). Upgrading 8x's host would delete that coverage.

## Checking a device

```bash
bash scripts/verify_device.sh docker:lab-hermes
bash scripts/verify_device.sh ssh:8x
bash scripts/verify_device.sh --matrix        # both, with the cell named
```

The script is read-only with respect to the deployed plugin: it stages the working
tree under a scratch directory and runs the real-host gate there. It never rsyncs
over the installed plugin and never restarts the gateway.

What it proves, in order:

1. **identity** — the plugin version installed, the host version the CLI reports
2. **stage** — the working tree reaches the device (unique scratch dir, so a stale
   root-owned staging directory cannot wedge the next run)
3. **import preflight** — the tree imports against *that* host, before anything is deployed
4. **compat gate** — `scripts/check_compat.py`, exit code captured directly
5. **host version cross-check** — the gate's own detection must agree with
   `hermes --version`. A container-flattened install reports `0.0.0` from
   `importlib.metadata` while its CLI says `0.22.5`; grading the first one is how a
   verification becomes unreproducible
6. **deployed integrity** — only meaningful when the device claims the same version
   as the tree *and* the tree is clean; otherwise it says why it is not comparable
7. **startup log** — anchored on the last `Starting Hermes Gateway` banner, and it
   prints the last `zulip …` lines rather than declaring silence a success

## Migration path, in practice

Upgrading is the plugin's own version-gated updater, not a git pull: bump
`__version__`, regenerate `checksums.txt`, tag a release, then run `update.sh` on the
device. Pushing to `main` deploys nothing. A device that is several releases behind is
only safe to update when its on-disk `updater.py` parses the manifest out of the
release *being installed* (#205) — `scripts/verify_device.sh` reports a behind device
rather than failing it, so an intentional lag is visible instead of alarming.

Order for a release:

1. `bash scripts/verify_device.sh --matrix` on a clean tree at the new version
2. cut the release
3. update `lab-hermes`, re-verify — the new/new cell
4. update `8x`'s **plugin only**, re-verify — the old/new cell
5. leave `8x`'s Hermes where it is unless the floor itself moves
