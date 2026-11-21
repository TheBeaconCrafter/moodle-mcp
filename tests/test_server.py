import asyncio

import pytest

from moodle_mcp import server

WRITES = {"moodle_submit_assignment", "moodle_call", "moodle_download"}


def _tools():
    return asyncio.run(server.mcp.list_tools())


def test_all_tools_registered():
    names = {t.name for t in _tools()}
    assert len(names) == 17  # bump this when you add a tool
    assert {"moodle_deadlines", "moodle_read", "moodle_functions"} <= names


def test_only_the_writing_tools_are_marked_writable():
    writable = {t.name for t in _tools() if not (t.annotations and t.annotations.readOnlyHint)}
    assert writable == WRITES


def test_every_tool_has_a_description():
    assert all((t.description or "").strip() for t in _tools())


def test_http_bearer_guard_rejects_missing_or_wrong_token():
    calls = []

    async def app(scope, receive, send):
        calls.append(scope)

    guarded = server._bearer_guard(app, "s3cret")
    sent = []

    async def send(msg):
        sent.append(msg)

    async def go(headers):
        sent.clear()
        await guarded({"type": "http", "headers": headers}, None, send)
        return sent[0]["status"] if sent else 200

    assert asyncio.run(go([])) == 401
    assert asyncio.run(go([(b"authorization", b"Bearer nope")])) == 401
    assert asyncio.run(go([(b"authorization", b"Bearer s3cret")])) == 200
    assert len(calls) == 1


def test_guard_reports_missing_config_instead_of_raising(monkeypatch):
    monkeypatch.setattr(server, "_client", None)
    out = server.moodle_courses()
    assert out["errorcode"] == "notoken"


def test_guard_catches_unexpected_errors_and_redacts_the_token(monkeypatch):
    token = "z" * 32

    class Boom:
        def __init__(self):
            self.token = token

        def whoami(self):
            raise OSError(f"failed GET https://moodle.example.edu/x?token={token}")

    monkeypatch.setattr(server, "_client", Boom())
    out = server.moodle_whoami()
    assert "error" in out and token not in out["error"] and "OSError" in out["error"]


def test_moodle_call_passes_params_through_without_name_clashes(monkeypatch):
    seen = {}

    class Fake:
        token = "z" * 32

        def call(self, wsfunction, /, **params):
            seen.update(wsfunction=wsfunction, params=params)
            return {"ok": True}

    monkeypatch.setattr(server, "_client", Fake())
    assert server.moodle_call("core_course_get_contents", {"fn": 1, "a": 2}) == {"ok": True}
    assert seen == {"wsfunction": "core_course_get_contents", "params": {"fn": 1, "a": 2}}


def test_instructions_mark_moodle_content_as_untrusted():
    text = server.mcp.instructions or ""
    assert "untrusted" in text and "moodle_call" in text


def _no_uvicorn(monkeypatch):
    import uvicorn

    runs = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: runs.append(kw))
    return runs


@pytest.mark.parametrize("token", ["", "short-secret"])
def test_http_mode_refuses_to_start_without_a_strong_token(monkeypatch, capsys, token):
    runs = _no_uvicorn(monkeypatch)
    monkeypatch.setenv("MOODLE_MCP_HTTP_TOKEN", token)
    with pytest.raises(SystemExit) as e:
        server.main(["--http"])
    assert e.value.code == 2 and runs == []
    assert "MOODLE_MCP_HTTP_TOKEN" in capsys.readouterr().err


def test_http_mode_starts_with_a_token_or_explicit_no_auth(monkeypatch, capsys):
    runs = _no_uvicorn(monkeypatch)
    monkeypatch.setenv("MOODLE_MCP_HTTP_TOKEN", "x" * 32)
    server.main(["--http"])
    assert runs and runs[-1]["host"] == "127.0.0.1"
    monkeypatch.delenv("MOODLE_MCP_HTTP_TOKEN")
    server.main(["--http", "--no-auth"])
    assert len(runs) == 2 and "WITHOUT authentication" in capsys.readouterr().err
    monkeypatch.setenv("MOODLE_MCP_HTTP_TOKEN", "short")
    with pytest.raises(SystemExit):
        server.main(["--http", "--no-auth"])
