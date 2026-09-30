"""Tests for zulip.refs -- validated, actionable GitHub refs.

Every test mocks the GitHub API via the injectable ``http`` fetcher; nothing
here touches the network. The security tests assert that a rejected URL never
reaches a fetcher at all.
"""

import pytest

from zulip import refs
from zulip.refs import clear_ref_cache, render_refs


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeGitHub:
    """Records every API URL it is asked for and returns a fixed response."""

    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return FakeResponse(self.status)


@pytest.fixture(autouse=True)
def _isolate_cache():
    clear_ref_cache()
    yield
    clear_ref_cache()


def _marker(url, label=None):
    if label is None:
        return f"[[zulip_ref: {url}]]"
    return f"[[zulip_ref: {url} | {label}]]"


PR_URL = "https://github.com/octo/hello/pull/128"
ISSUE_URL = "https://github.com/octo/hello/issues/7"
COMMIT_URL = "https://github.com/octo/hello/commit/abcdef1234567890"
RUN_URL = "https://github.com/octo/hello/actions/runs/42"
PR_LINK = f"[octo/hello#128]({PR_URL})"
ISSUE_LINK = f"[octo/hello#7]({ISSUE_URL})"
COMMIT_LINK = f"[octo/hello@abcdef1]({COMMIT_URL})"
RUN_LINK = f"[octo/hello run #42]({RUN_URL})"


class TestSuccessfulRendering:
    @pytest.mark.asyncio
    async def test_valid_pull_renders_link(self):
        fake = FakeGitHub(status=200)
        assert await render_refs(_marker(PR_URL), http=fake) == PR_LINK
        assert fake.calls == ["https://api.github.com/repos/octo/hello/pulls/128"]

    @pytest.mark.asyncio
    async def test_valid_issue_renders_link(self):
        fake = FakeGitHub(status=200)
        assert await render_refs(_marker(ISSUE_URL), http=fake) == ISSUE_LINK
        assert fake.calls == ["https://api.github.com/repos/octo/hello/issues/7"]

    @pytest.mark.asyncio
    async def test_valid_commit_renders_link(self):
        fake = FakeGitHub(status=200)
        assert await render_refs(_marker(COMMIT_URL), http=fake) == COMMIT_LINK

    @pytest.mark.asyncio
    async def test_valid_actions_run_renders_link(self):
        fake = FakeGitHub(status=200)
        assert await render_refs(_marker(RUN_URL), http=fake) == RUN_LINK
        assert fake.calls == ["https://api.github.com/repos/octo/hello/actions/runs/42"]

    @pytest.mark.asyncio
    async def test_explicit_label_overrides_default(self):
        fake = FakeGitHub(status=200)
        out = await render_refs(_marker(PR_URL, "the fix"), http=fake)
        assert out == f"[the fix]({PR_URL})"

    @pytest.mark.asyncio
    async def test_surrounding_text_and_multiple_refs_preserved(self):
        fake = FakeGitHub(status=200)
        text = f"See {_marker(PR_URL)} and {_marker(ISSUE_URL, 'issue 7')} please."
        out = await render_refs(text, http=fake)
        assert out == f"See {PR_LINK} and [issue 7]({ISSUE_URL}) please."

    @pytest.mark.asyncio
    async def test_marker_keyword_is_case_insensitive(self):
        fake = FakeGitHub(status=200)
        out = await render_refs(f"[[ZULIP_REF: {PR_URL}]]", http=fake)
        assert out == PR_LINK

    @pytest.mark.asyncio
    async def test_no_marker_returns_text_unchanged(self):
        fake = FakeGitHub(status=200)
        text = "nothing to see here"
        assert await render_refs(text, http=fake) == text
        assert fake.calls == []

    @pytest.mark.asyncio
    async def test_empty_text_returns_unchanged(self):
        assert await render_refs("", http=FakeGitHub()) == ""


class TestDegradation:
    @pytest.mark.asyncio
    async def test_404_degrades(self):
        fake = FakeGitHub(status=404)
        assert await render_refs(_marker(PR_URL), http=fake) == f"`{PR_URL}`"

    @pytest.mark.asyncio
    async def test_rate_limit_403_degrades(self):
        fake = FakeGitHub(status=403)
        assert await render_refs(_marker(PR_URL), http=fake) == f"`{PR_URL}`"

    @pytest.mark.asyncio
    async def test_rate_limit_429_degrades(self):
        fake = FakeGitHub(status=429)
        assert await render_refs(_marker(PR_URL), http=fake) == f"`{PR_URL}`"

    @pytest.mark.asyncio
    async def test_server_error_degrades(self):
        fake = FakeGitHub(status=503)
        assert await render_refs(_marker(PR_URL), http=fake) == f"`{PR_URL}`"

    @pytest.mark.asyncio
    async def test_timeout_degrades(self):
        fake = FakeGitHub(error=TimeoutError("timed out"))
        assert await render_refs(_marker(PR_URL), http=fake) == f"`{PR_URL}`"

    @pytest.mark.asyncio
    async def test_network_error_degrades(self):
        fake = FakeGitHub(error=ConnectionError("no route to host"))
        assert await render_refs(_marker(PR_URL), http=fake) == f"`{PR_URL}`"

    @pytest.mark.asyncio
    async def test_unexpected_exception_degrades(self):
        fake = FakeGitHub(error=RuntimeError("boom"))
        assert await render_refs(_marker(PR_URL), http=fake) == f"`{PR_URL}`"

    @pytest.mark.asyncio
    async def test_response_without_status_degrades(self):
        async def http(url):
            return object()

        assert await render_refs(_marker(PR_URL), http=http) == f"`{PR_URL}`"

    @pytest.mark.asyncio
    async def test_malformed_url_degrades_without_fetch(self):
        fake = FakeGitHub(status=200)
        out = await render_refs(_marker("not a url at all"), http=fake)
        assert out == "`not a url at all`"
        assert fake.calls == []

    @pytest.mark.asyncio
    async def test_failure_still_delivers_surrounding_reply(self):
        fake = FakeGitHub(status=404)
        text = f"Opened {_marker(PR_URL)} today."
        assert await render_refs(text, http=fake) == f"Opened `{PR_URL}` today."

    @pytest.mark.asyncio
    async def test_label_brackets_cannot_spoof_link_target(self):
        fake = FakeGitHub(status=200)
        out = await render_refs(_marker(PR_URL, "x](https://evil.test)"), http=fake)
        # The closing bracket in the label is escaped, so the markdown link
        # target stays the validated GitHub URL and cannot be spoofed.
        assert out == f"[x\\](https://evil.test)]({PR_URL})"


class TestSecurityRejections:
    OFF_HOST_AND_MALFORMED = [
        # Lookalike / off-host
        "https://github.com.evil.test/octo/hello/pull/1",
        "https://evilgithub.com/octo/hello/pull/1",
        "https://evil.test/github.com/octo/hello/pull/1",
        "https://raw.github.com/octo/hello/pull/1",
        # Port
        "https://github.com:443/octo/hello/pull/1",
        "https://github.com:8443/octo/hello/pull/1",
        # Credentials in the URL
        "https://user:pass@github.com/octo/hello/pull/1",
        "https://user@github.com/octo/hello/pull/1",
        # Query string / fragment
        "https://github.com/octo/hello/pull/1?token=abc",
        "https://github.com/octo/hello/pull/1#issuecomment-1",
        # Scheme downgrade
        "http://github.com/octo/hello/pull/1",
        # Extra path segments / trailing slash
        "https://github.com/octo/hello/pull/1/",
        "https://github.com/octo/hello/pull/1/files",
        # Not a supported ref shape
        "https://github.com/octo/hello",
        "https://github.com/octo/hello/releases/tag/v1",
        # Empty / whitespace
        "",
        "   ",
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", OFF_HOST_AND_MALFORMED)
    async def test_rejected_urls_never_fetch(self, url):
        fake = FakeGitHub(status=200)
        out = await render_refs(_marker(url), http=fake)
        assert fake.calls == [], f"fetched a rejected URL: {url!r}"
        assert "](" not in out  # never rendered as a markdown link
        assert out == f"`{url.strip()}`"

    @pytest.mark.asyncio
    async def test_api_origin_is_hardcoded(self):
        assert refs.GITHUB_API_ORIGIN == "https://api.github.com"

    @pytest.mark.asyncio
    async def test_api_origin_ignores_environment(self, monkeypatch):
        monkeypatch.setenv("GITHUB_API_ORIGIN", "https://evil.test")
        monkeypatch.setenv("ZULIP_GITHUB_API_ORIGIN", "https://evil.test")
        fake = FakeGitHub(status=200)
        await render_refs(_marker(PR_URL), http=fake)
        assert fake.calls == ["https://api.github.com/repos/octo/hello/pulls/128"]

    @pytest.mark.asyncio
    async def test_default_fetcher_sends_no_credentials(self, monkeypatch):
        captured = {}

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            return FakeResponse(200)

        monkeypatch.setattr("requests.get", fake_get)
        out = await render_refs(_marker(PR_URL), http=None)
        assert out == PR_LINK
        assert captured["url"] == "https://api.github.com/repos/octo/hello/pulls/128"
        header_names = {name.lower() for name in captured["headers"]}
        assert "authorization" not in header_names


class TestRateBudget:
    @pytest.mark.asyncio
    async def test_at_most_max_refs_validated_per_message(self):
        fake = FakeGitHub(status=200)
        text = " ".join(
            _marker(f"https://github.com/octo/hello/pull/{n}") for n in range(1, 6)
        )
        await render_refs(text, http=fake)
        assert len(fake.calls) == refs.MAX_REFS_DEFAULT == 3

    @pytest.mark.asyncio
    async def test_custom_max_refs_respected(self):
        fake = FakeGitHub(status=200)
        text = " ".join(
            _marker(f"https://github.com/octo/hello/pull/{n}") for n in range(1, 4)
        )
        await render_refs(text, max_refs=1, http=fake)
        assert len(fake.calls) == 1

    @pytest.mark.asyncio
    async def test_refs_beyond_budget_degrade_to_plain_text(self):
        fake = FakeGitHub(status=200)
        fourth = "https://github.com/octo/hello/pull/4"
        text = " ".join(
            _marker(f"https://github.com/octo/hello/pull/{n}") for n in range(1, 5)
        )
        out = await render_refs(text, http=fake)
        assert f"[octo/hello#4]({fourth})" not in out
        assert f"`{fourth}`" in out


class TestCaching:
    @pytest.mark.asyncio
    async def test_cache_hit_avoids_second_fetch(self):
        fake = FakeGitHub(status=200)
        assert await render_refs(_marker(PR_URL), http=fake) == PR_LINK
        assert await render_refs(_marker(PR_URL), http=fake) == PR_LINK
        assert len(fake.calls) == 1

    @pytest.mark.asyncio
    async def test_cache_expires_after_ttl(self, monkeypatch):
        clock = {"t": 1000.0}
        monkeypatch.setattr(refs, "_now", lambda: clock["t"])
        fake = FakeGitHub(status=200)

        await render_refs(_marker(PR_URL), http=fake)
        assert len(fake.calls) == 1

        clock["t"] += refs.CACHE_TTL_SECONDS - 1
        await render_refs(_marker(PR_URL), http=fake)
        assert len(fake.calls) == 1  # still cached

        clock["t"] += 2
        await render_refs(_marker(PR_URL), http=fake)
        assert len(fake.calls) == 2  # TTL elapsed, revalidated

    @pytest.mark.asyncio
    async def test_cached_failure_degrades_without_refetch(self):
        failing = FakeGitHub(status=404)
        assert await render_refs(_marker(PR_URL), http=failing) == f"`{PR_URL}`"

        ok = FakeGitHub(status=200)
        # Same URL, still inside the TTL: cached failure applies, no fetch.
        assert await render_refs(_marker(PR_URL), http=ok) == f"`{PR_URL}`"
        assert ok.calls == []
