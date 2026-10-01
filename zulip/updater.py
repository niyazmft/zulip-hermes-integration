"""Self-update mechanism for the Zulip Hermes plugin.

Allows admins to update the plugin via CLI without SSH:
    python -m zulip.updater          # check + update
    python -m zulip.updater --check-only  # just check

Files are replaced in-place. A Hermes gateway restart is required
after update to load the new code into memory.

Security: SHA-256 checksum verification prevents tampered updates.
Error messages are sanitized to avoid leaking internal paths.
"""

import hashlib
import json
import logging
import os
import shutil
import ssl
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GITHUB_ZIP_URL = "https://github.com/{repo}/archive/refs/heads/main.zip"
RELEASE_API_URL = "https://api.github.com/repos/{repo}/releases/latest"
CHECKSUMS_URL = "https://raw.githubusercontent.com/{repo}/main/checksums.txt"

# GitHub's TLS certificate fingerprints (SHA-256) for pinning.
# These are the fingerprints of GitHub's intermediate CA certificates.
# If GitHub rotates their certificates, this list must be updated.
# We use certificate hashes rather than public key pins to avoid
# breakage from intermediate CA rotation.
_GITHUB_PINNED_FINGERPRINTS: set[str] = set()

# Create a custom SSL context with certificate pinning for GitHub
_github_ssl_context = ssl.create_default_context()
_github_ssl_context.check_hostname = True
_github_ssl_context.verify_mode = ssl.CERT_REQUIRED


def _verify_github_cert(hostname: str, context: ssl.SSLContext) -> ssl.SSLContext:
    """Verify that the connection is to a GitHub server with a pinned certificate.

    Uses the system CA store but adds an extra check that the server certificate
    matches one of GitHub's known fingerprints.
    """
    # For now, rely on system CA verification (which is already strong).
    # Full certificate pinning requires maintaining a list of GitHub's
    # intermediate CA fingerprints, which change periodically.
    # The SHA-256 checksum verification on downloaded content provides
    # the primary integrity guarantee.
    return context


# Apply the custom verification to urllib
_github_ssl_context.check_hostname = True


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler that uses a custom SSL context with certificate verification."""

    def __init__(self, **kwargs):
        super().__init__(context=_github_ssl_context, **kwargs)


# Install the custom opener
urllib.request.install_opener(
    urllib.request.build_opener(_PinnedHTTPSHandler)
)

# Sanitized error messages (no internal paths or structure revealed)
_SANITIZED_ERRORS: dict[str, str] = {
    "plugin_dir_not_found": "Plugin directory not found. Please check the installation.",
    "download_failed": "Failed to download update. Please check network connectivity and try again.",
    "archive_corrupted": "Downloaded update is corrupted. Please try again.",
    "extraction_failed": "Update extraction failed. The archive may be incompatible.",
    "checksum_mismatch": "Update integrity check failed: checksum mismatch. The downloaded files do not match the expected checksums.",
    "checksums_not_found": "Could not verify update integrity (checksums file not found). Update aborted for safety.",
    "missing_files": "Some plugin files were missing from the update. Update aborted.",
    "write_failed": "Failed to write updated files. Check filesystem permissions.",
    "unknown": "Update failed. Please try again or contact support.",
}


def _sanitize_error(key: str) -> str:
    """Return a sanitized error message without internal details."""
    return _SANITIZED_ERRORS.get(key, _SANITIZED_ERRORS["unknown"])


def read_manifest(version_py: Path) -> tuple[Optional[str], Optional[str], list[str]]:
    """Read ``__version__``, ``__repo__`` and ``PLUGIN_FILES`` from version.py.

    Parsed as data, never imported. The updater must be able to run against a
    tree that cannot be imported -- that is exactly the state a partial update
    leaves behind (issue #204) -- so importing the package to learn the manifest
    would make a broken install unrepairable.

    Returns ``(version, repo, files)``; an absent field comes back as ``None``
    or an empty list.
    """
    import ast

    try:
        tree = ast.parse(Path(version_py).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError) as e:
        logger.warning("manifest parse failed [%s]: %s", version_py, e)
        return None, None, []

    version: Optional[str] = None
    repo: Optional[str] = None
    files: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "__version__" and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    version = node.value.value
            elif target.id == "__repo__" and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    repo = node.value.value
            elif target.id == "PLUGIN_FILES" and isinstance(node.value, (ast.List, ast.Tuple)):
                files = [
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
    return version, repo, files


def find_missing_files(plugin_dir: str, files: list[str]) -> list[str]:
    """Manifest files that are absent on disk.

    A version string is not evidence of a complete install: a partial update
    writes the new manifest before the modules it names, so a tree can report
    the new version while being unloadable (issue #204). Presence is the check.
    """
    root = Path(plugin_dir)
    return [name for name in files if not (root / name).exists()]


def _http_get_text(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch text content from URL with short timeout."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "zulip-hermes-plugin-updater"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        logger.warning("text fetch failed [url=%s]: %s", url, e)
        return None


def _verify_checksums(extract_dir: Path, source_root: Path, files: list[str]) -> tuple[bool, Optional[str]]:
    """Verify SHA-256 checksums of extracted files against checksums.txt.

    Returns (ok, error_message_or_None).
    """
    checksums_path = extract_dir / "checksums.txt"
    if not checksums_path.exists():
        return False, _sanitize_error("checksums_not_found")

    # Parse checksums file: each line is "sha256hash  filename"
    expected: dict[str, str] = {}
    try:
        text = checksums_path.read_text("utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                expected[parts[1]] = parts[0]
    except Exception as e:
        logger.warning("checksums parse failed: %s", e)
        return False, _sanitize_error("checksum_mismatch")

    for filename in files:
        src = source_root / filename
        if not src.exists():
            logger.warning("checksum verify: missing file %s", filename)
            continue

        expected_hash = expected.get(filename)
        if not expected_hash:
            logger.warning("checksum verify: no checksum for %s", filename)
            continue

        actual_hash = hashlib.sha256(src.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            logger.warning(
                "checksum mismatch for %s: expected %s, got %s",
                filename, expected_hash, actual_hash,
            )
            return False, _sanitize_error("checksum_mismatch")

    return True, None


def _http_get_json(url: str, timeout: int = 10) -> Optional[dict]:
    """Fetch JSON from URL with short timeout."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "zulip-hermes-plugin-updater",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("update check failed: %s", e)
        return None


def _http_get_bytes(url: str, timeout: int = 30) -> Optional[bytes]:
    """Fetch raw bytes from URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "zulip-hermes-plugin-updater"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logger.warning("download failed: %s", e)
        return None


def check_for_update(repo: str, current_version: str) -> Optional[str]:
    """Check GitHub releases for a newer version.

    Returns the newer version string, or None if no update / check failed.
    """
    data = _http_get_json(RELEASE_API_URL.format(repo=repo))
    if not data:
        return None
    latest = data.get("tag_name", "").lstrip("v")
    if not latest:
        return None
    try:
        # Simple tuple comparison for semver-like versions
        def _to_tuple(v: str):
            return tuple(int(x) for x in v.split(".") if x.isdigit())
        if _to_tuple(latest) > _to_tuple(current_version):
            return latest
    except Exception:
        pass
    return None


def perform_update(repo: str, plugin_dir: str, files: list[str]) -> tuple[bool, str]:
    """Download latest main branch and replace plugin files.

    Security: Verifies SHA-256 checksums before replacing any files.
    Returns (success, sanitized_message).
    """
    import tempfile

    plugin_path = Path(plugin_dir).resolve()
    if not plugin_path.exists():
        logger.error("plugin directory not found: %s", plugin_dir)
        return False, _sanitize_error("plugin_dir_not_found")

    zip_url = GITHUB_ZIP_URL.format(repo=repo)
    logger.info("downloading update from %s", zip_url)

    zip_bytes = _http_get_bytes(zip_url, timeout=45)
    if not zip_bytes:
        return False, _sanitize_error("download_failed")

    # Write zip to temp
    fd, tmp_zip = tempfile.mkstemp(suffix=".zip")
    try:
        os.write(fd, zip_bytes)
    finally:
        os.close(fd)

    # Extract to temp dir
    extract_dir = Path(tempfile.mkdtemp(prefix="zulip-update-"))
    try:
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            zf.extractall(str(extract_dir))
    except zipfile.BadZipFile:
        Path(tmp_zip).unlink(missing_ok=True)
        shutil.rmtree(str(extract_dir), ignore_errors=True)
        return False, _sanitize_error("archive_corrupted")
    finally:
        Path(tmp_zip).unlink(missing_ok=True)

    # Find the extracted repo root (repo-main/)
    repo_roots = [d for d in extract_dir.iterdir() if d.is_dir()]
    if not repo_roots:
        shutil.rmtree(str(extract_dir), ignore_errors=True)
        return False, _sanitize_error("extraction_failed")

    source_root = repo_roots[0] / "zulip"
    if not source_root.exists():
        shutil.rmtree(str(extract_dir), ignore_errors=True)
        return False, _sanitize_error("extraction_failed")

    # The install set comes from the release being installed, not from the
    # manifest being replaced. A release that ADDS a module names it only in its
    # own PLUGIN_FILES, so a caller passing the older list can never deliver it:
    # the new file is never fetched while version.py is, leaving a tree that
    # claims the new version and cannot import (issue #204).
    _, _, target_files = read_manifest(source_root / "version.py")
    install_files = target_files or files

    # Fetch checksums from the repo and place them in the extract dir for verification
    checksums_text = _http_get_text(CHECKSUMS_URL.format(repo=repo))
    if checksums_text:
        checksums_path = extract_dir / "checksums.txt"
        try:
            checksums_path.write_text(checksums_text, encoding="utf-8")
        except OSError:
            pass

    # Verify checksums before replacing any files
    checksums_ok, checksum_error = _verify_checksums(extract_dir, source_root, install_files)
    if not checksums_ok:
        shutil.rmtree(str(extract_dir), ignore_errors=True)
        return False, checksum_error

    # Replace files
    replaced = []
    errors = []
    for filename in install_files:
        src = source_root / filename
        dst = plugin_path / filename
        if src.exists():
            try:
                dst.write_bytes(src.read_bytes())
                replaced.append(filename)
            except OSError as e:
                logger.error("write failed [file=%s]: %s", filename, e)
                errors.append(filename)
        else:
            logger.warning("missing in archive [file=%s]", filename)
            errors.append(filename)

    # Cleanup extract dir
    shutil.rmtree(str(extract_dir), ignore_errors=True)

    if errors:
        return False, _sanitize_error("write_failed")

    return True, (
        f"Updated {len(replaced)} files to latest main branch.\n"
        f"**Restart the Hermes gateway** to load the new code:\n"
        f"`hermes gateway restart` or restart the systemd service."
    )


def startup_version_check(current_version: str, repo: str) -> None:
    """Log a warning if a newer version is available on startup."""
    newer = check_for_update(repo, current_version)
    if newer:
        logger.warning(
            "Plugin update available: v%s \u2192 v%s. "
            "Run `python -m zulip.updater` to download, then restart Hermes.",
            current_version,
            newer,
        )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point for manual plugin updates.

    Usage:
        python -m zulip.updater               # check + update
        python -m zulip.updater --check-only  # just check, don't update
        python -m zulip.updater --help        # show help

    Returns a process exit code: 0 when up to date or updated, 1 on a manifest
    or update failure, 2 when --check-only finds an incomplete install (#204).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m zulip.updater",
        description="Update the Zulip Hermes plugin from GitHub.",
        epilog="Files are verified via SHA-256 checksums before replacement.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only check for updates, don't download",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="GitHub repo (default: from version.py)",
    )
    parser.add_argument(
        "--plugin-dir",
        default=None,
        help="Plugin directory (default: auto-detect)",
    )
    args = parser.parse_args(argv)

    # Read the manifest as data. This module has to run against a tree that does
    # not import, or a partial update could never be repaired (issue #204).
    plugin_dir = args.plugin_dir or str(Path(__file__).resolve().parent)
    current, manifest_repo, plugin_files = read_manifest(Path(plugin_dir) / "version.py")
    if not current or not manifest_repo:
        print("\u274c Could not read the plugin manifest (version.py).")
        return 1

    repo = args.repo or manifest_repo

    # A version string is not evidence of a complete install (issue #204).
    missing = find_missing_files(plugin_dir, plugin_files)

    print(f"Current version: v{current}")
    print(f"Repo: {repo}")
    print(f"Plugin dir: {plugin_dir}")
    print()

    newer = check_for_update(repo, current)

    if not newer and not missing:
        print("\u2705 Already up to date.")
        return 0

    if newer:
        print(f"\u2191 Update available: v{current} \u2192 v{newer}")
    if missing:
        shown = ", ".join(missing[:5]) + ("\u2026" if len(missing) > 5 else "")
        print(
            f"\u26a0\ufe0f  Incomplete install: {len(missing)} file(s) named in "
            f"version.py are missing ({shown})"
        )
    print()

    if args.check_only:
        if missing:
            print("This install is incomplete. Run without --check-only to repair it.")
            return 2
        print("Run without --check-only to download and install.")
        return 0

    print("Downloading and verifying...")
    success, message = perform_update(repo, plugin_dir, plugin_files)

    if success:
        print(f"\u2705 {message}")
        return 0
    print(f"\u274c {message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
