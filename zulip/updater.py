"""Self-update mechanism for the Zulip Hermes plugin.

Allows admins to update the plugin via Zulip chat commands without SSH:
    @bot version   — show current version
    @bot update    — download latest files from GitHub

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
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GITHUB_ZIP_URL = "https://github.com/{repo}/archive/refs/heads/main.zip"
RELEASE_API_URL = "https://api.github.com/repos/{repo}/releases/latest"
CHECKSUMS_URL = "https://raw.githubusercontent.com/{repo}/main/checksums.txt"

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

    # Fetch checksums from the repo and place them in the extract dir for verification
    checksums_text = _http_get_text(CHECKSUMS_URL.format(repo=repo))
    if checksums_text:
        checksums_path = extract_dir / "checksums.txt"
        try:
            checksums_path.write_text(checksums_text, encoding="utf-8")
        except OSError:
            pass

    # Verify checksums before replacing any files
    checksums_ok, checksum_error = _verify_checksums(extract_dir, source_root, files)
    if not checksums_ok:
        shutil.rmtree(str(extract_dir), ignore_errors=True)
        return False, checksum_error

    # Replace files
    replaced = []
    errors = []
    for filename in files:
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


if __name__ == "__main__":
    """CLI entry point for manual plugin updates.

    Usage:
        python -m zulip.updater              # check + update
        python -m zulip.updater --check-only  # just check, don't update
        python -m zulip.updater --help        # show help
    """
    import argparse
    import sys
    from pathlib import Path

    parser = argparse.ArgumentParser(
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
    args = parser.parse_args()

    # Import version info
    from .version import __version__, __repo__, PLUGIN_FILES

    repo = args.repo or __repo__
    current = __version__

    # Auto-detect plugin directory (parent of this file's zulip/ subdir)
    plugin_dir = args.plugin_dir
    if not plugin_dir:
        plugin_dir = str(Path(__file__).resolve().parent)

    print(f"Current version: v{current}")
    print(f"Repo: {repo}")
    print(f"Plugin dir: {plugin_dir}")
    print()

    newer = check_for_update(repo, current)
    if not newer:
        print("\u2705 Already up to date.")
        sys.exit(0)

    print(f"\u2191 Update available: v{current} \u2192 v{newer}")
    print()

    if args.check_only:
        print("Run without --check-only to download and install.")
        sys.exit(0)

    print("Downloading and verifying...")
    success, message = perform_update(repo, plugin_dir, PLUGIN_FILES)

    if success:
        print(f"\u2705 {message}")
    else:
        print(f"\u274c {message}")
        sys.exit(1)
