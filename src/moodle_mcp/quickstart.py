"""`moodle quickstart`: sign in to Moodle and connect it to ChatGPT through OpenAI's Secure MCP Tunnel.

Walks through every step in an interactive terminal UI and asks before it changes anything:

1. sign in to Moodle (password with auto-renew where the site allows it, otherwise a token);
2. choose read-only or writes;
3. download tunnel-client from github.com/openai/tunnel-client and check its SHA-256;
4. store the OpenAI runtime API key in a private file and write a tunnel-client profile;
5. run `tunnel-client doctor`;
6. install a background service (systemd user unit / launchd agent / Windows Startup entry), or run now.

This is the one module that starts other programs (tunnel-client, systemctl, launchctl, wscript);
the MCP server and client never do.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import webbrowser
import zipfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import client
from .client import (
    Moodle,
    MoodleError,
    _write_private,
    app_login_type,
    check_site_url,
    load_config,
    parse_token,
    password_login,
    redact,
    remember_password,
)

RELEASES_API = "https://api.github.com/repos/openai/tunnel-client/releases/latest"
TUNNELS_URL = "https://platform.openai.com/settings/organization/tunnels"
API_KEYS_URL = "https://platform.openai.com/settings/organization/api-keys"
CONNECTORS_URL = "https://chatgpt.com/#settings/Connectors"
PROFILE_NAME = "moodle"
SERVICE_NAME = "moodle-tunnel"
LAUNCHD_LABEL = "com.moodle-mcp.tunnel"
TUNNEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,127}")
MAX_ZIP_BYTES = 300 * 1024 * 1024


# ---------------------------------------------------------------- paths and pure helpers (tested)

@dataclass(frozen=True)
class Paths:
    config_dir: Path
    bin_dir: Path

    @property
    def profile_dir(self) -> Path:
        return self.config_dir / "tunnel"

    @property
    def profile_file(self) -> Path:
        return self.profile_dir / f"{PROFILE_NAME}.yaml"

    @property
    def key_file(self) -> Path:
        return self.config_dir / "tunnel-runtime.key"

    @property
    def tunnel_client(self) -> Path:
        return self.bin_dir / ("tunnel-client.exe" if os.name == "nt" else "tunnel-client")


def default_paths() -> Paths:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        bin_dir = base / "moodle-mcp" / "tunnel-client"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        bin_dir = base / "moodle-mcp" / "tunnel-client"
    return Paths(config_dir=client.CONFIG_PATH.parent, bin_dir=bin_dir)


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


def extract_release(data: bytes, dest: Path) -> Path:
    """Unpack the release zip's top-level files into dest (no sub-paths, so nothing can escape it)."""
    dest.mkdir(parents=True, exist_ok=True)
    exe = None
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if info.is_dir() or "/" in name or name in ("", ".", ".."):
                continue
            target = dest / name
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            if name in ("tunnel-client", "tunnel-client.exe"):
                exe = target
    if exe is None:
        raise MoodleError("The tunnel-client download didn't contain the tunnel-client program.", "badrelease")
    if os.name != "nt":
        for f in dest.iterdir():
            if f.name in ("tunnel-client", "cloudflared"):
                f.chmod(0o755)
    return exe


def mcp_command(moodle_mcp: str, allow_writes: bool) -> str:
    """The command line tunnel-client starts. tunnel-client splits it like a shell (backslash escapes), so the
    path uses forward slashes, which Windows accepts too, and is quoted so spaces survive."""
    path = moodle_mcp.replace("\\", "/").replace('"', '\\"')
    return f'"{path}"' + (" --allow-writes" if allow_writes else "")


def init_args(paths: Paths, tunnel_id: str, command: str) -> list[str]:
    return [str(paths.tunnel_client), "init", "--sample", "sample_mcp_stdio_local", "--profile", PROFILE_NAME,
            "--profile-dir", str(paths.profile_dir), "--tunnel-id", tunnel_id, "--mcp-command", command,
            "--control-plane-api-key-ref", f"file:{paths.key_file.as_posix()}",
            "--health-listen-addr", "127.0.0.1:0", "--force"]


def run_args(paths: Paths) -> list[str]:
    return [str(paths.tunnel_client), "run", "--profile-file", str(paths.profile_file)]


def systemd_unit(paths: Paths) -> str:
    exec_start = " ".join(f'"{a}"' for a in run_args(paths))
    return f"""[Unit]
Description=OpenAI Secure MCP Tunnel for moodle-mcp
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={exec_start}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def launchd_plist(paths: Paths, log_file: Path) -> str:
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    args = "\n".join(f"    <string>{esc(a)}</string>" for a in run_args(paths))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{esc(str(log_file))}</string>
  <key>StandardErrorPath</key><string>{esc(str(log_file))}</string>
</dict>
</plist>
"""


def windows_runner(paths: Paths) -> str:
    """PowerShell loop that restarts tunnel-client if it exits (Windows has no user-level service manager)."""
    args = " ".join("'" + a.replace("'", "''") + "'" for a in run_args(paths)[1:])
    exe = str(paths.tunnel_client).replace("'", "''")
    return f"""# Started at sign-in by moodle-mcp quickstart. Delete the Startup entry to stop it.
while ($true) {{
  & '{exe}' {args}
  Start-Sleep -Seconds 10
}}
"""


def windows_launcher(ps1: Path) -> str:
    cmd = f'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{ps1}""'
    return f'CreateObject("WScript.Shell").Run "{cmd}", 0, False\r\n'


# ---------------------------------------------------------------- UI

class Abort(Exception):
    pass


class UI:
    def __init__(self):
        import questionary
        from rich.console import Console

        self.q = questionary
        self.c = Console(highlight=False)

    def _ask(self, question):
        answer = question.unsafe_ask()
        if answer is None:
            raise Abort()
        return answer

    def step(self, n: int, total: int, title: str) -> None:
        self.c.print()
        self.c.rule(f"[bold #F98012]{n}/{total}[/]  [bold]{title}[/]", align="left", style="#3a332d")

    def info(self, text: str) -> None:
        self.c.print(text)

    def ok(self, text: str) -> None:
        self.c.print(f"[green]✓[/] {text}")

    def warn(self, text: str) -> None:
        self.c.print(f"[yellow]![/] {text}")

    def err(self, text: str) -> None:
        self.c.print(f"[red]✗[/] {text}")

    def confirm(self, text: str, default: bool = True) -> bool:
        return self._ask(self.q.confirm(text, default=default))

    def text(self, text: str, default: str = "", validate=None) -> str:
        return self._ask(self.q.text(text, default=default, validate=validate)).strip()

    def secret(self, text: str, validate=None) -> str:
        return self._ask(self.q.password(text, validate=validate)).strip()

    def select(self, text: str, choices: list[tuple[str, str]]) -> str:
        return self._ask(self.q.select(text, choices=[self.q.Choice(t, value=v) for t, v in choices]))

    def spin(self, text: str):
        return self.c.status(text, spinner="dots")


# ---------------------------------------------------------------- steps

def _run(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def step_moodle(ui: UI) -> None:
    cfg = load_config()
    if cfg.get("token") and cfg.get("url"):
        try:
            with ui.spin("Checking your saved Moodle sign-in…"):
                who = Moodle().whoami()
            ui.ok(f"Signed in as [bold]{who['fullname']}[/] on {who['site']}"
                  + (" (renews itself)" if who.get("auto_renew") else ""))
            if ui.confirm("Keep this sign-in?", default=True):
                return
        except MoodleError as e:
            ui.warn(f"The saved sign-in doesn't work any more ({e.code or 'error'}). Let's sign in again.")

    def valid_url(v: str):
        try:
            check_site_url(v)
            return True
        except MoodleError as e:
            return str(e)

    url = check_site_url(ui.text("Your Moodle site URL", default=cfg.get("url") or "https://",
                                 validate=valid_url))
    with ui.spin("Asking the site how the Moodle app signs in…"):
        login_type = app_login_type(url)
    method = "token"
    if login_type == 1:
        method = ui.select("This site lets the Moodle app sign in with a username and password. How do you "
                           "want to sign in?", [
                               ("Username and password (recommended: the token can renew itself)", "password"),
                               ("Paste a token from the Security keys page", "token"),
                           ])
    from .cli import store_sign_in

    while True:
        try:
            if method == "password":
                username = ui.text("Moodle username", validate=lambda v: bool(v.strip()) or "Required")
                password = ui.secret("Moodle password", validate=lambda v: bool(v) or "Required")
                with ui.spin("Signing in…"):
                    token = password_login(url, username, password)
                    who = Moodle(url=url, token=token).whoami()
                remember = ui.confirm("Remember the password so the token renews itself when it expires?",
                                      default=True)
                if remember:
                    where = remember_password(url, username, password)
                    ui.ok(f"Password saved in {where}.")
                store_sign_in(url, token, username=username, remember=remember)
            else:
                ui.info(f"In Moodle: avatar → Preferences → Security keys ({url}/user/managetoken.php), press "
                        "[bold]Reset[/] on [italic]Moodle mobile web service[/] and copy the key. Or paste the "
                        "app launch link (see docs/getting-your-token.md).")
                raw = ui.secret("Token or launch link")
                try:
                    token = parse_token(raw)
                except ValueError as e:
                    raise MoodleError(str(e), "badtoken") from None
                with ui.spin("Checking the token…"):
                    who = Moodle(url=url, token=token).whoami()
                store_sign_in(url, token)
            ui.ok(f"Signed in as [bold]{who['fullname']}[/] on {who['site']}.")
            return
        except MoodleError as e:
            ui.err(redact(str(e)))
            if not ui.confirm("Try again?", default=True):
                raise Abort() from None


def step_writes(ui: UI) -> bool:
    ui.info("ChatGPT will act as you on Moodle. Read-only mode lets it read everything but never submit or "
            "change anything, even if a forum post or file tries to talk it into it.")
    return ui.select("Which mode?", [
        ("Read-only (recommended)", "ro"),
        ("Allow writes (submitting assignments, moodle_call writes)", "rw"),
    ]) == "rw"


def find_tunnel_client(paths: Paths) -> Path | None:
    if paths.tunnel_client.exists():
        return paths.tunnel_client
    found = shutil.which("tunnel-client")
    return Path(found) if found else None


def download_tunnel_client(ui: UI, paths: Paths) -> Path:
    slug = platform_slug()
    with httpx.Client(timeout=httpx.Timeout(30, read=120), follow_redirects=True,
                      headers={"User-Agent": "moodle-mcp-quickstart", "Accept": "application/vnd.github+json"}) as h:
        with ui.spin("Looking up the latest tunnel-client release…"):
            rel = h.get(RELEASES_API)
            rel.raise_for_status()
            rel = rel.json()
            tag = rel["tag_name"]
            assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
            name = asset_name(tag, slug)
            if name not in assets or "SHA256SUMS.txt" not in assets:
                raise MoodleError(f"Release {tag} has no {name}. Install tunnel-client by hand.", "badrelease")
            sums = parse_sha256sums(h.get(assets["SHA256SUMS.txt"]).raise_for_status().text)
        with ui.spin(f"Downloading {name}…"):
            r = h.get(assets[name])
            r.raise_for_status()
            if len(r.content) > MAX_ZIP_BYTES:
                raise MoodleError("The tunnel-client download is unexpectedly large.", "badrelease")
        digest = hashlib.sha256(r.content).hexdigest()
        if sums.get(name) != digest:
            raise MoodleError(f"{name} doesn't match the SHA-256 published in SHA256SUMS.txt; not installing it.",
                              "badchecksum")
        ui.ok(f"Checked SHA-256 against the release's SHA256SUMS.txt ({digest[:16]}…).")
        exe = extract_release(r.content, paths.bin_dir)
    ui.ok(f"Installed tunnel-client {tag} to {paths.bin_dir}")
    return exe


def step_tunnel_client(ui: UI, paths: Paths) -> Path:
    exe = find_tunnel_client(paths)
    if exe:
        ui.ok(f"tunnel-client found: {exe}")
        if not ui.confirm("Use it? (No downloads the latest release instead)", default=True):
            exe = None
    if exe is None:
        if not ui.confirm(f"Download OpenAI's tunnel-client from github.com/openai/tunnel-client into "
                          f"{paths.bin_dir}?", default=True):
            raise Abort()
        exe = download_tunnel_client(ui, paths)
    return exe


def step_openai(ui: UI, paths: Paths) -> str:
    ui.info("You need a [bold]tunnel id[/] and a [bold]runtime API key[/] from the OpenAI platform:\n"
            f"  • Tunnels:          {TUNNELS_URL}\n"
            f"  • Runtime API keys: {API_KEYS_URL}")
    if ui.confirm("Open those pages in your browser?", default=False):
        webbrowser.open(TUNNELS_URL)
        webbrowser.open(API_KEYS_URL)
    old_id = ""
    if paths.profile_file.exists():
        m = re.search(r'tunnel_id:\s*"?([^"\s]+)', paths.profile_file.read_text(encoding="utf-8", errors="ignore"))
        old_id = m.group(1) if m else ""
    tunnel_id = ui.text("Tunnel id", default=old_id,
                        validate=lambda v: bool(TUNNEL_ID_RE.fullmatch(v.strip())) or "That doesn't look like a tunnel id")
    if paths.key_file.exists() and ui.confirm("Keep the runtime API key saved earlier?", default=True):
        return tunnel_id
    key = ui.secret("Runtime API key (hidden)", validate=lambda v: len(v.strip()) >= 20 or "That looks too short")
    _write_private(paths.key_file, key.encode("utf-8"))
    ui.ok(f"Saved the key to {paths.key_file} (only your user account can read it).")
    return tunnel_id


def moodle_mcp_path() -> str:
    found = shutil.which("moodle-mcp")
    if found:
        return str(Path(found).resolve())
    sibling = Path(sys.executable).with_name("moodle-mcp.exe" if os.name == "nt" else "moodle-mcp")
    if sibling.exists():
        return str(sibling)
    raise MoodleError("Couldn't find the moodle-mcp command. Install moodle-mcp with uv first.", "nomcp")


def step_profile(ui: UI, paths: Paths, tunnel_id: str, allow_writes: bool) -> None:
    command = mcp_command(moodle_mcp_path(), allow_writes)
    paths.profile_dir.mkdir(parents=True, exist_ok=True)
    with ui.spin("Writing the tunnel-client profile…"):
        r = _run(init_args(paths, tunnel_id, command))
    if r.returncode != 0:
        raise MoodleError("tunnel-client init failed:\n" + (r.stderr or r.stdout).strip()[-2000:], "initfailed")
    if os.name != "nt":
        paths.profile_file.chmod(0o600)
    ui.ok(f"Profile written: {paths.profile_file}")
    with ui.spin("Running tunnel-client doctor…"):
        d = _run([str(paths.tunnel_client), "doctor", "--profile-file", str(paths.profile_file), "--json"])
    try:
        report = json.loads(d.stdout)
    except ValueError:
        report = {}
    failed = [c for c in report.get("checks", []) if c.get("status") == "FAIL"]
    if report.get("result") in ("ok", "pass") or (d.returncode == 0 and not failed):
        ui.ok("tunnel-client doctor: all checks passed.")
        return
    for c in failed:
        ui.err(f"{c.get('id')}: {c.get('summary')}")
        for n in c.get("next", [])[:2]:
            ui.info(f"    → {n}")
    if not failed:
        ui.warn((d.stderr or d.stdout).strip()[-1500:] or "tunnel-client doctor reported a problem.")
    if not ui.confirm("Continue anyway?", default=False):
        raise Abort()


def install_service(ui: UI, paths: Paths) -> bool:
    system = platform.system()
    if system == "Linux":
        if not shutil.which("systemctl") or _run(["systemctl", "--user", "show-environment"]).returncode != 0:
            ui.warn("No systemd user session here, so no service was installed.")
            return False
        unit = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / \
            f"{SERVICE_NAME}.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(paths), encoding="utf-8")
        for args in (["daemon-reload"], ["enable", SERVICE_NAME], ["restart", SERVICE_NAME]):
            r = _run(["systemctl", "--user", *args])
            if r.returncode != 0:
                raise MoodleError(f"systemctl --user {' '.join(args)} failed: {r.stderr.strip()}", "service")
        ui.ok(f"Installed and started the systemd user service {SERVICE_NAME} ({unit}).")
        if ui.confirm("Keep it running while you're logged out (loginctl enable-linger)?", default=True):
            r = _run(["loginctl", "enable-linger", os.environ.get("USER") or ""])
            (ui.ok("Linger enabled.") if r.returncode == 0 else
             ui.warn(f"enable-linger failed ({r.stderr.strip()}); run `sudo loginctl enable-linger $USER` yourself."))
        ui.info(f"Logs: journalctl --user -u {SERVICE_NAME} -f")
        return True
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        log = Path.home() / "Library" / "Logs" / "moodle-tunnel.log"
        plist.parent.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(launchd_plist(paths, log), encoding="utf-8")
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", domain, str(plist)])
        r = _run(["launchctl", "bootstrap", domain, str(plist)])
        if r.returncode != 0:
            raise MoodleError(f"launchctl bootstrap failed: {r.stderr.strip()}", "service")
        ui.ok(f"Installed and started the launchd agent {LAUNCHD_LABEL}. Logs: {log}")
        return True
    if system == "Windows":
        ps1 = paths.profile_dir / "run-tunnel.ps1"
        ps1.write_text(windows_runner(paths), encoding="utf-8")
        startup = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Microsoft" / \
            "Windows" / "Start Menu" / "Programs" / "Startup"
        startup.mkdir(parents=True, exist_ok=True)
        vbs = startup / f"{SERVICE_NAME}.vbs"
        vbs.write_text(windows_launcher(ps1), encoding="utf-8")
        subprocess.Popen(["wscript.exe", str(vbs)], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        ui.ok(f"Added {vbs.name} to your Startup folder and started the tunnel in the background.")
        ui.info(f"To stop it for good, delete {vbs} and end tunnel-client in Task Manager.")
        return True
    ui.warn(f"Don't know how to install a service on {system}.")
    return False


def step_service(ui: UI, paths: Paths) -> None:
    choice = ui.select("ChatGPT can only reach Moodle while tunnel-client runs. How should it run?", [
        ("In the background, started automatically (recommended)", "service"),
        ("Right now in this terminal (stops when you close it)", "foreground"),
        ("Don't start it; I'll run it myself", "none"),
    ])
    if choice == "service":
        if install_service(ui, paths):
            return
        choice = "foreground" if ui.confirm("Run it in this terminal instead?", default=True) else "none"
    if choice == "none":
        ui.info("Start it any time with:\n  " + " ".join(f'"{a}"' if " " in a else a for a in run_args(paths)))
        return
    finish(ui, paths)
    ui.info("Starting tunnel-client. Press Ctrl+C to stop.")
    try:
        subprocess.run(run_args(paths))
    except KeyboardInterrupt:
        pass
    raise SystemExit(0)


def finish(ui: UI, paths: Paths) -> None:
    from rich.panel import Panel

    ui.c.print()
    ui.c.print(Panel.fit(
        "[bold]Last step, in ChatGPT[/]\n\n"
        f"1. Open {CONNECTORS_URL}\n"
        "2. Add a custom MCP server and choose [bold]Tunnel[/] as the connection.\n"
        "3. Pick your tunnel (or paste its id) and save.\n"
        "4. In a new chat, enable the connector and ask: [italic]what's due this week?[/]\n\n"
        "[dim]Check the Moodle sign-in any time with `moodle status`. Run `moodle quickstart` again to change "
        "anything.[/]",
        title="Almost done", border_style="#F98012"))


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):  # ✓/✗ on an old Windows code page: degrade, don't crash
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("✗ moodle quickstart is interactive; run it in a terminal.")
    try:
        ui = UI()
    except ImportError as e:
        sys.exit(f"✗ The quickstart needs the `rich` and `questionary` packages ({e}). Reinstall moodle-mcp.")
    from rich.panel import Panel

    paths = default_paths()
    ui.c.print(Panel.fit(
        "[bold]moodle-mcp quickstart[/]\n"
        "Connects your Moodle to ChatGPT through OpenAI's Secure MCP Tunnel.\n"
        "[dim]Nothing is changed without asking first. Ctrl+C quits at any point.[/]",
        border_style="#F98012"))
    total = 5
    try:
        ui.step(1, total, "Sign in to Moodle")
        step_moodle(ui)
        ui.step(2, total, "Read-only or writes")
        allow_writes = step_writes(ui)
        ui.step(3, total, "OpenAI tunnel-client")
        paths = Paths(paths.config_dir, step_tunnel_client(ui, paths).parent)
        ui.step(4, total, "Tunnel id and API key")
        tunnel_id = step_openai(ui, paths)
        step_profile(ui, paths, tunnel_id, allow_writes)
        ui.step(5, total, "Keep it running")
        step_service(ui, paths)
        finish(ui, paths)
    except (Abort, KeyboardInterrupt):
        ui.c.print("\n[dim]Stopped. Nothing after the last ✓ was changed. Run `moodle quickstart` to pick up again.[/]")
        sys.exit(1)
    except (MoodleError, httpx.HTTPError, OSError, subprocess.SubprocessError) as e:
        ui.err(redact(str(e)))
        sys.exit(1)


if __name__ == "__main__":
    main()
