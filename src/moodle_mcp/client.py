"""Thin Moodle web-service client.

Talks to the REST endpoint (webservice/rest/server.php) with a user token,
the same kind the official Moodle app uses. Every helper returns plain
dicts/lists already trimmed down to the fields worth reading.
"""

from __future__ import annotations

import base64
import html
import io
import json
import logging
import os
import re
import time
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import httpx

DEFAULT_URL = "https://moodle.trincoll.edu"
CONFIG_PATH = Path(os.environ.get("MOODLE_MCP_CONFIG", "~/.config/moodle-mcp/config.json")).expanduser()
MAX_FILE_REDIRECTS = 5
DOWNLOAD_ROOT = "~/Downloads/Moodle"
DOWNLOAD_MAX_BYTES = 1024 * 1024 * 1024  # 1 GB
UPLOAD_MAX_BYTES = 100 * 1024 * 1024
FETCH_MAX_BYTES = 50 * 1024 * 1024  # files read into memory for their text
ZIP_MAX_PARTS = 500  # slides in a .pptx
ZIP_MAX_BYTES = 100 * 1024 * 1024  # total unpacked XML read from a .docx/.pptx/.xlsx
PDF_MAX_PAGES = 1000
TEXT_MAX_CHARS = 5_000_000

# httpx logs every request URL at INFO, and file downloads carry the token in the query string.
# FastMCP turns INFO logging on, so keep these loggers quiet.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class MoodleError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


_TOKEN_PARAM = re.compile(r"((?:ws)?token=)[^&\s'\"<>]+", re.IGNORECASE)


def redact(text: str, token: str | None = None) -> str:
    """Strip a token from text meant for a person or a model: the literal value and any token=/wstoken= param."""
    text = str(text)
    if token:
        text = text.replace(token, "***")
    return _TOKEN_PARAM.sub(lambda m: m.group(1) + "***", text)


# ---------------------------------------------------------------- config

def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
    env_url = os.environ.get("MOODLE_URL", "").strip()
    if env_url:
        # The saved token belongs to the saved site; never send it to a different one.
        if env_url.rstrip("/").lower() != str(cfg.get("url", "")).rstrip("/").lower():
            cfg.pop("token", None)
        cfg["url"] = env_url
    if os.environ.get("MOODLE_TOKEN"):
        cfg["token"] = os.environ["MOODLE_TOKEN"]
    cfg.setdefault("url", DEFAULT_URL)
    return cfg


# Characters and escapes that let a URL path mean something other than what it says once a server decodes it.
_BAD_URL_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")
_ENCODED_DOT_SLASH = re.compile(r"%(2e|2f|5c)", re.IGNORECASE)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def check_site_url(url: str) -> str:
    """Normalise a Moodle site URL and insist on https (plain http only for a Moodle on this machine)."""
    url = (url or "").strip().rstrip("/")
    u = urlparse(url)
    if not u.scheme or not u.hostname or "@" in u.netloc or u.query or u.fragment:
        raise MoodleError(f"{url!r} is not a Moodle site URL (expected something like https://moodle.school.edu).",
                          "badurl")
    scheme = u.scheme.lower()
    if scheme != "https" and not (scheme == "http" and u.hostname.lower() in _LOCAL_HOSTS):
        raise MoodleError(f"The Moodle site URL must start with https:// (got {url}); the token would travel "
                          "unencrypted otherwise.", "badurl")
    return url


def clear_config() -> bool:
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
        return True
    return False


def save_config(cfg: dict) -> None:
    """Write the config atomically. The file is created 0600 from the start (never briefly world-readable)
    in a 0700 folder; on Windows those modes don't set ACLs, so it relies on the profile folder's ACLs."""
    folder = CONFIG_PATH.parent
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    tmp = folder / f".{CONFIG_PATH.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(cfg, indent=2).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- staying signed in
#
# Moodle tokens can expire (sites often set a few months). Two things keep a long-running server working:
#  1. When Moodle says the token is invalid, the client re-reads the config file, so running `moodle auth`
#     in a terminal fixes a running server without a restart.
#  2. On sites with a Moodle password (not SSO), `moodle auth --password --remember` keeps the password in
#     the OS keychain (or a file named by MOODLE_MCP_PASSWORD_FILE) and the client fetches a fresh token itself.

KEYRING_SERVICE = "moodle-mcp"
TOKEN_ERRORS = {"invalidtoken", "accessexception"}


def _keyring():
    try:
        import keyring  # optional extra: moodle-mcp[keyring]
    except ImportError:
        return None
    return keyring


def _keyring_user(url: str, username: str) -> str:
    return f"{url.rstrip('/').lower()}|{username}"


def remember_password(url: str, username: str, password: str) -> str:
    """Store the password in the OS keychain. Returns where it went, or raises MoodleError.

    With MOODLE_MCP_PASSWORD_FILE set, the user keeps the password in that file themselves, so nothing is stored."""
    if os.environ.get("MOODLE_MCP_PASSWORD_FILE", "").strip():
        return "MOODLE_MCP_PASSWORD_FILE (not copied anywhere)"
    kr = _keyring()
    if kr is None:
        raise MoodleError("Remembering a password needs the `keyring` package. Reinstall with the extra: "
                          "uv tool install --force \"moodle-mcp[keyring] @ git+https://github.com/TheBeaconCrafter/"
                          "moodle-mcp\", or point MOODLE_MCP_PASSWORD_FILE at a file only you can read.", "nokeyring")
    try:
        kr.set_password(KEYRING_SERVICE, _keyring_user(url, username), password)
    except Exception as e:
        raise MoodleError(f"Couldn't save the password to the system keychain ({type(e).__name__}). On a headless "
                          "machine, use MOODLE_MCP_PASSWORD_FILE instead.", "nokeyring") from None
    return "the system keychain"


def recall_password(url: str, username: str) -> str | None:
    """The remembered password: MOODLE_MCP_PASSWORD_FILE first (handy for systemd credentials), then the keychain."""
    path = os.environ.get("MOODLE_MCP_PASSWORD_FILE", "").strip()
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYRING_SERVICE, _keyring_user(url, username))
    except Exception:
        return None


def forget_password(url: str, username: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, _keyring_user(url, username))
        except Exception:
            pass


def password_login(url: str, username: str, password: str, http: httpx.Client | None = None) -> str:
    """Trade a Moodle username/password for a mobile-app token via login/token.php (sites without SSO only)."""
    url = check_site_url(url)
    own = http is None
    http = http or httpx.Client(timeout=httpx.Timeout(connect=10, read=60, write=60, pool=10), follow_redirects=False,
                                headers={"User-Agent": "moodle-mcp/0.1 (MoodleMobile)"})
    try:
        r = http.post(f"{url}/login/token.php",
                      data={"username": username, "password": password, "service": "moodle_mobile_app"})
        out = r.json() if r.status_code < 400 and r.content else None
    except (httpx.HTTPError, ValueError) as e:
        raise MoodleError(redact(f"Password sign-in failed: {type(e).__name__}", password), "network") from None
    finally:
        if own:
            http.close()
    if not isinstance(out, dict):
        raise MoodleError(f"Password sign-in failed (HTTP {r.status_code}). Does this site use single sign-on?",
                          "loginfailed")
    if not out.get("token"):
        msg = redact(str(out.get("error") or "Moodle refused the sign-in"), password)
        raise MoodleError(f"Password sign-in failed: {msg}", out.get("errorcode") or "loginfailed")
    return str(out["token"])


def parse_token(raw: str) -> str:
    """Accept a bare token or the moodlemobile://token=... link the app launch page produces."""
    raw = raw.strip()
    m = re.search(r"token=([A-Za-z0-9+/=_-]+)", raw)
    if m:
        decoded = base64.b64decode(m.group(1) + "===").decode()
        parts = decoded.split(":::")
        if len(parts) < 2:
            raise ValueError("That link didn't contain a token.")
        return parts[1]
    if not re.fullmatch(r"[A-Za-z0-9]{20,64}", raw):
        raise ValueError("That doesn't look like a Moodle token (expected ~32 letters/digits).")
    return raw


# ---------------------------------------------------------------- formatting

class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "section"}

    SKIP = {"script", "style", "template"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.skip = 0
        # Content a browser wouldn't show (hidden attribute, display:none) is dropped, so text hidden from the
        # student on the page can't reach the model either. Tracks one hidden element and its nesting depth.
        self.hidden_tag: str | None = None
        self.hidden_depth = 0

    @staticmethod
    def _is_hidden(attrs) -> bool:
        a = {k.lower(): (v or "") for k, v in attrs}
        style = re.sub(r"\s+", "", a.get("style", "").lower())
        return "hidden" in a or "display:none" in style or "visibility:hidden" in style

    def handle_starttag(self, tag, attrs):
        if self.hidden_tag is not None:
            if tag == self.hidden_tag:
                self.hidden_depth += 1
            return
        if tag not in self.VOID and self._is_hidden(attrs):
            self.hidden_tag, self.hidden_depth = tag, 1
            return
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith("http"):
                self._href = href
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if self.hidden_tag is not None:
            if tag == self.hidden_tag:
                self.hidden_depth -= 1
                if self.hidden_depth == 0:
                    self.hidden_tag = None
            return
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)  # a stray </script> must not turn skipping back on for good
        elif tag == "a" and getattr(self, "_href", None):
            self.out.append(f" <{self._href}>")
            self._href = None
        elif tag in self.BLOCK and tag != "li":
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip and self.hidden_tag is None:
            self.out.append(data)


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    p = _TextExtractor()
    p.feed(html)
    p.close()
    text = "".join(p.out).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fmt_time(ts: int | None) -> str | None:
    if not ts:
        return None
    d = datetime.fromtimestamp(ts)
    # Built by hand because %-d / %-I don't exist on Windows.
    return f"{d:%a %b} {d.day} {d.year} {d.hour % 12 or 12}:{d:%M %p}"


FLATTEN_MAX_DEPTH = 10
_PARAM_NAME = re.compile(r"[A-Za-z0-9_]+")
# Names the client sets itself. PHP trims and decodes parameter names (" wsfunction", "ws.function" and
# "wsfunction" can all land on $_POST['wsfunction']), so only plain identifiers are accepted and these are refused.
_RESERVED_PARAMS = {"wstoken", "wsfunction", "moodlewsrestformat"}


def _flatten(prefix: str, value: Any, out: dict, depth: int = 0) -> None:
    """Moodle REST wants nested params as courseids[0]=1&options[0][name]=x.

    Every dict key at every level must be a plain identifier ([A-Za-z0-9_]+), and the top level may not use
    the names the client sets itself (wstoken, wsfunction, moodlewsrestformat, moodlewssetting*)."""
    if depth > FLATTEN_MAX_DEPTH:
        raise MoodleError(f"Parameters are nested more than {FLATTEN_MAX_DEPTH} levels deep.", "badparams")
    if isinstance(value, dict):
        for k, v in value.items():
            k = str(k)
            if "[" in k or "]" in k:
                raise MoodleError(f"Parameter names can't contain [ or ]: {k!r}. Nest dicts/lists instead.", "badparams")
            if not _PARAM_NAME.fullmatch(k):
                raise MoodleError(f"Parameter names may only use letters, digits and _: {k!r}.", "badparams")
            if not prefix and (k.lower() in _RESERVED_PARAMS or k.lower().startswith("moodlewssetting")):
                raise MoodleError(f"{k!r} is set by moodle-mcp itself and can't be passed as a parameter.", "badparams")
            _flatten(f"{prefix}[{k}]" if prefix else k, v, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out, depth + 1)
    elif isinstance(value, bool):
        out[prefix] = int(value)
    elif value is not None:
        out[prefix] = value


# ---------------------------------------------------------------- client

class Moodle:
    def __init__(self, url: str | None = None, token: str | None = None):
        cfg = load_config()
        self.url = check_site_url(url or cfg["url"])
        self.token = token or cfg.get("token")
        # Only a token that came from the config file may be swapped for a fresh one later.
        self._renewable = token is None and not os.environ.get("MOODLE_TOKEN") and url is None
        if not self.token:
            raise MoodleError(
                "No Moodle token configured. Run `moodle auth` in a terminal to add one.", "notoken"
            )
        # No automatic redirects: a redirected POST would replay wstoken to wherever Location points.
        self.http = httpx.Client(timeout=httpx.Timeout(connect=10, read=60, write=60, pool=10), follow_redirects=False,
                                 headers={"User-Agent": "moodle-mcp/0.1 (MoodleMobile)"})
        self._me: dict | None = None

    # -- raw ------------------------------------------------------------
    @contextmanager
    def _net(self, what: str = "Moodle"):
        """Turn httpx network errors into MoodleError with the token scrubbed (httpx messages include URLs)."""
        try:
            yield
        except httpx.HTTPError as e:
            raise MoodleError(redact(f"{what}: network error ({type(e).__name__}: {e})", self.token), "network") from None

    def renew_token(self) -> bool:
        """Swap an expired token for a working one without a restart. Returns True if the token changed:
        either the config file now holds a different token (the user ran `moodle auth`), or a remembered
        password got a new one from login/token.php (which is then saved)."""
        if not self._renewable:
            return False
        cfg = load_config()
        if str(cfg.get("url", "")).rstrip("/").lower() != self.url.lower():
            return False
        if cfg.get("token") and cfg["token"] != self.token:
            self.token, self._me = cfg["token"], None
            return True
        username = cfg.get("username")
        if not (cfg.get("remember") and username):
            return False
        password = recall_password(self.url, username)
        if not password:
            return False
        try:
            token = password_login(self.url, username, password, self.http)
        except MoodleError:
            return False
        if token == self.token:
            return False
        # Update the file itself (not the env-merged view) so env settings never get written to disk.
        saved = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
        saved.update(token=token, saved_at=int(time.time()))
        save_config(saved)
        self.token, self._me = token, None
        return True

    def call(self, wsfunction: str, /, **params) -> Any:
        try:
            return self._call(wsfunction, params)
        except MoodleError as e:
            if e.code in TOKEN_ERRORS and self.renew_token():
                return self._call(wsfunction, params)
            raise

    def _call(self, wsfunction: str, params: dict) -> Any:
        data: dict = {}
        _flatten("", params, data)
        data.update(wstoken=self.token, wsfunction=wsfunction, moodlewsrestformat="json")
        with self._net(wsfunction):
            r = self.http.post(f"{self.url}/webservice/rest/server.php", data=data)
        if r.is_redirect:
            raise MoodleError(f"{wsfunction}: Moodle answered with a redirect (HTTP {r.status_code}). "
                              "Check the site URL (https, www, sub-path).", "redirect")
        if r.status_code >= 400:
            raise MoodleError(f"{wsfunction}: Moodle returned HTTP {r.status_code}.", "httperror")
        try:
            out = r.json() if r.content else None
        except ValueError:
            raise MoodleError(f"{wsfunction}: Moodle did not answer with JSON. Check the site URL.", "badresponse") from None
        if isinstance(out, dict) and "exception" in out:
            code = out.get("errorcode")
            msg = out.get("message", "Moodle error")
            if code in ("invalidtoken", "accessexception") and "token" in msg.lower():
                msg += " — the token is missing or expired; run `moodle auth` again."
            raise MoodleError(f"{wsfunction}: {msg}", code)
        return out

    @property
    def me(self) -> dict:
        if self._me is None:
            self._me = self.call("core_webservice_get_site_info")
        return self._me

    def whoami(self) -> dict:
        m = self.me
        return {
            "site": m.get("sitename"), "url": m.get("siteurl"), "release": m.get("release"),
            "userid": m.get("userid"), "username": m.get("username"), "fullname": m.get("fullname"),
            "functions_available": len(m.get("functions", [])),
            **self.token_info(),
        }

    def token_info(self) -> dict:
        """How old the saved token is and whether it can renew itself (no secrets)."""
        cfg = load_config() if self._renewable else {}
        info: dict = {}
        if cfg.get("saved_at") and cfg.get("token") == self.token:
            info["token_age_days"] = int((time.time() - cfg["saved_at"]) // 86400)
        info["auto_renew"] = bool(self._renewable and cfg.get("remember") and cfg.get("username"))
        return info

    def has_function(self, name: str) -> bool:
        return any(f.get("name") == name for f in self.me.get("functions", []))

    def functions(self, match: str | None = None) -> list[str]:
        """Names of every web-service function this token may call, optionally filtered by a substring."""
        names = sorted(f["name"] for f in self.me.get("functions", []))
        return [n for n in names if match.lower() in n.lower()] if match else names

    # -- courses ----------------------------------------------------------
    def courses(self, which: str = "inprogress") -> list[dict]:
        """which: inprogress | future | past | all"""
        if which != "all" and self.has_function("core_course_get_enrolled_courses_by_timeline_classification"):
            res = self.call("core_course_get_enrolled_courses_by_timeline_classification",
                            classification=which, limit=0)
            raw = res.get("courses", [])
        else:
            raw = self.call("core_enrol_get_users_courses", userid=self.me["userid"])
        return [{
            "id": c["id"], "shortname": _u(c.get("shortname")), "fullname": _u(c.get("fullname")),
            "start": fmt_time(c.get("startdate")), "end": fmt_time(c.get("enddate")),
            "progress": c.get("progress"), "url": f"{self.url}/course/view.php?id={c['id']}",
        } for c in raw]

    def course_contents(self, course_id: int, include_descriptions: bool = True) -> list[dict]:
        sections = self.call("core_course_get_contents", courseid=course_id)
        out = []
        for s in sections:
            mods = []
            for m in s.get("modules", []):
                if m.get("uservisible") is False:
                    continue
                mod = {"cmid": m["id"], "type": m.get("modname"), "name": _u(m.get("name")),
                       "instance": m.get("instance"), "url": m.get("url")}
                dates = [f"{d['label']} {fmt_time(d['timestamp'])}" for d in m.get("dates", []) if d.get("timestamp")]
                if dates:
                    mod["dates"] = dates
                if include_descriptions and m.get("description"):
                    mod["description"] = html_to_text(m["description"])[:1500]
                files = [{"filename": f["filename"], "fileurl": f.get("fileurl"), "size": f.get("filesize"),
                          "modified": fmt_time(f.get("timemodified"))}
                         for f in m.get("contents", []) if f.get("type") == "file"]
                if files:
                    mod["files"] = files
                if m.get("modname") == "url":
                    ext = [f.get("fileurl") for f in m.get("contents", []) if f.get("type") == "url"]
                    if ext:
                        mod["external_url"] = ext[0]
                comp = m.get("completiondata")
                if comp:
                    mod["completed"] = bool(comp.get("state"))
                mods.append(mod)
            sec = {"section": s.get("section"), "name": _u(s.get("name")), "modules": mods}
            if include_descriptions and s.get("summary"):
                sec["summary"] = html_to_text(s["summary"])[:3000]
            if mods or sec.get("summary"):
                out.append(sec)
        return out

    def search(self, query: str, course_ids: list[int] | None = None) -> list[dict]:
        """Case-insensitive search over module names, descriptions, section text and file names."""
        q = query.lower()
        ids = course_ids or [c["id"] for c in self.courses()]
        hits = []
        for cid in ids:
            for sec in self.course_contents(cid):
                if q in (sec.get("summary") or "").lower() or q in (sec.get("name") or "").lower():
                    hits.append({"course": cid, "section": sec["name"], "match": "section text",
                                 "excerpt": _excerpt(sec.get("summary") or sec["name"], q)})
                for m in sec["modules"]:
                    blob = " ".join([m.get("name") or "", m.get("description") or "",
                                     *[f["filename"] for f in m.get("files", [])]]).lower()
                    if q in blob:
                        hits.append({"course": cid, "section": sec["name"], "cmid": m["cmid"],
                                     "type": m["type"], "name": m["name"], "url": m["url"],
                                     "excerpt": _excerpt(m.get("description") or m["name"], q)})
        return hits

    # -- deadlines --------------------------------------------------------
    def deadlines(self, days_ahead: int = 14, days_back: int = 0, course_id: int | None = None) -> list[dict]:
        now = int(time.time())
        params = dict(timesortfrom=now - days_back * 86400, timesortto=now + days_ahead * 86400,
                      limitnum=50, limittononsuspendedevents=True)
        if course_id:
            res = self.call("core_calendar_get_action_events_by_course", courseid=course_id,
                            timesortfrom=params["timesortfrom"], timesortto=params["timesortto"], limitnum=50)
        else:
            res = self.call("core_calendar_get_action_events_by_timesort", **params)
        out = []
        for e in res.get("events", []):
            out.append({
                "name": _u(e.get("activityname") or e.get("name")),
                "course": _u((e.get("course") or {}).get("shortname")),
                "course_id": (e.get("course") or {}).get("id"),
                "type": e.get("modulename"), "cmid": _cmid(e.get("url")),
                "due": fmt_time(e.get("timesort")), "due_ts": e.get("timesort"),
                "overdue": e.get("timesort", now) < now or bool(e.get("overdue")),
                "action": (e.get("action") or {}).get("name"),
                "url": e.get("url"),
            })
        return out

    # -- assignments ------------------------------------------------------
    def assignments(self, course_ids: list[int] | None = None, include_status: bool = True,
                    only_open: bool = False) -> list[dict]:
        ids = course_ids or [c["id"] for c in self.courses()]
        res = self.call("mod_assign_get_assignments", courseids=ids)
        now = time.time()
        out = []
        for c in res.get("courses", []):
            for a in c.get("assignments", []):
                if only_open and a.get("duedate") and a.get("cutoffdate") and a["cutoffdate"] < now:
                    continue
                item = {
                    "assign_id": a["id"], "cmid": a["cmid"], "course": _u(c.get("shortname")), "course_id": c["id"],
                    "name": _u(a["name"]), "due": fmt_time(a.get("duedate")), "due_ts": a.get("duedate") or None,
                    "cutoff": fmt_time(a.get("cutoffdate")),
                    "url": f"{self.url}/mod/assign/view.php?id={a['cmid']}",
                }
                if include_status:
                    try:
                        item.update(self._assign_status_brief(a["id"]))
                    except MoodleError as e:
                        item["status_error"] = str(e)
                out.append(item)
        out.sort(key=lambda x: x.get("due_ts") or 1 << 40)
        return out

    def _assign_status_brief(self, assign_id: int) -> dict:
        st = self.call("mod_assign_get_submission_status", assignid=assign_id)
        last = st.get("lastattempt") or {}
        sub = last.get("submission") or (last.get("teamsubmission") or {})
        fb = st.get("feedback") or {}
        brief = {"submission": sub.get("status", "nosubmission"),
                 "grading": last.get("gradingstatus")}
        if fb.get("gradefordisplay"):
            brief["grade"] = html_to_text(fb["gradefordisplay"])
        return brief

    def assignment(self, assign_id: int | None = None, cmid: int | None = None) -> dict:
        """Full detail for one assignment: instructions, attachments, dates, submission, feedback."""
        if assign_id is None:
            if cmid is None:
                raise MoodleError("Give assign_id or cmid.")
            cm = self.call("core_course_get_course_module", cmid=cmid)["cm"]
            assign_id, course_id = cm["instance"], cm["course"]
        else:
            course_id = None
        res = self.call("mod_assign_get_assignments", courseids=[course_id] if course_id else [])
        a = next((a for c in res.get("courses", []) for a in c.get("assignments", []) if a["id"] == assign_id), None)
        if not a:
            raise MoodleError(f"Assignment {assign_id} not found in your courses.")
        st = self.call("mod_assign_get_submission_status", assignid=assign_id)
        last = st.get("lastattempt") or {}
        sub = last.get("submission") or {}
        sub_files, sub_text = [], None
        for p in sub.get("plugins", []):
            for fa in p.get("fileareas", []) or []:
                sub_files += [{"filename": f["filename"], "fileurl": f.get("fileurl")} for f in fa.get("files", [])]
            for ef in p.get("editorfields", []) or []:
                if ef.get("text"):
                    sub_text = html_to_text(ef["text"])
        plugins = [c["plugin"] for c in a.get("configs", [])
                   if c.get("subtype") == "assignsubmission" and c.get("name") == "enabled" and c.get("value") == "1"
                   and c.get("plugin") != "comments"]
        fb = st.get("feedback") or {}
        feedback_text = []
        for p in fb.get("plugins", []) or []:
            for ef in p.get("editorfields", []) or []:
                if ef.get("text"):
                    feedback_text.append(html_to_text(ef["text"]))
        return {
            "assign_id": a["id"], "cmid": a["cmid"], "name": _u(a["name"]),
            "url": f"{self.url}/mod/assign/view.php?id={a['cmid']}",
            "opens": fmt_time(a.get("allowsubmissionsfromdate")), "due": fmt_time(a.get("duedate")),
            "cutoff": fmt_time(a.get("cutoffdate")), "grading_due": fmt_time(a.get("gradingduedate")),
            "submission_types": plugins,
            "requires_submit_button": bool(a.get("submissiondrafts")),
            "requires_statement": bool(a.get("requiresubmissionstatement")),
            "instructions": html_to_text(a.get("intro")),
            "attachments": [{"filename": f["filename"], "fileurl": f.get("fileurl")} for f in a.get("introattachments", [])],
            "submission_status": sub.get("status", "nosubmission"),
            "last_modified": fmt_time(sub.get("timemodified")),
            "grading_status": last.get("gradingstatus"),
            "can_edit": last.get("canedit"), "can_submit": last.get("cansubmit"),
            "submitted_files": sub_files, "submitted_text": sub_text,
            "grade": html_to_text(fb.get("gradefordisplay")) or None,
            "feedback": "\n\n".join(feedback_text) or None,
            "time_remaining": None if not a.get("duedate") else _remaining(a["duedate"]),
        }

    # -- grades -----------------------------------------------------------
    def grades(self, course_id: int) -> list[dict]:
        res = self.call("gradereport_user_get_grade_items", courseid=course_id, userid=self.me["userid"])
        items = []
        for ug in res.get("usergrades", []):
            for g in ug.get("gradeitems", []):
                items.append({
                    "item": g.get("itemname") or ("Course total" if g.get("itemtype") == "course" else g.get("itemtype")),
                    "type": g.get("itemmodule") or g.get("itemtype"), "cmid": g.get("cmid"),
                    "grade": g.get("gradeformatted"), "max": g.get("grademax"),
                    "percent": g.get("percentageformatted"), "weight": g.get("weightformatted"),
                    "feedback": html_to_text(g.get("feedback")) or None,
                })
        return items

    def course_overview_grades(self) -> list[dict]:
        res = self.call("gradereport_overview_get_course_grades", userid=self.me["userid"])
        names = {c["id"]: c["shortname"] for c in self.courses("all")}
        return [{"course_id": g["courseid"], "course": names.get(g["courseid"]), "grade": g.get("grade")}
                for g in res.get("grades", [])]

    # -- modules ----------------------------------------------------------
    def read_module(self, cmid: int) -> dict:
        """Open any course activity/resource by its cmid and return what's readable in it."""
        cm = self.call("core_course_get_course_module", cmid=cmid)["cm"]
        kind, inst, course = cm["modname"], cm["instance"], cm["course"]
        base = {"cmid": cmid, "type": kind, "name": _u(cm.get("name")), "course_id": course,
                "url": f"{self.url}/mod/{kind}/view.php?id={cmid}"}

        def by_course(fn: str, key: str) -> dict:
            res = self.call(fn, courseids=[course])
            return next((x for x in res.get(key, []) if x.get("id") == inst or x.get("coursemodule") == cmid), {})

        if kind == "assign":
            return {**base, **self.assignment(assign_id=inst)}
        if kind == "page":
            p = by_course("mod_page_get_pages_by_courses", "pages")
            return {**base, "intro": html_to_text(p.get("intro")), "content": html_to_text(p.get("content")),
                    "files": _files(p.get("contentfiles"))}
        if kind == "resource":
            r = by_course("mod_resource_get_resources_by_courses", "resources")
            return {**base, "intro": html_to_text(r.get("intro")), "files": _files(r.get("contentfiles"))}
        if kind == "url":
            u = by_course("mod_url_get_urls_by_courses", "urls")
            return {**base, "external_url": u.get("externalurl"), "intro": html_to_text(u.get("intro"))}
        if kind == "folder":
            f = by_course("mod_folder_get_folders_by_courses", "folders")
            contents = self.call("core_course_get_contents", courseid=course,
                                 options=[{"name": "cmid", "value": cmid}])
            files = [{"filename": c["filename"], "path": c.get("filepath"), "fileurl": c.get("fileurl")}
                     for s in contents for m in s.get("modules", []) if m["id"] == cmid
                     for c in m.get("contents", []) if c.get("type") == "file"]
            return {**base, "intro": html_to_text(f.get("intro")), "files": files}
        if kind == "label":
            lb = by_course("mod_label_get_labels_by_courses", "labels")
            return {**base, "content": html_to_text(lb.get("intro"))}
        if kind == "forum":
            return {**base, "discussions": self.forum_discussions(inst)}
        if kind == "quiz":
            q = by_course("mod_quiz_get_quizzes_by_courses", "quizzes")
            att = self.call("mod_quiz_get_user_attempts", quizid=inst, status="all").get("attempts", [])
            best = None
            try:
                best = self.call("mod_quiz_get_user_best_grade", quizid=inst).get("grade")
            except MoodleError:
                pass
            return {**base, "intro": html_to_text(q.get("intro")), "opens": fmt_time(q.get("timeopen")),
                    "closes": fmt_time(q.get("timeclose")), "time_limit_min": (q.get("timelimit") or 0) // 60 or None,
                    "attempts_allowed": q.get("attempts") or "unlimited", "grade_max": q.get("grade"),
                    "your_attempts": [{"state": a["state"], "finished": fmt_time(a.get("timefinish")),
                                       "grade": a.get("sumgrades")} for a in att],
                    "best_grade": best}
        # Fallback: whatever core_course_get_contents knows about it.
        return {**base, "note": f"No dedicated reader for '{kind}'; showing the course listing entry.",
                "entry": next((m for s in self.course_contents(course) for m in s["modules"] if m["cmid"] == cmid), None)}

    # -- forums -----------------------------------------------------------
    def forum_discussions(self, forum_id: int, limit: int = 15) -> list[dict]:
        res = self.call("mod_forum_get_forum_discussions", forumid=forum_id, sortorder=-1, page=0, perpage=limit)
        return [{"discussion_id": d["discussion"], "subject": _u(d.get("subject") or d.get("name")),
                 "author": d.get("userfullname"), "posted": fmt_time(d.get("created")), "posted_ts": d.get("created"),
                 "replies": d.get("numreplies"), "message": html_to_text(d.get("message"))[:2500],
                 "attachments": _files(d.get("attachments"))}
                for d in res.get("discussions", [])]

    def discussion(self, discussion_id: int) -> list[dict]:
        res = self.call("mod_forum_get_discussion_posts", discussionid=discussion_id, sortby="created",
                        sortdirection="ASC")
        return [{"author": (p.get("author") or {}).get("fullname"), "posted": fmt_time(p.get("timecreated")),
                 "subject": _u(p.get("subject")), "message": html_to_text(p.get("message")),
                 "attachments": _files(p.get("attachments"))} for p in res.get("posts", [])]

    def announcements(self, course_ids: list[int] | None = None, per_course: int = 5) -> list[dict]:
        ids = course_ids or [c["id"] for c in self.courses()]
        forums = self.call("mod_forum_get_forums_by_courses", courseids=ids)
        out = []
        for f in forums:
            if f.get("type") != "news":
                continue
            for d in self.forum_discussions(f["id"], per_course):
                out.append({"course_id": f["course"], **d})
        out.sort(key=lambda d: d.get("posted_ts") or 0, reverse=True)
        return out

    def notifications(self, limit: int = 20, unread_only: bool = False) -> list[dict]:
        res = self.call("message_popup_get_popup_notifications", useridto=self.me["userid"],
                        newestfirst=1, limit=limit, offset=0)
        out = [{"subject": _u(n.get("subject")), "read": n.get("read"), "when": fmt_time(n.get("timecreated")),
                "text": html_to_text(n.get("smallmessage") or n.get("fullmessage"))[:600],
                "url": n.get("contexturl")} for n in res.get("notifications", [])]
        return [n for n in out if not n["read"]] if unread_only else out

    # -- files ------------------------------------------------------------
    def _same_origin(self, url: str) -> bool:
        """True when `url` has exactly the configured site's scheme and host[:port], with no user@ part."""
        site, u = urlparse(self.url), urlparse(url)
        return (bool(u.scheme) and bool(u.netloc) and "@" not in u.netloc
                and u.scheme.lower() == site.scheme.lower() and u.netloc.lower() == site.netloc.lower())

    def _check_file_path(self, url: str, what: str = "badfileurl") -> None:
        """Insist that `url`'s path is a plain pluginfile.php path directly under the configured site's path.

        Checked on the raw text, before httpx normalises anything: no backslashes, control characters,
        `.`/`..` segments or percent-encoded dots/slashes (which servers may decode into those), and the
        path must start with <site path>/pluginfile.php/ or <site path>/webservice/pluginfile.php/."""
        if _BAD_URL_CHARS.search(url):
            raise MoodleError("Refusing a file URL with backslashes or control characters.", what)
        path = urlsplit(url).path
        if _ENCODED_DOT_SLASH.search(path):
            raise MoodleError("Refusing a file URL with encoded dots or slashes in its path.", what)
        if any(seg in (".", "..") for seg in path.split("/")):
            raise MoodleError("Refusing a file URL with . or .. in its path.", what)
        site_path = urlsplit(self.url).path.rstrip("/")
        if not (path.startswith(f"{site_path}/pluginfile.php/") or path.startswith(f"{site_path}/webservice/pluginfile.php/")):
            raise MoodleError("That is not a Moodle file URL on your site (expected …/pluginfile.php/…).", what)

    def _file_url(self, fileurl: str) -> str:
        """The token-bearing download URL for a Moodle `fileurl`, after checking it belongs to this site.

        Only pluginfile.php URLs under the configured site get the token; anything else could hand it to a
        third party or another app on the same host (a file URL can come from a teacher-written page or a
        forum post)."""
        fileurl = fileurl.strip()
        if not self._same_origin(fileurl):
            raise MoodleError("Refusing to fetch a file from outside your Moodle site. "
                              "Pass a fileurl returned by another moodle_* tool.", "badfileurl")
        self._check_file_path(fileurl)
        u = urlsplit(fileurl)
        site_path = urlsplit(self.url).path.rstrip("/")
        path = u.path
        if path.startswith(f"{site_path}/pluginfile.php/"):
            path = f"{site_path}/webservice{path[len(site_path):]}"
        query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k not in ("token", "wstoken")]
        query.append(("token", self.token))
        return urlunsplit((u.scheme, u.netloc, path, urlencode(query), ""))

    def _open_file(self, fileurl: str) -> httpx.Response:
        """GET a Moodle file as a streamed response (wrap it in `closing()`).

        Redirects are followed by hand, at most MAX_FILE_REDIRECTS times, and only to pluginfile.php URLs on
        the same site that pass the same path checks as the original URL."""
        url = self._file_url(fileurl)
        for _ in range(MAX_FILE_REDIRECTS + 1):
            with self._net("file download"):
                r = self.http.send(self.http.build_request("GET", url), stream=True)
            if r.is_redirect:
                location = r.headers.get("location", "")
                r.close()
                loc_path = urlsplit(location).path
                if (_BAD_URL_CHARS.search(location) or _ENCODED_DOT_SLASH.search(loc_path)
                        or any(seg in (".", "..") for seg in loc_path.split("/"))):
                    raise MoodleError("The file download redirected to a suspicious URL; not following it.", "redirect")
                url = urljoin(url, location)
                if not self._same_origin(url):
                    raise MoodleError("The file download redirected to another site; not following it.", "redirect")
                self._check_file_path(url, "redirect")
                continue
            if r.status_code >= 400:
                r.close()
                raise MoodleError(f"File download failed (HTTP {r.status_code}).", "httperror")
            return r
        raise MoodleError("The file download redirected too many times.", "redirect")

    def fetch_file(self, fileurl: str) -> tuple[bytes, str, str]:
        with closing(self._open_file(fileurl)) as r, self._net("file download"):
            data = _read_capped(r, FETCH_MAX_BYTES)
            ctype = r.headers.get("content-type", "")
        if "application/json" in ctype:
            _raise_if_moodle_error(data)
        name = unquote(Path(urlparse(fileurl).path).name)
        return data, ctype, name

    def download(self, fileurl: str, dest: str = DOWNLOAD_ROOT, root: str | None = None) -> str:
        """Stream a Moodle file into `dest` and return the new file's path. Never overwrites: a clash gets
        ` (1)`, ` (2)`… appended. With `root`, `dest` (absolute, or relative to root) must resolve inside root."""
        d = _dest_dir(dest, root)
        name = safe_filename(urlparse(fileurl).path)
        with closing(self._open_file(fileurl)) as r, self._net("file download"):
            ctype = r.headers.get("content-type", "")
            if "application/json" in ctype:
                data = _read_capped(r, DOWNLOAD_MAX_BYTES)
                _raise_if_moodle_error(data)
                chunks = iter([data])
            else:
                chunks = r.iter_bytes()
            d.mkdir(parents=True, exist_ok=True)
            path, fh = _create_unique(d, name)
            try:
                with fh:
                    total = 0
                    for chunk in chunks:
                        total += len(chunk)
                        if total > DOWNLOAD_MAX_BYTES:
                            raise MoodleError(f"File is larger than {DOWNLOAD_MAX_BYTES // 2**20} MB; not saving it.",
                                              "toolarge")
                        fh.write(chunk)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
        return str(path)

    def file_text(self, fileurl: str) -> str:
        data, ctype, name = self.fetch_file(fileurl)
        return extract_text(data, ctype, name)

    # -- writes -----------------------------------------------------------
    def upload_draft(self, path: str, itemid: int = 0) -> int:
        p = check_upload_path(path)
        with p.open("rb") as fh, self._net("file upload"):
            r = self.http.post(f"{self.url}/webservice/upload.php",
                               data={"token": self.token, "filearea": "draft", "itemid": itemid},
                               files={"file_1": (p.name, fh)})
        if r.status_code >= 300:
            raise MoodleError(f"File upload failed (HTTP {r.status_code}).", "httperror")
        try:
            res = r.json()
        except ValueError:
            raise MoodleError("File upload: Moodle did not answer with JSON.", "badresponse") from None
        if isinstance(res, dict) and ("error" in res or "exception" in res):
            raise MoodleError(res.get("error") or res.get("message") or "File upload failed", res.get("errorcode"))
        return res[0]["itemid"]

    def save_submission(self, assign_id: int, text_html: str | None = None,
                        file_paths: list[str] | None = None, submit_for_grading: bool = False) -> dict:
        plugindata: dict = {}
        if text_html is not None:
            plugindata["onlinetext_editor"] = {"text": text_html, "format": 1, "itemid": 0}
        if file_paths:
            for fp in file_paths:  # check every file before uploading any of them
                check_upload_path(fp)
            itemid = 0
            for fp in file_paths:
                itemid = self.upload_draft(fp, itemid)
            plugindata["files_filemanager"] = itemid
        if not plugindata:
            raise MoodleError("Nothing to submit: give text and/or files.")
        warnings = self.call("mod_assign_save_submission", assignmentid=assign_id, plugindata=plugindata)
        result = {"saved": True, "warnings": warnings or []}
        if submit_for_grading:
            w2 = self.call("mod_assign_submit_for_grading", assignmentid=assign_id, acceptsubmissionstatement=1)
            result["submitted_for_grading"] = True
            result["warnings"] += w2 or []
        result["status_now"] = self._assign_status_brief(assign_id)
        return result


# ---------------------------------------------------------------- helpers

_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f<>:"|?*]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(raw: str) -> str:
    """A plain file name from a URL path: decoded first, then reduced to its last part, so `..%2f..%2fx` can't escape."""
    name = unquote(raw).replace("\\", "/").rsplit("/", 1)[-1].strip()
    if re.match(r"^[A-Za-z]:", name):
        raise MoodleError("Refusing a file name with a drive prefix.", "badfilename")
    name = _UNSAFE_NAME_CHARS.sub("_", name).rstrip(". ")
    if name in ("", ".", ".."):
        raise MoodleError("The file URL has no usable file name.", "badfilename")
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name


def _dest_dir(dest: str, root: str | None) -> Path:
    d = Path(dest).expanduser()
    if root is None:
        return d
    base = Path(root).expanduser()
    if not d.is_absolute():
        d = base / d
    base_r, d_r = base.resolve(), d.resolve()
    if d_r != base_r and base_r not in d_r.parents:
        raise MoodleError(f"dest_dir must be inside {root} (give a subfolder name such as 'CHEM 111').", "baddest")
    return d_r


_READ_VERBS = {"get", "search", "list", "count", "check", "can", "is", "has"}
_PREDICATE_VERBS = {"check", "can", "is", "has"}  # yes/no questions such as mod_forum_can_add_discussion
# Words that mean a write anywhere in the name, even after a read verb (get_..._and_save would be odd, but be safe).
# "read" is deliberately not here: Moodle's mark-as-read functions use "mark", and "read" shows up in getters.
_WRITE_WORDS = {"add", "create", "update", "delete", "remove", "save", "submit", "set", "send", "mark", "edit",
                "insert", "upload", "import", "reset", "lock", "unlock", "grant", "revoke", "toggle", "process",
                "start", "finish", "accept", "decline", "confirm", "copy", "move", "duplicate", "restore", "approve",
                "reject", "change", "enable", "disable", "block", "unblock", "mute", "unmute", "star", "unstar",
                "trigger", "log", "join", "launch", "record", "notify", "subscribe", "unsubscribe", "complete",
                "purge", "clear", "viewed"}
# Functions that hand out credentials (e.g. tool_mobile_get_autologin_key) are never "read-only" here.
_CREDENTIAL_WORDS = {"token", "tokens", "autologin", "key", "keys", "password", "session"}
# Standard Moodle plugin types (plus core). local_* and anything unknown is refused in read-only mode: a site's
# own plugins can name a write however they like.
_KNOWN_COMPONENTS = {
    "core", "mod", "tool", "block", "gradereport", "report", "message", "enrol", "auth", "qbank", "qtype",
    "assignsubmission", "assignfeedback", "booktool", "quizaccess", "theme", "format", "filter", "editor",
    "availability", "customfield", "aiplacement", "ai", "h5plib", "contenttype", "repository", "gradingform",
    "ltiservice",
}
# Getters known (or, where noted, believed) to change state despite their names.
_STATEFUL_GETTERS = {
    "mod_quiz_get_attempt_data",  # moves the attempt's current page and logs an "attempt viewed" event
    "mod_quiz_process_attempt",  # saves answers / finishes the attempt (also caught by "process")
    "core_message_get_messages",  # markasread=1 marks the returned messages as read
    "mod_lesson_get_page_data",  # believed to record lesson progress/timer for the page; not fully certain
    "mod_scorm_insert_scorm_tracks",  # writes SCORM tracking data (also caught by "insert")
}


def is_read_only_function(wsfunction: str) -> bool:
    """Best-effort guess, from the name alone, that a web-service function only reads.

    This is a name check, not a guarantee: read-only mode can only promise that moodle_call refuses names
    that look like writes. Moodle names functions <component>_<verb>_<thing>, where the component is one or
    two words (core_get_string, core_course_get_contents, mod_assign_get_assignments). The rules:
    the component must be a standard Moodle plugin type (no local_* or unknown types); the verb must be a
    read verb (get/search/list/count, or a check/can/is/has question); no word may name a credential or a
    write (for can_<action> questions the action word itself is allowed); "view" anywhere but at the end
    counts as a write (*_view_* functions log a view and can tick activity completion); and a short list of
    getters known to change state is refused."""
    if not re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)+", wsfunction or ""):
        return False
    if wsfunction in _STATEFUL_GETTERS:
        return False
    words = wsfunction.split("_")
    if words[0] not in _KNOWN_COMPONENTS:
        return False
    if words[1] in _READ_VERBS:
        vi = 1
    elif len(words) > 2 and words[2] in _READ_VERBS:
        vi = 2
    else:
        return False
    if set(words) & _CREDENTIAL_WORDS:
        return False
    if "view" in words[:-1]:
        return False
    checked = list(words)
    if words[vi] == "can" and vi + 1 < len(words):
        del checked[vi + 1]  # mod_forum_can_add_discussion asks whether adding is allowed; it doesn't add
    return not (set(checked) & _WRITE_WORDS)


def upload_roots() -> list[Path]:
    """Folders submissions may upload from: MOODLE_MCP_UPLOAD_DIRS (os.pathsep-separated), else the home folder."""
    raw = os.environ.get("MOODLE_MCP_UPLOAD_DIRS", "")
    dirs = [d.strip() for d in raw.split(os.pathsep) if d.strip()] or [str(Path.home())]
    return [Path(d).expanduser().resolve() for d in dirs]


def check_upload_path(path: str) -> Path:
    """Resolve a file the user wants to submit and refuse anything that looks like it shouldn't leave the machine.

    The file must be a regular file of at most UPLOAD_MAX_BYTES inside an upload root, with no hidden
    (dot-named) folder or file below that root, and must not be this tool's own config file."""
    try:
        p = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        raise MoodleError(f"File not found: {path}", "badupload") from None
    if not p.is_file():
        raise MoodleError(f"Not a regular file: {path}", "badupload")
    if p == CONFIG_PATH.expanduser().resolve():
        raise MoodleError("Refusing to upload the moodle-mcp config file (it holds your token).", "badupload")
    root = next((r for r in upload_roots() if p == r or r in p.parents), None)
    if root is None:
        raise MoodleError(f"Refusing to upload {path}: it is outside the allowed folders "
                          f"({', '.join(str(r) for r in upload_roots())}). Set MOODLE_MCP_UPLOAD_DIRS to change them.",
                          "badupload")
    if any(part.startswith(".") for part in p.relative_to(root).parts):
        raise MoodleError(f"Refusing to upload {path}: hidden files and folders (names starting with '.') "
                          "are off limits.", "badupload")
    size = p.stat().st_size
    if size > UPLOAD_MAX_BYTES:
        raise MoodleError(f"{p.name} is {size // 2**20} MB; the upload limit is {UPLOAD_MAX_BYTES // 2**20} MB.",
                          "badupload")
    return p


def _create_unique(d: Path, name: str):
    """Create a new file in `d` without ever replacing one; returns (path, binary file handle)."""
    stem, suffix = Path(name).stem, Path(name).suffix
    d_r = d.resolve()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for n in range(1000):
        path = d / (name if n == 0 else f"{stem} ({n}){suffix}")
        if path.resolve().parent != d_r:
            raise MoodleError("Refusing to write outside the download folder.", "baddest")
        try:
            fd = os.open(path, flags, 0o644)
        except FileExistsError:
            continue
        return path, os.fdopen(fd, "wb")
    raise MoodleError(f"Too many files named {name} in {d}.", "baddest")


def _read_capped(r: httpx.Response, limit: int) -> bytes:
    buf = bytearray()
    for chunk in r.iter_bytes():
        buf += chunk
        if len(buf) > limit:
            raise MoodleError(f"File is larger than {limit // 2**20} MB.", "toolarge")
    return bytes(buf)


def _raise_if_moodle_error(data: bytes) -> None:
    """pluginfile.php reports errors (bad token, no access) as a JSON body instead of the file."""
    if data[:1] != b"{":
        return
    try:
        err = json.loads(data)
    except ValueError:
        return
    if isinstance(err, dict) and ("error" in err or "exception" in err or "errorcode" in err):
        raise MoodleError(err.get("error") or err.get("message") or "File download failed", err.get("errorcode"))

def _u(s):
    return html.unescape(s) if isinstance(s, str) else s


def _files(lst) -> list[dict]:
    return [{"filename": f.get("filename"), "fileurl": f.get("fileurl"), "size": f.get("filesize")}
            for f in (lst or [])]


def _cmid(url: str | None) -> int | None:
    m = re.search(r"[?&]id=(\d+)", url or "")
    return int(m.group(1)) if m else None


def _excerpt(text: str, q: str, width: int = 160) -> str:
    i = text.lower().find(q)
    if i < 0:
        return text[:width]
    s = max(0, i - width // 2)
    return ("…" if s else "") + text[s:s + width].replace("\n", " ") + "…"


def _remaining(due: int) -> str:
    delta = due - time.time()
    sign = "" if delta >= 0 else "overdue by "
    delta = abs(delta)
    d, h = int(delta // 86400), int(delta % 86400 // 3600)
    return f"{sign}{d}d {h}h" if d else f"{sign}{h}h {int(delta % 3600 // 60)}m"


def extract_text(data: bytes, ctype: str, name: str) -> str:
    low = name.lower()
    if low.endswith(".pdf") or "pdf" in ctype:
        return _guarded_extract(_pdf_text, data, name)
    if low.endswith((".docx", ".pptx", ".xlsx")):
        return _guarded_extract(_office_text, data, name)
    if "html" in ctype or low.endswith((".html", ".htm")):
        return html_to_text(data.decode("utf8", "ignore"))
    if ctype.startswith("text/") or low.endswith((".txt", ".md", ".c", ".h", ".py", ".java", ".csv", ".json", ".s")):
        return data.decode("utf8", "ignore")
    raise MoodleError(f"Can't extract text from {name} ({ctype}); use download instead.")


def _guarded_extract(fn, data: bytes, name: str) -> str:
    """Run a parser over an untrusted file; any parser failure becomes a plain MoodleError."""
    try:
        return fn(data, name)
    except MoodleError:
        raise
    except Exception as e:
        raise MoodleError(f"Couldn't read {name} ({type(e).__name__}); try download instead.", "badfile") from None


def _pdf_text(data: bytes, name: str) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    chunks, total = [], 0
    for i, pg in enumerate(reader.pages):
        if i >= PDF_MAX_PAGES:
            chunks.append(f"[stopped after {PDF_MAX_PAGES} pages]")
            break
        chunk = f"--- page {i + 1} ---\n{(pg.extract_text() or '').strip()}"
        total += len(chunk)
        chunks.append(chunk)
        if total > TEXT_MAX_CHARS:
            chunks.append(f"[stopped after {TEXT_MAX_CHARS:,} characters]")
            break
    return "\n\n".join(chunks)


def _office_text(data: bytes, name: str) -> str:
    low = name.lower()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if low.endswith(".docx"):
            parts = ["word/document.xml"]
        elif low.endswith(".pptx"):
            parts = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                           key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
            if len(parts) > ZIP_MAX_PARTS:
                raise MoodleError(f"{name} has more than {ZIP_MAX_PARTS} slides; download it instead.", "toolarge")
        else:
            parts = ["xl/sharedStrings.xml"]
        chunks, budget = [], ZIP_MAX_BYTES
        for i, part in enumerate(parts):
            # Read at most budget+1 bytes: the sizes in the zip header can lie (zip bombs).
            with z.open(part) as fh:
                raw = fh.read(budget + 1)
            budget -= len(raw)
            if budget < 0:
                raise MoodleError(f"{name} unpacks to more than {ZIP_MAX_BYTES // 2**20} MB; download it instead.",
                                  "toolarge")
            xml = re.sub(r"</w:p>|</a:p>|</si>", "\n", raw.decode("utf8", "ignore"))
            txt = re.sub(r"<[^>]+>", "", xml)
            chunks.append((f"--- slide {i + 1} ---\n" if low.endswith(".pptx") else "") + txt.strip())
        return "\n\n".join(chunks)
