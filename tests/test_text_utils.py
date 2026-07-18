"""Tests for zulip.text_utils — HTML stripping, chunking, topic directives."""

import re

import pytest

from zulip.text_utils import (
    strip_html_to_text,
    chunk_text,
    extract_topic_directive,
    create_mention_regex,
    normalize_mention,
    strip_onchar_prefix,
    resolve_onchar_prefixes,
    DEFAULT_ONCHAR_PREFIXES,
)


class TestStripHtmlToText:
    def test_plain_text_unchanged(self):
        assert strip_html_to_text("Hello world") == "Hello world"

    def test_strip_tags(self):
        assert strip_html_to_text("<p>Hello</p>") == "Hello"
        assert strip_html_to_text("<b>Bold</b> and <i>italic</i>") == "Bold and italic"

    def test_decode_entities(self):
        assert strip_html_to_text("1 &lt; 2 &amp; 3 &gt; 4") == "1 < 2 & 3 > 4"
        assert strip_html_to_text('say &quot;hello&quot;') == 'say "hello"'
        assert strip_html_to_text("it\u0026#39;s") == "it's"

    def test_normalize_mentions(self):
        assert strip_html_to_text("@**Alice** hello") == "@Alice hello"
        assert strip_html_to_text("Hey @**Bob Smith** there") == "Hey @Bob Smith there"

    def test_combined(self):
        html = '<p>@**Bot** said &lt;hello&gt; to <a href="x">@**User**</a></p>'
        result = strip_html_to_text(html)
        assert result == "@Bot said <hello> to @User"

    def test_empty(self):
        assert strip_html_to_text("") == ""

    def test_quick_path_no_html(self):
        text = "Just plain text without any special chars"
        assert strip_html_to_text(text) == text


class TestChunkText:
    def test_short_text_no_split(self):
        assert chunk_text("hello", limit=100) == ["hello"]

    def test_empty_text(self):
        assert chunk_text("", limit=100) == []

    def test_length_mode_split(self):
        text = "a " * 50  # ~100 chars
        chunks = chunk_text(text, limit=20)
        assert len(chunks) > 1
        assert all(len(c) <= 20 for c in chunks)
        assert " ".join(chunks) == text.strip()

    def test_length_mode_prefers_word_boundary(self):
        text = "word1 word2 word3 word4 word5"
        chunks = chunk_text(text, limit=12)
        # Should split at word boundaries
        assert all(len(c) <= 12 for c in chunks)

    def test_newline_mode(self):
        text = "line1\nline2\nline3\nline4"
        chunks = chunk_text(text, limit=100, mode="newline")
        assert chunks == ["line1\nline2\nline3\nline4"]

    def test_newline_mode_overflow(self):
        text = "line1\nline2\nline3\nline4"
        chunks = chunk_text(text, limit=15, mode="newline")
        # Should split on newlines up to limit
        assert len(chunks) > 1
        assert all(len(c) <= 15 for c in chunks)

    def test_newline_single_line_too_long(self):
        text = "a " * 50
        chunks = chunk_text(text, limit=20, mode="newline")
        assert len(chunks) > 1
        assert all(len(c) <= 20 for c in chunks)

    def test_exact_boundary(self):
        text = "x" * 20
        assert chunk_text(text, limit=20) == ["x" * 20]


class TestExtractTopicDirective:
    def test_no_directive(self):
        text, topic = extract_topic_directive("Hello world")
        assert text == "Hello world"
        assert topic is None

    def test_basic_directive(self):
        text, topic = extract_topic_directive("[[zulip_topic: General Chat]] Hello")
        assert text == "Hello"
        assert topic == "General Chat"

    def test_directive_lowercase(self):
        text, topic = extract_topic_directive("[[zulip_topic: general]] hi")
        assert text == "hi"
        assert topic == "general"

    def test_directive_with_extra_whitespace(self):
        text, topic = extract_topic_directive("  [[zulip_topic:  My Topic  ]]  Hello")
        assert text == "Hello"
        assert topic == "My Topic"

    def test_empty_topic(self):
        text, topic = extract_topic_directive("[[zulip_topic:  ]] Hello")
        assert text == "Hello"
        # Empty topic returns empty string (caller decides to treat as None or use default)
        assert topic == ""


class TestMentionRegex:
    def test_matches_mention(self):
        regex = create_mention_regex("bot")
        assert regex.search("hey @bot there")
        assert regex.search("@BOT hello")  # case insensitive

    def test_no_false_positive(self):
        regex = create_mention_regex("bot")
        assert not regex.search("@bottle")
        assert not regex.search("robot")

    def test_special_chars_escaped(self):
        regex = create_mention_regex("bot.name")
        assert regex.search("@bot.name hi")
        assert not regex.search("@botXname hi")


class TestNormalizeMention:
    def test_removes_mention(self):
        regex = create_mention_regex("bot")
        assert normalize_mention("@bot hello", regex) == "hello"
        assert normalize_mention("hey @bot there", regex) == "hey there"

    def test_no_mention_untouched(self):
        regex = create_mention_regex("bot")
        assert normalize_mention("hello world", regex) == "hello world"

    def test_none_regex(self):
        assert normalize_mention("hello  world", None) == "hello world"


class TestStripOncharPrefix:
    def test_prefix_triggered(self):
        triggered, stripped = strip_onchar_prefix("> hello", [">", "!"])
        assert triggered is True
        assert stripped == "hello"

    def test_exclamation_triggered(self):
        triggered, stripped = strip_onchar_prefix("!help", [">", "!"])
        assert triggered is True
        assert stripped == "help"

    def test_no_prefix(self):
        triggered, stripped = strip_onchar_prefix("hello", [">", "!"])
        assert triggered is False
        assert stripped == "hello"

    def test_whitespace_before_prefix(self):
        triggered, stripped = strip_onchar_prefix("  > hello", [">"])
        assert triggered is True
        assert stripped == "hello"


class TestResolveOncharPrefixes:
    def test_default(self):
        assert resolve_onchar_prefixes(None) == list(DEFAULT_ONCHAR_PREFIXES)
        assert resolve_onchar_prefixes("") == list(DEFAULT_ONCHAR_PREFIXES)

    def test_custom(self):
        assert resolve_onchar_prefixes("?,@") == ["?", "@"]

    def test_whitespace_trimmed(self):
        assert resolve_onchar_prefixes(" > , ! ") == [">", "!"]
