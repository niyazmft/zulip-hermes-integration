"""Tests for zulip.version and zulip.updater."""

from unittest.mock import patch, MagicMock

import pytest

from zulip.version import __version__, __repo__, PLUGIN_FILES


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
