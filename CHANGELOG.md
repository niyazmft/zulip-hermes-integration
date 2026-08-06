# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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

## [Unreleased]

### Added
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

## [1.0.0] - 2026-07-15

### Added
- Initial Zulip platform adapter for Hermes Gateway
- Stream and DM message support
- Basic event queue polling
- Topic threading via `_topic_cache`
