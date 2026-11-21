"""Installing and updating tunnel-client: release selection, checksum, layout, updates."""

import hashlib
import io
import os
import sys
import zipfile

import httpx
import pytest

from moodle_mcp import tunnelclient as tc
from moodle_mcp.client import MoodleError

EXE = tc.EXE


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
    assert tc.platform_slug(system, machine) == slug
    assert tc.asset_name("v1.2.3", slug) == f"tunnel-client-v1.2.3-{slug}.zip"


def test_platform_slug_rejects_unknown():
    with pytest.raises(MoodleError):
        tc.platform_slug("Linux", "riscv64")


def test_parse_sha256sums_and_versions():
    h = "a" * 64
    assert tc.parse_sha256sums(f"{h}  x.zip\n{h.upper()} *y.zip\njunk\n") == {"x.zip": h, "y.zip": h}
    assert tc.parse_version("0.0.21+714f2d6 (git sha: 714f2d6)") == "0.0.21"
    assert tc.parse_version("v0.1.0") == "0.1.0"
    assert tc.version_tuple("0.0.22") > tc.version_tuple("0.0.21") > tc.version_tuple(None)


def test_extract_release_keeps_files_inside_dest(tmp_path):
    exe = tc.extract_release(_zip({EXE: b"bin", "LICENSE": b"l", "../evil": b"x", "sub/nested": b"y"}), tmp_path / "d")
    assert exe == tmp_path / "d" / EXE
    assert sorted(p.name for p in (tmp_path / "d").iterdir()) == sorted([EXE, "LICENSE"])
    assert not (tmp_path / "evil").exists()


def test_extract_release_needs_the_program(tmp_path):
    with pytest.raises(MoodleError):
        tc.extract_release(_zip({"README": b"x"}), tmp_path / "d")
    assert not (tmp_path / "d").exists()


def _fake_github(monkeypatch, releases):
    """releases: list of (tag, zip bytes, published sha) served in order by /releases/latest."""
    state = {"i": 0}

    def handler(request: httpx.Request):
        tag, data, sha = releases[min(state["i"], len(releases) - 1)]
        name = tc.asset_name(tag, tc.platform_slug())
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": tag, "assets": [
                {"name": name, "browser_download_url": f"https://dl.example/{tag}/{name}"},
                {"name": "SHA256SUMS.txt", "browser_download_url": f"https://dl.example/{tag}/SHA256SUMS.txt"}]})
        if request.url.path.endswith("SHA256SUMS.txt"):
            return httpx.Response(200, text=f"{sha}  {name}\n")
        state["i"] += 1
        return httpx.Response(200, content=data)

    real = httpx.Client
    monkeypatch.setattr(tc, "_http", lambda: real(transport=httpx.MockTransport(handler)))


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Install into tmp_path; pretend we're not on macOS and nothing is on PATH; fake --version from the file."""
    monkeypatch.setattr(tc.platform, "system", lambda: "Windows" if os.name == "nt" else "Linux")
    monkeypatch.setattr(tc, "install_root", lambda: tmp_path / "root")
    cmd = tmp_path / "root" / EXE if os.name == "nt" else tmp_path / "home" / ".local" / "bin" / EXE
    monkeypatch.setattr(tc, "command_path", lambda: cmd)
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    monkeypatch.setattr(tc, "installed_version", lambda p: p.resolve().read_bytes().decode() if p.exists() else None)
    monkeypatch.setattr(tc, "add_to_user_path", lambda folder: True)
    return cmd


def _release(version):
    data = _zip({EXE: version.encode(), "cloudflared": b"cf"})
    return (f"v{version}", data, hashlib.sha256(data).hexdigest())


def test_install_refuses_a_checksum_mismatch(monkeypatch, tmp_path, sandbox):
    tag, data, _ = _release("0.0.21")
    _fake_github(monkeypatch, [(tag, data, "0" * 64)])
    with pytest.raises(MoodleError) as e:
        tc.install_or_update()
    assert e.value.code == "badchecksum" and not sandbox.exists()


def test_install_then_update_keeps_the_stable_command(monkeypatch, tmp_path, sandbox):
    if os.name != "nt" and not hasattr(os, "symlink"):
        pytest.skip("no symlinks")
    _fake_github(monkeypatch, [_release("0.0.21"), _release("0.0.22")])
    first = tc.install_or_update()
    assert first.path == sandbox and first.version == "0.0.21" and first.manager == "moodle-mcp"
    assert tc.find().version == "0.0.21"
    second = tc.install_or_update()
    assert second.path == sandbox and second.version == "0.0.22"
    assert tc.install_or_update().version == "0.0.22"  # already latest: nothing to do
    if os.name != "nt":
        versions = sorted(d.name for d in (tmp_path / "root" / "versions").iterdir())
        assert versions == ["v0.0.21", "v0.0.22"]  # previous version kept for rollback
        assert (tmp_path / "root" / "versions" / "v0.0.22" / "cloudflared").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink layout is Linux-only")
def test_linux_prunes_old_versions(monkeypatch, tmp_path, sandbox):
    _fake_github(monkeypatch, [_release("0.0.1"), _release("0.0.2"), _release("0.0.3")])
    for _ in range(3):
        tc.install_or_update()
    assert sorted(d.name for d in (tmp_path / "root" / "versions").iterdir()) == ["v0.0.2", "v0.0.3"]


def test_foreign_install_is_left_alone(monkeypatch, tmp_path, sandbox):
    other = tmp_path / "elsewhere" / EXE
    other.parent.mkdir()
    other.write_bytes(b"0.0.1")
    monkeypatch.setattr(tc.shutil, "which", lambda name: str(other))
    found = tc.find()
    assert found.manager == "other" and found.path == other
    with pytest.raises(MoodleError) as e:
        tc.install_or_update()
    assert e.value.code == "foreign"


def test_macos_without_homebrew_explains(monkeypatch, tmp_path):
    monkeypatch.setattr(tc.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tc, "brew", lambda: None)
    monkeypatch.setattr(tc, "command_path", lambda: tmp_path / "nope")
    monkeypatch.setattr(tc.shutil, "which", lambda name: None)
    with pytest.raises(MoodleError) as e:
        tc.install_or_update()
    assert e.value.code == "nobrew" and "brew.sh" in str(e.value)
