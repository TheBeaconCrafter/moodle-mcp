"""Install, find and update OpenAI's tunnel-client in the standard place for each OS.

tunnel-client is a general tool, not a moodle-mcp internal, so it goes where you'd install it by hand and
lands on PATH, usable for other tunnels too:

- macOS: Homebrew, `openai/tools/tunnel-client` (the path OpenAI supports; its release ZIPs aren't notarized).
- Linux: versions in ~/.local/share/tunnel-client/versions/<tag>/, with ~/.local/bin/tunnel-client a symlink to
  the current one. An update unpacks the new version and swaps the link; the previous version stays for rollback.
- Windows: %LOCALAPPDATA%\\Programs\\tunnel-client, added to your user PATH. An update renames the old files
  aside first, so it works while tunnel-client is running.

Release ZIPs are checked against the release's SHA256SUMS.txt before anything is unpacked. That catches a
corrupt download or a tampered mirror; it is not a signature, so it can't vouch for a release that was
compromised on GitHub itself. Every request (redirects included) must be https.
Like quickstart.py, this module runs other programs (tunnel-client, brew, systemctl, launchctl).
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx

from .client import MoodleError

RELEASES_API = "https://api.github.com/repos/openai/tunnel-client/releases/latest"
BREW_FORMULA = "openai/tools/tunnel-client"
MAX_ZIP_BYTES = 300 * 1024 * 1024
MAX_SUMS_BYTES = 1024 * 1024
TAG_RE = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?")
MEMBER_RE = re.compile(r"[A-Za-z0-9._+-]+")  # a plain file name: no folders, drive letters or :streams
KEEP_VERSIONS = 2
SERVICE_NAME = "moodle-tunnel"
LAUNCHD_LABEL = "com.moodle-mcp.tunnel"
EXE = "tunnel-client.exe" if os.name == "nt" else "tunnel-client"

Say = Callable[[str], None]


def _quiet(_: str) -> None:
    pass


def _is_cwd(folder: str | Path) -> bool:
    try:
        return Path(folder or ".").resolve() == Path.cwd().resolve()
    except (OSError, RuntimeError):
        return False


def which(name: str) -> str | None:
    """shutil.which, but never a program in the current directory: Windows looks there first, so a file
    dropped into the folder you run this from could otherwise be picked up as tunnel-client or moodle-mcp."""
    path = os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep) if d and not _is_cwd(d))
    found = shutil.which(name, path=path)
    if not found or not Path(found).is_absolute() or _is_cwd(Path(found).parent):
        return None
    return found


def no_cwd_exe_search() -> None:
    """On Windows, stop programs started from here (and shutil.which) from searching the current directory."""
    if os.name == "nt":
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"


# ---------------------------------------------------------------- layout and pure helpers

def install_root() -> Path:
    """Where moodle-mcp installs tunnel-client's files on Linux and Windows."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Programs" / "tunnel-client"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "tunnel-client"


def command_path() -> Path:
    """The stable path of the command this installer manages (what services and PATH point at)."""
    if os.name == "nt":
        return install_root() / EXE
    return Path.home() / ".local" / "bin" / EXE


def platform_slug(system: str | None = None, machine: str | None = None) -> str:
    """tunnel-client's release naming: linux|darwin|windows - amd64|arm64."""
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    os_part = {"linux": "linux", "darwin": "darwin", "windows": "windows"}.get(system)
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if not os_part or not arch:
        raise MoodleError(f"tunnel-client has no build for {system}/{machine}. Install it by hand from "
                          "https://github.com/openai/tunnel-client/releases.", "unsupported")
    return f"{os_part}-{arch}"


def asset_name(tag: str, slug: str) -> str:
    return f"tunnel-client-{tag}-{slug}.zip"


def parse_sha256sums(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            out[parts[1].lstrip("*")] = parts[0].lower()
    return out


def parse_version(text: str) -> str | None:
    m = re.search(r"\bv?(\d+\.\d+\.\d+)", text or "")
    return m.group(1) if m else None


def version_tuple(v: str | None) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".")) if v else ()


def extract_release(data: bytes, dest: Path) -> Path:
    """Unpack the release zip's top-level files into dest (no sub-paths, so nothing can escape it)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        members = [i for i in z.infolist() if not i.is_dir()]
        names = {i.filename.replace("\\", "/") for i in members}
        if EXE not in names:
            raise MoodleError("The tunnel-client download didn't contain the tunnel-client program.", "badrelease")
        dest.mkdir(parents=True, exist_ok=True)
        for info in members:
            name = info.filename
            if not MEMBER_RE.fullmatch(name) or name in (".", ".."):
                continue  # sub-paths, drive letters (C:x), NTFS streams (name:stream) and the like
            with z.open(info) as src, open(dest / name, "wb") as out:
                shutil.copyfileobj(src, out)
    if os.name != "nt":
        for f in dest.iterdir():
            if f.name in ("tunnel-client", "cloudflared"):
                f.chmod(0o755)
    return dest / EXE


# ---------------------------------------------------------------- finding what's installed

@dataclass
class Installed:
    path: Path          # the command as you'd run it (stable across updates)
    version: str | None
    manager: str        # "homebrew" | "moodle-mcp" | "other"


def _run(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def installed_version(path: Path) -> str | None:
    try:
        r = _run([str(path), "--version"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(r.stdout or r.stderr)


def brew() -> str | None:
    return which("brew")


def brew_command_path() -> Path | None:
    """Homebrew's stable opt path for the formula (survives upgrades), if it's installed."""
    b = brew()
    if not b:
        return None
    r = _run([b, "--prefix", BREW_FORMULA])
    candidate = Path(r.stdout.strip()) / "bin" / "tunnel-client" if r.returncode == 0 and r.stdout.strip() else None
    return candidate if candidate and candidate.exists() else None


def _under_brew_prefix(path: Path) -> bool:
    b = brew()
    if not b:
        return False
    try:
        r = _run([b, "--prefix"], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    prefix = r.stdout.strip()
    if r.returncode != 0 or not prefix:
        return False
    try:
        path.resolve().relative_to(Path(prefix).resolve())
        return True
    except (ValueError, OSError):
        return False


def classify(path: Path) -> str:
    resolved = str(path.resolve())
    if platform.system() == "Darwin" and ("/Cellar/tunnel-client/" in resolved or "/opt/tunnel-client/" in resolved):
        return "homebrew"
    if platform.system() == "Linux" and "/Cellar/tunnel-client/" in resolved and _under_brew_prefix(path):
        return "homebrew"  # Homebrew on Linux
    try:
        path.resolve().relative_to(install_root().resolve())
        return "moodle-mcp"
    except ValueError:
        return "other"


def find() -> Installed | None:
    """The tunnel-client to use: Homebrew's on macOS, ours, or whatever is on PATH."""
    for candidate in (brew_command_path() if platform.system() == "Darwin" else None, command_path()):
        if candidate and candidate.exists():
            return Installed(candidate, installed_version(candidate), classify(candidate))
    on_path = which("tunnel-client")
    if on_path:
        p = Path(on_path)
        return Installed(p, installed_version(p), classify(p))
    return None


# ---------------------------------------------------------------- releases

@dataclass
class Release:
    tag: str
    zip_url: str
    sums_url: str
    zip_name: str

    @property
    def version(self) -> str | None:
        return parse_version(self.tag)


def _require_https(url) -> None:
    if httpx.URL(str(url)).scheme != "https":
        raise MoodleError("A tunnel-client download didn't use https; not installing it.", "badrelease")


def _https_only(request: httpx.Request) -> None:
    _require_https(request.url)  # every request, so a redirect to http:// is refused too


def _http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True,
                        headers={"User-Agent": "moodle-mcp", "Accept": "application/vnd.github+json"},
                        event_hooks={"request": [_https_only]})


def _get_capped(h: httpx.Client, url: str, cap: int) -> bytes:
    """GET url (https only, also after redirects), reading at most `cap` bytes."""
    _require_https(url)
    with h.stream("GET", url) as r:
        r.raise_for_status()
        _require_https(r.url)
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > cap:
                raise MoodleError("The tunnel-client download is unexpectedly large.", "badrelease")
    return bytes(buf)


def latest_release(slug: str | None = None) -> Release:
    slug = slug or platform_slug()
    with _http() as h:
        r = h.get(RELEASES_API)
        r.raise_for_status()
        _require_https(r.url)
        rel = r.json()
    tag = rel.get("tag_name")
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        raise MoodleError("The latest tunnel-client release has an unexpected tag; install it by hand.", "badrelease")
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    name = asset_name(tag, slug)
    if name not in assets or "SHA256SUMS.txt" not in assets:
        raise MoodleError(f"tunnel-client {tag} has no {name}. Install it by hand.", "badrelease")
    for url in (assets[name], assets["SHA256SUMS.txt"]):
        _require_https(url)
    return Release(tag, assets[name], assets["SHA256SUMS.txt"], name)


def download_verified(rel: Release, say: Say = _quiet) -> bytes:
    with _http() as h:
        sums = parse_sha256sums(_get_capped(h, rel.sums_url, MAX_SUMS_BYTES).decode("utf-8", "replace"))
        say(f"Downloading {rel.zip_name}…")
        data = _get_capped(h, rel.zip_url, MAX_ZIP_BYTES)
    digest = hashlib.sha256(data).hexdigest()
    if sums.get(rel.zip_name) != digest:
        raise MoodleError(f"{rel.zip_name} doesn't match the SHA-256 in the release's SHA256SUMS.txt; not "
                          "installing it.", "badchecksum")
    say(f"SHA-256 matches the release's SHA256SUMS.txt ({digest[:16]}…).")
    return data


# ---------------------------------------------------------------- installing

def _install_linux(data: bytes, tag: str) -> Path:
    root = install_root()
    version_dir = root / "versions" / tag
    if not TAG_RE.fullmatch(tag) or version_dir.parent != root / "versions":
        raise MoodleError("Unexpected tunnel-client release tag; not installing it.", "badrelease")
    staging = root / "versions" / f".{tag}.{os.getpid()}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    extract_release(data, staging)
    shutil.rmtree(version_dir, ignore_errors=True)
    staging.rename(version_dir)
    link = command_path()
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(version_dir / EXE)
    os.replace(tmp, link)  # atomic: a running service keeps its old binary, the next start gets the new one
    _prune_versions(root / "versions", keep=version_dir)
    return link


def _prune_versions(versions: Path, keep: Path) -> None:
    dirs = sorted((d for d in versions.iterdir() if d.is_dir() and not d.name.startswith(".")),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    survivors = [keep] + [d for d in dirs if d != keep][:KEEP_VERSIONS - 1]
    for d in dirs:
        if d not in survivors:
            shutil.rmtree(d, ignore_errors=True)


def _install_windows(data: bytes) -> Path:
    root = install_root()
    root.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    for f in root.iterdir():  # a running .exe can be renamed but not overwritten
        if f.is_file() and ".old-" not in f.name:
            try:
                f.rename(f.with_name(f"{f.name}.old-{stamp}"))
            except OSError:
                pass
    extract_release(data, root)
    for f in root.glob("*.old-*"):
        try:
            f.unlink()
        except OSError:
            pass  # still running; removed on the next update
    add_to_user_path(root)
    return root / EXE


def add_to_user_path(folder: Path) -> bool:
    """Add folder to the Windows user PATH (HKCU\\Environment) and tell running programs. True if added."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
        try:
            current, kind = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, kind = "", winreg.REG_EXPAND_SZ
        parts = [p for p in current.split(";") if p]
        if any(os.path.normcase(os.path.expandvars(p)).rstrip("\\") == os.path.normcase(str(folder)) for p in parts):
            return False
        winreg.SetValueEx(key, "Path", 0, kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
                          else winreg.REG_EXPAND_SZ, ";".join(parts + [str(folder)]))
    try:
        import ctypes

        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None)
    except Exception:
        pass
    os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + str(folder)
    return True


def install_or_update(say: Say = _quiet, force: bool = False) -> Installed:
    """Install tunnel-client, or update it if a newer release exists. Returns what's installed afterwards."""
    current = find()
    if platform.system() == "Darwin":
        b = brew()
        if not b:
            raise MoodleError("On macOS, tunnel-client is installed with Homebrew. Install Homebrew from "
                              "https://brew.sh, then run this again.", "nobrew")
        verb = "upgrade" if current and current.manager == "homebrew" else "install"
        say(f"brew {verb} {BREW_FORMULA}…")
        r = _run([b, verb, BREW_FORMULA], timeout=900)
        if r.returncode != 0 and "already installed" not in (r.stderr + r.stdout):
            raise MoodleError(f"brew {verb} failed: {(r.stderr or r.stdout).strip()[-800:]}", "brew")
        found = find()
        if not found:
            raise MoodleError("Homebrew finished but tunnel-client isn't on PATH.", "brew")
        return found
    if current and current.manager == "other":
        raise MoodleError(f"tunnel-client at {current.path} was installed some other way; update it the same way "
                          "(or remove it and run this again).", "foreign")
    rel = latest_release()
    if current and not force and version_tuple(current.version) >= version_tuple(rel.version):
        say(f"tunnel-client {current.version} is the latest release.")
        return current
    data = download_verified(rel, say)
    path = _install_windows(data) if os.name == "nt" else _install_linux(data, rel.tag)
    say(f"Installed tunnel-client {rel.tag} at {path}.")
    if os.name != "nt" and str(command_path().parent) not in os.environ.get("PATH", "").split(os.pathsep):
        say(f"Note: {command_path().parent} isn't on your PATH yet. Run `uv tool update-shell` and open a new "
            "terminal to use `tunnel-client` directly.")
    return Installed(path, installed_version(path) or rel.version, "moodle-mcp")


def restart_service(say: Say = _quiet) -> None:
    """Restart the moodle tunnel service (if one is set up) so it runs the updated tunnel-client."""
    system = platform.system()
    if system == "Linux" and shutil.which("systemctl"):
        r = _run(["systemctl", "--user", "try-restart", SERVICE_NAME])
        if r.returncode == 0:
            say(f"Restarted the {SERVICE_NAME} service.")
    elif system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        if plist.exists():
            r = _run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"])
            if r.returncode == 0:
                say("Restarted the launchd agent.")
    elif system == "Windows":
        say("The background tunnel picks up the new version the next time it starts (sign out and in, or end "
            "tunnel-client in Task Manager; it restarts by itself).")


# ---------------------------------------------------------------- `moodle tunnel …`

def cmd_tunnel(action: str) -> int:
    no_cwd_exe_search()
    say = print
    try:
        if action == "status":
            current = find()
            if not current:
                print("tunnel-client isn't installed. Run `moodle tunnel install` or `moodle quickstart`.")
                return 1
            print(f"tunnel-client {current.version or '?'} at {current.path} (managed by {current.manager})")
            if platform.system() != "Darwin" and current.manager != "other":
                rel = latest_release()
                newer = version_tuple(rel.version) > version_tuple(current.version)
                print(f"Latest release: {rel.tag}" + (" — run `moodle tunnel update`" if newer else " (up to date)"))
            return 0
        current = find()
        if action == "install" and current:
            print(f"tunnel-client {current.version or '?'} is already installed at {current.path}.")
            return 0
        before = current.version if current else None
        after = install_or_update(say)
        if before and after.version != before:
            restart_service(say)
        return 0
    except (MoodleError, httpx.HTTPError, OSError, subprocess.SubprocessError) as e:
        print(f"✗ {e}")
        return 1
