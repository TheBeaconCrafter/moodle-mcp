"""`moodle quickstart`: sign in to Moodle and connect it to ChatGPT through OpenAI's Secure MCP Tunnel.

Walks through every step in an interactive terminal UI and asks before it changes anything:

1. sign in to Moodle (password with auto-renew where the site allows it, otherwise a token);
2. choose read-only or writes;
3. install or update OpenAI's tunnel-client in its standard place (see tunnelclient.py);
4. store the OpenAI runtime API key in a private file and write a tunnel-client profile;
5. run `tunnel-client doctor`;
6. install a background service (systemd user unit / launchd agent / Windows Startup entry), or run now.

This module and tunnelclient.py are the only ones that start other programs (tunnel-client, brew,
systemctl, launchctl, wscript); the MCP server and client never do.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import client
from . import tunnelclient as tc
from .client import (
    Moodle,
    MoodleError,
    _write_private,
    app_login_type,
    check_site_url,
    load_config,
    parse_token,
    password_file_problem,
    password_login,
    redact,
)

TUNNELS_URL = "https://platform.openai.com/settings/organization/tunnels"
API_KEYS_URL = "https://platform.openai.com/settings/organization/api-keys"
CONNECTORS_URL = "https://chatgpt.com/#settings/Connectors"
PROFILE_NAME = "moodle"
SERVICE_NAME = tc.SERVICE_NAME
LAUNCHD_LABEL = tc.LAUNCHD_LABEL
TUNNEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,127}")


# ---------------------------------------------------------------- paths and pure helpers (tested)

@dataclass(frozen=True)
class Paths:
    config_dir: Path
    tunnel_client: Path  # the stable command path (survives tunnel-client updates)

    @property
    def profile_dir(self) -> Path:
        return self.config_dir / "tunnel"

    @property
    def profile_file(self) -> Path:
        return self.profile_dir / f"{PROFILE_NAME}.yaml"

    @property
    def key_file(self) -> Path:
        return self.config_dir / "tunnel-runtime.key"


def default_paths(tunnel_client: Path | None = None) -> Paths:
    return Paths(config_dir=client.CONFIG_PATH.parent, tunnel_client=tunnel_client or tc.command_path())


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


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _systemd_quote(arg: str) -> str:
    """One ExecStart argument, double-quoted: backslash and quote escaped, % (specifiers) and $ (variables)
    doubled so systemd passes the path through literally."""
    if _CONTROL_CHARS.search(arg):
        raise MoodleError("A path for the service contains control characters; not writing the unit.", "badpath")
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def systemd_unit(paths: Paths) -> str:
    exec_start = " ".join(_systemd_quote(a) for a in run_args(paths))
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


def _ps_quote(text: str) -> str:
    """A PowerShell single-quoted string. PowerShell also ends such a string at the typographic quotes
    U+2018..U+201B, so those are doubled like '."""
    for q in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        text = text.replace(q, q * 2)
    return f"'{text}'"


def _system32() -> Path:
    return Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32"


def windows_runner(paths: Paths) -> str:
    """PowerShell loop that restarts tunnel-client if it exits (Windows has no user-level service manager).
    Written as UTF-8 with a BOM so Windows PowerShell 5.1 reads non-ASCII profile paths correctly."""
    args = " ".join(_ps_quote(a) for a in run_args(paths)[1:])
    exe = _ps_quote(str(paths.tunnel_client))
    return f"""# Started at sign-in by moodle-mcp quickstart. Delete the Startup entry to stop it.
while ($true) {{
  & {exe} {args}
  Start-Sleep -Seconds 10
}}
"""


def windows_launcher(ps1: Path) -> str:
    """The Startup-folder .vbs that starts the runner hidden. Written as UTF-16 (with a BOM), which wscript
    reads, so a non-ASCII profile path survives; powershell.exe is named by its full path."""
    powershell = _system32() / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    cmd = f'""{powershell}"" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{ps1}""'
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
        secret = token = None  # what was just typed in (and the token it gave) never shows up in an error
        try:
            if method == "password":
                username = ui.text("Moodle username", validate=lambda v: bool(v.strip()) or "Required")
                password = secret = ui.secret("Moodle password", validate=lambda v: bool(v) or "Required")
                with ui.spin("Signing in…"):
                    token = password_login(url, username, password)
                    who = Moodle(url=url, token=token).whoami()
                problem = password_file_problem(include_auto=False)
                if problem:
                    ui.warn(f"{problem} The password won't be remembered.")
                    remember = False
                else:
                    remember = ui.confirm("Remember the password so the token renews itself when it expires?",
                                          default=True)
                # Only now, with the sign-in checked, is anything remembered or saved (in one locked step).
                where = store_sign_in(url, token, username=username, password=password, remember=remember)
                if where:
                    ui.ok(f"Password saved in {where}.")
            else:
                ui.info(f"In Moodle: avatar → Preferences → Security keys ({url}/user/managetoken.php), press "
                        "[bold]Reset[/] on [italic]Moodle mobile web service[/] and copy the key. Or paste the "
                        "app launch link (see docs/getting-your-token.md).")
                raw = secret = ui.secret("Token or launch link")
                try:
                    token = parse_token(raw)
                except ValueError as e:
                    raise MoodleError(str(e), "badtoken") from None
                with ui.spin("Checking the token…"):
                    who = Moodle(url=url, token=token).whoami()
                store_sign_in(url, token)
            ui.ok(f"Signed in as [bold]{who['fullname']}[/] on {who['site']}.")
            return
        except (MoodleError, OSError) as e:
            ui.err(redact(redact(str(e), secret), token))
            if not ui.confirm("Try again?", default=True):
                raise Abort() from None


def step_writes(ui: UI) -> bool:
    ui.info("ChatGPT will act as you on Moodle. Read-only mode lets it read everything but never submit or "
            "change anything, even if a forum post or file tries to talk it into it.")
    return ui.select("Which mode?", [
        ("Read-only (recommended)", "ro"),
        ("Allow writes (submitting assignments, moodle_call writes)", "rw"),
    ]) == "rw"


def step_tunnel_client(ui: UI) -> Path:
    """Find tunnel-client, offer to update it, or install it in its standard place. Returns the command path."""
    with ui.spin("Looking for tunnel-client…"):
        current = tc.find()
    say = ui.ok
    if current:
        ui.ok(f"tunnel-client {current.version or '?'} found at {current.path} (managed by {current.manager}).")
        if current.manager == "other":
            ui.info("It was installed some other way, so update it the same way when you want to.")
            return current.path
        if current.manager == "homebrew":
            if ui.confirm("Run `brew upgrade` for it now?", default=False):
                with ui.spin("Updating with Homebrew…"):
                    current = tc.install_or_update(say)
            return current.path
        with ui.spin("Checking for a newer release…"):
            rel = tc.latest_release()
        if tc.version_tuple(rel.version) <= tc.version_tuple(current.version):
            ui.ok("It's the latest release.")
            return current.path
        if ui.confirm(f"Update it to {rel.tag}?", default=True):
            with ui.spin(f"Updating to {rel.tag}…"):
                current = tc.install_or_update(say)
            tc.restart_service(ui.info)
        return current.path
    where = ("with Homebrew (brew install openai/tools/tunnel-client)" if platform.system() == "Darwin"
             else f"to {tc.install_root()}, with the `tunnel-client` command at {tc.command_path()}")
    ui.info("tunnel-client is installed like any other command line tool, so you can use it for other tunnels "
            "and update it later with `moodle tunnel update`.")
    if not ui.confirm(f"Install OpenAI's tunnel-client {where}?", default=True):
        raise Abort()
    with ui.spin("Installing tunnel-client…"):
        installed = tc.install_or_update(say)
    return installed.path


def step_openai(ui: UI, paths: Paths) -> str:
    ui.info("You need two things from the OpenAI Platform (not your ChatGPT login):\n\n"
            "[bold]1. A tunnel[/]: OpenAI's record for this private connection.\n"
            "   Create it on the Tunnels page and attach your [bold]ChatGPT workspace[/],\n"
            "   so it shows up in ChatGPT. Then copy its id (tunnel_…).\n\n"
            "[bold]2. A runtime API key[/]: lets tunnel-client serve that tunnel.\n"
            "   It can't read your Moodle. Create it on the API keys page:\n"
            "   choose [bold]Restricted[/], set [bold]Tunnels: Read + Use[/], everything else None.\n"
            "   Not an \"All\" key, not an admin key. Your account itself also needs\n"
            "   Tunnels Read + Use (organization owners have it).\n\n"
            f"  Tunnels:  {TUNNELS_URL}\n"
            f"  API keys: {API_KEYS_URL}\n"
            "[dim]  More: docs/openai-tunnel.md → \"The tunnel id and the runtime API key\"[/]")
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
    who = ("only your user account can read it" if os.name != "nt"
           else "it's in your profile folder and inherits its permissions")
    ui.ok(f"Saved the key to {paths.key_file} ({who}).")
    return tunnel_id


def moodle_mcp_path() -> str:
    """The moodle-mcp command next to this Python (the uv tool environment) first, then PATH, never the
    current directory."""
    sibling = Path(sys.executable).with_name("moodle-mcp.exe" if os.name == "nt" else "moodle-mcp")
    if sibling.is_file():
        return str(sibling)
    found = tc.which("moodle-mcp")
    if found:
        return str(Path(found).resolve())
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
        if not tc.which("systemctl") or _run(["systemctl", "--user", "show-environment"]).returncode != 0:
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
        ps1.write_text(windows_runner(paths), encoding="utf-8-sig")
        startup = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Microsoft" / \
            "Windows" / "Start Menu" / "Programs" / "Startup"
        startup.mkdir(parents=True, exist_ok=True)
        vbs = startup / f"{SERVICE_NAME}.vbs"
        vbs.write_text(windows_launcher(ps1), encoding="utf-16", newline="")
        subprocess.Popen([str(_system32() / "wscript.exe"), str(vbs)],
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
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
    tc.no_cwd_exe_search()
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
        paths = default_paths(step_tunnel_client(ui))
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
