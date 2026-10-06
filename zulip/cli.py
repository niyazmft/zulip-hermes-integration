"""Standalone / CLI concern for the Zulip plugin.

Everything the plugin does *outside* a live gateway process lives here:

* :func:`check_requirements` / :func:`validate_config` / :func:`env_enablement`
  — the requirements probe, credential validation and env seeding the plugin
  hands to ``register(ctx)`` so Hermes can decide whether and how to enable it.
* :func:`interactive_setup` — the ``hermes gateway setup`` flow.
* :func:`main` / :func:`effective_config` / :func:`run_config_wizard` — the
  ``zulip config`` command: what the plugin *effectively* resolved for every knob
  and where each value came from, plus a curated wizard that writes only
  deviations to ``.env`` (epic #211, child #216). Reached through the
  ``config.sh`` shim, because Hermes exposes no ``plugins config`` subcommand.
* :func:`standalone_send` — out-of-process delivery
  (``PlatformEntry.standalone_sender_fn``), which Hermes calls from
  ``tools/send_message_tool._send_via_adapter`` when no gateway adapter is live
  in the current process, e.g. ``hermes cron run <job>``. Without it,
  ``deliver: zulip[:<stream_id>]`` jobs fail with ``No live adapter for platform
  'zulip'``.

Layering: L4 (composition) — depends on L0-L2 (``runtime_scope``, ``logger``,
``text_utils``, ``secret_guard``, ``audit_logger``, ``settings``, ``probe``,
``zulip_client``, ``media``, ``refs``).

``zulip.adapter`` re-exports these under their historical private names
(``_env_enablement``, ``_resolve_standalone_credentials``, ``_standalone_send``)
so ``from zulip.adapter import _standalone_send`` keeps working; ``adapter`` is
deliberately never imported here — that would be a cycle.

The SDK handle, the client cache and the address parser are reached through
``zulip_client``, the module that owns them, so a patch of
``zulip.zulip_client`` takes effect on both the live adapter send path and this
one. ``refs.render_refs`` and ``media.upload_file_to_zulip`` are likewise
reached through their owning modules (the Wave 5 rule).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import media
from . import refs
from . import runtime_scope
from . import settings
from . import zulip_client
from .audit_logger import AuditLogger
from .logger import mask_pii
from .probe import _normalize_base_url, base_url_error
from .secret_guard import (
    block_secret_leaks_enabled,
    collect_known_secrets,
    describe_leaked_secrets,
    find_leaked_secrets,
)
from .text_utils import extract_topic_directive, strip_think_blocks, truncate_text

logger = logging.getLogger(__name__)


# Topic used for out-of-process sends when the target carries no topic
# (no ``zulip:<stream>:<topic>`` thread id and no inline
# ``[[zulip_topic: …]]`` directive). Matches the in-process adapter's fallback for unknown chats.
STANDALONE_DEFAULT_TOPIC = "general"


def check_requirements() -> bool:
    """Return True if the zulip SDK is installed."""
    return zulip_client.import_zulip_sdk() is not None


def validate_config(config: Any) -> bool:
    """Validate that required credentials are present."""
    extra = getattr(config, "extra", {}) or {}
    return bool(
        (runtime_scope.get_setting("ZULIP_API_KEY") or extra.get("api_key"))
        and (runtime_scope.get_setting("ZULIP_EMAIL") or extra.get("email"))
        and (runtime_scope.get_setting("ZULIP_SITE") or extra.get("site"))
    )


def env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from environment variables."""
    key = runtime_scope.get_setting("ZULIP_API_KEY", "").strip()
    email = runtime_scope.get_setting("ZULIP_EMAIL", "").strip()
    site = runtime_scope.get_setting("ZULIP_SITE", "").strip()
    if not (key and email and site):
        return None

    return {"api_key": key, "email": email, "site": site}


def interactive_setup() -> None:
    """Interactive `hermes gateway setup` flow for the Zulip platform.

    Three credentials, then **one** question: use the recommended setup?
    Answering yes writes the ``ZULIP_PROFILE`` marker and stops — the marker is
    what makes a fresh install behave like a complete shared-room teammate
    without editing any file (epic #211). Answering no keeps the longer walk.

    Lazy-imports ``hermes_cli.setup`` helpers so the plugin stays importable
    in non-CLI contexts (gateway runtime, tests).
    """
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("Zulip")
    existing_email = get_env_value("ZULIP_EMAIL")
    if existing_email:
        print_info(f"Zulip: already configured ({existing_email})")
        if not prompt_yes_no("Reconfigure Zulip?", False):
            return

    print_info("Connect Hermes to Zulip via a bot account.")
    print_info("   Create a bot at: Settings → Bots → Add a new bot (Generic bot)")
    print()

    site = prompt(
        "Zulip site URL (e.g. https://your-org.zulipchat.com)",
        default=get_env_value("ZULIP_SITE") or "",
    )
    if not site:
        print_warning("Site URL is required — skipping Zulip setup")
        return

    # https only, unless the operator already opted into insecure http
    # (Issue #137). The opt-in is read at validation time rather than captured
    # here, so a self-hosted http:// realm keeps working on later calls — the
    # sibling's bug was the flag being discarded after this point.
    normalized_site = _normalize_base_url(site)
    if not normalized_site:
        print_warning(base_url_error(site))
        return
    save_env_value("ZULIP_SITE", normalized_site)

    email = prompt(
        "Bot email address (e.g. hermes-bot@your-org.zulipchat.com)",
        default=get_env_value("ZULIP_EMAIL") or "",
    )
    if not email:
        print_warning("Bot email is required — skipping Zulip setup")
        return
    save_env_value("ZULIP_EMAIL", email.strip())

    api_key = prompt(
        "Bot API key",
        default=get_env_value("ZULIP_API_KEY") or "",
        password=True,
    )
    if not api_key:
        print_warning("API key is required — skipping Zulip setup")
        return
    save_env_value("ZULIP_API_KEY", api_key.strip())

    # One question, default yes (epic #211, child #215).
    #
    # The manifest declares 54 knobs. Walking them is a wall of prompts for
    # someone who just wants a working bot, and the recommended profile exists
    # so they never have to answer any of them: it supplies the whole posture
    # (mention-gated streams, DMs limited to the bot owner, activity trace,
    # on-demand history, observation, per-session queue, per-topic sessions and
    # the reaction triggers), while every ZULIP_* value set later still wins
    # over it. So the marker is written and the walk is skipped entirely.
    #
    # The "Allowed user emails" prompt is deliberately not asked on this path:
    # the owner is resolved from Zulip itself (#214), so the question is
    # redundant here, and it stays available to anyone who wants it.
    #
    # Re-running and answering yes is idempotent: save_env_value overwrites.
    if prompt_yes_no("Use the recommended setup?", True):
        save_env_value("ZULIP_PROFILE", runtime_scope.PROFILE_RECOMMENDED)
        print_success("Zulip configured with the recommended setup.")
        print_info(
            "   Answers @mentions in streams; DMs are for your Zulip bot owner."
        )
        print_info(
            "   Tip: Subscribe your bot to streams via Stream settings → "
            "Subscribers"
        )
        return

    # Declining keeps today's behaviour rather than inventing a second one: the
    # remaining declared settings are still walked here. Child #216 replaces
    # this branch with the curated wizard (~10 knobs, the rest behind
    # --advanced), which does not exist yet — pointing at it before it lands
    # would be exactly the fix-that-isn't-there this repo has been bitten by
    # (#204/#208).
    allowed = prompt(
        "Allowed user emails (comma-separated, or empty for none yet)",
        default=get_env_value("ZULIP_ALLOWED_USERS") or "",
    )
    if allowed:
        save_env_value("ZULIP_ALLOWED_USERS", allowed.strip())

    print_success("Zulip configured.")
    print_info("Tip: Subscribe your bot to streams via Stream settings → Subscribers")


def resolve_standalone_credentials(pconfig: Any) -> tuple[str, str, str]:
    """Return ``(site, email, api_key)`` from the environment or ``pconfig.extra``.

    Same precedence as :func:`validate_config`: the ``ZULIP_*`` environment
    variables win, then the platform config's ``extra`` mapping.
    """
    extra = getattr(pconfig, "extra", {}) or {}
    site = runtime_scope.get_setting("ZULIP_SITE") or extra.get("site") or ""
    email = runtime_scope.get_setting("ZULIP_EMAIL") or extra.get("email") or ""
    api_key = runtime_scope.get_setting("ZULIP_API_KEY") or extra.get("api_key") or ""
    return site, email, api_key


async def standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: Any = None,
    force_document: bool = False,
) -> dict:
    """Out-of-process Zulip delivery (Hermes ``standalone_sender_fn`` contract).

    Hermes calls this from ``tools/send_message_tool._send_via_adapter`` when
    no gateway adapter is live in the current process — e.g. ``hermes cron
    run <job>`` from the CLI, or cron running separately from the gateway.
    Without it, ``deliver: zulip[:<stream_id>]`` jobs fail with
    ``No live adapter for platform 'zulip'``.

    Arguments follow the contract in ``gateway/platform_registry.py``:

    * ``chat_id`` — ``<stream_id>`` or ``dm:<user_id>[,<user_id>…]`` (see
      :func:`zulip_client.parse_target`). Multiple comma-separated ids address
      a Zulip group direct message.
    * ``thread_id`` — the optional third segment of a ``zulip:<stream>:<topic>``
      target; used as the Zulip topic. An inline ``[[zulip_topic: …]]`` directive in
      the message wins over it, and :data:`STANDALONE_DEFAULT_TOPIC` is used
      when neither is present. Ignored for DMs.
    * ``media_files`` — local paths uploaded via ``/user_uploads`` and appended
      to the message as links, exactly like ``ZulipAdapter.send``.
    * ``force_document`` — accepted for contract compatibility; Zulip has no
      inline-vs-document distinction, so it has no effect.

    Returns ``{"success": True, "message_id": "<id>"}`` or ``{"error": "<why>"}``.
    Never raises: every failure is reported through the ``error`` key so the
    caller can record it as the job's delivery error.
    """
    site, email, api_key = resolve_standalone_credentials(pconfig)
    if not (site and email and api_key):
        return {
            "error": "Zulip not configured (ZULIP_SITE, ZULIP_EMAIL, ZULIP_API_KEY required)"
        }

    # Reasoning hygiene (Issue #152): the out-of-process path must drop a
    # leaked scratchpad block too, before anything is uploaded or sent.
    message = strip_think_blocks(message) or ""

    # Delivery audit (Issue #145): same outcomes as the live send path, so a
    # cron delivery that silently fails is a lookup rather than a mystery.
    audit = AuditLogger(
        data_dir=runtime_scope.get_profile_data_dir(),
        account_id=email or "default",
    )

    try:
        client = zulip_client.get_cached_client(site, email, api_key)
    except ImportError as e:
        await audit.log_deliver_failed("client_init_failed", chat_id=chat_id)
        return {"error": str(e)}
    except Exception as e:
        await audit.log_deliver_failed("client_init_failed", chat_id=chat_id)
        return {"error": f"Zulip client init failed: {e}"}

    try:
        target = zulip_client.parse_target(chat_id)
    except (TypeError, ValueError):
        await audit.log_deliver_failed("invalid_target", chat_id=chat_id)
        return {
            "error": (
                f"Invalid Zulip target {chat_id!r}: expected a numeric stream id "
                f"or 'dm:<user_id>[,<user_id>…]'"
            )
        }

    # Outbound secret guard (Issue #136). The out-of-process path needs this
    # just as much as send(): cron-injected text can carry a credential too.
    if block_secret_leaks_enabled():
        leaked = find_leaked_secrets(
            message,
            collect_known_secrets(
                getattr(pconfig, "extra", None),
                extra=[("zulip.api_key", api_key)],
                env=os.environ,
            ),
        )
        if leaked:
            summary = describe_leaked_secrets(leaked)
            logger.error(
                "zulip standalone delivery blocked: message contained host "
                "credentials [%s]",
                summary,
            )
            try:
                await audit.log_event(
                    "secret_leak_blocked",
                    {
                        "chat_id": mask_pii(str(chat_id)),
                        "direction": "outbound",
                        "transport": "standalone",
                        "sources": [hit.name for hit in leaked],
                        "count": len(leaked),
                    },
                )
                await audit.log_deliver_skipped(
                    "secret_leak_blocked", chat_id=chat_id
                )
            except Exception:
                pass  # auditing must never break the refusal itself
            return {
                "error": (
                    f"Refusing to deliver: the message contains {summary}. "
                    f"Remove it and rotate the credential."
                )
            }

    _connect_timeout, _read_timeout, send_timeout = settings.resolve_timeouts()
    content = message or ""

    # Media: upload first, then link — same shape as ZulipAdapter.send().
    uploaded_urls: list[str] = []
    if media_files:
        data_dir = runtime_scope.get_profile_data_dir()
        for file_path in media_files:
            if isinstance(file_path, (tuple, list)):
                # Some callers pass (path, is_voice) pairs.
                file_path = file_path[0]
            if not isinstance(file_path, str) or file_path.startswith(("http://", "https://")):
                logger.warning(
                    "zulip standalone send rejected media entry [entry=%s]",
                    mask_pii(str(file_path)),
                )
                continue
            try:
                uploaded_urls.append(
                    await media.upload_file_to_zulip(
                        client, file_path, data_dir, account_id=email
                    )
                )
            except Exception as e:
                logger.error(
                    "zulip standalone upload failed [file=%s]: %s",
                    mask_pii(file_path),
                    e,
                )
    if uploaded_urls:
        file_links = "\n".join(f"[{Path(u).name}]({u})" for u in uploaded_urls)
        content = f"{content}\n\n{file_links}" if content else file_links

    content, topic_directive = extract_topic_directive(content)

    # Rewrite actionable refs before truncation, so a marker can never be split
    # (Issue #150). Best-effort: a failure leaves the reply untouched. Read
    # through the owning module so a patch of ``zulip.refs`` takes effect here
    # exactly as it does on the live adapter send path.
    try:
        content = await refs.render_refs(content)
    except Exception as e:
        logger.warning(
            "zulip ref rendering failed, sending as-is: %s", mask_pii(str(e))
        )

    # Hard cap before chunking (mirrors sibling plugin's maxMessageLength).
    max_length = settings.resolve_max_message_length()
    if max_length > 0:
        content = truncate_text(content, max_length)

    prefix = settings.resolve_response_prefix()
    if prefix and content:
        content = prefix + content

    if target["type"] == "dm":
        payload = {"type": "private", "to": target["user_ids"], "content": content}
        audit_topic: str | None = None
    else:
        topic = topic_directive or (str(thread_id).strip() if thread_id else "") or STANDALONE_DEFAULT_TOPIC
        payload = {
            "type": "stream",
            "to": target["stream_id"],
            "topic": topic,
            "content": content,
        }
        audit_topic = topic

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(client.send_message, payload),
            timeout=send_timeout,
        )
    except asyncio.TimeoutError:
        await audit.log_deliver_failed(
            "send_timeout", chat_id=chat_id, topic=audit_topic
        )
        return {"error": f"Zulip send timed out after {send_timeout}s"}
    except Exception as e:
        await audit.log_deliver_failed(
            "send_exception", chat_id=chat_id, topic=audit_topic
        )
        return {"error": f"Zulip send failed: {e}"}

    if isinstance(result, dict) and result.get("result") == "success":
        message_id = str(result.get("id", ""))
        await audit.log_deliver_payload(
            chat_id=chat_id, topic=audit_topic, message_id=message_id
        )
        return {"success": True, "message_id": message_id}
    await audit.log_deliver_failed(
        "api_error", chat_id=chat_id, topic=audit_topic
    )
    if isinstance(result, dict):
        detail = " ".join(
            str(result[k]) for k in ("code", "msg") if result.get(k)
        ) or mask_pii(str(result))
    else:
        detail = mask_pii(str(result))
    return {"error": f"Zulip send failed: {detail}"}


# ---------------------------------------------------------------------------
# Effective configuration and the curated wizard (epic #211, child #216)
# ---------------------------------------------------------------------------

_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})


@dataclass(frozen=True)
class CuratedKnob:
    """One decision the wizard asks about.

    ``default`` is the *built-in* default, not the profile's value: it is what the
    plugin does with nothing set at all, and the wizard needs it to tell a
    deviation from a restatement of the baseline.
    """

    name: str
    default: str
    summary: str
    choices: tuple[str, ...] = ()


#: The tier that matters: the decisions which change what the bot *is*. The other
#: ~40 declared knobs are plumbing with a defensible default and stay behind
#: ``--advanced``, so this list stays short enough to read (#216).
#:
#: ``ZULIP_REQUIRE_MENTION`` is deliberately absent. It is inert in every mode --
#: each mode already implies its own trigger (#213) -- and offering a knob that
#: does nothing is worse than omitting it. ``ZULIP_CHATMODE`` is the one that
#: actually gates.
CURATED_KNOBS: tuple[CuratedKnob, ...] = (
    CuratedKnob(
        "ZULIP_CHATMODE",
        "onmessage",
        "Which stream messages the bot answers",
        ("onmessage", "oncall", "onchar"),
    ),
    CuratedKnob(
        "ZULIP_GROUP_POLICY",
        "open",
        "Who may trigger the bot in streams",
        ("open", "allowlist", "disabled"),
    ),
    CuratedKnob(
        "ZULIP_DM_POLICY",
        "open",
        "Who may DM the bot",
        ("open", "allowlist", "pairing", "disabled"),
    ),
    CuratedKnob(
        "ZULIP_OWNER_EMAIL",
        "",
        "The owner's Zulip email; found automatically under 'recommended'",
    ),
    CuratedKnob(
        "ZULIP_ACTIVITY_TRACE",
        "false",
        "Post one status message per run, edited while it works",
        ("true", "false"),
    ),
    CuratedKnob(
        "ZULIP_HISTORY_MODE",
        "off",
        "Quote real topic history into the prompt",
        ("off", "on-demand", "always"),
    ),
    CuratedKnob(
        "ZULIP_OBSERVE_GROUP",
        "false",
        "Remember non-addressed stream messages as topic context",
        ("true", "false"),
    ),
    CuratedKnob(
        "ZULIP_SESSION_QUEUE",
        "false",
        "Queue a message that arrives mid-run behind the running turn",
        ("true", "false"),
    ),
    CuratedKnob(
        "ZULIP_TOPIC_SESSIONS",
        "false",
        "Give each stream topic its own conversation session",
        ("true", "false"),
    ),
    CuratedKnob(
        "ZULIP_REACTION_TRIGGERS",
        "",
        'JSON emoji-name -> instruction map, e.g. {"+1": "Proceed."}',
    ),
    CuratedKnob(
        "ZULIP_MAX_MESSAGES_PER_MINUTE",
        "60",
        "Per-sender outbound rate limit; 0 disables",
    ),
)


@dataclass(frozen=True)
class ConfigRow:
    """One line of the effective-configuration view."""

    name: str
    value: Optional[str]
    source: str
    summary: str = ""


def _is_true(raw: Optional[str]) -> bool:
    return (raw or "").strip().lower() in _TRUE_WORDS


def manifest_path() -> Path:
    """The plugin manifest, which declares every knob the plugin reads."""
    return Path(__file__).resolve().parent / "plugin.yaml"


def declared_settings() -> list[tuple[str, str]]:
    """``(name, description)`` for every declared knob, in manifest order.

    The manifest is the single source of truth for *which* knobs exist, so the
    view cannot drift from what the plugin actually reads -- the parity test in
    ``tests/test_manifest_parity.py`` already ties the manifest to the code.
    """
    import yaml  # local: the plugin must import without PyYAML outside the CLI

    data = yaml.safe_load(manifest_path().read_text(encoding="utf-8")) or {}
    rows: list[tuple[str, str]] = []
    for section in ("requires_env", "optional_env"):
        for entry in data.get(section) or []:
            rows.append((entry["name"], str(entry.get("description", "")).strip()))
    return rows


def baseline_value(knob: CuratedKnob) -> str:
    """What the profile-or-built-in default supplies, *ignoring* the environment.

    This is what the wizard measures a deviation against: restating it must leave
    no entry in ``.env``, or the file stops being a list of deviations and becomes
    a copy of the profile.
    """
    preset = runtime_scope.preset_for_profile(runtime_scope.active_profile())
    if preset is not None and knob.name in preset:
        return preset[knob.name]
    return knob.default


def effective_config(*, advanced: bool = False) -> list[ConfigRow]:
    """The effective value and its origin for every knob (#216).

    ``source`` is ``env`` (set explicitly), ``profile`` (supplied by
    ``ZULIP_PROFILE``) or ``default`` (neither, so the built-in applies).
    Provenance is the whole reason child #212's resolver returns it.
    """
    rows: list[ConfigRow] = []
    curated = {knob.name for knob in CURATED_KNOBS}
    for knob in CURATED_KNOBS:
        resolved = runtime_scope.resolve_setting(knob.name, knob.default)
        rows.append(
            ConfigRow(knob.name, resolved.value, resolved.source, knob.summary)
        )
    if advanced:
        for name, description in declared_settings():
            if name in curated:
                continue
            resolved = runtime_scope.resolve_setting(name)
            rows.append(ConfigRow(name, resolved.value, resolved.source, description))
    return rows


def soft_gate_observe_conflict() -> bool:
    """Whether the two mutually exclusive knobs are both effectively on."""
    return _is_true(
        runtime_scope.resolve_setting("ZULIP_SOFT_GATE", "false").value
    ) and _is_true(runtime_scope.resolve_setting("ZULIP_OBSERVE_GROUP", "false").value)


def render_config(*, advanced: bool = False) -> str:
    """Render the effective configuration, with the source of every value."""
    rows = effective_config(advanced=advanced)
    width = max((len(row.name) for row in rows), default=0)
    profile = runtime_scope.active_profile()

    lines = [
        f"Zulip effective configuration — profile: {profile or '<none>'}",
        "",
        "  source  env      = you set it explicitly",
        "          profile  = supplied by ZULIP_PROFILE=recommended",
        "          default  = the plugin's built-in; <unset> means exactly that",
        "",
    ]
    for row in rows:
        value = row.value if row.value not in (None, "") else "<unset>"
        lines.append(f"  {row.name:<{width}}  {value:<30}  {row.source}")

    if not advanced:
        lines += [
            "",
            "  The remaining declared knobs are plumbing with a default of their",
            "  own; add --advanced to list them.",
        ]
    if soft_gate_observe_conflict():
        # Same string the adapter logs at startup, so the CLI and the log cannot
        # describe the same misconfiguration differently.
        lines += ["", f"  warning: {settings.SOFT_GATE_OBSERVE_CONFLICT}"]
    return "\n".join(lines)


# --- .env editing: deviations only ------------------------------------------

_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def env_file_path() -> Path:
    """``<hermes home>/.env`` — the same file ``hermes gateway setup`` writes."""
    return Path(runtime_scope.get_profile_data_dir()).expanduser() / ".env"


def _read_env_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _set_env_lines(lines: list[str], name: str, value: str) -> list[str]:
    """Set ``name`` in place, collapsing duplicates, appending when absent."""
    out: list[str] = []
    replaced = False
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) == name:
            if replaced:
                continue  # a duplicate: keep only the first
            out.append(f"{name}={value}")
            replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"{name}={value}")
    return out


def _remove_env_lines(lines: list[str], name: str) -> tuple[list[str], bool]:
    """Drop every assignment to ``name``, reporting whether anything was removed."""
    out = [
        line
        for line in lines
        if not ((match := _ASSIGNMENT.match(line)) and match.group(1) == name)
    ]
    return out, len(out) != len(lines)


def _env_file_value(lines: list[str], name: str) -> str:
    """The value ``name`` holds in the file being edited, or ``''``.

    The wizard edits ``.env`` but a standalone ``config.sh`` run has not sourced
    it, so the process environment is not the environment the *gateway* will see.
    The file has to be consulted first, or the wizard would show a value the
    gateway is not going to use and the operator would be editing blind.
    """
    for line in lines:
        match = _ASSIGNMENT.match(line)
        if match and match.group(1) == name:
            return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def write_env_lines(path: Path, lines: list[str]) -> None:
    """Write ``.env`` atomically, keeping it owner-only.

    Mirrors the other stores (``policy``, ``dedupe_store``): a partial write must
    never leave a credential file truncated.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), suffix=".tmp", delete=False
    ) as handle:
        handle.write("\n".join(lines).rstrip("\n") + "\n")
        temp_path = handle.name
    os.replace(temp_path, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _ask_curated(knob: CuratedKnob, current: str, source: str) -> Optional[str]:
    """One wizard question. Enter keeps the value; ``-`` returns to the baseline."""
    hint = f" [{'/'.join(knob.choices)}]" if knob.choices else ""
    print()
    print(f"{knob.name} — {knob.summary}")
    print(f"  now: {current or '<unset>'}  ({source}){hint}")
    try:
        raw = input("  new value (Enter to keep, - to unset): ").strip()
    except EOFError:
        return None
    if not raw:
        return None
    if raw == "-":
        return baseline_value(knob)
    return raw


def run_config_wizard(
    *, env_path: Optional[Path] = None, prompter: Any = None
) -> list[str]:
    """Ask the curated questions and write **only the deviations** (#216).

    Returns the changes made, so the caller can report them and tests can assert
    on them. A value equal to the profile-or-built-in baseline is written as an
    *absence*: the profile stays the baseline and ``.env`` stays a short list of
    the places this install differs from it, rather than a ballooning copy.
    """
    path = Path(env_path) if env_path is not None else env_file_path()
    lines = _read_env_lines(path)
    changes: list[str] = []

    for knob in CURATED_KNOBS:
        # The file wins: it is what the gateway will load, and it is what this
        # wizard is about to edit.
        from_file = _env_file_value(lines, knob.name)
        if from_file:
            current, source = from_file, runtime_scope.SOURCE_ENV
        else:
            resolved = runtime_scope.resolve_setting(knob.name, knob.default)
            current, source = resolved.value or "", resolved.source
        answer = (prompter or _ask_curated)(knob, current, source)
        if answer is None:
            continue
        answer = answer.strip()
        if answer == current:
            continue
        if answer == baseline_value(knob):
            lines, removed = _remove_env_lines(lines, knob.name)
            if removed:
                changes.append(f"{knob.name} -> back to the baseline (removed)")
            continue
        lines = _set_env_lines(lines, knob.name, answer)
        changes.append(f"{knob.name}={answer}")

    if changes:
        write_env_lines(path, lines)
    return changes


def run_config_command(
    *,
    advanced: bool = False,
    wizard: bool = False,
    env_path: Optional[Path] = None,
    prompter: Any = None,
    out: Any = print,
) -> int:
    """``zulip config``: show the effective configuration, optionally editing it."""
    if wizard:
        changes = run_config_wizard(env_path=env_path, prompter=prompter)
        target = Path(env_path) if env_path is not None else env_file_path()
        if changes:
            out("Zulip configuration updated:")
            for change in changes:
                out(f"  {change}")
        else:
            out("Zulip configuration unchanged.")
        out(f"  written to: {target}")
        out("")
    out(render_config(advanced=advanced))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for the ``config.sh`` shim (epic #211, child #216).

    Hermes exposes no ``plugins config`` subcommand, so this is reached through
    the shell shim that ships beside ``update.sh``.
    """
    parser = argparse.ArgumentParser(
        prog="zulip config",
        description="Show or change the Zulip plugin's effective configuration.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="config",
        choices=("config",),
        help="the only subcommand today",
    )
    parser.add_argument(
        "--advanced",
        action="store_true",
        help="also list the plumbing knobs — everything the plugin reads",
    )
    parser.add_argument(
        "--wizard",
        action="store_true",
        help="ask about the curated settings and write only deviations",
    )
    args = parser.parse_args(argv)
    return run_config_command(advanced=args.advanced, wizard=args.wizard)


if __name__ == "__main__":  # pragma: no cover - exercised via config.sh
    raise SystemExit(main())
