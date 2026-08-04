"""Text processing utilities for Zulip message content.

- HTML-to-text conversion
- Message chunking for long responses
- Inline topic directives
- Mention normalization
- Onchar trigger prefix handling
"""

import re
from typing import Optional

# Single-pass HTML entity replacement
HTML_ENTITY_MAP = {
    "&lt;": "<",
    "&gt;": ">",
    "&amp;": "&",
    "&quot;": '"',
    "&#39;": "'",
}
HTML_ENTITY_RE = re.compile("|".join(re.escape(k) for k in HTML_ENTITY_MAP))

HTML_TAG_RE = re.compile(r"<[^>]+>")
ZULIP_MENTION_RE = re.compile(r"@\*\*([^*]+)\*\*")
TOPIC_DIRECTIVE_RE = re.compile(r"^\s*\[\[zulip_topic:\s*([^]]+?)\s*\]\]\s*", re.IGNORECASE)

DEFAULT_ONCHAR_PREFIXES = [">", "!"]


def strip_html_to_text(html: str) -> str:
    """Strip HTML tags, decode entities, normalize Zulip mentions.

    Single-pass entity replacement for performance.
    """
    if not html:
        return ""
    # Quick path: no HTML to process
    if "<" not in html and "&" not in html and "@**" not in html:
        return html.strip()

    text = html
    text = HTML_TAG_RE.sub("", text)
    text = HTML_ENTITY_RE.sub(lambda m: HTML_ENTITY_MAP[m.group(0)], text)
    text = ZULIP_MENTION_RE.sub(r"@\1", text)
    return text.strip()


def chunk_text(text: str, limit: int = 4000, mode: str = "length") -> list[str]:
    """Split text into chunks that fit within Zulip's message limit.

    Args:
        text: The text to split.
        limit: Maximum characters per chunk.
        mode: "length" (hard split), "newline" (split on newlines first),
              or "markdown" (respects code blocks, blockquotes, lists).

    Returns:
        List of text chunks.
    """
    if not text or len(text) <= limit:
        return [text] if text else []

    if mode == "markdown":
        return _chunk_markdown_text(text, limit)

    chunks: list[str] = []
    remaining = text

    if mode == "newline":
        lines = text.split("\n")
        current = ""
        for line in lines:
            candidate = (current + "\n" + line).strip() if current else line
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = line
                # If a single line exceeds limit, fall back to length mode
                if len(current) > limit:
                    chunks.extend(_chunk_by_length(current, limit))
                    current = ""
        if current:
            chunks.append(current)
    else:
        chunks = _chunk_by_length(text, limit)

    return [c for c in chunks if c]


def _chunk_by_length(text: str, limit: int) -> list[str]:
    """Hard split by character count, preferring word boundaries."""
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        # Try to break at a word boundary
        if end < len(text):
            # Look backwards for space
            space_pos = text.rfind(" ", start, end)
            if space_pos > start:
                end = space_pos
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
        # Skip leading whitespace
        while start < len(text) and text[start] == " ":
            start += 1
    return chunks


def _chunk_markdown_text(text: str, limit: int) -> list[str]:
    """Split text into chunks respecting markdown formatting boundaries.

    Preserves:
    - Code blocks (fenced with ```)
    - Blockquotes (>)
    - Lists (ordered and unordered)
    - Tables

    Falls back to _chunk_by_length for content that cannot be split
    at a formatting boundary.
    """
    if not text or len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current = ""
    lines = text.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]

        # Detect fenced code block start
        if line.strip().startswith("```"):
            block_lines = [line]
            i += 1
            while i < len(lines):
                block_lines.append(lines[i])
                if lines[i].strip().startswith("```"):
                    i += 1
                    break
                i += 1
            block = "\n".join(block_lines)
            candidate = (current + "\n" + block).strip() if current else block
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                # If the block itself exceeds limit, split it
                if len(block) > limit:
                    chunks.extend(_chunk_by_length(block, limit))
                else:
                    current = block
            continue

        candidate = (current + "\n" + line).strip() if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # Try to break at a formatting boundary
            # Check if this line is a list item, blockquote, or table
            stripped = line.strip()
            is_formatting = (
                stripped.startswith("-")
                or stripped.startswith("*")
                or stripped.startswith(">")
                or stripped.startswith("|")
                or stripped[0:1].isdigit()
            )
            if is_formatting and len(line) <= limit:
                current = line
            else:
                # Fall back to length-based split
                chunks.extend(_chunk_by_length(line, limit))
                current = ""
        i += 1

    if current:
        chunks.append(current)

    return [c for c in chunks if c]


def extract_topic_directive(text: str) -> tuple[str, Optional[str]]:
    """Extract a [[zulip_topic: Name]] directive from the start of text.

    Returns:
        (remaining_text, topic_or_None)
    """
    match = TOPIC_DIRECTIVE_RE.search(text)
    if not match:
        return text, None
    topic = match.group(1).strip()
    remaining = text[match.end():].lstrip()
    return remaining, topic


def create_mention_regex(
    bot_username: str, bot_full_name: Optional[str] = None
) -> re.Pattern:
    """Create a pre-compiled regex matching mentions of this bot.

    Zulip does not write mentions as ``@local-part``. It renders them from the
    user's *display name*::

        @**Soju**          personal mention
        @_**Soju**         silent mention
        @**Soju|12**       disambiguated by user id

    and ``strip_html_to_text()`` reduces the first of those to a bare
    ``@Soju`` before gating ever sees it. Matching only ``@{bot_username}``
    therefore never fires on a real mention, which silently makes
    ``oncall``/``onchar`` unreachable.

    The plain local-part form is kept because it still appears in hand-typed
    text. Prefer Zulip's own ``mentioned`` flag where available; this is the
    fallback for when it is not.
    """
    alternatives = [rf"{re.escape(bot_username)}\b"]
    if bot_full_name:
        escaped_name = re.escape(bot_full_name)
        # Markup form, for content that has not been stripped.
        alternatives.append(rf"_?\*\*{escaped_name}(?:\|\d+)?\*\*")
        # Post-strip form. This is the one that fires in practice.
        alternatives.append(rf"{escaped_name}\b")
    return re.compile(rf"@(?:{'|'.join(alternatives)})", re.IGNORECASE)


def normalize_mention(text: str, mention_regex: Optional[re.Pattern]) -> str:
    """Remove bot mention from text and normalize whitespace."""
    if mention_regex is None:
        return re.sub(r"\s+", " ", text).strip()
    text = mention_regex.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_onchar_prefix(text: str, prefixes: list[str]) -> tuple[bool, str]:
    """Check if text starts with any trigger prefix and strip it.

    Returns:
        (triggered, stripped_text)
    """
    stripped = text.lstrip()
    for prefix in prefixes:
        if not prefix:
            continue
        if stripped.startswith(prefix):
            return True, stripped[len(prefix):].lstrip()
    return False, text


def resolve_onchar_prefixes(env_value: Optional[str]) -> list[str]:
    """Parse comma-separated env string into prefix list."""
    if not env_value:
        return list(DEFAULT_ONCHAR_PREFIXES)
    parts = [p.strip() for p in env_value.split(",")]
    cleaned = [p for p in parts if p]
    return cleaned if cleaned else list(DEFAULT_ONCHAR_PREFIXES)


def convert_markdown_tables(text: str) -> str:
    """Convert standard markdown tables to Zulip-compatible format.

    Zulip uses a different table syntax than standard markdown.
    This function converts standard markdown tables to Zulip's format
    by ensuring proper alignment and spacing.

    Standard markdown:
    | Header 1 | Header 2 |
    |----------|----------|
    | Cell 1   | Cell 2   |

    Zulip format (same, but with proper alignment markers):
    | Header 1 | Header 2 |
    | --- | --- |
    | Cell 1 | Cell 2 |
    """
    if "|" not in text:
        return text

    lines = text.split("\n")
    result: list[str] = []
    in_table = False
    table_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            in_table = True
            table_lines.append(line)
        else:
            if in_table:
                # Process the collected table
                result.extend(_format_zulip_table(table_lines))
                table_lines = []
                in_table = False
            result.append(line)

    if in_table and table_lines:
        result.extend(_format_zulip_table(table_lines))

    return "\n".join(result)


def _format_zulip_table(table_lines: list[str]) -> list[str]:
    """Format a single markdown table for Zulip compatibility."""
    if len(table_lines) < 2:
        return table_lines

    # The second line should be the separator row
    # Standard: |---|---|
    # Zulip expects: | --- | --- |
    formatted: list[str] = []
    for i, line in enumerate(table_lines):
        if i == 1:
            # This is the separator row - ensure Zulip-compatible format
            cells = [c.strip() for c in line.split("|") if c.strip()]
            new_cells = []
            for cell in cells:
                # Ensure at least "---" in each cell
                cleaned = cell.replace("-", "").replace(":", "").strip()
                if not cleaned:
                    new_cells.append("---")
                else:
                    new_cells.append(cell)
            formatted.append("|" + "|".join(f" {c} " for c in new_cells) + "|")
        else:
            formatted.append(line)

    return formatted
