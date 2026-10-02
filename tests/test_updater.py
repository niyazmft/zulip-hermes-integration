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
import zipfile
from pathlib import Path

import pytest

from zulip.updater import (
    find_missing_files,
    main,
    perform_update,
    read_manifest,
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
        """The state device 8x was left in: manifest present, modules absent."""
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
    install. Device 8x had a hand-written replacement, which is how it drifted
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
        # No newer release available, so only install integrity is under test.
        monkeypatch.setattr("zulip.updater.check_for_update", lambda *a, **k: None)
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
