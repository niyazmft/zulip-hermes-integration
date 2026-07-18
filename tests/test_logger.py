"""Tests for zulip.logger — structured logging and PII masking."""

import pytest

from zulip.logger import format_zulip_log, mask_pii


class TestFormatZulipLog:
    def test_basic_fields(self):
        result = format_zulip_log("msg received", account="test", msg_id=42)
        assert result == "msg received [account=test msg_id=42]"

    def test_skips_none(self):
        result = format_zulip_log("msg", a="x", b=None, c="y")
        assert "b=" not in result
        assert "a=x" in result
        assert "c=y" in result

    def test_skips_empty_string(self):
        result = format_zulip_log("msg", a="x", b="")
        assert "b=" not in result

    def test_skips_false(self):
        result = format_zulip_log("msg", a=True, b=False)
        assert "a=True" in result
        assert "b=" not in result

    def test_no_fields(self):
        assert format_zulip_log("plain msg") == "plain msg"

    def test_json_values(self):
        result = format_zulip_log("event", data={"k": "v"})
        assert 'data={"k": "v"}' in result


class TestMaskPii:
    def test_email(self):
        assert mask_pii("alice@example.com") == "a***@example.com"
        assert mask_pii("a@b.com") == "***@b.com"

    def test_numeric_id_short(self):
        assert mask_pii("42") == "**"

    def test_numeric_id_medium(self):
        assert mask_pii("12345") == "1***5"

    def test_numeric_id_long(self):
        assert mask_pii("123456789") == "12***89"

    def test_stream_name(self):
        assert mask_pii("general") == "ge***al"

    def test_short_string(self):
        assert mask_pii("ab") == "**"
        assert mask_pii("a") == "**"

    def test_prefixed_user(self):
        assert mask_pii("user:alice@example.com") == "user:a***@example.com"

    def test_prefixed_dm(self):
        assert mask_pii("dm:alice@example.com") == "dm:a***@example.com"

    def test_prefixed_stream(self):
        assert mask_pii("stream:general") == "stream:ge***al"
        assert mask_pii("stream:general:topic") == "stream:ge***al:to***ic"

    def test_prefixed_zulip(self):
        assert mask_pii("zulip:alice@example.com") == "zulip:a***@example.com"

    def test_none(self):
        assert mask_pii(None) == ""

    def test_int(self):
        assert mask_pii(123456) == "12***56"
