# Security Policy

This document describes the security model of the **Zulip plugin for Hermes**
(`niyazmft/zulip-hermes-integration`), what the plugin actually guarantees, and —
just as importantly — what it does **not**.

Every claim below is meant to be checkable against the source. Where a claim maps
to code, the module and symbol are named (`zulip/adapter.py::ZulipAdapter.send`).
If this file and the code disagree, the code is wrong *and* this file is wrong;
please report either as a security issue.

> **Sibling repo warning.** The TypeScript sibling (`openclaw-zulip-bridge`) has
> its own `SECURITY.md`. The two share a thesis, not an implementation. Do not
> assume a control documented there exists here, or vice versa. This file
> documents the Python/Hermes adapter only.

---

## Supported Versions

| Component | Supported version |
|-----------|-------------------|
| Plugin | `1.9.2` — the current release; security fixes land on the latest release and `main` (`zulip/version.py::__version__`, `__repo__`) |
| Hermes gateway | `>= 0.18.2` (`zulip/version.py::__min_hermes__`) |

Security patches are applied to the latest release only. Older plugin versions are
not maintained. If you run a fork or an unreleased commit, state the exact commit
in any report.

### Version-gated features

Some capabilities require a newer gateway than the minimum. On an older gateway
the feature degrades gracefully; the plugin still loads and runs.

| Feature | First supported gateway | Behaviour below it |
|---------|-------------------------|--------------------|
| Native exec-approval buttons (Zulip `zform` widget) | Hermes `0.21.3` (`v2026.9.14`) | The gateway never calls the hook; approval prompts use the gateway's **plain-text** `/approve` / `/deny` instructions (`zulip/adapter.py`, guarded import of `gateway.platforms.base.ExecApprovalPrompt`). |
| Activity trace (`ProcessingOutcome` lifecycle hooks) | introduced alongside `ExecApprovalPrompt` | The hooks are never called, so the trace does not run (`zulip/adapter.py`, guarded import of `ProcessingOutcome`). |
| Per-task session context (`HERMES_SESSION_*`) | gateways that publish `gateway.session_context` | Tool steps cannot be attributed to a session and are dropped, never guessed (`zulip/adapter.py`, guarded import of `get_session_env`). |

The exec-approval widget maps each button to the same `/approve`-family message a
user would type, so authorization is identical either way
(`zulip/adapter.py::ZulipAdapter._EA_ZFORM_REPLY`).

---

## Reporting a Vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

Please use GitHub's **private vulnerability reporting** for this repository
(Security → *Report a vulnerability* on the GitHub repo page). If you cannot use
that channel, contact the maintainer directly on GitHub: **[@niyazmft](https://github.com/niyazmft)**.

Please include:

- plugin version (`zulip/version.py::__version__`) and commit,
- Hermes gateway version,
- a minimal reproduction,
- the concrete impact (what an attacker gains, and from what position).

**Response expectation:** an acknowledgement within **48 hours**. If you do not
hear back, please ping the maintainer again. We will coordinate disclosure with
you and credit you unless you ask otherwise.

---

## Security Model

The plugin is a **Hermes platform adapter** loaded *in-process* by the gateway
(`zulip/adapter.py::register`). It receives `MessageEvent`s, hands them to the
gateway's agent, and sends the agent's replies back to Zulip. It is a transport
and policy layer, **not a sandbox**.

Trust boundaries:

- The **operator** chooses the Zulip realm, bot credentials, DM/stream policies
  and rate limits, and controls the gateway's tool policy.
- The **gateway** runs the agent and owns tool/filesystem policy.
- The **plugin** decides whether a given inbound message is delivered to the
  agent, and whether an outbound message is transmitted.

A user who can already make the agent run tools or read host files operates
*inside* the gateway's trust boundary. The plugin can constrain what leaves
through *this* platform; it cannot revoke what the agent was already allowed to
do. See [Explicit Non-Guarantees](#explicit-non-guarantees).

---

## Credential Handling

### Credentials the plugin uses

| Credential | Environment variable | Config field | Purpose |
|------------|----------------------|--------------|---------|
| Bot API key | `ZULIP_API_KEY` | `api_key` | Authenticates every Zulip API request |
| Bot email | `ZULIP_EMAIL` | `email` | Identifies the bot account |
| Realm URL | `ZULIP_SITE` | `site` | Zulip server endpoint |

### Resolution order

For the single-account path, **environment variables take precedence over the
platform config/extra**:

```python
self.api_key = os.getenv("ZULIP_API_KEY") or extra.get("api_key", "")
```

(`zulip/adapter.py::ZulipAdapter.__init__`; same precedence in
`zulip/accounts.py::AccountResolver._single_account`.) Put the API key in the
environment, not a config file, so an agent that reads config files does not
automatically read the key as well.

`zulip/accounts.py::AccountResolver` can parse a multi-account `accounts:` map,
but **the adapter itself is single-account** — it reads one `api_key`/`email`/
`site` at construction and the resolver is not wired into `register()`. Treat
multi-account as unshipped.

### Transmission

Zulip authenticates with **HTTP Basic** (`Authorization: Basic base64(email:api_key)`),
so the API key is sent on **every** request. The base URL is therefore required
to be `https://` and a public host:

- `zulip/probe.py::_normalize_base_url` rejects non-HTTP(S) schemes, `http://`,
  internal/private address literals, and `localhost` by default.
- The adapter validates `ZULIP_SITE` at construction and refuses to start on a
  bad value (`zulip/adapter.py::ZulipAdapter.__init__`, Issue #137).
- `ZULIP_ALLOW_INSECURE_HTTP=1` is an explicit operator opt-in for a self-hosted
  realm on a trusted network; it relaxes the scheme check and the
  private/localhost check **together**, and logs a warning when used
  (`zulip/probe.py::allow_insecure_http_enabled`, `_normalize_base_url`).

### `mask_pii()` — what it does and does not cover

`zulip/logger.py::mask_pii` is a **best-effort, pattern-based** redactor applied
at log call sites. It recognises:

- emails → `n***@domain.com`
- numeric IDs → length-aware (`12***78`)
- API-key-shaped strings (32+ alphanumeric, or 40+ base64) → `abcd***wxyz`
- IPv4 / IPv6 literals
- prefixed values (`user:`, `dm:`, `zulip:`, `stream:`)

It does **not** know the *values* of your credentials, and it is not applied to
every string automatically — it only protects the fields a call site chooses to
wrap. A credential pasted into free-form message text is **not** covered by
`mask_pii`; that is the job of the outbound secret guard below.

### Outbound secret guard (Issue #136)

`zulip/secret_guard.py` is the last hop before a message reaches Zulip. It
collects credential-shaped values from:

- the platform config tree (keys matching
  `(api[_-]?key|apikey|token|secret|passwd|password|credential)`,
  `SECRET_KEY_PATTERN`),
- credential-shaped environment variables,
- the adapter's own `api_key`, registered explicitly
  (`zulip/adapter.py::ZulipAdapter._known_secrets`),

ignoring any value shorter than `MIN_SECRET_LENGTH` (12 chars). When a known
value appears verbatim in outbound content, `find_leaked_secrets` matches it and
the send is **refused**:

- `zulip/adapter.py::ZulipAdapter.send` (normal delivery), and
- `zulip/adapter.py::_standalone_send` (out-of-process cron delivery).

Both fail closed and audit the refusal as `secret_leak_blocked`. The audit line
names only **where** the value came from (`sources`, e.g. `env.ZULIP_API_KEY`) and
**never the value** (`zulip/secret_guard.py::describe_leaked_secrets`). The guard
is enabled unless `ZULIP_BLOCK_SECRET_LEAKS` is set to a falsey value.

`zulip/secret_guard.py::redact_secrets` exists as a defence-in-depth alternative
for writes that bypass `send()`, and is unit-tested, but it is **not currently
called by the activity-trace edit path** — see
[Explicit Non-Guarantees](#explicit-non-guarantees).

### What appears in logs vs the audit log

| Destination | Contents | Controls |
|-------------|----------|----------|
| Process logs (`logging`, `zulip.*` namespaces; format `message [k=v k=v]` via `zulip/logger.py::format_zulip_log`) | Operational messages: connection, polling, drops, rate-limit warnings, send/upload failures. Sensitive fields are wrapped in `mask_pii`. | The host's logging configuration (level, sink, retention). |
| Audit log (`zulip/audit_logger.py::AuditLogger`) | JSON-lines security events (see below), plus best-effort write-failure warnings on the process log. | Plugin-owned; see rotation below. |

The audit log path is `{HERMES_DATA_DIR or ~/.hermes}/audit/{account_id}.audit.log`,
where `account_id` is the bot email (or `default`) — set in
`zulip/adapter.py::ZulipAdapter.__init__`. It rotates at 1 MB, keeping the last 3
rotated files (`zulip/audit_logger.py`, `MAX_FILE_SIZE`, `MAX_ROTATED_FILES`).

### File permissions

State files the plugin persists are written with **`0600`**:

- DM/stream allowlist — `{data_dir}/zulip_allowlist.json`
  (`zulip/policy.py::PolicyEngine._save_to_disk`),
- queue state (`zulip/queue_manager.py`),
- dedupe state (`zulip/dedupe_store.py`).

The **audit log is created with the process umask**, not an explicit `chmod`
(`zulip/audit_logger.py::AuditLogger.log` opens the file for append). On a host
where `HERMES_DATA_DIR` is broader than the service account, tighten the
directory permissions yourself.

---

## Upload and Allowlist Guarantees

`zulip/media.py::upload_file_to_zulip` is the only path that attaches a local
file to a Zulip message. It applies, in order:

1. **Explicit name-based denylist, before any allowlist check.**
   `zulip/media.py::deny_reason` refuses:
   - any path component named `credentials`, `sessions` or `transcripts`;
   - a basename containing `credential`, `api_key`, `apikey`, `api-key`,
     `secret`, `token`, `passwd` or `password`;
   - a basename matching `*.env`/`.env*`, `*.audit.log*`,
     `zulip_allowlist.json`, or `zulip_(dedupe|queue)_*.json`.

   A refusal raises `SensitiveUploadRefused` and is recorded as a
   `media_upload_blocked` audit event (`zulip/media.py::_audit_refused_upload`).
   This exists precisely because the data dir is *itself* an allowed root that
   holds the audit log and persisted state, so a root check alone can never
   refuse them (Issue #138). The denylist also overrides operator allow dirs: an
   allow dir cannot authorise a credential file.

2. **Atomic symlink rejection.** The file is opened with
   `os.O_RDONLY | os.O_NOFOLLOW`, so a symlink is refused by the kernel in the
   same syscall that opens it — no `lstat`-then-`open` TOCTOU window
   (`zulip/media.py::upload_file_to_zulip`, `_resolve_candidate`).

3. **Root containment.** The realpath must be a true descendant of an allowed
   root. Containment uses `Path.relative_to`, not string-prefix matching, so
   `/tmp/allowed-but-not-really` is not accepted under `/tmp/allowed`
   (`zulip/media.py::_is_within`). Allowed roots are:
   - the system temp dir,
   - the plugin data dir (`HERMES_DATA_DIR`),
   - the bot workspace, `<tmp>/hermes_bot_workspace`,
   - every dir in `HERMES_MEDIA_ALLOW_DIRS`.

4. **Local paths only.** Values in `media_files` that look like `http(s)://`
   URLs are rejected before upload (`zulip/adapter.py::ZulipAdapter.send`,
   `_standalone_send`).

### `HERMES_MEDIA_ALLOW_DIRS` semantics

`HERMES_MEDIA_ALLOW_DIRS` is a **defense-in-depth** layer, not the primary gate.
It is the same operator allowlist the gateway core already honours
(`gateway.platforms.base._media_delivery_allowed_roots` /
`filter_media_delivery_paths`). By the time a send reaches
`upload_file_to_zulip`, the gateway has already run its own allowlist **and**
denylist. The plugin:

- accepts a **colon- or comma-separated** list of absolute directories
  (`os.pathsep` or `,`), skipping empty/dud entries;
- resolves entries with `expanduser().resolve()`;
- deliberately must **not be stricter** than the gateway's own decision, or an
  operator-approved path (e.g. a screenshot cache dir) would be silently dropped
  a second time (comment in `zulip/media.py::upload_file_to_zulip`).

Because it is a *root* allowlist, it does not by itself prevent a sensitive file
that happens to live under an allowed root — that is what the name-based denylist
in step 1 is for.

---

## SSRF Filtering (`zulip/probe.py`)

### Base-URL validation

`zulip/probe.py::_normalize_base_url` is applied to `ZULIP_SITE` during the
health probe (`probe_zulip`) and at adapter construction
(`zulip/adapter.py::ZulipAdapter.__init__`). It rejects:

- any scheme other than `http`/`https` (`file://`, `gopher://`, …),
- `http://` unless `ZULIP_ALLOW_INSECURE_HTTP=1` (cleartext API key),
- host literals matching `_PRIVATE_PREFIXES` (`127.`, `10.`,
  `172.16.`–`172.31.`, `192.168.`, `169.254.`, `0.`, `255.`),
- the AWS metadata IP `169.254.169.254`,
- `localhost` / `localhost.localdomain`.

**Precision — what this is *not*.** `zulip/probe.py::_is_internal_host` is
**hostname string-prefix matching, not DNS resolution**. It does not catch:

- encoded IP forms (`0x7f000001`, decimal `2130706433`, IPv6-mapped
  `[::ffff:127.0.0.1]`),
- a public DNS name that resolves to a private address,
- IPv6 private ranges.

The `ZULIP_ALLOW_INSECURE_HTTP` opt-in relaxes the scheme **and** the
private/localhost checks together, and logs a warning when it does. That is by
design — a LAN Zulip *is* a private address — but it means the opt-in is a real
reduction in transport safety, not a no-op.

### Media-URL validation

`zulip/probe.py::_validate_media_url` accepts only `http(s)` and rejects
internal hosts and `localhost`; it is unit-tested
(`tests/test_probe.py`, `tests/test_secure_transport.py`).

**Precision.** `_validate_media_url` and the inbound download helper
`zulip/media.py::download_upload` (which additionally pins the request to the
configured Zulip origin and a `/user_uploads/` path) are **not currently called
by the adapter's inbound message path**. Today inbound Zulip uploads are not
fetched; message HTML is reduced to text
(`zulip/adapter.py::_handle_message` → `strip_html_to_text`) and handed to the
agent. Treat this filtering as protecting future/other callers, **not** as a live
inbound control. The same hostname-matching limitation as above applies.

---

## Audit-Event Coverage

Events are written as JSON lines by `zulip/audit_logger.py`.

**Emitted today** (each with a calling site):

| Event | Meaning | Emitted from |
|-------|---------|--------------|
| `rate_limit_exceeded` | A sender exceeded the per-sender window | `zulip/adapter.py::_handle_message` |
| `policy_block` | Stream (`kind=stream`) or DM (`kind=dm`) denied by policy | `zulip/adapter.py::_handle_message` (two call sites) |
| `secret_leak_blocked` | Outbound message refused for containing a host credential | `zulip/adapter.py::ZulipAdapter._refuse_secret_leak`, `_standalone_send` |
| `media_upload_blocked` | An upload was refused by the name-based denylist | `zulip/media.py::_audit_refused_upload` |
| `activity_trace_recovered` | An interrupted trace message was finalized after restart | `zulip/adapter.py` (trace recovery path) |

**Defined but never emitted** (helpers exist, no call site — do not treat these
as coverage): `monitor_start`, `monitor_stop`, `auth_failure`
(`zulip/audit_logger.py::log_monitor_start`, `log_monitor_stop`,
`log_auth_failure`).

### Gaps — what is *not* audited

- **Delivery outcomes.** Whether a reply/upload actually reached Zulip is not
  audited today; send failures surface on the process log only (tracked in
  Issue #145). "No `secret_leak_blocked` line" therefore does **not** imply a
  message was delivered.
- **Non-policy message drops.** Stream drops for "no trigger" (chatmode/mention
  gating), stream-filter misses, and self-message filtering are process-log
  `debug` lines, not audit events (`zulip/adapter.py::_handle_message`).
- **Allowlist / symlink refusals.** Only the *name-based denylist* refusal is
  audited. An upload refused because its path was outside an allowed root, or
  because it was a symlink, raises `ValueError` and is **not** audited
  (`zulip/media.py::upload_file_to_zulip`).
- **Recovery re-dispatch.** `zulip/recovery.py::recover_interrupted_messages`
  logs to the process log; it does not emit audit events.
- **Trace edits.** Activity-trace message edits go through the SDK directly, not
  through `send()`, and are not scanned by the secret guard.

Audit write failures are logged as a warning rather than silently dropped
(`zulip/audit_logger.py::AuditLogger.log`), but the audit log is a **local,
mutable file** — it is not append-only to a remote sink and is not tamper-proof.

---

## Explicit Non-Guarantees

These are the limits a reader must not assume away:

1. **The plugin cannot stop an agent from *reading* a config file or the
   environment.** Only the gateway's tool policy can. The outbound secret guard
   (`zulip/secret_guard.py`) stops the value being **transmitted**; it does not
   prevent the read (Issues #136/#137). If an agent is allowed to read a file
   holding a credential, the plugin cannot put that genie back in the bottle —
   it can only refuse to relay a message containing a value it recognises as a
   credential.
2. **The secret guard only catches *known* values.** It matches exact substrings
   of credentials collected from config, environment, and the adapter's own key,
   ignoring values under 12 characters. A credential read from some other source,
   a short secret, or a transformed encoding (base64, reversed, split across
   messages) is not caught. Disabling `ZULIP_BLOCK_SECRET_LEAKS` removes even
   that.
3. **The activity trace is not secret-scanned.** Trace status messages (including
   agent-authored `zulip_progress` notes) are posted/edited through the SDK, not
   through `send()`. `redact_secrets()` exists for this case but is not wired in
   yet.
4. **`mask_pii()` is not a data-loss-prevention guarantee.** It masks fields at
   chosen call sites by pattern; it is not applied to all output and does not
   recognise arbitrary secret values.
5. **The SSRF checks are hostname/string based, not resolver based** (see
   [SSRF Filtering](#ssrf-filtering-zulipprobepy)).
6. **The rate limiter is in-memory, per process.** It resets on restart and is
   not shared across processes or profiles (`zulip/rate_limiter.py`).
7. **The plugin is not a sandbox for the agent's tools, filesystem, network, or
   memory.** Those are the gateway's and operator's responsibility.

---

## Multi-User Data Isolation

### Direct messages — isolated per user (by default)

A DM's chat id encodes its recipient set:
`dm:<user_id>[,<user_id>…]` (`zulip/adapter.py::_private_chat_id`). The gateway
scopes session state by that id, so **one-to-one DMs get per-user sessions**. A
group DM carries *every* recipient in the id, so the group shares one session —
by design, not a bug.

Long DM conversations are rotated: after `ZULIP_DM_SESSION_TURN_LIMIT` turns
(default 20) the chat id gains a `:session:<n>` suffix, which starts a fresh
agent session and resets context (`zulip/adapter.py::_handle_message`).

### Streams — shared by design

For streams the chat id is the **stream id**, so by default every topic in a
stream maps to one session and all participants share that agent context and
memory. This is intentional for a teammate-style bot.

Operators can opt into per-topic sessions with `ZULIP_TOPIC_SESSIONS=1`, which
puts the topic into the event's `thread_id`; the gateway then scopes sessions per
topic (`zulip/adapter.py::_topic_sessions_enabled`). Even then, a topic is
**shared** by everyone posting in it — it is a conversation boundary, not a
per-user one.

Shared-topic engagement, group "observe" mode and self-message filtering are
tracked in Issues #155, #153 and #152.

### Host-global scope is the operator's decision

The plugin does not partition the host's memory, tools, or filesystem per Zulip
user. Anything an agent can reach through the gateway is reachable regardless of
which user is talking. Restricting that is a gateway/tool-policy decision.

### Profile multiplexing caveat (Issue #156)

Settings such as `ZULIP_DM_POLICY`, `ZULIP_ALLOWED_USERS`,
`ZULIP_GROUP_POLICY`, `ZULIP_TOPIC_SESSIONS` and `HERMES_DATA_DIR` are read from
**process environment and a single data directory**, and the adapter is
instantiated once per configured platform entry. If a host multiplexes multiple
Hermes profiles through one process, or shares `HERMES_DATA_DIR` between them,
these settings (and the persisted allowlist / audit log) are **not isolated per
profile**. Running profiles as separate gateway processes with distinct data
dirs is the safe configuration until per-profile isolation is specified in
Issue #156.

---

## Operationally Recommended

1. **Use a dedicated bot account** with the minimum realm role it needs.
2. **Put the API key in the environment**, not a config file an agent might read.
3. **Keep HTTPS.** Only set `ZULIP_ALLOW_INSECURE_HTTP=1` for a trusted,
   self-hosted realm, and understand it also permits private/localhost hosts.
4. **Restrict DMs and streams.** Prefer `ZULIP_DM_POLICY=allowlist` or
   `pairing`, and set `ZULIP_GROUP_POLICY`/`ZULIP_ALLOWED_USERS` rather than
   leaving `open` — the default is permissive for backward compatibility.
5. **Leave the secret guard on** (`ZULIP_BLOCK_SECRET_LEAKS` unset/true).
6. **Tighten the data dir.** `chmod 700` `HERMES_DATA_DIR` so the `0600` state
   files and the umask-created audit log are not world-readable.
7. **Set a rate limit** (`ZULIP_MAX_MESSAGES_PER_MINUTE`, default 60 per sender).
8. **Review the audit log** at `{HERMES_DATA_DIR}/audit/` — and remember the
   [gaps](#gaps--what-is-not-audited) above when reading it.
