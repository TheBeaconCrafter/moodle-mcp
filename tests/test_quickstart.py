"""The quickstart's non-interactive parts: release selection, checksum, extraction and service files."""

import contextlib
import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest

from moodle_mcp import quickstart as qs
from moodle_mcp.client import MoodleError

TAG = "v0.0.99"


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize("system,machine,slug", [
    ("Linux", "x86_64", "linux-amd64"), ("Linux", "aarch64", "linux-arm64"),
    ("Darwin", "arm64", "darwin-arm64"), ("Windows", "AMD64", "windows-amd64"), ("Windows", "ARM64", "windows-arm64"),
])
def test_platform_slug(system, machine, slug):
    assert qs.platform_slug(system, machine) == slug
    assert qs.asset_name(TAG, slug) == f"tunnel-client-{TAG}-{slug}.zip"


def test_platform_slug_rejects_unknown():
    with pytest.raises(MoodleError):
        qs.platform_slug("Linux", "riscv64")


def test_parse_sha256sums():
    h = "a" * 64
    sums = qs.parse_sha256sums(f"{h}  tunnel-client-{TAG}-linux-amd64.zip\n{h.upper()} *SHA-file.zip\njunk line\n")
    assert sums == {f"tunnel-client-{TAG}-linux-amd64.zip": h, "SHA-file.zip": h}


def test_extract_release_keeps_files_inside_dest(tmp_path):
    data = _zip({"tunnel-client": b"bin", "LICENSE": b"l", "../evil": b"x", "sub/nested": b"y"})
    exe = qs.extract_release(data, tmp_path / "bin")
    assert exe == tmp_path / "bin" / "tunnel-client"
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["LICENSE", "tunnel-client"]
    assert not (tmp_path / "evil").exists()


def test_extract_release_needs_the_program(tmp_path):
    with pytest.raises(MoodleError):
        qs.extract_release(_zip({"README": b"x"}), tmp_path)


def test_profile_command_uses_a_key_file_and_quotes_the_server(tmp_path):
    paths = qs.Paths(tmp_path / "cfg", tmp_path / "bin")
    cmd = qs.mcp_command("/opt/my tools/moodle-mcp", allow_writes=False)
    assert cmd == '"/opt/my tools/moodle-mcp"'
    assert qs.mcp_command("/x/moodle-mcp", allow_writes=True).endswith(" --allow-writes")
    args = qs.init_args(paths, "tunnel_abc123", cmd)
    assert args[args.index("--mcp-command") + 1] == cmd
    assert args[args.index("--control-plane-api-key-ref") + 1] == f"file:{paths.key_file.as_posix()}"
    assert "--force" in args and args[args.index("--profile-dir") + 1] == str(paths.profile_dir)


def test_service_files_run_the_profile_without_secrets(tmp_path):
    paths = qs.Paths(tmp_path / "cfg", tmp_path / "bin")
    paths.config_dir.mkdir()
    paths.key_file.write_text("sk-secret-value")
    for text in (qs.systemd_unit(paths), qs.launchd_plist(paths, tmp_path / "log"), qs.windows_runner(paths)):
        assert str(paths.profile_file) in text and "sk-secret-value" not in text
    assert "Restart=always" in qs.systemd_unit(paths)
    assert "<key>KeepAlive</key><true/>" in qs.launchd_plist(paths, tmp_path / "log")
    vbs = qs.windows_launcher(Path("C:/Users/x/run-tunnel.ps1"))
    assert vbs.startswith('CreateObject("WScript.Shell").Run "powershell.exe') and ', 0, False' in vbs


class _UI:
    def __init__(self):
        self.lines = []

    def spin(self, text):
        return contextlib.nullcontext()

    def ok(self, text):
        self.lines.append(text)


def _fake_github(monkeypatch, zip_bytes, published_hash):
    name = qs.asset_name(TAG, qs.platform_slug())

    def handler(request: httpx.Request):
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": TAG, "assets": [
                {"name": name, "browser_download_url": f"https://dl.example/{name}"},
                {"name": "SHA256SUMS.txt", "browser_download_url": "https://dl.example/SHA256SUMS.txt"}]})
        if request.url.path.endswith("SHA256SUMS.txt"):
            return httpx.Response(200, text=f"{published_hash}  {name}\n")
        return httpx.Response(200, content=zip_bytes)

    real = httpx.Client
    monkeypatch.setattr(qs.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))


def test_download_refuses_a_checksum_mismatch(monkeypatch, tmp_path):
    data = _zip({"tunnel-client.exe": b"a", "tunnel-client": b"a"})
    _fake_github(monkeypatch, data, "0" * 64)
    with pytest.raises(MoodleError) as e:
        qs.download_tunnel_client(_UI(), qs.Paths(tmp_path, tmp_path / "bin"))
    assert e.value.code == "badchecksum" and not (tmp_path / "bin").exists()


def test_download_installs_a_verified_release(monkeypatch, tmp_path):
    data = _zip({"tunnel-client.exe": b"a", "tunnel-client": b"a"})
    _fake_github(monkeypatch, data, hashlib.sha256(data).hexdigest())
    exe = qs.download_tunnel_client(_UI(), qs.Paths(tmp_path, tmp_path / "bin"))
    assert exe.parent == tmp_path / "bin" and exe.exists()


def test_windows_paths_use_forward_slashes():
    assert qs.mcp_command(r"C:\Users\me\.local\bin\moodle-mcp.exe", False) == '"C:/Users/me/.local/bin/moodle-mcp.exe"'
