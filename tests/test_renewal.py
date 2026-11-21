"""Staying signed in: picking up a new token from the config file, and renewing with a remembered password."""

import json
import os
import stat

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
    assert "Removed the remembered password" in capsys.readouterr().out


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


class _NoBackend:
    """A keyring module whose backend fails, like keyring on a headless Linux box."""

    class errors:
        class PasswordDeleteError(Exception):
            pass

    def set_password(self, *a):
        raise RuntimeError("No recommended backend was available")

    def get_password(self, *a):
        raise RuntimeError("No recommended backend was available")

    def delete_password(self, *a):
        raise self.errors.PasswordDeleteError()


class _Keychain(_NoBackend):
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, pw):
        self.store[(service, user)] = pw

    def get_password(self, service, user):
        return self.store.get((service, user))


def test_headless_machine_falls_back_to_a_private_password_file(monkeypatch):
    monkeypatch.delenv("MOODLE_MCP_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(mc, "_keyring", lambda: _NoBackend())
    where = mc.remember_password(URL, "s1", PASSWORD)
    path = mc.default_password_file()
    assert str(path) in where and path.name == "auto-password.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"site": URL, "username": "s1", "password": PASSWORD}
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert mc.recall_password(URL, "s1") == PASSWORD
    assert mc.forget_password(URL, "s1") == "deleted" and not path.exists()


def test_keychain_wins_and_clears_an_old_password_file(monkeypatch):
    monkeypatch.delenv("MOODLE_MCP_PASSWORD_FILE", raising=False)
    mc._write_private(mc.default_password_file(), b"old")
    kc = _Keychain()
    monkeypatch.setattr(mc, "_keyring", lambda: kc)
    assert mc.remember_password(URL, "s1", PASSWORD) == "the system keychain"
    assert not mc.default_password_file().exists()
    assert mc.recall_password(URL, "s1") == PASSWORD


def test_fallback_password_file_renews_an_expired_token(monkeypatch):
    monkeypatch.delenv("MOODLE_MCP_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(mc, "_keyring", lambda: _NoBackend())
    mc.remember_password(URL, "s1", PASSWORD)
    mc.save_config({"url": URL, "token": OLD, "username": "s1", "remember": True, "saved_at": 0})
    logins = []
    c = _client(_handler(NEW, logins))
    assert c.whoami()["auto_renew"] is True and c.token == NEW and logins == ["s1"]


def test_upload_refuses_the_fallback_password_file(monkeypatch, tmp_path):
    monkeypatch.delenv("MOODLE_MCP_PASSWORD_FILE", raising=False)
    path = mc.default_password_file()
    mc._write_private(path, b"secret")
    monkeypatch.setenv("MOODLE_MCP_UPLOAD_DIRS", str(path.parent))
    with pytest.raises(mc.MoodleError) as e:
        mc.check_upload_path(str(path))
    assert "password file" in str(e.value)


# ---------------------------------------------------------------- the automatic password file is bound to one account


def test_fallback_password_file_only_serves_its_own_account(monkeypatch):
    monkeypatch.setattr(mc, "_keyring", lambda: _NoBackend())
    mc.remember_password(URL, "s1", PASSWORD)
    assert mc.recall_password(URL + "/", "s1") == PASSWORD
    assert mc.recall_password(URL, "s2") is None
    assert mc.recall_password("https://other.example.org", "s1") is None
    assert mc.forget_password("https://other.example.org", "s1") != "deleted"
    assert mc.forget_password(URL, "s2") != "deleted"
    assert mc.default_password_file().exists()
    assert mc.forget_password(URL, "s1") == "deleted" and not mc.default_password_file().exists()


def test_old_plain_password_file_is_not_read_or_deleted(monkeypatch):
    monkeypatch.setattr(mc, "_keyring", lambda: None)
    old = mc.CONFIG_PATH.parent / "password"
    mc._write_private(old, PASSWORD.encode())
    assert mc.recall_password(URL, "s1") is None
    mc.forget_password(URL, "s1")
    assert old.exists()


def test_a_user_password_file_at_the_automatic_path_is_never_written_or_deleted(monkeypatch):
    path = mc.default_password_file()
    mc._write_private(path, PASSWORD.encode())
    monkeypatch.setenv("MOODLE_MCP_PASSWORD_FILE", str(path))
    monkeypatch.setattr(mc, "_keyring", lambda: _Keychain())
    assert "not copied" in mc.remember_password(URL, "s1", "new password")
    mc.forget_password(URL, "s1")
    assert path.read_bytes() == PASSWORD.encode()
    assert mc.recall_password(URL, "s1") == PASSWORD


def test_status_reports_a_broken_automatic_password_file(monkeypatch):
    monkeypatch.setattr(mc, "_keyring", lambda: None)
    mc._write_private(mc.default_password_file(), b"not json")
    assert "expected format" in mc.password_file_problem()
    assert mc.password_file_problem(include_auto=False) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_status_reports_an_automatic_password_file_others_can_read(monkeypatch):
    monkeypatch.setattr(mc, "_keyring", lambda: _NoBackend())
    mc.remember_password(URL, "s1", PASSWORD)
    mc.default_password_file().chmod(0o644)
    assert "chmod 600" in mc.password_file_problem()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_write_private_leaves_an_existing_folder_mode_alone(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    mc._write_private(shared / "f", b"x")
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755
    assert stat.S_IMODE((shared / "f").stat().st_mode) == 0o600
    mc._write_private(tmp_path / "new" / "f", b"x")
    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o700


# ---------------------------------------------------------------- moodle auth checks before it remembers anything


def _auth(monkeypatch, argv, password, whoami):
    monkeypatch.setattr("sys.argv", ["moodle", "auth", *argv])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": password)
    monkeypatch.setattr(cli, "password_login", lambda url, user, pw: NEW if pw == PASSWORD else (_ for _ in ()).throw(
        mc.MoodleError("Password sign-in failed: Invalid login", "invalidlogin")))
    monkeypatch.setattr(cli, "app_login_type", lambda url: 1)
    monkeypatch.setattr(cli.Moodle, "whoami", whoami)
    cli.main()


def _who(self):
    return {"fullname": "Sam", "username": "s1", "site": "Example", "functions_available": 1}


def _rejected(self):
    raise mc.MoodleError("Invalid token", "invalidtoken")


def test_failed_sign_in_remembers_nothing_and_keeps_the_old_setup(monkeypatch):
    monkeypatch.setattr(mc, "_keyring", lambda: _NoBackend())
    mc.remember_password(URL, "s1", "old password")
    mc.save_config({"url": URL, "token": OLD, "username": "s1", "remember": True})
    before = mc.CONFIG_PATH.read_text()
    with pytest.raises(SystemExit):
        _auth(monkeypatch, ["--url", "https://new.example.org", "--password", "--username", "s1", "--remember"],
              PASSWORD, _rejected)
    assert mc.CONFIG_PATH.read_text() == before
    assert mc.recall_password(URL, "s1") == "old password"
    assert mc.recall_password("https://new.example.org", "s1") is None


def test_wrong_password_remembers_nothing(monkeypatch):
    monkeypatch.setattr(mc, "_keyring", lambda: _NoBackend())
    with pytest.raises(SystemExit) as e:
        _auth(monkeypatch, ["--url", URL, "--password", "--username", "s1", "--remember"], "wrong", _who)
    assert "wrong" not in str(e.value)
    assert not mc.default_password_file().exists() and not mc.CONFIG_PATH.exists()


def test_switching_accounts_keeps_the_new_fallback_password(monkeypatch, capsys):
    monkeypatch.setattr(mc, "_keyring", lambda: _NoBackend())
    mc.remember_password(URL, "s0", "old password")
    mc.save_config({"url": URL, "token": OLD, "username": "s0", "remember": True})
    _auth(monkeypatch, ["--url", URL, "--password", "--username", "s1", "--remember"], PASSWORD, _who)
    assert "Password saved" in capsys.readouterr().out
    assert mc.recall_password(URL, "s1") == PASSWORD
    saved = json.loads(mc.CONFIG_PATH.read_text())
    assert saved["username"] == "s1" and saved["remember"] is True and saved["token"] == NEW


def test_store_sign_in_forgets_the_old_password_when_only_the_site_changes(monkeypatch):
    kr = _FakeKeyring({("moodle-mcp", f"{URL}|s1"): PASSWORD})
    monkeypatch.setattr(mc, "_keyring", lambda: kr)
    mc.save_config({"url": URL, "token": OLD, "username": "s1", "remember": True})
    monkeypatch.setenv("MOODLE_URL", "https://env.example.org")  # the saved file decides, not the env view
    cli.store_sign_in("https://new.example.org", NEW)
    assert kr.deleted == [f"{URL}|s1"]


def test_rejected_token_hint_quotes_the_url(monkeypatch):
    with pytest.raises(SystemExit) as e:
        _auth(monkeypatch, ["--url", URL + "/a;b"], "c" * 32, _rejected)
    assert f"moodle auth --url '{URL}/a;b' --password" in str(e.value)
