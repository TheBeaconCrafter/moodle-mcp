"""Staying signed in: picking up a new token from the config file, and renewing with a remembered password."""

import json
import os

import httpx
import pytest

from moodle_mcp import cli
from moodle_mcp import client as mc

URL = "https://moodle.example.edu"
OLD, NEW = "a" * 32, "b" * 32
PASSWORD = "correct horse battery staple"


def _handler(valid_token, logins):
    def handler(request: httpx.Request):
        form = dict(httpx.QueryParams(request.content.decode()))
        if request.url.path.endswith("/login/token.php"):
            logins.append(form["username"])
            if form["password"] == PASSWORD:
                return httpx.Response(200, json={"token": NEW, "privatetoken": "p"})
            return httpx.Response(200, json={"error": f"Invalid login, password was {form['password']}",
                                             "errorcode": "invalidlogin"})
        if form["wstoken"] != valid_token:
            return httpx.Response(200, json={"exception": "moodle_exception", "errorcode": "invalidtoken",
                                             "message": "Invalid token - token expired"})
        return httpx.Response(200, json={"sitename": "Example", "userid": 7, "username": "s1", "fullname": "Sam",
                                         "functions": [{"name": "core_webservice_get_site_info"}]})
    return handler


def _client(handler):
    c = mc.Moodle()
    c.http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_running_client_picks_up_a_new_token_from_moodle_auth():
    mc.save_config({"url": URL, "token": OLD})
    c = _client(_handler(NEW, []))
    mc.save_config({"url": URL, "token": NEW})  # the user ran `moodle auth` while the server kept running
    assert c.whoami()["fullname"] == "Sam"
    assert c.token == NEW


def test_remembered_password_renews_an_expired_token(tmp_path, monkeypatch):
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD)
    pw.chmod(0o600)
    monkeypatch.setenv("MOODLE_MCP_PASSWORD_FILE", str(pw))
    mc.save_config({"url": URL, "token": OLD, "username": "s1", "remember": True, "saved_at": 0})
    logins = []
    c = _client(_handler(NEW, logins))
    assert c.whoami()["auto_renew"] is True
    assert logins == ["s1"] and c.token == NEW
    saved = json.loads(mc.CONFIG_PATH.read_text())
    assert saved["token"] == NEW and saved["saved_at"] > 0 and PASSWORD not in mc.CONFIG_PATH.read_text()


def test_no_renewal_without_remember():
    mc.save_config({"url": URL, "token": OLD, "username": "s1"})
    logins = []
    c = _client(_handler(NEW, logins))
    with pytest.raises(mc.MoodleError) as e:
        c.whoami()
    assert e.value.code == "invalidtoken" and logins == []


def test_explicit_or_env_tokens_are_never_swapped(monkeypatch):
    mc.save_config({"url": URL, "token": NEW})
    c = mc.Moodle(url=URL, token=OLD)
    c.http = httpx.Client(transport=httpx.MockTransport(_handler(NEW, [])))
    with pytest.raises(mc.MoodleError):
        c.whoami()
    monkeypatch.setenv("MOODLE_TOKEN", OLD)
    c = _client(_handler(NEW, []))
    with pytest.raises(mc.MoodleError):
        c.whoami()


def test_renewal_never_uses_a_token_saved_for_another_site(monkeypatch):
    mc.save_config({"url": URL, "token": OLD})
    c = _client(_handler(NEW, []))
    mc.save_config({"url": "https://other.example.org", "token": NEW})
    with pytest.raises(mc.MoodleError):
        c.whoami()


def test_renewal_treats_a_different_path_case_as_another_site():
    mc.save_config({"url": URL + "/Moodle", "token": OLD})
    c = _client(_handler(NEW, []))
    mc.save_config({"url": URL + "/moodle", "token": NEW})
    with pytest.raises(mc.MoodleError):
        c.whoami()
    assert c.token == OLD


def test_password_login_errors_never_echo_the_password():
    http = httpx.Client(transport=httpx.MockTransport(_handler(NEW, [])))
    with pytest.raises(mc.MoodleError) as e:
        mc.password_login(URL, "s1", "wrong-pass-123", http)
    assert "wrong-pass-123" not in str(e.value) and e.value.code == "invalidlogin"
    assert mc.password_login(URL, "s1", PASSWORD, http) == NEW


def test_password_file_wins_over_keyring(tmp_path, monkeypatch):
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD + "\n")
    pw.chmod(0o600)
    monkeypatch.setenv("MOODLE_MCP_PASSWORD_FILE", str(pw))
    assert mc.recall_password(URL, "s1") == PASSWORD


def test_status_exits_1_when_signed_out(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["moodle", "status"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1
    assert "moodle auth" in capsys.readouterr().out


# ---------------------------------------------------------------- config lock and renewal races


def _pw_file(tmp_path, monkeypatch):
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD)
    pw.chmod(0o600)
    monkeypatch.setenv("MOODLE_MCP_PASSWORD_FILE", str(pw))


def test_config_lock_is_exclusive_and_cleaned_up():
    lock = mc.CONFIG_PATH.with_name(mc.CONFIG_PATH.name + ".lock")
    with mc.config_lock():
        assert lock.exists()
        with pytest.raises(mc.MoodleError) as e:
            with mc.config_lock(timeout=0.1):
                pass
        assert e.value.code == "locked"
    assert not lock.exists()
    with mc.config_lock(timeout=0.1):
        pass


def test_stale_config_lock_is_taken_over():
    import os
    import time

    lock = mc.CONFIG_PATH.with_name(mc.CONFIG_PATH.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("12345")
    old = time.time() - mc.LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))
    with mc.config_lock(timeout=0.5):
        pass
    assert not lock.exists()


def _racing_handler(on_login):
    inner = _handler(NEW, [])

    def handler(request):
        if request.url.path.endswith("/login/token.php"):
            on_login()
        return inner(request)
    return handler


def test_renewal_does_not_overwrite_a_concurrent_moodle_auth(tmp_path, monkeypatch):
    _pw_file(tmp_path, monkeypatch)
    other = "c" * 32
    mc.save_config({"url": URL, "token": OLD, "username": "s1", "remember": True})
    # While the client signs in, the user runs `moodle auth` with another token.
    c = _client(_racing_handler(lambda: mc.save_config({"url": URL, "token": other, "saved_at": 1})))
    assert c.renew_token() is True
    assert c.token == other
    assert json.loads(mc.CONFIG_PATH.read_text())["token"] == other


def test_renewal_does_not_undo_a_concurrent_logout(tmp_path, monkeypatch):
    _pw_file(tmp_path, monkeypatch)
    mc.save_config({"url": URL, "token": OLD, "username": "s1", "remember": True})
    c = _client(_racing_handler(mc.clear_config))
    assert c.renew_token() is False
    assert not mc.CONFIG_PATH.exists() and c.token == OLD


def test_renewal_does_not_write_while_another_process_holds_the_lock(tmp_path, monkeypatch):
    _pw_file(tmp_path, monkeypatch)
    monkeypatch.setattr(mc, "LOCK_TIMEOUT", 0.1)
    mc.save_config({"url": URL, "token": OLD, "username": "s1", "remember": True})
    lock = mc.CONFIG_PATH.with_name(mc.CONFIG_PATH.name + ".lock")
    lock.write_text("1")
    c = _client(_handler(NEW, []))
    assert c.renew_token() is False
    assert json.loads(mc.CONFIG_PATH.read_text())["token"] == OLD


# ---------------------------------------------------------------- logout


class _FakeKeyring:
    class errors:
        class PasswordDeleteError(Exception):
            pass

    def __init__(self, store=None, broken=False):
        self.store = dict(store or {})
        self.broken = broken
        self.deleted = []

    def delete_password(self, service, key):
        if self.broken:
            raise RuntimeError("locked keychain")
        if (service, key) not in self.store:
            raise self.errors.PasswordDeleteError(key)
        del self.store[(service, key)]
        self.deleted.append(key)


def _logout(monkeypatch):
    monkeypatch.setattr("sys.argv", ["moodle", "logout"])
    cli.main()


def test_logout_uses_the_saved_site_not_moodle_url(monkeypatch, capsys):
    kr = _FakeKeyring({("moodle-mcp", f"{URL}/Moodle|s1"): PASSWORD})
    monkeypatch.setattr(mc, "_keyring", lambda: kr)
    mc.save_config({"url": URL + "/Moodle", "token": OLD, "username": "s1", "remember": True})
    monkeypatch.setenv("MOODLE_URL", "https://other.example.org")
    _logout(monkeypatch)
    assert kr.deleted == [f"{URL}/Moodle|s1"] and not mc.CONFIG_PATH.exists()
    assert "keychain" in capsys.readouterr().out


def test_logout_reports_a_keychain_failure(monkeypatch, capsys):
    monkeypatch.setattr(mc, "_keyring", lambda: _FakeKeyring(broken=True))
    mc.save_config({"url": URL, "token": OLD, "username": "s1", "remember": True})
    _logout(monkeypatch)
    assert "Couldn't remove" in capsys.readouterr().err
    assert not mc.CONFIG_PATH.exists()


def test_logout_mentions_the_password_file_is_kept(tmp_path, monkeypatch, capsys):
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD)
    pw.chmod(0o600)
    monkeypatch.setenv("MOODLE_MCP_PASSWORD_FILE", str(pw))
    monkeypatch.setattr(mc, "_keyring", lambda: None)
    mc.save_config({"url": URL, "token": OLD, "username": "s1", "remember": True})
    _logout(monkeypatch)
    assert "not deleted" in capsys.readouterr().out and pw.exists()


def test_forget_password_statuses(monkeypatch):
    monkeypatch.setattr(mc, "_keyring", lambda: None)
    assert mc.forget_password(URL, "s1") == "nokeyring"
    monkeypatch.setattr(mc, "_keyring", lambda: _FakeKeyring())
    assert mc.forget_password(URL, "s1") == "absent"
    monkeypatch.setattr(mc, "_keyring", lambda: _FakeKeyring({("moodle-mcp", f"{URL}|s1"): "x"}))
    assert mc.forget_password(URL, "s1") == "deleted"


# ---------------------------------------------------------------- password file permissions


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_password_file_readable_by_others_is_refused(tmp_path, monkeypatch, mode):
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD)
    pw.chmod(mode)
    monkeypatch.setenv("MOODLE_MCP_PASSWORD_FILE", str(pw))
    assert mc.recall_password(URL, "s1") is None
    assert "chmod 600" in mc.password_file_problem()
    pw.chmod(0o600)
    assert mc.recall_password(URL, "s1") == PASSWORD and mc.password_file_problem() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_status_explains_a_bad_password_file(tmp_path, monkeypatch, capsys):
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD)
    pw.chmod(0o644)
    monkeypatch.setenv("MOODLE_MCP_PASSWORD_FILE", str(pw))
    mc.save_config({"url": URL, "token": NEW, "username": "s1", "remember": True})
    handler = _handler(NEW, [])
    real_init = mc.Moodle.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self.http = httpx.Client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(mc.Moodle, "__init__", init)
    monkeypatch.setattr("sys.argv", ["moodle", "status"])
    cli.main()
    out = capsys.readouterr().out
    assert "Auto-renew: off" in out and "chmod 600" in out


def test_missing_password_file_is_a_problem_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("MOODLE_MCP_PASSWORD_FILE", str(tmp_path / "nope"))
    assert mc.recall_password(URL, "s1") is None
    assert "can't be opened" in mc.password_file_problem()



@pytest.mark.parametrize("argv", [["moodle", "--json", "status"], ["moodle", "status", "--json"]])
def test_json_flag_works_before_or_after_the_subcommand(monkeypatch, capsys, argv):
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit):
        cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["errorcode"] == "notoken"


def test_app_login_type_reads_public_config():
    def handler(request):
        assert "tool_mobile_get_public_config" in str(request.url)
        return httpx.Response(200, json=[{"error": False, "data": {"typeoflogin": 1}}])
    assert mc.app_login_type(URL, httpx.Client(transport=httpx.MockTransport(handler))) == 1

    def broken(request):
        return httpx.Response(500, text="nope")
    assert mc.app_login_type(URL, httpx.Client(transport=httpx.MockTransport(broken))) is None
