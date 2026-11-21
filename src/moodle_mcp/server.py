"""MCP server exposing Moodle as tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .client import Moodle, MoodleError, redact

mcp = FastMCP(
    "moodle",
    instructions=(
        "Tools for the user's Moodle site, acting as the user with their own permissions. "
        "Ids: course_id = course/view.php?id=…, cmid = the id in any mod/<type>/view.php?id=… link, "
        "assign_id = the assignment instance id returned by moodle_assignments. "
        "Start with moodle_deadlines for 'what is due', moodle_courses for ids, moodle_course_contents to browse. "
        "moodle_deadlines only lists unfinished assignments/quizzes; readings and in-class work live on course pages. "
        "moodle_submit_assignment changes the user's real submission: show exactly what will be sent and get a "
        "clear yes first, and only pass submit_for_grading=true when they say to finalise."
    ),
)

_client: Moodle | None = None

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def m() -> Moodle:
    global _client
    if _client is None:
        _client = Moodle()
    return _client


def _guard(fn) -> Any:
    """Run `fn()` and turn any failure into an {"error": ...} result with the token scrubbed out.

    Pass a lambda so that m() (which reads the config and can fail) runs inside the guard too."""
    try:
        return fn()
    except MoodleError as e:
        return {"error": redact(str(e), _token()), "errorcode": e.code}
    except Exception as e:  # anything else (OSError, ValueError, a bad zip...) must not leak a URL with the token
        return {"error": redact(f"{type(e).__name__}: {e}", _token())}


def _token() -> str | None:
    return _client.token if _client is not None else None


@mcp.tool(annotations=READ)
def moodle_whoami() -> dict:
    """Check the connection: site name, Moodle version, and the logged-in user."""
    return _guard(lambda: m().whoami())


@mcp.tool(annotations=READ)
def moodle_courses(which: str = "inprogress") -> Any:
    """List enrolled courses with their ids. which: inprogress (default) | future | past | all."""
    return _guard(lambda: m().courses(which))


@mcp.tool(annotations=READ)
def moodle_course_contents(course_id: int, include_descriptions: bool = True) -> Any:
    """Every section of a course with its activities/resources (cmid, type, name, dates, files, links)
    and the section text (where teachers often put weekly readings and 'for next class' notes)."""
    return _guard(lambda: m().course_contents(course_id, include_descriptions))


@mcp.tool(annotations=READ)
def moodle_search(query: str, course_ids: list[int] | None = None) -> Any:
    """Find activities, files and section text mentioning `query` across current courses (or the given ones)."""
    return _guard(lambda: m().search(query, course_ids))


@mcp.tool(annotations=READ)
def moodle_deadlines(days_ahead: int = 14, days_back: int = 0, course_id: int | None = None) -> Any:
    """Unfinished assignment/quiz deadlines from the Moodle timeline. days_back > 0 also returns overdue items.
    Dates here can lag edits teachers make; confirm critical ones with moodle_assignment."""
    return _guard(lambda: m().deadlines(days_ahead, days_back, course_id))


@mcp.tool(annotations=READ)
def moodle_assignments(course_ids: list[int] | None = None, include_status: bool = True,
                       only_open: bool = False) -> Any:
    """All assignments in current courses (or the given ones), sorted by due date, with
    submission status (new/draft/submitted/nosubmission) and grade when include_status is true."""
    return _guard(lambda: m().assignments(course_ids, include_status, only_open))


@mcp.tool(annotations=READ)
def moodle_assignment(assign_id: int | None = None, cmid: int | None = None) -> Any:
    """One assignment in full: instructions, attachment files, dates, time remaining, accepted submission
    types, what's already submitted, grade and feedback. Pass assign_id or cmid."""
    return _guard(lambda: m().assignment(assign_id, cmid))


@mcp.tool(annotations=READ)
def moodle_grades(course_id: int | None = None) -> Any:
    """Grade items for one course (grade, max, %, weight, feedback). Without course_id: course totals for all courses."""
    if course_id is None:
        return _guard(lambda: m().course_overview_grades())
    return _guard(lambda: m().grades(course_id))


@mcp.tool(annotations=READ)
def moodle_read(cmid: int) -> Any:
    """Open any activity/resource by cmid and return its readable content: page text, file list for
    resources/folders, link target for URLs, assignment detail, forum discussions, quiz dates/attempts."""
    return _guard(lambda: m().read_module(cmid))


@mcp.tool(annotations=READ)
def moodle_file_text(fileurl: str, offset: int = 0, max_chars: int = 40000) -> Any:
    """Text of a Moodle file (PDF, DOCX, PPTX, XLSX strings, HTML, plain text) from a `fileurl` returned by
    another tool. Long files come back in slices: call again with offset to continue."""
    def run():
        text = m().file_text(fileurl)
        chunk = text[offset:offset + max_chars]
        return {"total_chars": len(text), "offset": offset, "next_offset": offset + len(chunk)
                if offset + len(chunk) < len(text) else None, "text": chunk}
    return _guard(run)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def moodle_download(fileurl: str, dest_dir: str = "~/Downloads/Moodle") -> Any:
    """Save a Moodle file to disk (default ~/Downloads/Moodle) and return the local path."""
    return _guard(lambda: {"path": m().download(fileurl, dest_dir)})


@mcp.tool(annotations=READ)
def moodle_announcements(course_ids: list[int] | None = None, per_course: int = 5) -> Any:
    """Latest posts in each course's Announcements forum, newest first."""
    return _guard(lambda: m().announcements(course_ids, per_course))


@mcp.tool(annotations=READ)
def moodle_discussion(discussion_id: int) -> Any:
    """All posts in one forum discussion (ids come from moodle_announcements / moodle_read on a forum)."""
    return _guard(lambda: m().discussion(discussion_id))


@mcp.tool(annotations=READ)
def moodle_notifications(limit: int = 20, unread_only: bool = False) -> Any:
    """The bell-icon notifications: new grades, feedback, forum posts, due-date reminders."""
    return _guard(lambda: m().notifications(limit, unread_only))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
def moodle_submit_assignment(assign_id: int, text_html: str | None = None, file_paths: list[str] | None = None,
                             submit_for_grading: bool = False) -> Any:
    """Save the user's submission for an assignment: online text (HTML) and/or local files. This REPLACES the
    current submission's text/files. Leaves it as a draft unless submit_for_grading=true, which finalises it
    (and accepts the submission statement). Only call after the user has approved the exact content."""
    return _guard(lambda: m().save_submission(assign_id, text_html, file_paths, submit_for_grading))


@mcp.tool(annotations=READ)
def moodle_functions(match: str | None = None) -> Any:
    """Names of the Moodle web-service functions this token can call (filter with `match`, e.g. 'calendar').
    Use it to find a function for moodle_call when no dedicated tool fits."""
    return _guard(lambda: m().functions(match))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
def moodle_call(wsfunction: str, params: dict | None = None) -> Any:
    """Escape hatch: call any Moodle web-service function the token allows, e.g.
    wsfunction='mod_quiz_get_quizzes_by_courses', params={'courseids': [123]}. Nested params are fine.
    Treat any function that writes (save/submit/update/delete/send) like a submission: confirm first."""
    return _guard(lambda: m().call(wsfunction, **(params or {})))


def _bearer_guard(app, token: str):
    """Wrap an ASGI app so every HTTP request must carry `Authorization: Bearer <token>`."""
    import hmac

    expected = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await app(scope, receive, send)

    return guarded


def serve_http(port: int = 8765, path: str = "/mcp") -> None:
    """Serve MCP over Streamable HTTP on 127.0.0.1 only (for tunnels such as OpenAI's Secure MCP Tunnel).

    If MOODLE_MCP_HTTP_TOKEN is set, requests must send it as a bearer token."""
    import os

    import uvicorn

    mcp.settings.port = port
    mcp.settings.streamable_http_path = path
    mcp.settings.stateless_http = True
    app = mcp.streamable_http_app()
    token = os.environ.get("MOODLE_MCP_HTTP_TOKEN", "").strip()
    if token:
        app = _bearer_guard(app, token)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main(argv: list[str] | None = None) -> None:
    import argparse

    p = argparse.ArgumentParser(prog="moodle-mcp", description="Moodle MCP server (stdio by default).")
    p.add_argument("--http", action="store_true", help="serve Streamable HTTP on 127.0.0.1 instead of stdio")
    p.add_argument("--port", type=int, default=8765, help="HTTP port (default 8765)")
    p.add_argument("--path", default="/mcp", help="HTTP endpoint path (default /mcp)")
    a = p.parse_args(argv)
    if a.http:
        serve_http(a.port, a.path)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
