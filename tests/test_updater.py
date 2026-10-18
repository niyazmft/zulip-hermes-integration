"""Tests for the self-updater's manifest handling (issue #204).

Background: the updater used to fetch the files named in the *installed*
``PLUGIN_FILES``. A release that ADDS a module names it only in its own
manifest, so it was never downloaded -- while ``version.py`` (which is in the
old list) was. The result was a tree that reported the new version, shipped a
new ``adapter.py``, and could not import:

    ModuleNotFoundError: No module named 'zulip.display_names'

and it could not self-repair, because the updater imported the package in order
to read its own manifest.

These tests pin the three properties that fix it:
1. the manifest is read as data, never imported;
2. the install set comes from the *target* manifest;
3. an incomplete install is reported rather than called "up to date".
"""

import hashlib
import io
import logging
import zipfile
from pathlib import Path

import pytest

from zulip.updater import (
    LookupFailed,
    UpdateCheck,
    check_for_update,
    find_missing_files,
    main,
    perform_update,
    read_manifest,
    startup_version_check,
)


def _manifest_text(version: str, files: list[str]) -> str:
    """A minimal version.py declaring exactly ``files``."""
    body = "".join(f'    "{name}",\n' for name in files)
    return (
        '"""Single source of truth for the plugin version."""\n\n'
        f'__version__ = "{version}"\n'
        '__repo__ = "owner/repo"\n'
        '__min_hermes__ = "0.18.2"\n\n'
        f"PLUGIN_FILES = [\n{body}]\n"
    )


def _write_version_py(path: Path, version: str, files: list[str]) -> None:
    path.write_text(_manifest_text(version, files), encoding="utf-8")


class TestReadManifest:
    def test_reads_version_repo_and_files(self, tmp_path):
        names = ["__init__.py", "version.py", "plugin.yaml", "a.py"]
        _write_version_py(tmp_path / "version.py", "1.2.3", names)
        version, repo, files = read_manifest(tmp_path / "version.py")
        assert version == "1.2.3"
        assert repo == "owner/repo"
        assert files == names

    def test_does_not_import_the_package(self, tmp_path):
        """A manifest whose module raises on import must still be readable.

        This is the point: the updater has to run against a tree that cannot be
        imported, which is exactly what a partial update leaves behind.
        """
        (tmp_path / "version.py").write_text(
            "raise RuntimeError('deliberately unimportable')\n"
            "__version__ = '9.9.9'\n"
            "__repo__ = 'owner/repo'\n"
            "PLUGIN_FILES = ['version.py']\n",
            encoding="utf-8",
        )
        version, repo, files = read_manifest(tmp_path / "version.py")
        assert (version, repo, files) == ("9.9.9", "owner/repo", ["version.py"])

    def test_missing_file_is_not_fatal(self, tmp_path):
        assert read_manifest(tmp_path / "nope.py") == (None, None, [])

    def test_malformed_file_is_not_fatal(self, tmp_path):
        broken = tmp_path / "version.py"
        broken.write_text("this is not python(((\n", encoding="utf-8")
        assert read_manifest(broken) == (None, None, [])

    def test_non_string_entries_are_skipped(self, tmp_path):
        (tmp_path / "version.py").write_text(
            "__version__ = '1.0.0'\n"
            "__repo__ = 'owner/repo'\n"
            "PLUGIN_FILES = ['ok.py', 42, None]\n",
            encoding="utf-8",
        )
        _, _, files = read_manifest(tmp_path / "version.py")
        assert files == ["ok.py"]


class TestFindMissingFiles:
    def test_detects_absent_files(self, tmp_path):
        (tmp_path / "present.py").write_text("x", encoding="utf-8")
        assert find_missing_files(str(tmp_path), ["present.py", "gone.py"]) == ["gone.py"]

    def test_complete_install_reports_nothing(self, tmp_path):
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        assert find_missing_files(str(tmp_path), ["a.py"]) == []


def _make_archive(files: dict[str, str]) -> bytes:
    """A GitHub-style source zip: ``<repo>-main/zulip/<files>``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in files.items():
            zf.writestr(f"zulip-hermes-integration-main/zulip/{name}", body)
    return buf.getvalue()


def _checksums_block(files: dict[str, str]) -> str:
    return "".join(
        f"{hashlib.sha256(body.encode()).hexdigest()}  {name}\n"
        for name, body in files.items()
    )


class TestPerformUpdateUsesTargetManifest:
    """The regression tests for #204."""

    def _install(self, monkeypatch, tmp_path, installed_files, target_files):
        """Run perform_update with a caller-supplied (older) file list.

        The target archive's manifest lists exactly the files it ships, so any
        file the caller's list omits can only arrive via the target manifest.
        """
        names = sorted(set(target_files) | {"version.py"})
        archive_files = dict(target_files)
        archive_files["version.py"] = _manifest_text("2.0.0", names)
        archive = _make_archive(archive_files)
        checksums = _checksums_block(archive_files)

        monkeypatch.setattr("zulip.updater._http_get_bytes", lambda *a, **k: archive)
        monkeypatch.setattr("zulip.updater._http_get_text", lambda *a, **k: checksums)

        plugin_dir = tmp_path / "installed"
        plugin_dir.mkdir()
        for name, body in installed_files.items():
            (plugin_dir / name).write_text(body, encoding="utf-8")

        ok, message = perform_update("owner/repo", str(plugin_dir), list(installed_files))
        return ok, message, plugin_dir

    def test_module_added_by_the_release_is_installed(self, monkeypatch, tmp_path):
        installed = {"version.py": "old", "__init__.py": "old"}
        target = {
            "__init__.py": "new",
            "brand_new_module.py": "print('hello')\n",
        }
        ok, message, plugin_dir = self._install(monkeypatch, tmp_path, installed, target)
        assert ok, message
        # Named only by the TARGET manifest -- the old list could never fetch it.
        assert (plugin_dir / "brand_new_module.py").exists()

    def test_existing_files_are_still_replaced(self, monkeypatch, tmp_path):
        installed = {"version.py": "old", "__init__.py": "old"}
        target = {"__init__.py": "new"}
        ok, message, plugin_dir = self._install(monkeypatch, tmp_path, installed, target)
        assert ok, message
        assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "new"

    def test_a_broken_install_is_repairable(self, monkeypatch, tmp_path):
        """The broken state an incomplete install leaves behind: manifest present, modules absent."""
        installed = {"version.py": "old"}
        target = {
            "__init__.py": "new",
            "display_names.py": "class DisplayNameCache: pass\n",
        }
        ok, message, plugin_dir = self._install(monkeypatch, tmp_path, installed, target)
        assert ok, message
        assert (plugin_dir / "display_names.py").exists()
        assert (plugin_dir / "__init__.py").exists()

    def test_missing_source_file_fails_loudly(self, monkeypatch, tmp_path):
        """A declared file absent from the archive must not silently pass."""
        archive_files = {"version.py": _manifest_text("2.0.0", ["version.py", "ghost.py"])}
        archive = _make_archive(archive_files)
        monkeypatch.setattr("zulip.updater._http_get_bytes", lambda *a, **k: archive)
        monkeypatch.setattr(
            "zulip.updater._http_get_text", lambda *a, **k: _checksums_block(archive_files)
        )
        plugin_dir = tmp_path / "installed"
        plugin_dir.mkdir()
        (plugin_dir / "version.py").write_text("old", encoding="utf-8")

        ok, _ = perform_update("owner/repo", str(plugin_dir), ["version.py"])
        assert ok is False


class TestUpdateShellScriptShips:
    """update.sh must reach the installed plugin directory (#205/#207/#208).

    The README documents ``bash ~/.hermes/plugins/zulip/update.sh``, but the
    script lived at the repo root and was absent from PLUGIN_FILES -- so the
    updater never deployed it and that documented path did not exist on a fresh
    install. A hand-written replacement on one deployment is how it drifted
    into a variant that claimed restarts it never performed.
    """

    def test_listed_in_plugin_files(self):
        from zulip.version import PLUGIN_FILES

        assert "update.sh" in PLUGIN_FILES

    def test_present_in_the_package(self):
        package_dir = Path(__file__).parent.parent / "zulip"
        assert (package_dir / "update.sh").is_file()

    def test_checksummed(self):
        """An unchecksummed file is installed unverified."""
        checksums = (Path(__file__).parent.parent / "checksums.txt").read_text(
            encoding="utf-8"
        )
        names = [line.split(None, 1)[1] for line in checksums.splitlines() if line.strip()]
        assert "update.sh" in names

    def test_every_declared_file_exists(self):
        from zulip.version import PLUGIN_FILES

        package_dir = Path(__file__).parent.parent / "zulip"
        missing = [name for name in PLUGIN_FILES if not (package_dir / name).exists()]
        assert missing == []

    def test_deployed_script_is_executable(self, monkeypatch, tmp_path):
        """write_bytes drops the mode, so the updater must restore it."""
        archive_files = {
            "version.py": _manifest_text("2.0.0", ["version.py", "update.sh"]),
            "update.sh": "#!/bin/bash\necho hi\n",
        }
        archive = _make_archive(archive_files)
        monkeypatch.setattr("zulip.updater._http_get_bytes", lambda *a, **k: archive)
        monkeypatch.setattr(
            "zulip.updater._http_get_text", lambda *a, **k: _checksums_block(archive_files)
        )
        plugin_dir = tmp_path / "installed"
        plugin_dir.mkdir()
        (plugin_dir / "version.py").write_text("old", encoding="utf-8")

        ok, message = perform_update("owner/repo", str(plugin_dir), ["version.py"])
        assert ok, message
        mode = (plugin_dir / "update.sh").stat().st_mode
        assert mode & 0o111, "deployed update.sh must be executable"


class TestCheckOnlyDetectsIncompleteInstall:
    """--check-only must not call a broken tree 'up to date'."""

    def _run_cli(self, monkeypatch, plugin_dir, argv):
        # A lookup that ANSWERED that nothing is newer: only install integrity
        # is under test here. The old stub returned None, the same value a
        # failed lookup used to return -- which is issue #264's conflation, so
        # this stub now says which fact it means.
        monkeypatch.setattr(
            "zulip.updater.check_for_update",
            lambda *a, **k: UpdateCheck(status="current", latest="1.0.0"),
        )
        return main(["--plugin-dir", str(plugin_dir), *argv])

    def test_incomplete_install_exits_nonzero(self, monkeypatch, tmp_path, capsys):
        _write_version_py(tmp_path / "version.py", "1.0.0", ["version.py", "absent.py"])
        (tmp_path / "version.py").exists()  # written by the helper

        code = self._run_cli(monkeypatch, tmp_path, ["--check-only"])
        out = capsys.readouterr().out
        assert code == 2
        assert "Incomplete install" in out
        assert "absent.py" in out

    def test_complete_install_reports_up_to_date(self, monkeypatch, tmp_path, capsys):
        # version.py is written by the helper; do not clobber it afterwards.
        _write_version_py(tmp_path / "version.py", "1.0.0", ["version.py"])
        assert (tmp_path / "version.py").exists()

        code = self._run_cli(monkeypatch, tmp_path, ["--check-only"])
        out = capsys.readouterr().out
        assert code == 0
        assert "Already up to date" in out

    def test_unreadable_manifest_fails_cleanly(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "version.py").write_text("not python(((\n", encoding="utf-8")
        code = self._run_cli(monkeypatch, tmp_path, ["--check-only"])
        assert code == 1
        assert "Could not read the plugin manifest" in capsys.readouterr().out


class TestFailedLookupIsNotUpToDate:
    """#264: a lookup that did not answer is a third fact, not "up to date".

    ``check_for_update`` used to return ``None`` both for "the latest release
    is not newer" and for "the lookup failed", and ``main`` printed the
    reassuring answer for both. The two facts now have two values, and a failed
    lookup exits non-zero so cron / ``update.sh`` can tell them apart.
    """

    FAILED = UpdateCheck(status="failed", error="HTTP Error 403: rate limit exceeded")

    def _run(self, monkeypatch, plugin_dir, argv, check):
        monkeypatch.setattr("zulip.updater.check_for_update", lambda *a, **k: check)
        return main(["--plugin-dir", str(plugin_dir), *argv])

    def test_failed_lookup_exits_nonzero_and_names_the_failure(
        self, monkeypatch, tmp_path, capsys
    ):
        _write_version_py(tmp_path / "version.py", "1.0.0", ["version.py"])

        code = self._run(monkeypatch, tmp_path, ["--check-only"], self.FAILED)
        out = capsys.readouterr().out

        assert code == 3, "a check that could not run must not exit 0"
        assert "Could not check for updates" in out
        assert "403" in out, "stdout must name the failure, not only logger.warning"
        assert "Already up to date" not in out

    def test_failed_lookup_without_check_only_is_not_reported_as_nothing_to_do(
        self, monkeypatch, tmp_path, capsys
    ):
        _write_version_py(tmp_path / "version.py", "1.0.0", ["version.py"])

        code = self._run(monkeypatch, tmp_path, [], self.FAILED)
        out = capsys.readouterr().out

        assert code == 3
        assert "Already up to date" not in out
        assert "Downloading" not in out, "nothing was compared, so nothing may be installed"

    def test_failed_lookup_does_not_hide_an_incomplete_install(
        self, monkeypatch, tmp_path, capsys
    ):
        """The integrity answer must not depend on the network."""
        _write_version_py(
            tmp_path / "version.py", "1.0.0", ["version.py", "absent.py"]
        )

        code = self._run(monkeypatch, tmp_path, ["--check-only"], self.FAILED)
        out = capsys.readouterr().out

        assert code == 2
        assert "Incomplete install" in out
        assert "absent.py" in out

    def test_successful_lookup_with_nothing_newer_exits_0(
        self, monkeypatch, tmp_path, capsys
    ):
        _write_version_py(tmp_path / "version.py", "1.0.0", ["version.py"])
        check = UpdateCheck(status="current", latest="1.0.0")

        code = self._run(monkeypatch, tmp_path, ["--check-only"], check)

        assert code == 0
        assert "Already up to date" in capsys.readouterr().out

    def test_successful_lookup_with_a_newer_release_still_reports_it(
        self, monkeypatch, tmp_path, capsys
    ):
        _write_version_py(tmp_path / "version.py", "1.0.0", ["version.py"])
        check = UpdateCheck(status="update", latest="2.0.0")

        code = self._run(monkeypatch, tmp_path, ["--check-only"], check)
        out = capsys.readouterr().out

        assert code == 0
        assert "Update available: v1.0.0 \u2192 v2.0.0" in out


class TestCheckForUpdateDistinguishesFailureFromNoUpdate:
    """The two facts, at the source: one value each."""

    def _check(self, monkeypatch, impl):
        monkeypatch.setattr("zulip.updater._http_get_json", impl)
        return check_for_update("owner/repo", "1.0.0")

    def test_lookup_error_is_failed_not_current(self, monkeypatch):
        def boom(*a, **k):
            raise LookupFailed("HTTP Error 403: rate limit exceeded")

        result = self._check(monkeypatch, boom)

        assert result.status == "failed"
        assert result.latest is None
        assert "403" in result.error

    def test_no_newer_release_is_current(self, monkeypatch):
        result = self._check(monkeypatch, lambda *a, **k: {"tag_name": "v1.0.0"})
        assert result.status == "current"
        assert result.error is None

    def test_newer_release_is_update(self, monkeypatch):
        result = self._check(monkeypatch, lambda *a, **k: {"tag_name": "v2.0.0"})
        assert result.status == "update"
        assert result.latest == "2.0.0"

    def test_release_without_a_comparable_version_is_failed(self, monkeypatch):
        result = self._check(monkeypatch, lambda *a, **k: {"tag_name": "nightly"})
        assert result.status == "failed"
        assert result.latest is None
        assert result.error

    def test_empty_payload_is_failed(self, monkeypatch):
        result = self._check(monkeypatch, lambda *a, **k: {})
        assert result.status == "failed"
        assert result.error


class TestStartupVersionCheckReportsAFailedLookup:
    """#264: the connect-time notifier must not silently decide "no update"."""

    def _text(self, caplog):
        return " ".join(r.getMessage() for r in caplog.records)

    def test_failed_lookup_warns_and_does_not_claim_an_update(
        self, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            "zulip.updater.check_for_update",
            lambda *a, **k: UpdateCheck(
                status="failed", error="HTTP Error 503: Service Unavailable"
            ),
        )

        with caplog.at_level(logging.WARNING, logger="zulip.updater"):
            startup_version_check("1.0.0", "owner/repo")

        text = self._text(caplog)
        assert "503" in text, "the operator must be told the check failed"
        assert "update available" not in text.lower(), "we do not know that"

    def test_nothing_newer_is_quiet(self, monkeypatch, caplog):
        monkeypatch.setattr(
            "zulip.updater.check_for_update",
            lambda *a, **k: UpdateCheck(status="current", latest="1.0.0"),
        )

        with caplog.at_level(logging.WARNING, logger="zulip.updater"):
            startup_version_check("1.0.0", "owner/repo")

        assert caplog.records == []

    def test_newer_release_is_still_announced(self, monkeypatch, caplog):
        monkeypatch.setattr(
            "zulip.updater.check_for_update",
            lambda *a, **k: UpdateCheck(status="update", latest="2.0.0"),
        )

        with caplog.at_level(logging.WARNING, logger="zulip.updater"):
            startup_version_check("1.0.0", "owner/repo")

        text = self._text(caplog)
        assert "2.0.0" in text


class TestHttpGetJsonSignalsFailure:
    """The HTTP layer must fail *loudly and specifically*, on every path.

    Observed live while delegating this fix: a generated ``_http_get_json``
    caught ``HTTPError`` and ``socket.timeout``, neither of which the module
    imports, so the failure path raised ``NameError`` while handling the very
    network failure #264 is about. The ``py_compile`` gate passed it -- these
    tests are what actually catch it.
    """

    def _fake_response(self, body: bytes):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return body

        return _Resp()

    def test_transport_failure_raises_lookup_failed(self, monkeypatch):
        import urllib.error

        from zulip.updater import LookupFailed, _http_get_json

        def boom(*a, **k):
            raise urllib.error.URLError("Name or service not known")

        monkeypatch.setattr("zulip.updater.urllib.request.urlopen", boom)

        with pytest.raises(LookupFailed, match="Name or service not known"):
            _http_get_json("https://api.github.com/repos/owner/repo/releases/latest")

    def test_any_exception_type_is_wrapped(self, monkeypatch):
        from zulip.updater import LookupFailed, _http_get_json

        def boom(*a, **k):
            raise RuntimeError("proxy said no")

        monkeypatch.setattr("zulip.updater.urllib.request.urlopen", boom)

        with pytest.raises(LookupFailed, match="proxy said no"):
            _http_get_json("https://api.github.com/x")

    def test_non_object_payload_is_a_failure(self, monkeypatch):
        from zulip.updater import LookupFailed, _http_get_json

        monkeypatch.setattr(
            "zulip.updater.urllib.request.urlopen",
            lambda *a, **k: self._fake_response(b'["not", "an", "object"]'),
        )

        with pytest.raises(LookupFailed, match="not a JSON object"):
            _http_get_json("https://api.github.com/x")

    def test_success_returns_the_object(self, monkeypatch):
        from zulip.updater import _http_get_json

        monkeypatch.setattr(
            "zulip.updater.urllib.request.urlopen",
            lambda *a, **k: self._fake_response(b'{"tag_name": "v9.9.9"}'),
        )

        assert _http_get_json("https://api.github.com/x") == {"tag_name": "v9.9.9"}
