"""Automatic clean-up of files the MCP server saved to ~/Downloads/Moodle.

Only files moodle_download created are tracked (in a private manifest next to the config), so anything you put
in the folder yourself is never touched. A tracked file is deleted once it's older than the retention period
(MOODLE_MCP_DOWNLOAD_DAYS, default 7; 0 turns clean-up off), unless you changed it since: an edited file
is yours now and is dropped from the manifest instead. A file you moved elsewhere is simply forgotten.
Folders that become empty are removed too, up to (not including) ~/Downloads/Moodle itself.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import client
from .client import DOWNLOAD_ROOT, _write_private

DEFAULT_DAYS = 7
CHECK_EVERY = 3600  # seconds between automatic clean-ups


def manifest_path() -> Path:
    return client.CONFIG_PATH.parent / "downloads.json"


def root() -> Path:
    return Path(DOWNLOAD_ROOT).expanduser()


def retention_days() -> float:
    """Days to keep downloads. MOODLE_MCP_DOWNLOAD_DAYS=0 (or off/never) turns automatic clean-up off."""
    raw = os.environ.get("MOODLE_MCP_DOWNLOAD_DAYS", "").strip().lower()
    if not raw:
        return DEFAULT_DAYS
    if raw in ("off", "never", "no", "false"):
        return 0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_DAYS


def _load() -> dict:
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("files"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"files": [], "last_cleanup": 0}


def _save(data: dict) -> None:
    _write_private(manifest_path(), json.dumps(data, indent=1).encode("utf-8"))


def _inside_root(p: Path) -> bool:
    try:
        p.resolve().relative_to(root().resolve())
        return True
    except (ValueError, OSError):
        return False


def record(path: str | Path, now: float | None = None) -> None:
    """Remember a file the MCP server just downloaded, with its size and mtime to spot later edits."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return
    data = _load()
    data["files"] = [f for f in data["files"] if f.get("path") != str(p)]
    data["files"].append({"path": str(p), "size": st.st_size, "mtime": st.st_mtime, "saved_at": now or time.time()})
    _save(data)


def _unchanged(p: Path, entry: dict) -> bool:
    try:
        st = p.stat()
    except OSError:
        return False
    return st.st_size == entry.get("size") and abs(st.st_mtime - float(entry.get("mtime", 0))) < 2


def _prune_empty_dirs(start: Path) -> None:
    top = root().resolve()
    d = start
    while d != top and _inside_root(d):
        try:
            d.rmdir()  # only succeeds when empty
        except OSError:
            return
        d = d.parent


def tracked() -> list[dict]:
    """Tracked downloads that still exist, oldest first, with their age in days."""
    now = time.time()
    out = []
    for e in _load()["files"]:
        p = Path(e.get("path", ""))
        if p.exists():
            out.append({"path": str(p), "age_days": round((now - float(e.get("saved_at", now))) / 86400, 1),
                        "edited": not _unchanged(p, e)})
    return sorted(out, key=lambda e: -e["age_days"])


def cleanup(days: float | None = None, everything: bool = False, now: float | None = None) -> dict:
    """Delete tracked downloads older than `days` (or all tracked ones with everything=True).

    Edited files are kept and forgotten; missing ones are forgotten. Returns what happened."""
    now = now or time.time()
    days = retention_days() if days is None else days
    data = _load()
    removed, kept_edited, remaining = [], [], []
    for e in data["files"]:
        p = Path(e.get("path", ""))
        if not p.exists() or not _inside_root(p):
            continue  # moved, deleted, or a manifest entry that points outside the folder: forget it
        old = everything or (days > 0 and now - float(e.get("saved_at", now)) > days * 86400)
        if not old:
            remaining.append(e)
        elif not _unchanged(p, e):
            kept_edited.append(str(p))  # you changed it, so it's yours now
        else:
            try:
                p.unlink()
                removed.append(str(p))
                _prune_empty_dirs(p.parent)
            except OSError:
                remaining.append(e)  # in use (e.g. open on Windows); try again next time
    data["files"] = remaining
    data["last_cleanup"] = now
    _save(data)
    return {"removed": removed, "kept_edited": kept_edited, "tracked": len(remaining), "days": days}


def maybe_cleanup() -> dict | None:
    """Run cleanup() if automatic clean-up is on and it hasn't run in the last hour. Never raises."""
    try:
        if retention_days() <= 0:
            return None
        if time.time() - float(_load().get("last_cleanup", 0)) < CHECK_EVERY:
            return None
        return cleanup()
    except Exception:
        return None
