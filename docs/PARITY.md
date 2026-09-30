# Parity: the Hermes and OpenClaw Zulip adapters

This repo (`zulip-hermes-integration`, Python) and its sibling
[`openclaw-zulip-bridge`](https://github.com/niyazmft/openclaw-zulip-bridge)
(TypeScript) implement the same Zulip adapter for two different agents. Parity
between them has been *accidental* — features crossed over only when someone
happened to notice, and drift ran both ways. This document makes the drift
visible instead of discoverable.

- **Machine-readable source of truth:** [`docs/parity-matrix.yaml`](parity-matrix.yaml)
- **Validator / drift reporter:** [`scripts/parity_check.py`](../scripts/parity_check.py)
- **CI:** [`.github/workflows/parity.yml`](../.github/workflows/parity.yml)
- **Tracking issue:** [#140](https://github.com/niyazmft/zulip-hermes-integration/issues/140) ·
  mirror [`openclaw-zulip-bridge#298`](https://github.com/niyazmft/openclaw-zulip-bridge/issues/298)

```bash
python3 scripts/parity_check.py            # validate + print the delta
python3 scripts/parity_check.py --json     # machine-readable report
python3 scripts/parity_check.py --self-test
```

The checker exits non-zero **only** on a schema error. Drift is reported but does
not fail the build — the matrix is a visibility tool, not a merge gate.

## How to read the matrix

Each capability and each config key carries one of three states:

| Status | Meaning |
|--------|---------|
| `both` | merged on `main` in **both** repos |
| `sister-only` | merged on `main` in the sibling (or a tracked fork), not yet here |
| `hermes-only` | merged on `main` here, **not verified** on `main` in the sibling |

**Status is keyed off merge state on `main`, never releases.** A feature can be
merged and unreleased on either side — the sibling's `v2026.9.1` pre-release
notes described work that was not in that tag — so "released" is not a safe proxy
for "merged". Update a status when the parity item lands (or is verified), not at
release time.

`hermes-only` means "present here, not verified there" — for a handful of items
(such as group-DM recipient handling, #154) the sibling has not been checked yet,
and the note says so. Verify on the sibling side before claiming `both`.

## Capability matrix

| Capability | Status | Hermes ref | Sister ref |
|------------|--------|------------|------------|
| Outbound message-length cap and chunking | `both` | `#126` | `config-schema.ts#maxMessageLength` |
| Relative markdown upload paths resolved against the realm base URL | `both` | `#126` | `uploads.ts` |
| Outbound secret guard (refuse to transmit host credentials) | `both` | `#136` | `secret-guard.ts` |
| HTTPS required for `ZULIP_SITE`, explicit insecure-http opt-in | `both` | `#137` | `config-schema.ts#allowInsecureHttp` |
| Upload denylist for credential / config / audit / session files | `both` | `#138` | `uploads.ts` |
| Progressive activity trace | `both` | `#139` | `activity-trace.ts` |
| Persistent dedupe store (TTL + LRU, debounced atomic save) | `both` | `dedupe_store.py` | `dedupe-store.ts` |
| Rotating JSON-lines audit log | `both` | `audit_logger.py` | `audit-logger.ts` |
| DM / group / chatmode traffic policy | `both` | `policy.py` | `policy.ts` |
| Emoji reaction status indicators | `both` | `reactions.py` | `reactions.ts` |
| Per-sender sliding-window rate limit | `both` | `rate_limiter.py` | `rate_limit_exceeded` |
| Group DM replies reach every recipient | `hermes-only` | `#154` | *unverified* |
| Audit delivery outcomes (`delivered` / `skipped+reason` / `empty` / `dispatched`) | `sister-only` | `#145` | `audit-logger.ts` |
| Host-compat and outbound-payload CI gates | `sister-only` | `#141` | `check-compat.js` |
| `SECURITY.md` threat model | `sister-only` | `#142` | `SECURITY.md` |
| History-aware context | `sister-only` | `#148` | `history-context.ts` |
| In-channel reaction triggers | `sister-only` | `#149` | `reaction-triggers.ts` |
| Actionable refs (validated clickable links) | `sister-only` | `#150` | `refs.ts` |
| Per-session message queue | `sister-only` | `#151` | `session-queue.ts` |
| Inbound hygiene (strip reasoning, cache names, drop own messages) | `sister-only` | `#152` | — |
| Soft gate + observe group | `sister-only` | `#153` | — |
| Sticky topic engagement | `sister-only` | `#155` | — |
| Settings / state scoped to the Hermes profile | `sister-only` | `#156` | — |
| Session archive repair for hosts without hard links | `sister-only` | — | `session-archive-repair.ts` |
| Native exec-approval buttons via the `zform` widget | `hermes-only` | `#131` | — |
| Admin actions (`/streams`, `/user`, `/pin`, `/unpin`) | `hermes-only` | `admin_actions.py` | *different action set* |
| Per-topic conversation sessions | `hermes-only` | `_topic_sessions_enabled` | — |
| Per-stream chatmode overrides (`ZULIP_STREAM_OVERRIDES`) | `hermes-only` | `_resolve_stream_overrides` | — |
| Out-of-process cron delivery (`standalone_sender_fn`) | `hermes-only` | `_standalone_send` | — |
| Self-updater with `checksums.txt` integrity manifest | `hermes-only` | `updater.py` | — |

### Config keys

The full env-to-camelCase table lives in `docs/parity-matrix.yaml` (51 entries).
Two findings worth calling out:

- **Derivable pairs dominate.** 29 knobs are `both`; most sibling names are the
  exact camelCase form of our `ZULIP_*` name (see §6).
- **Five knobs are code-only** — they are read by the plugin but missing from
  `zulip/plugin.yaml`, so onboarding never prompts for them and the manifest↔code
  parity check (#141) will flag them: `ZULIP_DM_POLICY`,
  `ZULIP_DM_SESSION_TURN_LIMIT`, `ZULIP_MAX_MESSAGES_PER_MINUTE`,
  `ZULIP_MAX_MESSAGE_LENGTH`, `ZULIP_BLOCK_STREAMING`.

Non-derivable pairs that the naming rule cannot reproduce and that must be
carried explicitly: `ZULIP_ALLOWED_USERS → allowFrom`, and the nested
`ZULIP_REACTION_* → reactions.*` group.

## Drift summary

At the last review (`last_reviewed: 2026-09-30`):

- **Sister-only (27 items):** the sibling/fork feature wave is well ahead on
  observability and inbound intelligence — delivery-outcome auditing (#145),
  history-aware context (#148), reaction triggers (#149), actionable refs (#150),
  per-session queue (#151), inbound hygiene (#152), soft gate (#153), sticky
  engagement (#155), profile-scoped state (#156) — plus the CI/security
  scaffolding (#141, #142) and session archive repair.
- **Hermes-only (14 items):** the interactive and operational surface — exec
  approval buttons, admin commands, per-topic sessions, stream overrides,
  standalone cron delivery, the self-updater — plus a small set of config knobs
  and the group-DM fix pending sibling verification.

---

# Shared adapter spec — draft v0.1.0

A language-agnostic spec both runtimes implement against. This is **draft**:
sections are settled incrementally, and the version moves when a section
changes semantics, not when a repo ships.

## 1. Scope and versioning

The spec covers the parts of the adapter that must agree for the two runtimes to
behave like one product to a Zulip user: message shaping, traffic policy, dedupe,
config naming, security rules, and the audit event schema. It does **not**
constrain host-specific plumbing (how each agent scopes sessions, tool calling,
or delivery). `spec_version` in `parity-matrix.yaml` tracks this document.

## 2. Host vocabulary mapping

The gateway and the adapter speak different vocabularies. Outbound routing uses
the **host** key; the Zulip-side names are event-side only.

| Concept | Zulip side | Host / outbound side |
|---------|------------|----------------------|
| Topic | event `subject`; adapter event metadata `topic` | send metadata **`thread_id`** |
| Stream | `stream_id` | `chat_id` (numeric string) |
| DM | `dm:<user_id>[,<user_id>…]` | same form |
| DM session epoch | — | `:session:N` suffix, parsed by the adapter |

Rules:

- Every outbound path **must read the host key** (`thread_id`) first. The
  per-stream topic cache is a fallback only. Reading `metadata.get("topic")`
  directly reintroduced #143 (a reply landing in the last-active topic); all
  outbound read sites funnel through `_metadata_topic()` /`_metadata_topic`-style
  helpers for exactly this reason.
- A **new** outbound path is the risk surface: replies, typing start/stop,
  exec-approval prompts and media sends all resolve the topic this way today, and
  a new path must not regress to the cache.
- The sibling hits the same class of problem from the other direction: its hook
  payloads identify the *agent run*, not the room, so it maintains correlation in
  the plugin. **The plugin owns room correlation**; the host owns run identity.

## 3. Traffic-policy semantics

| Knob | Values | Default | Notes |
|------|--------|---------|-------|
| `chatmode` | `onmessage`, `oncall`, `onchar` | `onmessage` | per-stream overrides are Hermes-only |
| `onchar` prefixes | list | `>`, `!` | only meaningful in `onchar` |
| `requireMention` | bool | `true` | stream messages without a trigger are dropped |
| `dmPolicy` | `open`, `allowlist`, `pairing`, `disabled` | `open` | `pairing` codes live 24 h |
| `groupPolicy` | `open`, `allowlist`, `disabled` | `open` | no `pairing` mode for streams |

Semantics both runtimes share:

- In `oncall`/`onchar`, a stream message that is neither a mention nor a trigger
  is dropped **silently** (debug log only).
- `dmPolicy: pairing` issues a short code and only promotes an email to the
  allowlist on explicit approval; the code expires after 24 h.
- The sibling requires `allowFrom` to include `*` when `dmPolicy: open`; treat
  that as a validation rule to add, not a divergence to preserve.

## 4. Dedupe TTL

| Property | Value |
|----------|-------|
| TTL | 5 min (`300000` ms) |
| Max entries | 2000 |
| Eviction | LRU, expired entries pruned on insert |
| Persistence | `data_dir/zulip_dedupe_<safe_account>.json`, owner-only (`0600`) |
| Write policy | debounced 5000 ms; atomic temp-file + `os.replace` |

Both runtimes already match these values; the spec exists so the next change to
the TTL is made in both, not one.

## 5. Security rules

### 5.1 Upload allowlist and denylist

Evaluation order matters: **denylist first**, then allowlist roots.

- **Allowlist roots:** the temp dir, the plugin data dir, and operator dirs from
  `HERMES_MEDIA_ALLOW_DIRS`. Files must resolve *under* a root; symlinks are
  refused (`O_NOFOLLOW`, atomic inode check) to close the TOCTOU swap.
- **Denylist (name-based, before roots):** any path component in
  `{credentials, sessions, transcripts}`; a basename containing a
  credential-shaped fragment (`credential`, `api_key`, `apikey`, `api-key`,
  `secret`, `token`, `passwd`, `password`); explicit patterns for `.env`,
  `*.audit.log[.N]`, `zulip_allowlist.json`, `zulip_dedupe_*.json`,
  `zulip_queue_*.json`.
- **Why a name check and not just roots:** the data dir is itself an allowed
  root, so a root check can never refuse the files that matter most — the audit
  log, the persisted allowlist, and the queue/dedupe state. The threat is a
  prompt-injected agent attaching a config file. A false refusal costs one upload
  plus an audit line; a false accept publishes a credential.

### 5.2 Outbound secret guard

- `MIN_SECRET_LENGTH = 12`; values shorter than this are never treated as
  credentials, so ordinary prose is never blocked.
- Credential-shaped key regex (identical in both repos):
  `(api[_-]?key|apikey|token|secret|passwd|password|credential)` (case-insensitive).
- Sources: the host config tree (walk depth ≤ 6), credential-shaped environment
  variables, and explicit `extra` pairs the caller knows are sensitive.
- **Two modes:** `find_leaked_secrets` is the send choke point (refuse the
  message); `redact_secrets` covers writes that bypass `send` (notably in-place
  activity-trace edits), replacing values with `[redacted]` so a status message
  never goes stale *and* never leaks.
- **Never log or audit a value** — only the *name* of the source key.

### 5.3 HTTPS policy

- `ZULIP_SITE` must be `https://` on a public host by default. The bot API key
  is HTTP Basic on **every** request, so a plain-http realm puts it on the wire
  in cleartext.
- `ZULIP_ALLOW_INSECURE_HTTP` / `allowInsecureHttp` relaxes the **scheme and the
  private-host checks together** — a LAN Zulip is itself a private address, so
  relaxing only one would still reject the case the opt-in exists for.
- Read the opt-in **where the decision is made** (URL normalization), not from a
  value threaded by hand: the sibling had a bug where the option was dropped on
  re-normalization, so a realm that validated at setup failed at runtime.
- **Inbound media URLs are different:** their host can be influenced by an
  inbound message, so media-URL validation stays strict regardless of the opt-in.

## 6. Config key naming derivation

Rule: a shared knob is named `ZULIP_<SCREAMING_SNAKE_CASE>` as an env var
(declared in `zulip/plugin.yaml` as `requires_env`/`optional_env`) and
`<lowerCamelCase>` in the sibling's `channels.zulip.*` zod schema. The camelCase
name is the derivation of the env name:

```
ZULIP_ALLOW_INSECURE_HTTP   ->  allowInsecureHttp
ZULIP_MEDIA_MAX_MB          ->  mediaMaxMb
ZULIP_BLOCK_SECRET_LEAKS    ->  blockSecretLeaks
ZULIP_MAX_MESSAGE_LENGTH    ->  maxMessageLength
```

Algorithm: strip `ZULIP_`, lowercase, split on `_`, join as lowerCamelCase
(first part lower, the rest capitalized). `scripts/parity_check.py` enforces
this for every entry marked `derivable: true`.

Exceptions the rule cannot reproduce — listed explicitly in the matrix with
`derivable: false`:

| Hermes env | Sibling key | Why not derivable |
|------------|-------------|-------------------|
| `ZULIP_ALLOWED_USERS` | `allowFrom` | renamed concept |
| `ZULIP_REACTION_*` | `reactions.enabled` / `.clearOnFinish` / `.onStart` / `.onSuccess` / `.onError` | nested object |
| `ZULIP_SITE` | `site` (also `url`, `realm`) | aliases |

Two consequences are tracked as parity items, not papered over:

- A knob read by code but absent from `plugin.yaml` is invisible to onboarding —
  five such knobs exist today (§Capability matrix). This is the manifest↔code
  parity gap (#141).
- `ZULIP_TEXT_CHUNK_LIMIT → textChunkLimit` and
  `ZULIP_MAX_MESSAGE_LENGTH → maxMessageLength` are *different* knobs that both
  exist on both sides; do not fold them together when porting (#126 got this
  right).

## 7. Audit event schema

One JSON object per line, in `data_dir/audit/<account>.audit.log`, rotation at
1 MB keeping 3 rotated files. Canonical shape:

```json
{
  "ts": "2026-09-30T12:34:56Z",
  "event": "policy_block",
  "account": "<account id>",
  "details": { "sender_id": "…", "reason": "…", "kind": "dm" }
}
```

| Field | Type | Notes |
|-------|------|-------|
| `ts` | string | ISO-8601 UTC, seconds precision, `Z` suffix |
| `event` | string | snake_case event name (catalogue below) |
| `account` | string | canonical account id key |
| `details` | object | event-specific; never contains a credential value |

**Known drift to converge (this is the point of the schema):** the Hermes logger
emits `account_id` (snake_case) and nests everything under `details`; the sibling
emits `accountId` (camelCase) and spreads event-specific keys at the top level,
and its `ts` is ISO but its Hermes counterpart currently omits the `Z`. Pick the
canonical shape above and migrate both.

Event catalogue (shared names first):

| Event | Meaning |
|-------|---------|
| `monitor_start` / `monitor_stop` | poll loop lifecycle (`stop` carries `reason`) |
| `auth_failure` | credentials rejected |
| `rate_limit_exceeded` | per-sender rate limit hit (`sender_id`, `limit`) |
| `policy_block` | DM/group policy refused a sender (`sender_id`, `reason`, `kind`) |
| `recovery_attempt` | session/queue recovery kicked in |
| `secret_leak_blocked` | outbound message refused (`direction`, `sources` names, `count`) |
| `media_upload_blocked` | upload refused by the denylist (`file` masked, `reason`, `direction`) |
| `activity_trace_recovered` | an interrupted trace was finalized (`chat_id`, `topic`, `message_id`, `finalized`) |
| `delivery_outcome` | **proposed** (#145): `delivered` / `skipped:<reason>` / `empty` / `dispatched` |

---

## Maintaining this matrix

1. When a parity item lands, flip its `status` in `docs/parity-matrix.yaml` and
   update the rendered table above in the same PR.
2. Run `python3 scripts/parity_check.py` — it validates the schema and prints the
   new delta.
3. The `parity.yml` workflow runs the same check on every PR that touches these
   files, on push to `main`, and weekly, so drift shows up even when no one edits
   the matrix.
4. A non-empty delta is not a failure. It is a prompt: port the feature, file it,
   or record why the divergence is intentional.

## Appendix — reconciliation with issue #140

Issue #140's refinement listed a "Sister-only" seed set (#136, #137, #138, #139,
#145, #148–#156). Four of those (#136 secret guard, #137 HTTPS, #138 denylist,
#139 activity trace) have since **merged here** (#171, #172, #173), and #154
(group DM recipients) merged here in #169. Because the matrix keys off merge
state on `main`, they are classified `both` / `hermes-only` above rather than
`sister-only` — precisely the drift detection the issue asked for. The remaining
items keep the issue's `sister-only` classification.
