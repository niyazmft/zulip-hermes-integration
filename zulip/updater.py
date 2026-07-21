"""Self-update mechanism for the Zulip Hermes plugin.

Allows admins to update the plugin via Zulip chat commands without SSH:
    @bot version   — show current version
    @bot update    — download latest files from GitHub

Files are replaced in-place. A Hermes gateway restart is required
after update to load the new code into memory.
"""

import json
import logging
import os
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GITHUB_ZIP_URL = "https://github.com/{repo}/archive/refs/heads/main.zip"
RELEASE_API_URL = "https://api.github.com/repos/{repo}/releases/latest"


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

    Returns (success, message).
    """
    import tempfile

    plugin_path = Path(plugin_dir).resolve()
    if not plugin_path.exists():
        return False, f"Plugin directory not found: {plugin_dir}"

    zip_url = GITHUB_ZIP_URL.format(repo=repo)
    logger.info("downloading update from %s", zip_url)

    zip_bytes = _http_get_bytes(zip_url, timeout=45)
    if not zip_bytes:
        return False, "Failed to download update from GitHub. Check network."

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
        return False, "Downloaded archive is corrupted."
    finally:
        Path(tmp_zip).unlink(missing_ok=True)

    # Find the extracted repo root (repo-main/)
    repo_roots = [d for d in extract_dir.iterdir() if d.is_dir()]
    if not repo_roots:
        return False, "Archive extraction failed — no directory found."
    source_root = repo_roots[0] / "zulip"
    if not source_root.exists():
        return False, "Archive does not contain expected 'zulip/' directory."

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
                errors.append(f"{filename}: {e}")
        else:
            errors.append(f"{filename}: missing in archive")

    # Cleanup extract dir
    import shutil
    shutil.rmtree(str(extract_dir), ignore_errors=True)

    if errors:
        return False, f"Updated {len(replaced)} files, but errors: {'; '.join(errors)}"

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
            "Plugin update available: v%s → v%s. "
            "Type '@bot update' in Zulip to download, then restart Hermes.",
            current_version,
            newer,
        )
