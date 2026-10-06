"""Standalone / CLI concern for the Zulip plugin.

Everything the plugin does *outside* a live gateway process lives here:

* :func:`check_requirements` / :func:`validate_config` / :func:`env_enablement`
  — the requirements probe, credential validation and env seeding the plugin
  hands to ``register(ctx)`` so Hermes can decide whether and how to enable it.
* :func:`interactive_setup` — the ``hermes gateway setup`` flow.
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

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

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
