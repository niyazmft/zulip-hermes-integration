"""Actionable GitHub references in outbound message text.

An agent can emit an inline marker::

    [[zulip_ref: <github url> | <label>]]

The label is optional; when omitted it is derived from the URL. Before the
reply is chunked and sent, :func:`render_refs` validates each marker against
the GitHub REST API and rewrites it as a clickable markdown link::

    [[zulip_ref: https://github.com/owner/repo/pull/12]]
        -> [owner/repo#12](https://github.com/owner/repo/pull/12)

Security invariants (do not relax):

* Only ``https://github.com/...`` URLs matched by the anchored
  :data:`_REF_URL_RE` are ever fetched. Lookalike hosts, ports, embedded
  credentials, query strings and fragments never match, so the plugin never
  fetches a user-controlled origin.
* The API origin is the hardcoded module constant :data:`GITHUB_API_ORIGIN`.
  It is *not* configurable -- no environment variable can widen it into an
  SSRF primitive.
* Validation is unauthenticated. No ``Authorization`` header is ever sent, so
  private refs simply 404 and degrade.
* Every failure -- a malformed marker, a 404, a rate limit, a timeout or a
  network error -- degrades that one ref to backticked plain text (which Zulip
  will not auto-link). Rendering never raises and never drops the surrounding
  reply.
* Outcomes are cached for ~10 minutes and at most :data:`MAX_REFS_DEFAULT`
  refs are validated per message, keeping the plugin inside GitHub's
  unauthenticated rate budget (60 requests/hour/IP).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "GITHUB_API_ORIGIN",
    "MAX_REFS_DEFAULT",
    "CACHE_TTL_SECONDS",
    "clear_ref_cache",
    "render_refs",
]

# Hardcoded API origin. Never derive this from configuration or the input URL.
GITHUB_API_ORIGIN = "https://api.github.com"

MAX_REFS_DEFAULT = 3
CACHE_TTL_SECONDS = 600.0
REQUEST_TIMEOUT_SECONDS = 8.0
_CACHE_MAX_ENTRIES = 128

# A GitHub owner/repo segment: letters, digits, dot, underscore, hyphen. It
# deliberately excludes "/", "?", "#", ":" and whitespace so a matched path
# cannot smuggle a different origin, a port, a query string or extra segments.
_GITHUB_SEGMENT = r"[A-Za-z0-9_.-]+"
_OWNER_SEGMENT = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"

# Anchored: \A ... \Z over the whole string. The host must be exactly
# "github.com" followed by "/" -- never ":port", "@creds" or ".attacker".
_REF_URL_RE = re.compile(
    r"\Ahttps://github\.com/"
    r"(?P<owner>" + _OWNER_SEGMENT + r")/"
    r"(?P<repo>" + _GITHUB_SEGMENT + r")/"
    r"(?:"
    r"pull/(?P<pull>\d+)"
    r"|issues/(?P<issue>\d+)"
    r"|commit/(?P<commit>[0-9a-fA-F]{7,40})"
    r"|actions/runs/(?P<run>\d+)"
    r")\Z"
)

# The marker: [[zulip_ref: <url> | <label>]]. The body is single-line and may
# contain a lone "]" (labels are bracket-escaped before rendering); it stops
# at the first "]]".
_REF_MARKER_RE = re.compile(
    r"\[\[\s*zulip_ref\s*:\s*(?P<body>[^\n]*?)\s*\]\]",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class _Ref:
    """A validated-in-shape GitHub reference parsed from a marker."""

    kind: str  # "pull" | "issue" | "commit" | "run"
    owner: str
    repo: str
    ident: str
    url: str


# url -> (stored_at_monotonic, ok). Stores both successes and failures so a
# burst of the same ref costs at most one API call per TTL window.
_CACHE: dict[str, tuple[float, bool]] = {}


def clear_ref_cache() -> None:
    """Drop all cached validation outcomes (used by tests and reloads)."""
    _CACHE.clear()


def _now() -> float:
    # Indirection so tests can advance the clock without patching ``time``.
    return time.monotonic()


def _cache_get(url: str, now: float) -> Optional[bool]:
    entry = _CACHE.get(url)
    if entry is None:
        return None
    stored_at, ok = entry
    if now - stored_at >= CACHE_TTL_SECONDS:
        _CACHE.pop(url, None)
        return None
    return ok


def _cache_put(url: str, ok: bool, now: float) -> None:
    if url not in _CACHE and len(_CACHE) >= _CACHE_MAX_ENTRIES:
        oldest = min(_CACHE, key=lambda key: _CACHE[key][0])
        _CACHE.pop(oldest, None)
    _CACHE[url] = (now, ok)


def _parse_ref_url(raw: str) -> Optional[_Ref]:
    """Parse a raw URL into a :class:`_Ref`, or ``None`` when it is not an
    exact ``https://github.com`` pull/issue/commit/run URL."""
    match = _REF_URL_RE.match(raw)
    if match is None:
        return None
    owner = match.group("owner")
    repo = match.group("repo")
    # Defence in depth: no dot-only path segments even though the origin is
    # already pinned to the GitHub API.
    if owner in (".", "..") or repo in (".", ".."):
        return None
    if match.group("pull") is not None:
        kind, ident = "pull", match.group("pull")
    elif match.group("issue") is not None:
        kind, ident = "issue", match.group("issue")
    elif match.group("commit") is not None:
        kind, ident = "commit", match.group("commit")
    else:
        kind, ident = "run", match.group("run")
    return _Ref(kind=kind, owner=owner, repo=repo, ident=ident, url=raw)


def _default_label(ref: _Ref) -> str:
    if ref.kind in ("pull", "issue"):
        return f"{ref.owner}/{ref.repo}#{ref.ident}"
    if ref.kind == "commit":
        return f"{ref.owner}/{ref.repo}@{ref.ident[:7]}"
    return f"{ref.owner}/{ref.repo} run #{ref.ident}"


def _api_url(ref: _Ref) -> str:
    base = f"{GITHUB_API_ORIGIN}/repos/{ref.owner}/{ref.repo}"
    if ref.kind == "pull":
        return f"{base}/pulls/{ref.ident}"
    if ref.kind == "issue":
        return f"{base}/issues/{ref.ident}"
    if ref.kind == "commit":
        return f"{base}/commits/{ref.ident}"
    return f"{base}/actions/runs/{ref.ident}"


def _default_http(url: str) -> Any:
    """Synchronous, unauthenticated GET run in a worker thread.

    No ``Authorization`` header is ever attached: private refs return 404 and
    degrade rather than being fetched with the host's credentials.
    """
    import requests

    return requests.get(
        url,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "zulip-hermes-integration",
        },
    )


async def _fetch(url: str, http: Any) -> Any:
    if http is None:
        return await asyncio.to_thread(_default_http, url)
    result = http(url)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _validate(ref: _Ref, http: Any) -> bool:
    """Return True only when the API confirms the ref exists (HTTP 200)."""
    try:
        response = await _fetch(_api_url(ref), http)
        return getattr(response, "status_code", None) == 200
    except Exception:
        # 404, rate limit, timeout, connection reset, DNS failure, a broken
        # mock -- all degrade this one ref. Rendering must never raise.
        return False


def _degrade(url: str) -> str:
    return f"`{url}`"


def _link(ref: _Ref, label: str) -> str:
    # Escape brackets so a label cannot close the link and spoof its target.
    safe_label = label.replace("[", "\\[").replace("]", "\\]")
    return f"[{safe_label}]({ref.url})"


async def _render_validated(ref: _Ref, label: str, *, http: Any, now: float) -> str:
    if not label:
        label = _default_label(ref)
    ok = _cache_get(ref.url, now)
    if ok is None:
        ok = await _validate(ref, http)
        _cache_put(ref.url, ok, now)
    return _link(ref, label) if ok else _degrade(ref.url)


async def render_refs(
    text: str,
    *,
    max_refs: int = MAX_REFS_DEFAULT,
    http: Any = None,
) -> str:
    """Replace ``[[zulip_ref: ...]]`` markers with validated links.

    Call this *before* chunking so a marker can never be split across two
    messages. ``http`` is an injectable fetcher used by tests: a callable
    (sync or async) taking the API URL and returning an object with a
    ``status_code`` attribute. When ``None``, a real unauthenticated request
    is made to :data:`GITHUB_API_ORIGIN`.

    Never raises: every ref that cannot be validated is left as backticked
    plain text and the surrounding reply is returned intact.
    """
    if not text or "[[zulip_ref" not in text.lower():
        return text

    matches = list(_REF_MARKER_RE.finditer(text))
    if not matches:
        return text

    now = _now()
    parts: list[str] = []
    last = 0
    validated = 0

    for match in matches:
        parts.append(text[last:match.start()])
        raw_url, sep, raw_label = match.group("body").partition("|")
        raw_url = raw_url.strip()
        label = _WHITESPACE_RE.sub(" ", raw_label).strip() if sep else ""

        ref = _parse_ref_url(raw_url)
        if ref is None:
            # Malformed / off-host / credentials / port / query: never fetch.
            parts.append(_degrade(raw_url))
        elif validated >= max_refs:
            # Rate budget spent: degrade rather than call the API again.
            parts.append(_degrade(raw_url))
        else:
            validated += 1
            parts.append(await _render_validated(ref, label, http=http, now=now))
        last = match.end()

    parts.append(text[last:])
    return "".join(parts)
