"""Automatic clean-up of MCP downloads in ~/Downloads/Moodle."""

import os
import time

import pytest

from moodle_mcp import cli, downloads
from moodle_mcp import client as mc

DAY = 86400


@pytest.fixture
def droot(monkeypatch, tmp_path):
    r = tmp_path / "Downloads" / "Moodle"
    r.mkdir(parents=True)
    monkeypatch.setattr(downloads, "root", lambda: r)
    monkeypatch.delenv("MOODLE_MCP_DOWNLOAD_DAYS", raising=False)
    return r


def _download(path, days_ago, monkeypatch=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"pdf")
    downloads.record(path, now=time.time() - days_ago * DAY)


def test_old_downloads_are_deleted_and_empty_folders_pruned(droot, monkeypatch):
    old = droot / "CHEM 111" / "lab5.pdf"
    new = droot / "notes.pdf"
    _download(old, 10, monkeypatch)
    _download(new, 1, monkeypatch)
    res = downloads.cleanup()
    assert res["removed"] == [str(old)] and not old.exists() and new.exists()
    assert not (droot / "CHEM 111").exists() and droot.exists()
    assert [e["path"] for e in downloads.tracked()] == [str(new)]


def test_files_you_put_there_or_edited_are_never_deleted(droot, monkeypatch):
    mine = droot / "my-own.txt"
    mine.write_text("mine")
    edited = droot / "essay.docx"
    _download(edited, 30, monkeypatch)
    edited.write_bytes(b"my changes, longer than before")
    res = downloads.cleanup()
    assert mine.exists() and edited.exists()
    assert res["removed"] == [] and res["kept_edited"] == [str(edited)]
    assert downloads.tracked() == []  # an edited file is yours now


def test_moved_files_are_forgotten(droot, monkeypatch, tmp_path):
    f = droot / "slides.pptx"
    _download(f, 30, monkeypatch)
    f.rename(tmp_path / "slides.pptx")
    assert downloads.cleanup()["removed"] == [] and (tmp_path / "slides.pptx").exists()


def test_manifest_entries_outside_the_folder_are_ignored(droot, monkeypatch, tmp_path):
    outside = tmp_path / "important.txt"
    _download(outside, 30, monkeypatch)
    assert downloads.cleanup()["removed"] == [] and outside.exists()


def test_retention_setting_and_off_switch(droot, monkeypatch):
    f = droot / "a.pdf"
    _download(f, 3, monkeypatch)
    monkeypatch.setenv("MOODLE_MCP_DOWNLOAD_DAYS", "2")
    assert downloads.retention_days() == 2
    assert downloads.maybe_cleanup()["removed"] == [str(f)]
    monkeypatch.setenv("MOODLE_MCP_DOWNLOAD_DAYS", "0")
    assert downloads.maybe_cleanup() is None
    monkeypatch.setenv("MOODLE_MCP_DOWNLOAD_DAYS", "off")
    assert downloads.retention_days() == 0


def test_maybe_cleanup_runs_at_most_hourly(droot, monkeypatch):
    assert downloads.maybe_cleanup() is not None
    assert downloads.maybe_cleanup() is None


def test_clean_all_and_manifest_is_private(droot, monkeypatch):
    f = droot / "fresh.pdf"
    _download(f, 0, monkeypatch)
    assert downloads.cleanup(everything=True)["removed"] == [str(f)]
    assert downloads.manifest_path().parent == mc.CONFIG_PATH.parent
    if os.name != "nt":
        assert downloads.manifest_path().stat().st_mode & 0o077 == 0


def test_cli_downloads_list_and_clean(droot, monkeypatch, capsys):
    f = droot / "old.pdf"
    _download(f, 9, monkeypatch)
    monkeypatch.setattr("sys.argv", ["moodle", "downloads"])
    cli.main()
    assert "old.pdf" in capsys.readouterr().out
    monkeypatch.setattr("sys.argv", ["moodle", "downloads", "clean"])
    cli.main()
    assert "Deleted" in capsys.readouterr().out and not f.exists()


def test_server_download_tool_tracks_the_file(droot, monkeypatch):
    from moodle_mcp import server

    target = droot / "reading.pdf"

    class Fake:
        token = "t" * 32

        def download(self, fileurl, dest, root=None):
            target.write_bytes(b"pdf")
            return str(target)

    monkeypatch.setattr(server, "_client", Fake())
    out = server.moodle_download("https://moodle.example.edu/pluginfile.php/1/x/reading.pdf")
    assert out == {"path": str(target), "auto_delete_after_days": 7}
    assert [e["path"] for e in downloads.tracked()] == [str(target)]
