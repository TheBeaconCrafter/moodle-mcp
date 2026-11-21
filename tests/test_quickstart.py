"""The quickstart's non-interactive parts: the tunnel-client profile command and the service files."""

from pathlib import Path

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
