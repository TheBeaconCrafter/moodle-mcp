"""The quickstart's non-interactive parts: the tunnel-client profile command and the service files."""

import json
from pathlib import Path

import pytest

from moodle_mcp import quickstart as qs


def _paths(tmp_path):
    return qs.Paths(tmp_path / "cfg", tmp_path / "bin" / "tunnel-client")


def test_profile_command_uses_a_key_file_and_quotes_the_server(tmp_path):
    paths = _paths(tmp_path)
    cmd = qs.mcp_command("/opt/my tools/moodle-mcp", allow_writes=False)
    assert cmd == '"/opt/my tools/moodle-mcp"'
    assert qs.mcp_command("/x/moodle-mcp", allow_writes=True).endswith(" --allow-writes")
    args = qs.init_args(paths, "tunnel_abc123", cmd)
    assert args[0] == str(paths.tunnel_client)
    assert args[args.index("--mcp-command") + 1] == cmd
    assert args[args.index("--control-plane-api-key-ref") + 1] == f"file:{paths.key_file.as_posix()}"
    assert "--force" in args and args[args.index("--profile-dir") + 1] == str(paths.profile_dir)


def test_windows_paths_use_forward_slashes():
    assert qs.mcp_command(r"C:\Users\me\.local\bin\moodle-mcp.exe", False) == '"C:/Users/me/.local/bin/moodle-mcp.exe"'


def test_default_paths_point_at_the_stable_command(monkeypatch, tmp_path):
    monkeypatch.setattr(qs.tc, "command_path", lambda: tmp_path / "stable" / "tunnel-client")
    assert qs.default_paths().tunnel_client == tmp_path / "stable" / "tunnel-client"


def test_service_files_run_the_profile_without_secrets(tmp_path):
    paths = _paths(tmp_path)
    paths.config_dir.mkdir()
    paths.key_file.write_text("sk-secret-value")
    for text in (qs.systemd_unit(paths), qs.launchd_plist(paths, tmp_path / "log"), qs.windows_runner(paths)):
        assert "sk-secret-value" not in text
    for text in (qs.launchd_plist(paths, tmp_path / "log"), qs.windows_runner(paths)):
        assert str(paths.profile_file) in text and str(paths.tunnel_client) in text
    unit = qs.systemd_unit(paths)
    assert qs._systemd_quote(str(paths.profile_file)) in unit and qs._systemd_quote(str(paths.tunnel_client)) in unit
    assert "Restart=always" in qs.systemd_unit(paths)
    assert "<key>KeepAlive</key><true/>" in qs.launchd_plist(paths, tmp_path / "log")
    vbs = qs.windows_launcher(Path("C:/Users/x/run-tunnel.ps1"))
    assert vbs.startswith('CreateObject("WScript.Shell").Run """') and ", 0, False" in vbs
    assert 'WindowsPowerShell' in vbs and 'powershell.exe"" -NoProfile' in vbs


class _FakeUI:
    """Answers the quickstart's questions from a list and records what it shows."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.shown = []

    def _next(self, *a, **k):
        return self.answers.pop(0)

    text = secret = select = confirm = _next

    def info(self, t):
        self.shown.append(t)

    ok = warn = err = info

    def spin(self, _):
        import contextlib
        return contextlib.nullcontext()


def _password_site(monkeypatch, password, whoami=None):
    from moodle_mcp import client as mc

    monkeypatch.setattr(qs, "app_login_type", lambda url: 1)

    def login(url, user, pw):
        if pw != password:
            raise mc.MoodleError(f"Password sign-in failed: wrong password {pw}", "invalidlogin")
        return "n" * 32
    monkeypatch.setattr(qs, "password_login", login)
    monkeypatch.setattr(qs.Moodle, "whoami", whoami or (lambda self: {"fullname": "Sam", "site": "Example"}))


def test_quickstart_errors_never_show_the_typed_password(monkeypatch):
    from moodle_mcp import client as mc

    _password_site(monkeypatch, "right password")
    ui = _FakeUI(["https://moodle.example.edu", "password", "s1", "typed secret", False])
    with pytest.raises(qs.Abort):
        qs.step_moodle(ui)
    assert any("wrong password ***" in s for s in ui.shown)
    assert not any("typed secret" in s for s in ui.shown) and not mc.CONFIG_PATH.exists()


def test_quickstart_does_not_offer_to_remember_with_a_broken_password_file(monkeypatch, tmp_path):
    from moodle_mcp import client as mc

    _password_site(monkeypatch, "right password")
    monkeypatch.setenv("MOODLE_MCP_PASSWORD_FILE", str(tmp_path / "missing"))
    ui = _FakeUI(["https://moodle.example.edu", "password", "s1", "right password"])  # no remember question
    qs.step_moodle(ui)
    assert any("won't be remembered" in s for s in ui.shown)
    assert "remember" not in json.loads(mc.CONFIG_PATH.read_text())


def test_quickstart_remembers_only_after_the_sign_in_is_checked(monkeypatch):
    from moodle_mcp import client as mc

    def rejected(self):
        raise mc.MoodleError("Invalid token", "invalidtoken")
    _password_site(monkeypatch, "right password", whoami=rejected)
    monkeypatch.setattr(mc, "_keyring", lambda: None)
    ui = _FakeUI(["https://moodle.example.edu", "password", "s1", "right password", False])
    with pytest.raises(qs.Abort):
        qs.step_moodle(ui)
    assert not mc.default_password_file().exists() and not mc.CONFIG_PATH.exists()


# ---------------------------------------------------------------- service files and finding programs safely


def test_systemd_unit_escapes_specifiers_variables_and_quotes():
    assert qs._systemd_quote(r'/home/a b/%h/$HOME/"q"/back\slash') == r'"/home/a b/%%h/$$HOME/\"q\"/back\\slash"'
    with pytest.raises(qs.MoodleError):
        qs._systemd_quote("/home/me/evil\nExecStartPre=/bin/sh")


def test_powershell_quoting_doubles_typographic_quotes_too():
    assert qs._ps_quote("C:/Users/O'Brien") == "'C:/Users/O''Brien'"
    for q in ("\u2018", "\u2019", "\u201a", "\u201b"):
        assert qs._ps_quote(f"a{q}b") == f"'a{q}{q}b'"


def test_windows_service_files_use_bom_encodings_and_absolute_programs(monkeypatch, tmp_path):
    paths = qs.Paths(tmp_path / "cfg Jürgen", tmp_path / "bin" / "tunnel-client.exe")
    paths.profile_dir.mkdir(parents=True)
    monkeypatch.setattr(qs.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    started = []
    monkeypatch.setattr(qs.subprocess, "Popen", lambda args, **k: started.append(args))
    ui = _FakeUI([])
    assert qs.install_service(ui, paths)
    ps1 = (paths.profile_dir / "run-tunnel.ps1").read_bytes()
    assert ps1.startswith(b"\xef\xbb\xbf") and "Jürgen".encode() in ps1
    vbs_path = tmp_path / "AppData" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / \
        f"{qs.SERVICE_NAME}.vbs"
    vbs = vbs_path.read_bytes()
    assert vbs[:2] in (b"\xff\xfe", b"\xfe\xff")
    text = vbs.decode("utf-16")
    assert "Jürgen" in text and str(tmp_path / "Windows" / "System32" / "WindowsPowerShell") in text
    assert text.endswith('False\r\n') and "\r\r" not in text
    assert started == [[str(tmp_path / "Windows" / "System32" / "wscript.exe"), str(vbs_path)]]


def _fake_program(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / (name + (".exe" if qs.os.name == "nt" else ""))
    f.write_bytes(b"")
    f.chmod(0o755)
    return f


def test_which_never_picks_a_program_from_the_current_directory(monkeypatch, tmp_path):
    here = tmp_path / "here"
    planted = _fake_program(here, "moodle-mcp")
    monkeypatch.chdir(here)
    monkeypatch.setenv("PATH", qs.os.pathsep.join([".", str(here)]))
    assert planted.exists() and qs.tc.which("moodle-mcp") is None
    real = _fake_program(tmp_path / "bin", "moodle-mcp")
    monkeypatch.setenv("PATH", qs.os.pathsep.join([".", str(here), str(tmp_path / "bin")]))
    assert Path(qs.tc.which("moodle-mcp")).resolve() == real.resolve()


def test_moodle_mcp_path_prefers_the_command_next_to_python(monkeypatch, tmp_path):
    venv = tmp_path / "venv" / "bin"
    sibling = _fake_program(venv, "moodle-mcp")
    monkeypatch.setattr(qs.sys, "executable", str(venv / "python"))
    _fake_program(tmp_path / "elsewhere", "moodle-mcp")
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    assert qs.moodle_mcp_path() == str(sibling)


def test_api_key_message_matches_the_platform(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    ui = _FakeUI([False, "tunnel_abc123", "k" * 40])
    qs.step_openai(ui, paths)
    expected = "inherits its permissions" if qs.os.name == "nt" else "only your user account can read it"
    assert any(expected in s for s in ui.shown)
