"""Offline tests for the security hardening: none of these need a real Moodle or token."""

import logging

import httpx
import pytest

from moodle_mcp import client as mc

TOKEN = "t" * 32
SITE = "https://moodle.example.edu"
FILE = f"{SITE}/pluginfile.php/1/mod_resource/content/0/week3.txt"

# ---------------------------------------------------------------- file URLs


@pytest.mark.parametrize("url", [
    "https://evil.example.com/pluginfile.php/1/x.pdf",
    "http://moodle.example.edu/pluginfile.php/1/x.pdf",
    "https://user@moodle.example.edu/pluginfile.php/1/x.pdf",
    "https://moodle.example.edu.evil.com/pluginfile.php/1/x.pdf",
    "https://moodle.example.edu:8443/pluginfile.php/1/x.pdf",
    "https://moodle.example.edu/login/index.php",
    "https://moodle.example.edu/pluginfile.php",
    "//evil.example.com/pluginfile.php/1/x.pdf",
    "/pluginfile.php/1/x.pdf",
    "file:///etc/passwd",
])
def test_file_url_must_be_a_pluginfile_on_the_configured_site(fake_moodle, url):
    seen = []
    c = fake_moodle(lambda req: seen.append(req) or httpx.Response(200, content=b"x"))
    with pytest.raises(mc.MoodleError) as e:
        c.fetch_file(url)
    assert e.value.code == "badfileurl"
    assert TOKEN not in str(e.value)
    assert seen == []


def test_file_url_host_comparison_ignores_case(fake_moodle):
    c = fake_moodle(lambda req: httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"}))
    data, _, name = c.fetch_file("HTTPS://Moodle.Example.EDU/pluginfile.php/1/mod_resource/content/0/week3.txt")
    assert data == b"hello" and name == "week3.txt"


def test_file_url_keeps_query_and_replaces_any_existing_token(fake_moodle):
    c = fake_moodle(lambda req: httpx.Response(200))
    url = c._file_url(FILE + "?forcedownload=1&token=attacker")
    assert "forcedownload=1" in url
    assert "attacker" not in url
    assert url.count("token=") == 1


def test_client_does_not_follow_redirects_automatically(fake_moodle):
    c = fake_moodle(lambda req: httpx.Response(200))
    assert c.http.follow_redirects is False
    assert mc.Moodle(url=SITE, token=TOKEN).http.follow_redirects is False


def test_rest_call_redirect_is_an_error_not_a_replay(fake_moodle):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})

    c = fake_moodle(handler)
    with pytest.raises(mc.MoodleError) as e:
        c.call("core_webservice_get_site_info")
    assert e.value.code == "redirect"
    assert len(seen) == 1


def test_file_redirect_to_another_site_is_refused(fake_moodle):
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example.com/x.pdf"})

    c = fake_moodle(handler)
    with pytest.raises(mc.MoodleError) as e:
        c.fetch_file(FILE)
    assert e.value.code == "redirect"
    assert seen == ["moodle.example.edu"]


def test_file_redirect_within_the_site_is_followed(fake_moodle):
    def handler(req):
        if req.url.path.endswith("week3.txt"):
            return httpx.Response(303, headers={"location": "/webservice/pluginfile.php/1/other/week3-v2.txt"})
        return httpx.Response(200, content=b"moved", headers={"content-type": "text/plain"})

    c = fake_moodle(handler)
    assert c.fetch_file(FILE)[0] == b"moved"


def test_file_redirect_loop_stops(fake_moodle):
    n = []

    def handler(req):
        n.append(1)
        return httpx.Response(302, headers={"location": str(req.url)})

    c = fake_moodle(handler)
    with pytest.raises(mc.MoodleError):
        c.fetch_file(FILE)
    assert len(n) == mc.MAX_FILE_REDIRECTS + 1


def test_httpx_request_logging_is_silenced():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


# ---------------------------------------------------------------- error redaction


def test_redact_strips_the_token_and_token_params():
    text = f"GET https://x/pluginfile.php/1/a.pdf?forcedownload=1&token={TOKEN} wstoken=abc123&x=1 {TOKEN}"
    out = mc.redact(text, TOKEN)
    assert TOKEN not in out and "abc123" not in out
    assert "forcedownload=1" in out and "token=***" in out and "wstoken=***" in out


def test_network_errors_become_redacted_moodle_errors(fake_moodle):
    def handler(req):
        raise httpx.ConnectError(f"boom while fetching {req.url}", request=req)

    c = fake_moodle(handler)
    for go in (lambda: c.fetch_file(FILE), lambda: c.call("core_webservice_get_site_info")):
        with pytest.raises(mc.MoodleError) as e:
            go()
        assert e.value.code == "network"
        assert TOKEN not in str(e.value)


def test_http_status_errors_do_not_echo_the_url(fake_moodle):
    c = fake_moodle(lambda req: httpx.Response(500))
    for go in (lambda: c.fetch_file(FILE), lambda: c.call("core_webservice_get_site_info")):
        with pytest.raises(mc.MoodleError) as e:
            go()
        assert "500" in str(e.value) and TOKEN not in str(e.value)


def test_non_json_rest_answer_is_a_moodle_error(fake_moodle):
    c = fake_moodle(lambda req: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(mc.MoodleError) as e:
        c.call("core_webservice_get_site_info")
    assert e.value.code == "badresponse"


def test_call_params_may_use_any_key_name(fake_moodle):
    seen = {}

    def handler(req):
        from urllib.parse import parse_qs
        seen.update({k: v[0] for k, v in parse_qs(req.content.decode()).items()})
        return httpx.Response(200, json={})

    c = fake_moodle(handler)
    c.call("core_x_get_y", fn=1, self=2, params=3)
    assert seen["fn"] == "1" and seen["self"] == "2" and seen["wsfunction"] == "core_x_get_y"
