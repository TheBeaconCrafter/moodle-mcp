"""Automatic clean-up of files the MCP server saved to ~/Downloads/Moodle.

Only files moodle_download created are tracked (in a private manifest next to the config), so anything you put
in the folder yourself is never touched. A tracked file is deleted once it's older than the retention period
(MOODLE_MCP_DOWNLOAD_DAYS, default 7; 0 turns clean-up off), unless you changed it since: an edited file
is yours now and is dropped from the manifest instead. A file you moved elsewhere is simply forgotten.
Folders a download created are removed again once they're empty; folders you made are never removed, and
neither is ~/Downloads/Moodle itself.

How a file is recognised as the one that was downloaded: same device and file id (inode), same size, the exact
same modification time (nanoseconds), and the same SHA-256 (files over HASH_MAX_BYTES skip the hash and rely on
the rest). Anything that doesn't match is kept.

How a delete is kept inside the folder: the folder is resolved once, and right before each unlink or rmdir every
path component from the folder down is checked with lstat. If any of them is a symlink (or, on Windows, a
junction or other reparse point), or the file isn't a plain regular file, nothing is deleted. A small window
remains between that check and the unlink in which another program running as you could swap a folder for a
link; closing it fully needs dirfd-relative deletes (unlinkat), which Windows doesn't offer. Such a program
could already delete your files directly, so the check is about mistakes and stray links, not a hostile user.

The manifest is read-modified-written under a lock file (like the config) so two processes can't lose each
other's entries. On macOS/Linux a manifest that isn't yours or that group/other can access is ignored.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path

from . import client
from .client import DOWNLOAD_ROOT, MoodleError, _write_private, file_lock

DEFAULT_DAYS = 7
CHECK_EVERY = 3600  # seconds between automatic clean-ups
HASH_MAX_BYTES = 256 * 1024 * 1024  # larger files are recognised by id, size and mtime alone
LOCK_TIMEOUT = 2.0


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


def _empty() -> dict:
    return {"files": [], "dirs": [], "last_cleanup": 0}


def _load() -> dict:
    """The manifest, or an empty one if it's missing, broken, or (macOS/Linux) not private to this user."""
    try:
        fd = os.open(manifest_path(), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return _empty()
    try:
        with os.fdopen(fd, "rb") as fh:
            if os.name != "nt":
                st = os.fstat(fh.fileno())
                if st.st_uid != os.getuid() or st.st_mode & 0o077:
                    return _empty()
            data = json.loads(fh.read(16 * 1024 * 1024).decode("utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not (isinstance(data, dict) and isinstance(data.get("files"), list)):
        return _empty()
    data["files"] = [e for e in data["files"] if isinstance(e, dict)]
    data["dirs"] = [e for e in data.get("dirs", []) if isinstance(e, dict)]
    return data


def _save(data: dict) -> None:
    _write_private(manifest_path(), json.dumps(data, indent=1).encode("utf-8"))


def _lock():
    return file_lock(manifest_path(), timeout=LOCK_TIMEOUT)


def _top() -> Path | None:
    """~/Downloads/Moodle resolved once (it may itself be a link you set up), or None if it isn't a folder."""
    try:
        top = root().resolve()
    except (OSError, RuntimeError):
        return None
    return top if top.is_dir() else None


def _is_link(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    return stat.S_ISLNK(st.st_mode) or bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _checked(path: str | Path, top: Path, want_dir: bool = False) -> os.stat_result | None:
    """lstat of `path` if it's strictly inside `top` with no symlink/junction anywhere from top down to it, and
    is a regular file (or, with want_dir, a plain folder). None otherwise."""
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        return None
    try:
        rel = p.relative_to(top)
    except ValueError:
        return None
    if not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        return None
    try:
        st = os.lstat(top)
        if _is_link(st) or not stat.S_ISDIR(st.st_mode):
            return None
        cur = top
        for part in rel.parts:
            cur = cur / part
            st = os.lstat(cur)
            if _is_link(st):
                return None
        if not (stat.S_ISDIR(st.st_mode) if want_dir else stat.S_ISREG(st.st_mode)):
            return None
        resolved = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if top not in resolved.parents:
        return None
    return st


def _sha256(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _identity(path: Path, st: os.stat_result) -> dict:
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "dev": st.st_dev, "ino": st.st_ino,
            "sha256": _sha256(path) if st.st_size <= HASH_MAX_BYTES else None}


def _unchanged(p: Path, entry: dict, top: Path) -> bool:
    """True only if `p` is still the very file that was downloaded (see the module docstring)."""
    st = _checked(p, top)
    if st is None:
        return False
    if st.st_size != entry.get("size") or st.st_mtime_ns != entry.get("mtime_ns"):
        return False
    if entry.get("ino") and (st.st_ino != entry.get("ino") or st.st_dev != entry.get("dev")):
        return False
    if st.st_size <= HASH_MAX_BYTES and (not entry.get("sha256") or _sha256(p) != entry["sha256"]):
        return False
    return True


def record(path: str | Path, now: float | None = None, created_dirs: list[str] | None = None) -> None:
    """Remember a file the MCP server just downloaded (and the folders it created for it). Never raises:
    a file that can't be recorded is simply never cleaned up."""
    top = _top()
    if top is None:
        return
    p = Path(os.path.abspath(path))
    st = _checked(p, top)
    if st is None:
        return
    entry = {"path": str(p), **_identity(p, st), "saved_at": now or time.time()}
    dirs = []
    for d in created_dirs or []:
        d = Path(os.path.abspath(d))
        dst = _checked(d, top, want_dir=True)
        if dst is not None:
            dirs.append({"path": str(d), "dev": dst.st_dev, "ino": dst.st_ino})
    try:
        with _lock():
            data = _load()
            data["files"] = [f for f in data["files"] if f.get("path") != str(p)] + [entry]
            known = {d.get("path") for d in data["dirs"]}
            data["dirs"] += [d for d in dirs if d["path"] not in known]
            _save(data)
    except (MoodleError, OSError):
        pass


def _prune_dirs(data: dict, top: Path) -> None:
    """Remove recorded (download-created) folders that are empty now, deepest first. Folders you made, or a
    recorded one that was replaced since, are never touched."""
    keep = []
    for d in sorted(data["dirs"], key=lambda d: -len(Path(str(d.get("path", ""))).parts)):
        st = _checked(d.get("path", ""), top, want_dir=True)
        if st is None or (d.get("ino") and (st.st_ino != d.get("ino") or st.st_dev != d.get("dev"))):
            continue  # gone, replaced, or not safe to touch: forget it
        try:
            os.rmdir(d["path"])  # only succeeds when empty
        except OSError:
            keep.append(d)
    data["dirs"] = keep


def tracked() -> list[dict]:
    """Tracked downloads that still exist, oldest first, with their age in days."""
    now = time.time()
    top = _top()
    out = []
    if top is None:
        return out
    for e in _load()["files"]:
        p = Path(str(e.get("path") or ""))
        if e.get("path") and p.is_absolute() and p.exists():
            out.append({"path": str(p), "age_days": round((now - float(e.get("saved_at", now))) / 86400, 1),
                        "edited": not _unchanged(p, e, top)})
    return sorted(out, key=lambda e: -e["age_days"])


def cleanup(days: float | None = None, everything: bool = False, now: float | None = None) -> dict:
    """Delete tracked downloads older than `days` (or all tracked ones with everything=True).

    Edited files are kept and forgotten; missing ones are forgotten. Returns what happened."""
    now = now or time.time()
    days = retention_days() if days is None else days
    removed, kept_edited, remaining = [], [], []
    top = _top()
    with _lock():
        data = _load()
        for e in data["files"]:
            p = Path(str(e.get("path") or ""))
            if top is None or _checked(p, top) is None:
                continue  # moved, deleted, behind a link, or pointing outside the folder: forget it
            old = everything or (days > 0 and now - float(e.get("saved_at", now)) > days * 86400)
            if not old:
                remaining.append(e)
            elif not _unchanged(p, e, top):
                kept_edited.append(str(p))  # you changed it, so it's yours now
            elif _checked(p, top) is None:  # checked again right before the unlink
                continue
            else:
                try:
                    os.unlink(p)
                    removed.append(str(p))
                except OSError:
                    remaining.append(e)  # in use (e.g. open on Windows); try again next time
        data["files"] = remaining
        if top is not None:
            _prune_dirs(data, top)
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
