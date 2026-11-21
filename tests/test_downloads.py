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
    """Save a file the way moodle_download does: any folder it has to make is reported as created."""
    created, cur = [], path.parent
    while not cur.exists():
        created.insert(0, str(cur))
        cur = cur.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"pdf")
    downloads.record(path, now=time.time() - days_ago * DAY, created_dirs=created)


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

        def download(self, fileurl, dest, root=None, created_dirs=None):
            target.write_bytes(b"pdf")
            return str(target)

    monkeypatch.setattr(server, "_client", Fake())
    out = server.moodle_download("https://moodle.example.edu/pluginfile.php/1/x/reading.pdf")
    assert out == {"path": str(target), "auto_delete_after_days": 7}
    assert [e["path"] for e in downloads.tracked()] == [str(target)]



# ---------------------------------------------------------------- only the very file that was downloaded is deleted


def test_same_size_edit_with_the_same_mtime_is_kept(droot):
    f = droot / "essay.docx"
    _download(f, 30)
    st = f.stat()
    f.write_bytes(b"PDF")  # same size
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    res = downloads.cleanup()
    assert res["removed"] == [] and res["kept_edited"] == [str(f)] and f.exists()


def test_a_replacement_file_with_matching_metadata_is_kept(droot):
    f = droot / "notes.pdf"
    _download(f, 30)
    st = f.stat()
    other = droot / "other.pdf"
    other.write_bytes(b"pdf")
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(other, f)  # same name, size, mtime and content, but a different file
    assert downloads.cleanup()["removed"] == [] and f.exists()


def test_manifest_entries_without_an_identity_never_delete(droot):
    mine = droot / "mine.pdf"
    mine.write_bytes(b"pdf")
    st = mine.stat()
    downloads._save({"files": [{"path": str(mine), "size": st.st_size, "mtime": st.st_mtime, "saved_at": 0},
                               {"path": "", "saved_at": 0}, {"path": str(droot), "saved_at": 0}],
                     "dirs": [{"path": str(droot)}], "last_cleanup": 0})
    downloads.cleanup(everything=True)
    assert mine.exists() and droot.exists()


def test_folders_you_made_are_never_pruned(droot):
    yours = droot / "Mine"
    yours.mkdir()
    empty = droot / "Empty"
    empty.mkdir()
    _download(yours / "a.pdf", 30)
    _download(droot / "CHEM 111" / "Week 1" / "b.pdf", 30)
    assert len(downloads.cleanup()["removed"]) == 2
    assert yours.exists() and empty.exists() and not (droot / "CHEM 111").exists()


def test_a_created_folder_with_your_files_in_it_stays(droot):
    _download(droot / "CHEM 111" / "a.pdf", 30)
    (droot / "CHEM 111" / "my notes.txt").write_text("mine")
    downloads.cleanup()
    assert (droot / "CHEM 111" / "my notes.txt").exists()


def _link_dir(link, target):
    """A directory symlink, or on Windows a junction; skip the test if neither can be made here."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip("can't create a directory link here")


def test_a_folder_swapped_for_a_link_redirects_nothing(droot, tmp_path):
    f = droot / "CHEM 111" / "lab.pdf"
    _download(f, 30)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "lab.pdf"
    victim.write_bytes(b"pdf")
    (droot / "CHEM 111" / "lab.pdf").unlink()
    (droot / "CHEM 111").rmdir()
    _link_dir(droot / "CHEM 111", outside)
    # Even if the identity check were fooled, the path check refuses anything behind a link.
    assert downloads._checked(droot / "CHEM 111" / "lab.pdf", droot.resolve()) is None
    downloads.cleanup(everything=True)
    assert victim.exists() and outside.exists()


def test_a_symlinked_file_is_never_followed(droot, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    link = droot / "link.pdf"
    try:
        os.symlink(victim, link)
    except (OSError, NotImplementedError):
        pytest.skip("can't create a symlink here")
    assert downloads._checked(link, droot.resolve()) is None
    downloads.record(link)
    assert downloads.tracked() == [] and victim.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_manifest_others_can_write_is_ignored(droot):
    f = droot / "a.pdf"
    _download(f, 30)
    downloads.manifest_path().chmod(0o666)
    assert downloads.tracked() == []
    downloads.cleanup()
    assert f.exists()


def test_manifest_updates_wait_for_the_lock(droot, monkeypatch):
    monkeypatch.setattr(downloads, "LOCK_TIMEOUT", 0.1)
    f = droot / "a.pdf"
    _download(f, 30)
    with mc.file_lock(downloads.manifest_path()):
        assert downloads.maybe_cleanup() is None  # couldn't get the lock: nothing happens, nothing raised
        downloads.record(droot / "a.pdf")  # never raises
    assert f.exists()
    assert downloads.cleanup()["removed"] == [str(f)]


def test_client_download_reports_the_folders_it_created(fake_moodle, tmp_path):
    import httpx

    c = fake_moodle(lambda r: httpx.Response(200, content=b"pdf", headers={"content-type": "application/pdf"}))
    base = tmp_path / "dl"
    (base / "existing").mkdir(parents=True)
    created = []
    c.download("https://moodle.example.edu/pluginfile.php/1/mod_resource/content/1/a.pdf",
               "existing/new/deeper", root=str(base), created_dirs=created)
    assert created == [str((base / "existing" / "new").resolve()), str((base / "existing" / "new" / "deeper").resolve())]
