"""`moodle` command-line front end over the same client the MCP server uses."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time

from . import client
from .client import (
    Moodle,
    MoodleError,
    app_login_type,
    check_site_url,
    clear_config,
    config_lock,
    forget_password,
    load_config,
    parse_token,
    password_file_problem,
    password_login,
    read_saved_config,
    redact,
    remember_password,
    safe_code,
    save_config,
)

AUTH_HELP = """\
Most schools sign you in to Moodle through single sign-on, so there's no password to give this tool.
Instead it uses the same per-user token the official Moodle app gets. Two ways to grab it:

  A) Moodle → your avatar → Preferences → Security keys
     ({url}/user/managetoken.php)
     Click Reset on the "Moodle mobile web service" row; Moodle shows the new key
     once, so copy it then. If that row isn't there, sign in to the Moodle phone
     app once and it will appear.

  B) While logged in, open this in your browser:
     {url}/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=1&urlscheme=moodlemobile&confirmed=1
     Right-click "Click here to launch the app" → Copy Link Address, and paste that whole link.

The token is stored in {path} (readable only by your user account on macOS/Linux;
on Windows it lives in your profile folder).
"""


def store_sign_in(url: str, token: str, username: str | None = None, remember: bool = False) -> None:
    """Save a checked token (and, with remember, the account whose password was remembered) under the
    config lock, forgetting the password of a different account saved before."""
    extra = {"username": username, "remember": True} if remember and username else {}
    with config_lock():
        old = load_config()
        if old.get("username") and old.get("username") != extra.get("username"):
            forget_password(old.get("url", url), old["username"])
        save_config({"url": url, "token": token, "saved_at": int(time.time()), **extra})


def cmd_auth(args) -> None:
    cfg = load_config()
    try:
        raw_url = args.url or cfg.get("url") or input("Moodle site URL (e.g. https://moodle.yourschool.edu): ").strip()
        if not raw_url:
            raise MoodleError("No Moodle site URL given. Run `moodle auth --url https://moodle.yourschool.edu`.",
                              "badurl")
        url = check_site_url(raw_url)
    except MoodleError as e:
        sys.exit(f"✗ {e}")
    extra: dict = {}
    if args.remember and not args.password:
        sys.exit("✗ --remember only works with --password (a pasted token can't be renewed automatically).")
    if args.password:
        username = (args.username or input(f"Moodle username for {url}: ")).strip()
        password = getpass.getpass("Moodle password (hidden): ")
        try:
            token = password_login(url, username, password)
        except MoodleError as e:
            sys.exit(redact(f"✗ {e}", password))
        if args.remember:
            problem = password_file_problem()
            if problem:
                sys.exit(f"✗ {problem}")
            try:
                where = remember_password(url, username, password)
            except MoodleError as e:
                sys.exit(f"✗ {e}")
            extra = {"username": username, "remember": True}
            print(f"✓ Password saved in {where}. The token will renew itself when it expires.")
        else:
            forget_password(url, username)
    else:
        print(AUTH_HELP.format(url=url, path=client.CONFIG_PATH))
        raw = getpass.getpass("Paste token or launch link (hidden): ")
        try:
            token = parse_token(raw)
        except ValueError as e:
            sys.exit(f"✗ {e}")
    try:
        who = Moodle(url=url, token=token).whoami()
    except MoodleError as e:
        hint = ""
        if e.code == "invalidtoken" and app_login_type(url) == 1:
            hint = ("\n  This site signs the Moodle app in with a username and password, so try:\n"
                    f"    moodle auth --url {url} --password")
        sys.exit(redact(f"✗ Moodle rejected that token: {e}{hint}", token))
    except Exception as e:
        sys.exit(redact(f"✗ Couldn't check that token: {type(e).__name__}: {e}", token))
    try:
        store_sign_in(url, token, **extra)
    except MoodleError as e:
        sys.exit(f"✗ {e}")
    print(f"✓ Signed in as {who['fullname']} ({who['username']}) on {who['site']}. "
          f"{who['functions_available']} web-service functions available.")


def cmd_logout(args) -> None:
    """Delete the saved config and the keychain password for the account in the file itself (never the
    MOODLE_URL / MOODLE_TOKEN view of it, which may point at another site)."""
    status = None
    try:
        with config_lock():
            saved = read_saved_config() or {}
            if saved.get("url") and saved.get("username"):
                status = forget_password(saved["url"], saved["username"])
            removed = clear_config()
    except MoodleError as e:
        sys.exit(f"✗ {e}")
    print(f"✓ Removed {client.CONFIG_PATH}" if removed else "No saved token to remove.")
    if status == "deleted":
        print("✓ Removed the remembered password.")
    elif status == "failed":
        print("! Couldn't remove the remembered password from the system keychain. Delete the "
              "'moodle-mcp' entry in your keychain / credential manager by hand.", file=sys.stderr)
    elif status == "nokeyring" and saved.get("remember"):
        print("! The keyring package isn't installed, so a password remembered earlier couldn't be checked. "
              "Look for a 'moodle-mcp' entry in your keychain / credential manager.", file=sys.stderr)
    if os.environ.get("MOODLE_MCP_PASSWORD_FILE", "").strip():
        print("Note: MOODLE_MCP_PASSWORD_FILE is a file you manage yourself; it was not deleted.")


def cmd_status(args) -> None:
    """Exit 0 if the saved token works (renewing it if it can), 1 otherwise. Good for a scheduled check."""
    c = None
    try:
        c = Moodle()
        who = c.whoami()
    except MoodleError as e:
        msg = redact(str(e), c.token if c else None)
        if args.json:
            code = safe_code(redact(e.code, c.token if c else None)) if e.code else None
            _print({"ok": False, "error": msg, "errorcode": code})
        else:
            print(f"✗ {msg}")
        sys.exit(1)
    if args.json:
        return _print({"ok": True, **who})
    age = who.get("token_age_days")
    print(f"✓ Signed in as {who['fullname']} on {who['site']}.")
    print(f"  Token saved {age} days ago." if age is not None else "  Token age unknown (saved before moodle-mcp tracked it).")
    if who.get("auto_renew"):
        print("  Auto-renew: on (remembered password).")
    elif who.get("auto_renew_problem"):
        print(f"  Auto-renew: off. {who['auto_renew_problem']}")
    else:
        print("  Auto-renew: off. When the token expires, run `moodle auth` again; running servers pick it up.")


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_due(args, c: Moodle) -> None:
    items = c.deadlines(args.days, args.back)
    if args.json:
        return _print(items)
    if not items:
        print(f"Nothing on the Moodle timeline for the next {args.days} days.")
    for e in items:
        flag = "OVERDUE " if e["overdue"] else ""
        print(f"{flag}{e['due']:<26} {e['course'] or '':<20} {e['name']}\n{'':27}{e['url']}")


def cmd_courses(args, c: Moodle) -> None:
    items = c.courses(args.which)
    if args.json:
        return _print(items)
    for x in items:
        print(f"{x['id']:>6}  {x['shortname']:<22} {x['fullname']}")


def cmd_assignments(args, c: Moodle) -> None:
    items = c.assignments([args.course] if args.course else None, include_status=True)
    if args.json:
        return _print(items)
    for a in items:
        status = a.get("submission", "?")
        grade = f"  grade {a['grade']}" if a.get("grade") else ""
        print(f"{(a['due'] or 'no due date'):<26} {a['course']:<20} [{status}]{grade}  {a['name']}  (assign {a['assign_id']})")


def main() -> None:
    # ✓/✗ can't be encoded on a Windows console code page when output is piped; degrade instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    p = argparse.ArgumentParser(prog="moodle", description="Talk to Moodle from the terminal.")
    p.add_argument("--json", action="store_true", help="raw JSON output")
    # --json also works after the subcommand (`moodle status --json`); SUPPRESS keeps a leading --json intact.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", parents=[common], help="save your Moodle token")
    a.add_argument("--url", help="your Moodle site, e.g. https://moodle.yourschool.edu (asked for if not saved yet)")
    a.add_argument("--password", action="store_true",
                   help="sign in with your Moodle username and password instead of a token (sites without SSO)")
    a.add_argument("--username", help="username for --password (asked for if left out)")
    a.add_argument("--remember", action="store_true",
                   help="with --password: keep the password in the system keychain so the token renews itself")
    sub.add_parser("logout", parents=[common], help="delete the saved token (and any remembered password) from this computer")
    sub.add_parser("status", parents=[common], help="check the saved sign-in still works; exits 1 if not")
    sub.add_parser("quickstart", help="guided setup: sign in and connect ChatGPT through OpenAI's tunnel")
    sub.add_parser("whoami", parents=[common], help="check the connection")
    s = sub.add_parser("functions", parents=[common], help="list the web-service functions your token can call")
    s.add_argument("match", nargs="?", help="only names containing this text")
    s = sub.add_parser("courses", parents=[common], help="list courses with ids")
    s.add_argument("which", nargs="?", default="inprogress", choices=["inprogress", "future", "past", "all"])
    s = sub.add_parser("due", parents=[common], help="upcoming (and overdue) deadlines")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--back", type=int, default=14, help="days of overdue items to include")
    s = sub.add_parser("assignments", parents=[common], help="assignments with submission status")
    s.add_argument("course", type=int, nargs="?")
    s = sub.add_parser("contents", parents=[common], help="course sections and activities")
    s.add_argument("course", type=int)
    s = sub.add_parser("read", parents=[common], help="open an activity by cmid")
    s.add_argument("cmid", type=int)
    s = sub.add_parser("grades", parents=[common], help="grades for a course (or all course totals)")
    s.add_argument("course", type=int, nargs="?")
    s = sub.add_parser("file", parents=[common], help="print the text of a Moodle file URL")
    s.add_argument("fileurl")
    s = sub.add_parser("download", parents=[common], help="save a Moodle file")
    s.add_argument("fileurl")
    s.add_argument("--to", default="~/Downloads/Moodle")
    s = sub.add_parser("news", parents=[common], help="latest announcements")
    s = sub.add_parser("notifications", parents=[common], help="bell notifications")
    s = sub.add_parser("search", parents=[common], help="search course contents")
    s.add_argument("query")
    s = sub.add_parser("call", parents=[common], help="raw web-service call: moodle call fn '{\"courseid\": 1}'")
    s.add_argument("wsfunction")
    s.add_argument("params", nargs="?", default="{}")

    args = p.parse_args()
    if args.cmd == "auth":
        return cmd_auth(args)
    if args.cmd == "logout":
        return cmd_logout(args)
    if args.cmd == "quickstart":
        from .quickstart import main as quickstart_main
        return quickstart_main()
    if args.cmd == "status":
        return cmd_status(args)
    c = None
    try:
        c = Moodle()
        if args.cmd == "due":
            cmd_due(args, c)
        elif args.cmd == "courses":
            cmd_courses(args, c)
        elif args.cmd == "assignments":
            cmd_assignments(args, c)
        elif args.cmd == "whoami":
            _print(c.whoami())
        elif args.cmd == "functions":
            print("\n".join(c.functions(args.match)))
        elif args.cmd == "contents":
            _print(c.course_contents(args.course))
        elif args.cmd == "read":
            _print(c.read_module(args.cmid))
        elif args.cmd == "grades":
            _print(c.grades(args.course) if args.course else c.course_overview_grades())
        elif args.cmd == "file":
            print(c.file_text(args.fileurl))
        elif args.cmd == "download":
            print(c.download(args.fileurl, args.to))
        elif args.cmd == "news":
            _print(c.announcements())
        elif args.cmd == "notifications":
            _print(c.notifications())
        elif args.cmd == "search":
            _print(c.search(args.query))
        elif args.cmd == "call":
            _print(c.call(args.wsfunction, **json.loads(args.params)))
    except MoodleError as e:
        sys.exit(redact(f"✗ {e}", c.token if c else None))
    except Exception as e:  # print a one-line, token-free error instead of a traceback
        sys.exit(redact(f"✗ {type(e).__name__}: {e}", c.token if c else None))


if __name__ == "__main__":
    main()
