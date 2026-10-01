# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.9.2] - 2026-09-20

### Added
- **Native exec-approval buttons (zform widget)**: exec-approval prompts for dangerous commands now render as clickable **Allow Once / Allow Session / Always Allow / Deny** buttons in Zulip web/desktop. `ZulipAdapter` overrides the gateway's `_send_exec_approval_prompt` hook — which is what flips `supports_exec_approval_buttons()` to native-button mode — and attaches a `zform` choices widget via the message-send `widget_content` parameter (the same mechanism the official `trivia_bot` uses). Each button's canned reply is the equivalent plain-text command (`/approve`, `/approve session`, `/approve always`, `/deny`), so a click resolves through the existing authorization path with identical permissions. The approval text (command + reason) is sent as a separate message because a rendered widget replaces its own message body; clients without widget support show both messages as plain text. Requires **Hermes ≥ 0.21.3**. ([#131](https://github.com/niyazmft/zulip-hermes-integration/pull/131), fixes [#130](https://github.com/niyazmft/zulip-hermes-integration/issues/130))

### Fixed
- **Plugin failed to load on gateways predating the exec-approval hook**: `ExecApprovalPrompt` was imported unconditionally, so on Hermes ≤ 0.21.2 the entire adapter raised `ImportError` instead of merely disabling the new buttons (reproduced on a Hermes 0.20.0 gateway). The type is now imported defensively — older gateways keep the plain-text `/approve` prompt and load normally. ([#132](https://github.com/niyazmft/zulip-hermes-integration/pull/132))
- **Stale `checksums.txt`**: `adapter.py` changed without regenerating `checksums.txt`, which would have made `zulip update` abort with a checksum mismatch for every user. ([#132](https://github.com/niyazmft/zulip-hermes-integration/pull/132))

### Docs
- **Gateway compatibility**: README table documenting the Hermes ≥ 0.21.3 requirement for native approval buttons and the plain-text fallback on older gateways. ([#132](https://github.com/niyazmft/zulip-hermes-integration/pull/132))

### Internal
- **CI verifies `checksums.txt`**: CI now regenerates the checksums exactly as `.githooks/pre-push` does and fails if the committed file is stale, closing the gap that let [#131](https://github.com/niyazmft/zulip-hermes-integration/pull/131) ship a broken updater manifest. ([#133](https://github.com/niyazmft/zulip-hermes-integration/pull/133))

### Contributors
- [@niyazmft](https://github.com/niyazmft) — [#132](https://github.com/niyazmft/zulip-hermes-integration/pull/132), [#133](https://github.com/niyazmft/zulip-hermes-integration/pull/133)
- [@AungDev](https://github.com/AungDev) — [#131](https://github.com/niyazmft/zulip-hermes-integration/pull/131)

## [1.9.1] - 2026-09-08

### Added
- **Hard outbound message-length cap**: `ZulipAdapter.send()` and `_standalone_send()` now truncate content to `ZULIP_MAX_MESSAGE_LENGTH` (default 20000, `0` disables) *before* chunking, appending a `[...message truncated]` marker. Mirrors the sibling OpenClaw plugin's `maxMessageLength` guard — prevents very long content from breaking downstream consumers (e.g. memory plugins) that fail on oversized messages. ([#272](https://github.com/niyazmft/openclaw-zulip-bridge/pull/280))
- **Relative-path resolution for file uploads**: `upload_file_to_zulip()` now resolves bare relative filenames (e.g. `haiku.txt` from the agent workspace) against candidate roots in order — the given path, the bot workspace, the data dir, then tmpdir — each still gated by the allowlist and the `O_NOFOLLOW` symlink check. Previously a relative path resolved against the process CWD and was silently dropped. Mirrors the sibling OpenClaw plugin's relative-attachment resolution. ([#268](https://github.com/niyazmft/openclaw-zulip-bridge/pull/283))

## [1.9.0] - 2026-09-04

### Added
- **Native `MEDIA:` image/file delivery**: `ZulipAdapter` now overrides `send_image_file()` and `send_document()` (shared `_send_uploaded_media()` helper). Hermes core's `MEDIA:<path>` pipeline previously fell through to the base class's "Couldn't deliver the image attachment" stub for every screenshot/attachment; images/files are now uploaded via `upload_file_to_zulip()` and embedded inline (`![name](url)` for images, `[name](url)` for documents). Also hardens `upload_file_to_zulip()`: honors the `HERMES_MEDIA_ALLOW_DIRS` allowlist, replaces the `str.startswith()` containment check with `Path.relative_to()` (closes the sibling-directory prefix hole), and strips the `/api` suffix from `base_url` before appending the server-root-relative upload URI (fixes the double-slash 404). ([#121](https://github.com/niyazmft/zulip-hermes-integration/pull/121), [#123](https://github.com/niyazmft/zulip-hermes-integration/issues/123))

### Docs
- **Sibling-adapter cross-link**: README banner now points at the related `openclaw-zulip-bridge` project. ([#120](https://github.com/niyazmft/zulip-hermes-integration/pull/120))

### Contributors
- [@niyazmft](https://github.com/niyazmft) — [#120](https://github.com/niyazmft/zulip-hermes-integration/pull/120)
- [@oxedom](https://github.com/oxedom) — [#121](https://github.com/niyazmft/zulip-hermes-integration/pull/121)

## [1.8.1] - 2026-08-25

### Added
- **Out-of-process delivery**: register a `standalone_sender_fn` (`_standalone_send`) on the Hermes `PlatformEntry` so `deliver: zulip[:<stream_id>[:<topic>]]` cron jobs can send when no gateway adapter is live in the calling process (`hermes cron run <job>`, cron in its own process). Previously such sends failed with `No live adapter for platform 'zulip'`. Supports streams (`<stream_id>`), DMs (`dm:<user_id>`), the `zulip:<stream>:<topic>` thread segment as topic, inline `[[zulip_topic: …]]` directives, `ZULIP_RESPONSE_PREFIX`, media uploads, and `ZULIP_SEND_TIMEOUT`. ([#115](https://github.com/niyazmft/zulip-hermes-integration/pull/115))

### Fixed
- **Session-scoped DM parsing**: `_parse_target()` now strips the `:session:N` suffix from DM chat IDs (e.g. `dm:1032616:session:1`) so replies to rotated DM sessions are delivered correctly. Previously `int()` choked on the extra colons and silently dropped the message. ([#116](https://github.com/niyazmft/zulip-hermes-integration/pull/116), fixes [#111](https://github.com/niyazmft/zulip-hermes-integration/issues/111))
- **Startup recovery**: `recover_interrupted_messages()` called `Client.get_private_messages`, which does not exist in the zulip SDK, so every gateway start logged `zulip recovery: failed [error='Client' object has no attribute 'get_private_messages']` and interrupted DMs were never re-dispatched. It now fetches the last 100 direct messages via `Client.get_messages` (`is:dm` narrow). ([#114](https://github.com/niyazmft/zulip-hermes-integration/pull/114))
- **Queue re-registration**: the adapter now also re-registers its event queue when Zulip returns `BAD_REQUEST` with an "event newer than … pruned" message, not just `BAD_EVENT_QUEUE_ID`. Prevents the bot getting stuck on a stale queue. ([#112](https://github.com/niyazmft/zulip-hermes-integration/pull/112))
- **Cron delivery**: register `cron_deliver_env_var="ZULIP_HOME_CHANNEL"` on the platform entry so Hermes cron accepts `deliver: zulip[:<stream_id>]` targets. Previously preflight blocked such jobs with "delivery platform 'zulip' is not a known cron delivery target" and never ran them. ([#113](https://github.com/niyazmft/zulip-hermes-integration/pull/113))
- **Queue re-registration test coverage**: restored test coverage for the `BAD_EVENT_QUEUE_ID` recovery path in `tests/test_integration.py`. The queue-expiration test is now parametrized to cover both `BAD_EVENT_QUEUE_ID` and the `BAD_REQUEST` "pruned" cases. ([#118](https://github.com/niyazmft/zulip-hermes-integration/pull/118))

### Contributors
- [@niyazmft](https://github.com/niyazmft) — [#116](https://github.com/niyazmft/zulip-hermes-integration/pull/116), [#118](https://github.com/niyazmft/zulip-hermes-integration/pull/118)
- [@denniswebb](https://github.com/denniswebb) — [#112](https://github.com/niyazmft/zulip-hermes-integration/pull/112), [#113](https://github.com/niyazmft/zulip-hermes-integration/pull/113), [#114](https://github.com/niyazmft/zulip-hermes-integration/pull/114), [#115](https://github.com/niyazmft/zulip-hermes-integration/pull/115)

## [1.8.0] - 2026-08-06

### Added
- **Rate Limiting**: Per-sender sliding-window rate limiter (`RateLimiter`) with configurable `ZULIP_MAX_MESSAGES_PER_MINUTE` (default 60). Prevents message floods from exhausting resources.
- **Audit Logging**: Persistent JSON-line audit logger (`AuditLogger`) with 1MB rotation, 3 rotated files. Logs auth failures, rate limit hits, policy blocks, and monitor lifecycle events.
- **Admin Actions**: Stream CRUD (list/create/update/delete), user info, member info via `zulip/admin_actions.py`.
- **Message Pin/Star**: `star_message()` method on adapter — star/unstar messages.
- **New Commands**: `/streams`, `/user`, `/pin`, `/unpin` — admin-facing commands with natural language fallback to AI.
- **Connection Pooling**: `requests.HTTPAdapter` configured on Zulip client sessions (pool_connections=10, pool_maxsize=20) with retry on 429/5xx.

### Changed
- **TOCTOU Fix**: `_safe_delete_temp_file()` now uses `stat(follow_symlinks=False)` before unlink to prevent symlink swap races.
- **TOCTOU Fix**: `upload_file_to_zulip()` now uses `os.open()` with `O_NOFOLLOW` for atomic symlink rejection.
- **Input Validation**: All user-facing string inputs (search, fetch, subscribe) validated with 10KB max length.
- **JSON Size Limit**: `ZULIP_STREAM_OVERRIDES` capped at 10KB to prevent DoS.
- **Fallback Reader**: Trajectory file reads capped at 10MB to prevent OOM.
- **Updater Security**: HTTPS SSLContext verification added to update downloads.
- **Message ID Validation**: Overflow guard added (max 2^63-1).
- **PII Masking**: `mask_pii()` now detects IPv4, IPv6, and API key patterns.
- **Recovery Keys**: Sender email now SHA-256 hashed (16-char prefix) in recovery session keys.
- **File Permissions**: All persisted JSON files (dedupe, queue, policy) set to 0600.
- **Queue Debounce**: Increased from 2s to 5s for lower write frequency.
- **PLUGIN_FILES**: Updated to include new `admin_actions.py`, `audit_logger.py`, `rate_limiter.py`.

### Security
- Rate limiting prevents message flood attacks
- Audit trail for all security-relevant events
- TOCTOU races eliminated in temp file cleanup and media upload
- Input length validation prevents DoS via oversized queries
- PII leakage reduced via enhanced masking patterns
- Recovery session keys no longer contain plaintext emails
- Persisted data protected with restrictive file permissions

## [1.7.0] - 2026-07-27

### Added
- **Network Timeouts**: All Zulip SDK calls wrapped with `asyncio.wait_for()` via `_sdk_call()` helper. Configurable via `ZULIP_CONNECT_TIMEOUT` (30s), `ZULIP_READ_TIMEOUT` (60s), `ZULIP_SEND_TIMEOUT` (90s). Prevents gateway event loop from hanging on degraded networks.
- **Stream Filtering**: `ZULIP_STREAMS` env var restricts monitoring to specific stream names (default: `*` for all). Messages from non-monitored streams are silently dropped.
- **Response Prefix**: `ZULIP_RESPONSE_PREFIX` prepends a string to every outbound message (e.g. emoji branding).
- **Group Policy**: Separate `ZULIP_GROUP_POLICY` (`open`/`allowlist`/`disabled`) and `ZULIP_GROUP_ALLOW_FROM` for stream messages. Independent from DM policy.
- **Topic Resolution**: `resolve_topic(stream_id, topic)` method prepends `✔ ` to mark topics as resolved. Skips already-resolved topics.

### Changed
- `reactions.py` updated to accept optional `timeout` parameter on `add_reaction`/`remove_reaction`.
- `plugin.yaml` documented 6 new env vars.

## [1.6.0] - 2026-07-22

### Added
- **Admin Command Framework**: `/help`, `/status`, `/model` commands intercepted before AI dispatch. Extensible via `@register_command` decorator.
- **DM Policy & Pairing System**: Four policy modes — `open`, `allowlist`, `pairing`, `disabled`. Pairing mode generates random 6-char codes for secure onboarding.
- **Performance Caching**: LRU client cache (50 entries) + target cache (500 entries) to reduce repeated allocations.
- **Health Probe**: Pre-flight SSRF-safe connection validation with structured `health_status` logging.
- **Security Hardening**: SSRF URL validation, symlink rejection in workspace/media uploads, path traversal blocking.
- **Multi-Account Config**: `AccountResolver` supports backward-compatible single-account and multi-account configs.

### Changed
- `adapter.py` now uses cached clients via `_get_cached_client()` instead of creating new `Client()` instances per reconnect.
- `_send_single()` now uses `_parse_target()` cache for DM vs stream resolution.
- `update.sh` deployment script now runs `hermes gateway restart` in background via `nohup`.

## [1.10.0] - 2026-10-01

### Added
- **Progressive activity trace**: one bot-owned status message per run, edited in place as work proceeds (`✓ terminal — 1234 ms`), closed out as `**Done**`, `**Failed**` or `**Cancelled**`. Mode A adds a checkpoint per finished tool call; mode B offers the agent a `zulip_progress` tool for intent a tool call cannot reveal. Opt-in via `ZULIP_ACTIVITY_TRACE`. An orphaned board is closed as `Cancelled` at the next start, so a stale "Working" cannot outlive the process that posted it. ([#173](https://github.com/niyazmft/zulip-hermes-integration/pull/173), [#174](https://github.com/niyazmft/zulip-hermes-integration/pull/174), [#175](https://github.com/niyazmft/zulip-hermes-integration/pull/175), [#178](https://github.com/niyazmft/zulip-hermes-integration/pull/178), [#180](https://github.com/niyazmft/zulip-hermes-integration/pull/180))
- **In-channel action triggers**: a reaction on the bot's own stream message dispatches a configured instruction as a turn in that same topic. The synthetic turn carries the reacting human as its sender, so every existing authorisation decision still applies to *them*. Fires once per (message, emoji, user) and is audited. Off unless `ZULIP_REACTION_TRIGGERS` is set. ([#183](https://github.com/niyazmft/zulip-hermes-integration/pull/183), fixes [#163](https://github.com/niyazmft/zulip-hermes-integration/issues/163), [#164](https://github.com/niyazmft/zulip-hermes-integration/issues/164))
- **Sticky topic engagement**: after a mention, follow-ups in that topic are answered without re-mentioning the bot — `ZULIP_ENGAGEMENT_MODE=sticky_topic`, with `user`/`topic` scope, an idle TTL, an expiry notice and `/unlisten` to end it early. ([#184](https://github.com/niyazmft/zulip-hermes-integration/pull/184), fixes [#165](https://github.com/niyazmft/zulip-hermes-integration/issues/165), [#166](https://github.com/niyazmft/zulip-hermes-integration/issues/166))
- **History-aware context**: quotes a bounded slice of the current topic into the prompt, so "have we seen this before?" is answered from evidence rather than memory. `off` / `on-demand` / `always`, capped by message count, window and characters. ([#188](https://github.com/niyazmft/zulip-hermes-integration/pull/188), fixes [#148](https://github.com/niyazmft/zulip-hermes-integration/issues/148))
- **Stream watching**: `ZULIP_SOFT_GATE` dispatches every monitored stream message tagged `addressed=true/false`; `ZULIP_OBSERVE_GROUP` instead buffers non-addressed messages as bounded per-topic context. The two are mutually exclusive — soft gate dispatches everything, so the observe path is never reached. ([#188](https://github.com/niyazmft/zulip-hermes-integration/pull/188), fixes [#153](https://github.com/niyazmft/zulip-hermes-integration/issues/153))
- **Per-session message queue**: a message arriving mid-run waits behind the running turn instead of steering into it, so one person cannot redirect another's work. Separate topics still run in parallel, and past the cap a message dispatches immediately rather than being dropped. ([#189](https://github.com/niyazmft/zulip-hermes-integration/pull/189), fixes [#151](https://github.com/niyazmft/zulip-hermes-integration/issues/151))
- **Actionable refs**: GitHub pull, issue, commit and Actions-run links the agent writes are validated against the API before being rendered as links. An unconfirmable URL is left exactly as written, so rendering never makes prose worse. ([#186](https://github.com/niyazmft/zulip-hermes-integration/pull/186), fixes [#150](https://github.com/niyazmft/zulip-hermes-integration/issues/150))
- **DM pairing approval**: `python3 -m zulip.pairing list | approve | revoke`. Pending requests are persisted, so a code survives a gateway restart, and a running gateway picks an approval up on its next DM without a restart. ([#201](https://github.com/niyazmft/zulip-hermes-integration/pull/201), fixes [#198](https://github.com/niyazmft/zulip-hermes-integration/issues/198))
- **Secret guard**: refuses to send a message containing a host credential value. ([#171](https://github.com/niyazmft/zulip-hermes-integration/pull/171), fixes [#136](https://github.com/niyazmft/zulip-hermes-integration/issues/136))
- **Inbound hygiene and profile scoping**: model reasoning blocks are stripped from outbound text, display names are cached, and plugin state is scoped per Hermes profile. ([#186](https://github.com/niyazmft/zulip-hermes-integration/pull/186), fixes [#152](https://github.com/niyazmft/zulip-hermes-integration/issues/152), [#156](https://github.com/niyazmft/zulip-hermes-integration/issues/156))
- **Delivery audit**: records whether a turn actually delivered anything, so a run that produces no reply is no longer silent. ([#186](https://github.com/niyazmft/zulip-hermes-integration/pull/186), fixes [#145](https://github.com/niyazmft/zulip-hermes-integration/issues/145))
- **`ZULIP_SITE` must be https**, sensitive uploads are denied, and `PLUGIN_FILES` gained the modules added since. ([#172](https://github.com/niyazmft/zulip-hermes-integration/pull/172), fixes [#137](https://github.com/niyazmft/zulip-hermes-integration/issues/137), [#138](https://github.com/niyazmft/zulip-hermes-integration/issues/138))

### Fixed
- **Gateway-native slash commands were shadowed**: the plugin registered `/help`, `/status` and `/model`, so those never reached the gateway that owns them. They now fall through untouched. ([#191](https://github.com/niyazmft/zulip-hermes-integration/pull/191), fixes [#190](https://github.com/niyazmft/zulip-hermes-integration/issues/190))
- **Bare GitHub URLs were never validated**: the refs renderer only recognised `[[zulip_ref: …]]` markers, so ordinary URLs written in prose passed through unrendered. ([#195](https://github.com/niyazmft/zulip-hermes-integration/pull/195), fixes [#194](https://github.com/niyazmft/zulip-hermes-integration/issues/194))
- **User lookup called a method the SDK does not have**: `get_user_info` used `client.get_user`, which does not exist, so every reaction trigger was silently dropped and the display-name cache returned `None` on a miss. Now resolved through the methods the installed SDK actually exposes. ([#197](https://github.com/niyazmft/zulip-hermes-integration/pull/197), fixes [#196](https://github.com/niyazmft/zulip-hermes-integration/issues/196))
- **Group DMs replied only to the sender** instead of preserving the full recipient set. ([#169](https://github.com/niyazmft/zulip-hermes-integration/pull/169), fixes [#154](https://github.com/niyazmft/zulip-hermes-integration/issues/154))
- **Replies, typing indicators and approval prompts were not routed by the session's originating topic**, so a reply could land in the wrong topic. ([#144](https://github.com/niyazmft/zulip-hermes-integration/pull/144))
- **The event queue could serve a stale event set**: it now records the event types it registered for and re-registers when that set changes. ([#181](https://github.com/niyazmft/zulip-hermes-integration/pull/181), fixes [#162](https://github.com/niyazmft/zulip-hermes-integration/issues/162))
- **`/events` poll loop paced**, and the server's long-poll budget is learned rather than assumed. ([#170](https://github.com/niyazmft/zulip-hermes-integration/pull/170), fixes [#146](https://github.com/niyazmft/zulip-hermes-integration/issues/146))
- **`PLUGIN_FILES` was missing `activity_trace.py` and `secret_guard.py`**, so `zulip update` would fetch neither. ([#177](https://github.com/niyazmft/zulip-hermes-integration/pull/177))

### Docs
- **README, AGENTS.md and SETUP.md audited against the source.** Corrected fabricated and stale claims — test counts that disagreed with each other, a `ZULIP_EDIT_PLACEHOLDER` that does not exist, a `"Thinking..." placeholder` removed long ago, invented rendered examples, and a Python floor that did not match the syntax in use — documented the environment variables that had no coverage, and folded three overlapping install sections into one path. ([#199](https://github.com/niyazmft/zulip-hermes-integration/pull/199), [#200](https://github.com/niyazmft/zulip-hermes-integration/pull/200), [#202](https://github.com/niyazmft/zulip-hermes-integration/pull/202))
- **Contract gates, `SECURITY.md` and the sibling parity spec**. ([#182](https://github.com/niyazmft/zulip-hermes-integration/pull/182), fixes [#141](https://github.com/niyazmft/zulip-hermes-integration/issues/141), [#142](https://github.com/niyazmft/zulip-hermes-integration/issues/142), [#140](https://github.com/niyazmft/zulip-hermes-integration/issues/140))

### Internal
- **Generated release notes for contributor credit**, driven by `.github/release.yml`. ([#135](https://github.com/niyazmft/zulip-hermes-integration/pull/135))

### Contributors
- [@niyazmft](https://github.com/niyazmft) — [#135](https://github.com/niyazmft/zulip-hermes-integration/pull/135), [#169](https://github.com/niyazmft/zulip-hermes-integration/pull/169), [#170](https://github.com/niyazmft/zulip-hermes-integration/pull/170), [#171](https://github.com/niyazmft/zulip-hermes-integration/pull/171), [#172](https://github.com/niyazmft/zulip-hermes-integration/pull/172), [#173](https://github.com/niyazmft/zulip-hermes-integration/pull/173), [#174](https://github.com/niyazmft/zulip-hermes-integration/pull/174), [#175](https://github.com/niyazmft/zulip-hermes-integration/pull/175), [#177](https://github.com/niyazmft/zulip-hermes-integration/pull/177), [#178](https://github.com/niyazmft/zulip-hermes-integration/pull/178), [#180](https://github.com/niyazmft/zulip-hermes-integration/pull/180), [#181](https://github.com/niyazmft/zulip-hermes-integration/pull/181), [#182](https://github.com/niyazmft/zulip-hermes-integration/pull/182), [#183](https://github.com/niyazmft/zulip-hermes-integration/pull/183), [#184](https://github.com/niyazmft/zulip-hermes-integration/pull/184), [#186](https://github.com/niyazmft/zulip-hermes-integration/pull/186), [#188](https://github.com/niyazmft/zulip-hermes-integration/pull/188), [#189](https://github.com/niyazmft/zulip-hermes-integration/pull/189), [#191](https://github.com/niyazmft/zulip-hermes-integration/pull/191), [#195](https://github.com/niyazmft/zulip-hermes-integration/pull/195), [#197](https://github.com/niyazmft/zulip-hermes-integration/pull/197), [#199](https://github.com/niyazmft/zulip-hermes-integration/pull/199), [#200](https://github.com/niyazmft/zulip-hermes-integration/pull/200), [#201](https://github.com/niyazmft/zulip-hermes-integration/pull/201), [#202](https://github.com/niyazmft/zulip-hermes-integration/pull/202)
- [@AungDev](https://github.com/AungDev) — [#144](https://github.com/niyazmft/zulip-hermes-integration/pull/144)

## [1.0.0] - 2026-07-15

### Added
- Initial Zulip platform adapter for Hermes Gateway
- Stream and DM message support
- Basic event queue polling
- Topic threading via `_topic_cache`
- **Persistent Event Queue**: `ZulipQueueManager` persists `queue_id` + `last_event_id` to disk, survives gateway restarts, handles `BAD_EVENT_QUEUE_ID` gracefully
- **Message Deduplication**: `ZulipDedupeStore` prevents duplicate processing with 5-minute TTL and debounced disk persistence
- **Text Processing**: `strip_html_to_text()`, `chunk_text()` (length/newline modes), `extract_topic_directive()` for inline topic changes
- **Reaction Status Indicators**: Configurable emoji reactions (👀/✅/⚠️) for start/success/error states
- **Message Chunking**: Long responses split into multiple Zulip messages; topic directives extracted and applied
- **Inbound Media**: Download Zulip attachments with size validation and same-origin filtering
- **Outbound Uploads**: Send files via `/user_uploads` with path traversal security
- **Stream Trigger Modes**: `onmessage` (all), `oncall` (mention only), `onchar` (prefix trigger) with `ZULIP_CHATMODE`
- **Structured Logging**: Machine-parseable `[k=v]` format with PII masking for emails, IDs, and stream names

### Changed
- `adapter.py` refactored to use all new modules: queue manager, dedupe store, reactions, chunking, triggers, logging
