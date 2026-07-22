# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
