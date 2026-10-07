"""Tests for zulip.version and zulip.updater."""

import re
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from zulip.version import __version__, __repo__, PLUGIN_FILES

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestVersionInfo:
    def test_version_is_semver_like(self):
        parts = __version__.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_repo_has_owner_and_name(self):
        assert "/" in __repo__
        owner, name = __repo__.split("/")
        assert owner and name

    def test_plugin_files_listed(self):
        assert "adapter.py" in PLUGIN_FILES
        assert "version.py" in PLUGIN_FILES
        assert "plugin.yaml" in PLUGIN_FILES

    def test_every_package_module_is_shipped(self):
        """The self-updater only delivers files named in PLUGIN_FILES.

        A module missing here would be left behind by an update while
        adapter.py still imports it, so the plugin would not load
        (PR review catch).
        """
        package_dir = Path(__file__).resolve().parent.parent / "zulip"
        modules = {p.name for p in package_dir.glob("*.py")} | {"plugin.yaml"}
        missing = modules - set(PLUGIN_FILES)
        assert not missing, f"not shipped by the updater: {sorted(missing)}"

    def test_plugin_yaml_version_matches_version_module(self):
        """The manifest is where the gateway reads the version, and nothing reads it back.

        ``zulip/version.py`` declares itself the single source of truth, but the
        plugin never reads this field -- it imports ``__version__`` -- so a
        mismatch breaks nothing at import and no runtime path notices. What the
        gateway reports for the installed plugin comes from here, which is why
        the two have to agree.

        ``checksums.txt`` cannot catch this: it hashes the file's bytes, so a
        manifest whose ``version:`` disagrees has a *correct* checksum. It stays
        internally consistent while being wrong about what it means.
        """
        manifest = REPO_ROOT / "zulip" / "plugin.yaml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert data["version"] == __version__, (
            f"zulip/plugin.yaml says {data['version']!r} but version.py says "
            f"{__version__!r}; a release bumps both (docs/RELEASING.md steps 2-3)"
        )

    def test_changelog_records_this_version(self):
        """A release moves the version and its CHANGELOG entry in the same PR.

        ``scripts/release_notes.py <version>`` reads the section named for the
        version to build the release body, so a missing entry is discovered at
        release time -- with the tag already pushed -- instead of here, where it
        costs nothing.

        Matches a heading, not prose: the version also appears inside older
        entries, so a substring check would pass for the wrong reason.
        """
        changelog = REPO_ROOT / "CHANGELOG.md"
        headings = re.findall(
            r"^## \[([^\]]+)\]",
            changelog.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert __version__ in headings, (
            f"CHANGELOG.md has no '## [{__version__}]' section (found {headings[:3]}); "
            "scripts/release_notes.py would fail to build the release body"
        )


class TestUpdaterCheck:
    @patch("zulip.updater._http_get_json")
    def test_no_update_when_same_version(self, mock_get):
        from zulip.updater import check_for_update
        mock_get.return_value = {"tag_name": f"v{__version__}"}
        result = check_for_update(__repo__, __version__)
        assert result is None

    @patch("zulip.updater._http_get_json")
    def test_update_available(self, mock_get):
        from zulip.updater import check_for_update
        mock_get.return_value = {"tag_name": "v99.0.0"}
        result = check_for_update(__repo__, __version__)
        assert result == "99.0.0"

    @patch("zulip.updater._http_get_json")
    def test_check_failed_gracefully(self, mock_get):
        from zulip.updater import check_for_update
        mock_get.return_value = None
        result = check_for_update(__repo__, __version__)
        assert result is None


class TestUpdaterPerform:
    def test_update_fails_if_dir_missing(self, tmp_path):
        from zulip.updater import perform_update
        ok, msg = perform_update(__repo__, str(tmp_path / "nonexistent"), ["adapter.py"])
        assert ok is False
        assert "not found" in msg

    @patch("zulip.updater._http_get_bytes")
    def test_update_fails_on_bad_download(self, mock_get, tmp_path):
        from zulip.updater import perform_update
        mock_get.return_value = None
        plugin_dir = tmp_path / "zulip"
        plugin_dir.mkdir()
        ok, msg = perform_update(__repo__, str(plugin_dir), ["adapter.py"])
        assert ok is False
        assert "download" in msg.lower()


class TestStartupCheck:
    @patch("zulip.updater.check_for_update")
    @patch("zulip.updater.logger")
    def test_logs_warning_when_update_available(self, mock_logger, mock_check):
        from zulip.updater import startup_version_check
        mock_check.return_value = "99.0.0"
        startup_version_check(__version__, __repo__)
        mock_logger.warning.assert_called_once()
        assert "99.0.0" in str(mock_logger.warning.call_args)

    @patch("zulip.updater.check_for_update")
    @patch("zulip.updater.logger")
    def test_silent_when_up_to_date(self, mock_logger, mock_check):
        from zulip.updater import startup_version_check
        mock_check.return_value = None
        startup_version_check(__version__, __repo__)
        mock_logger.warning.assert_not_called()


class TestUpdaterSanitizeError:
    """Tests for _sanitize_error — sanitized error messages."""

    def test_known_error_key(self):
        from zulip.updater import _sanitize_error
        msg = _sanitize_error("download_failed")
        assert "network" in msg.lower()
        assert "/" not in msg  # No internal paths

    def test_unknown_error_key_falls_back(self):
        from zulip.updater import _sanitize_error
        msg = _sanitize_error("nonexistent_key")
        assert "try again" in msg.lower()

    def test_no_internal_paths_in_any_message(self):
        from zulip.updater import _sanitize_error, _SANITIZED_ERRORS
        for key in _SANITIZED_ERRORS:
            msg = _sanitize_error(key)
            assert "/" not in msg, f"{key} contains path: {msg}"
            assert "zulip" not in msg.lower() or key == "unknown", f"{key} contains 'zulip': {msg}"


class TestUpdaterHttpGetText:
    """Tests for _http_get_text."""

    @patch("urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        from zulip.updater import _http_get_text
        mock_response = MagicMock()
        mock_response.read.return_value = b"hello world"
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        result = _http_get_text("https://example.com/file.txt")
        assert result == "hello world"

    @patch("urllib.request.urlopen", side_effect=RuntimeError("timeout"))
    def test_failure_returns_none(self, mock_urlopen):
        from zulip.updater import _http_get_text
        result = _http_get_text("https://example.com/file.txt")
        assert result is None


class TestUpdaterVerifyChecksums:
    """Tests for _verify_checksums."""

    def test_missing_checksums_file(self, tmp_path):
        from zulip.updater import _verify_checksums
        ok, error = _verify_checksums(tmp_path, tmp_path, ["adapter.py"])
        assert ok is False
        assert "checksums" in error.lower()

    def test_matching_checksums(self, tmp_path):
        from zulip.updater import _verify_checksums
        import hashlib

        # Create a file and its checksum
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")

        checksum = hashlib.sha256(test_file.read_bytes()).hexdigest()
        checksums_file = tmp_path / "checksums.txt"
        checksums_file.write_text(f"{checksum}  test.py\n")

        ok, error = _verify_checksums(tmp_path, tmp_path, ["test.py"])
        assert ok is True
        assert error is None

    def test_mismatched_checksums(self, tmp_path):
        from zulip.updater import _verify_checksums

        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")

        checksums_file = tmp_path / "checksums.txt"
        checksums_file.write_text(f"{'0' * 64}  test.py\n")  # Wrong hash

        ok, error = _verify_checksums(tmp_path, tmp_path, ["test.py"])
        assert ok is False
        assert "mismatch" in error.lower()

    def test_skips_missing_files(self, tmp_path):
        from zulip.updater import _verify_checksums
        import hashlib

        # Create checksum for a file that doesn't exist
        checksums_file = tmp_path / "checksums.txt"
        checksums_file.write_text(f"{'0' * 64}  nonexistent.py\n")

        ok, error = _verify_checksums(tmp_path, tmp_path, ["nonexistent.py"])
        # Should pass because missing files are skipped
        assert ok is True

    def test_ignores_comments_and_empty_lines(self, tmp_path):
        from zulip.updater import _verify_checksums
        import hashlib

        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")

        checksum = hashlib.sha256(test_file.read_bytes()).hexdigest()
        checksums_file = tmp_path / "checksums.txt"
        checksums_file.write_text(
            f"# This is a comment\n\n{checksum}  test.py\n"
        )

        ok, error = _verify_checksums(tmp_path, tmp_path, ["test.py"])
        assert ok is True
