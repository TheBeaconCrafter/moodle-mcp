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
        assert str(paths.profile_file) in text and str(paths.tunnel_client) in text
        assert "sk-secret-value" not in text
    assert "Restart=always" in qs.systemd_unit(paths)
    assert "<key>KeepAlive</key><true/>" in qs.launchd_plist(paths, tmp_path / "log")
    vbs = qs.windows_launcher(Path("C:/Users/x/run-tunnel.ps1"))
    assert vbs.startswith('CreateObject("WScript.Shell").Run "powershell.exe') and ", 0, False" in vbs


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
