"""The no-login safety of the web server: requests from other sites and for other host names are refused, nothing is
rendered from game files as markup, the optional password, the response headers, the exposure warning, and the
limits on the patch folder and the notification URL. Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from support import ENGINE_DIR, GAME_PARAM, WEB_DIR, E, ServerCase, legacy_config, make_env

HOSTILE_IDS = ['PPSA01234"><img src=x onerror=alert(1)>', "PPSA01234');alert(1);('", "<script>alert(1)</script>", "PPSA1234", "PPSA012345",
               "ppsa01234", "CUSA01234 ", "../../etc/passwd", "PPSA01234\n", "", None, 12345, ["PPSA01234"]]
EVIL = {"Origin": "http://evil.example"}


def as_client(srv, ip, **kw):
    """A test client whose requests come from the address `ip` (as seen by the app)."""
    from fastapi.testclient import TestClient

    async def shim(scope, receive, send):
        if scope["type"] == "http":
            scope["client"] = (ip, 50000)
        await srv.app(scope, receive, send)
    return TestClient(shim, headers={"Content-Type": "application/json"}, **kw)


class SecurityCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config())
        self.scan = self.patch(self.srv, "run_scan", return_value=True)
        self.start = self.patch(self.srv, "start_engine", return_value=True)
        self.patch(self.srv, "LOGIN_DELAY", 0)
        self.patch(self.srv, "_throttle", self.srv.WS.LoginThrottle())

    def patch(self, obj, name, *new, **kw):
        p = mock.patch.object(obj, name, *new, **kw)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def env(self, **kw):
        p = mock.patch.dict(os.environ, kw)
        p.start()
        self.addCleanup(p.stop)

    def fresh_client(self):
        from fastapi.testclient import TestClient
        return TestClient(self.srv.app)                       # sends no Content-Type of its own


class CrossSite(SecurityCase):
    def test_a_page_of_another_site_cannot_start_stop_or_scan(self):
        (self.state / "run.lock").write_text(str(os.getpid()))                      # a run is "active": /api/stop would act
        for path in ("/api/run", "/api/stop", "/api/scan"):
            for origin in ("http://evil.example", "https://evil.example:8765", "null", "http://testserver:9999", "http://192.168.1.77:8765"):
                with self.subTest(path, origin=origin):
                    r = self.client.post(path, headers={"Origin": origin})
                    self.assertEqual(r.status_code, 403, r.text)
                    self.assertIn("did not come from this page", r.json()["detail"])
        self.scan.assert_not_called()
        self.start.assert_not_called()
        self.assertFalse((self.state / "stop.request").exists())

    def test_the_no_cors_form_post_of_the_issue_is_refused_before_anything_starts(self):
        for path in ("/api/run", "/api/stop", "/api/scan"):
            r = self.fresh_client().post(path, content="x=1", headers={"Origin": "http://evil.example", "Content-Type": "text/plain"})
            self.assertEqual(r.status_code, 403, path)
            r = self.fresh_client().post(path, content="x=1", headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(r.status_code, 415, path)                                   # no Origin at all: the content type stops it
            r = self.fresh_client().post(path)
            self.assertEqual(r.status_code, 415, path)
        self.scan.assert_not_called()
        self.start.assert_not_called()

    def test_every_method_that_changes_something_is_covered(self):
        for method, path in [("PUT", "/api/config"), ("PUT", "/api/overrides/PPSA01234"), ("DELETE", "/api/overrides/PPSA05678"), ("POST", "/api/import"),
                             ("POST", "/api/backups/config-20200101-000000.json/restore"), ("POST", "/api/failures/PPSA01234/retry"),
                             ("POST", "/api/failures/PPSA01234/dismiss"), ("POST", "/api/failures/PPSA01234/delete-staged"),
                             ("POST", "/api/failures/PPSA01234/action"), ("POST", "/api/logout"), ("POST", "/api/nothing-here")]:
            with self.subTest(method, path=path):
                self.assertEqual(self.client.request(method, path, headers=EVIL, content="{}").status_code, 403)
                self.assertEqual(self.fresh_client().request(method, path, content="{}", headers={"Content-Type": "text/plain"}).status_code, 415)
        self.assertEqual(json.loads(self.cfg_path.read_text()), legacy_config())

    def test_a_referer_is_checked_when_there_is_no_origin(self):
        self.assertEqual(self.client.post("/api/scan", headers={"Referer": "http://evil.example/page"}).status_code, 403)
        self.assertEqual(self.client.post("/api/scan", headers={"Referer": "http://testserver/"}).status_code, 200)
        self.scan.assert_called_once()

    def test_this_page_and_scripts_still_work(self):
        self.assertEqual(self.client.post("/api/scan", headers={"Origin": "http://testserver"}).status_code, 200)              # the page
        self.assertEqual(self.client.post("/api/scan").status_code, 200)                                                       # curl with JSON
        self.assertEqual(self.client.post("/api/scan", headers={"Content-Type": "application/json; charset=utf-8"}).status_code, 200)
        self.assertEqual(self.scan.call_count, 3)

    def test_it_works_over_http_on_a_lan_ip_a_local_name_and_a_tailscale_name(self):
        for host in ("192.168.1.20:8765", "my-nas.local:8765", "my-nas:8765", "my-nas.tail1234.ts.net:8765", "[fd00::5]:8765", "localhost:8765", "127.0.0.1:8765"):
            with self.subTest(host):
                h = {"Host": host}
                self.assertEqual(self.client.get("/api/health", headers=h).status_code, 200)
                self.assertEqual(self.client.get("/api/status", headers=h).status_code, 200)
                self.assertEqual(self.client.post("/api/scan", headers={**h, "Origin": f"http://{host}"}).status_code, 200)
                self.assertEqual(self.client.put("/api/config", json={"nice": 4}, headers={**h, "Origin": f"http://{host}"}).status_code, 200)
                self.assertEqual(self.client.post("/api/scan", headers={**h, "Origin": "http://evil.example"}).status_code, 403)
                self.assertEqual(self.client.get("/", headers=h).status_code, 200)

    def test_no_cors_headers_are_ever_sent(self):
        for r in (self.client.get("/api/status", headers=EVIL), self.client.options("/api/run", headers={**EVIL, "Access-Control-Request-Method": "POST"}),
                  self.client.post("/api/scan", headers=EVIL), self.client.get("/", headers=EVIL)):
            self.assertFalse([h for h in r.headers if h.lower().startswith("access-control-")], (r.request.method, r.request.url, dict(r.headers)))
        self.assertNotEqual(self.client.options("/api/run", headers={**EVIL, "Access-Control-Request-Method": "POST"}).status_code, 200)

    def test_behind_a_proxy_that_rewrites_the_host_the_public_name_must_be_listed(self):
        h = {"Host": "10.0.0.5:8765", "Origin": "https://ps5.example.com"}
        self.assertEqual(self.client.post("/api/scan", headers=h).status_code, 403)
        self.env(PS5PKG_ALLOWED_HOSTS="ps5.example.com")
        self.assertEqual(self.client.post("/api/scan", headers=h).status_code, 200)
        self.assertEqual(self.client.post("/api/scan", headers={**h, "Origin": "https://evil.example.com"}).status_code, 403)


class Hosts(SecurityCase):
    def test_an_unknown_host_gets_403_and_says_which_variable_to_set(self):
        r = self.client.get("/", headers={"Host": "evil.example.com:8765", "Accept": "text/html"})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.headers["content-type"].startswith("text/html"))
        self.assertIn("PS5PKG_ALLOWED_HOSTS", r.text)
        self.assertIn("evil.example.com", r.text)
        r = self.client.get("/api/status", headers={"Host": "evil.example.com"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("PS5PKG_ALLOWED_HOSTS=evil.example.com", r.json()["detail"])
        self.assertEqual(self.client.post("/api/scan", headers={"Host": "evil.example.com"}).status_code, 403)
        self.scan.assert_not_called()

    def test_the_host_in_the_message_cannot_inject_markup(self):
        r = self.client.get("/", headers={"Host": "<img src=x onerror=alert(1)>.example.com", "Accept": "text/html"})
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("<img src=x", r.text)

    def test_the_variable_adds_names(self):
        h = {"Host": "ps5.example.com:8765"}
        self.assertEqual(self.client.get("/api/status", headers=h).status_code, 403)
        self.env(PS5PKG_ALLOWED_HOSTS="other.example.org, ps5.example.com")
        self.assertEqual(self.client.get("/api/status", headers=h).status_code, 200)
        self.assertEqual(self.client.get("/api/status", headers={"Host": "evil.example.com"}).status_code, 403)
        self.env(PS5PKG_ALLOWED_HOSTS="*.example.net")
        self.assertEqual(self.client.get("/api/status", headers={"Host": "a.example.net"}).status_code, 200)

    def test_a_rebinding_page_cannot_read_or_change_anything(self):
        # the browser thinks it is talking to attacker.example (same origin!), which now points at this machine
        h = {"Host": "attacker.example:8765", "Origin": "http://attacker.example:8765"}
        for method, path in [("GET", "/api/config"), ("GET", "/api/export"), ("PUT", "/api/config"), ("POST", "/api/run")]:
            self.assertEqual(self.client.request(method, path, headers=h, content="{}").status_code, 403, (method, path))


class Escaping(SecurityCase):
    def test_a_source_with_a_hostile_title_id_is_not_recognised(self):
        env = make_env(self.dir / "env", game=False)
        for i, tid in enumerate(HOSTILE_IDS):
            g = Path(env["library"]) / f"Hostile {i}"
            (g / "sce_sys").mkdir(parents=True)
            (g / "eboot.bin").write_bytes(b"\0" * 16)
            (g / "sce_sys" / "param.json").write_text(json.dumps(dict(GAME_PARAM, titleId=tid)))
        good = Path(env["library"]) / "Example Game [PPSA01234]"
        (good / "sce_sys").mkdir(parents=True)
        (good / "eboot.bin").write_bytes(b"\0" * 16)
        (good / "sce_sys" / "param.json").write_text(json.dumps(GAME_PARAM))
        self.write_cfg({**E.DEFAULT_CFG, **env})
        r = subprocess.run([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), "plan"], capture_output=True, text=True,
                           env=os.environ.copy(), timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        plan = json.loads((self.state / "plan.json").read_text())
        self.assertEqual([p["title_id"] for p in plan], ["PPSA01234"])                # only the real one: the rest is "not recognised"
        self.assertIn("param.json has no valid title ID", (self.logs / "pkg_sync.log").read_text())
        for path in ("/api/plan", "/api/status", "/api/failures", "/api/ledger", "/api/storage"):
            body = self.client.get(path).text
            for bad in ("onerror", "<img", "<script", "alert(1)"):
                self.assertNotIn(bad, body, (path, bad))

    def test_an_id_cached_by_an_older_version_is_not_trusted_either(self):
        env = make_env(self.dir / "env")
        cfg = {**E.DEFAULT_CFG, **env}
        cache = {}
        E.discover(cfg, lambda *_: None, cache)
        self.assertTrue([k for k, v in cache.items() if isinstance(v, dict) and v.get("title_id") == "PPSA01234"])
        for v in cache.values():
            if isinstance(v, dict) and v.get("title_id"):
                v["title_id"] = HOSTILE_IDS[0]
        cands = E.discover(cfg, lambda *_: None, cache)
        self.assertEqual([c for c in cands if c.get("title_id")], [])
        self.assertTrue(all("not a valid title ID" in c["error"] for c in cands))
        self.assertEqual(E.choose(cfg, cands, {}), [])

    def test_the_helper(self):
        for tid in ("PPSA01234", "CUSA12345", "PPSA00000"):
            self.assertTrue(E.valid_title_id(tid), tid)
        for tid in HOSTILE_IDS:
            self.assertFalse(E.valid_title_id(tid), tid)

    def test_a_ps4_package_or_folder_with_a_hostile_title_id_is_not_taken(self):
        from support import make_ps4_pkg, make_sfo
        hostile = ['CUSA12345"><img src=x onerror=alert(1)>', "CUSA1234", "CUSA123456", "CUSA12345\n", "PPSA01234", "cusa12345"]
        for tid in hostile:                                          # a package whose content ID names no title: the PARAM.SFO is all there is
            head = E.ps5_ps4pkg.parse_header(make_ps4_pkg("UP9000-XXXX00000_00-EXAMPLEGAME00000", "Example", "01.00", "gd", title_id=tid))
            self.assertEqual((head["ps4"], head["title_id"]), (True, None), tid)
        head = E.ps5_ps4pkg.parse_header(make_ps4_pkg("UP9000-XXXX00000_00-EXAMPLEGAME00000", "Example", "01.00", "gd", title_id="CUSA12345"))
        self.assertEqual(head["title_id"], "CUSA12345")
        for i, tid in enumerate(hostile):                             # a PS4 game folder
            g = self.dir / f"ps4folder{i}"
            (g / "sce_sys").mkdir(parents=True)
            (g / "eboot.bin").write_bytes(b"\0" * 16)
            (g / "sce_sys" / "param.sfo").write_bytes(make_sfo(TITLE="Example", TITLE_ID=tid, APP_VER="01.00"))
            with self.assertRaises(ValueError, msg=tid):
                E.ident_image_or_folder(str(g))

    def test_api_responses_are_json_and_cannot_be_sniffed_into_something_else(self):
        (self.state / "status.json").write_text(json.dumps(dict(updated=1, results=[dict(name="<img src=x onerror=1>", status='x" onmouseover="1', why="<b>")])))
        r = self.client.get("/api/status")
        self.assertTrue(r.headers["content-type"].startswith("application/json"))
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")


class Page(SecurityCase):
    HTML = (WEB_DIR / "static" / "index.html").read_text()
    JS = (WEB_DIR / "static" / "app.js").read_text()

    def test_no_inline_script_or_event_handler_is_left(self):
        self.assertEqual(re.findall(r"<script\b[^>]*>", self.HTML), ['<script src="/static/app.js">'])
        self.assertEqual(re.findall(r"<script\b", self.JS), [])
        for name, text in (("index.html", self.HTML), ("app.js", self.JS)):
            with self.subTest(name):
                self.assertEqual(re.findall(r"""\bon[a-z]+\s*=\s*\\?["']""", text), [])             # onclick="..." in markup or in a template
                self.assertNotIn("javascript:", text.lower())
                self.assertNotRegex(text, r"""\b(eval|new Function)\s*\(""")
                self.assertNotRegex(text, r"""set(Timeout|Interval)\(\s*["'`]""")                  # a timer given a string is eval

    def test_the_script_is_served_as_a_file_and_the_page_loads_it(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<script src="/static/app.js">', r.text)
        js = self.client.get("/static/app.js")
        self.assertEqual(js.status_code, 200)
        self.assertIn("javascript", js.headers["content-type"])
        self.assertEqual(js.text, self.JS)
        self.assertEqual(self.client.get("/static/index.html").status_code, 404)                  # only that file, no directory is served
        self.assertEqual(self.client.get("/static/../server.py").status_code, 404)

    def test_the_security_headers_are_on_every_kind_of_response(self):
        for r in (self.client.get("/"), self.client.get("/static/app.js"), self.client.get("/api/status"), self.client.get("/api/health"),
                  self.client.get("/api/nothing-here"), self.client.post("/api/scan", headers=EVIL), self.client.get("/", headers={"Host": "evil.example.com"})):
            with self.subTest(r.request.url.path, status=r.status_code):
                csp = {d.split()[0]: d.split()[1:] for d in r.headers["content-security-policy"].split(";") if d.strip()}
                self.assertEqual(csp["script-src"], ["'self'"])                                   # no 'unsafe-inline', no 'unsafe-eval'
                self.assertNotIn("'unsafe-eval'", r.headers["content-security-policy"])
                self.assertEqual((csp["frame-ancestors"], csp["object-src"], csp["base-uri"], csp["default-src"]), (["'none'"],) * 4)
                self.assertEqual(csp["connect-src"], ["'self'"])
                self.assertEqual(r.headers["x-frame-options"], "DENY")
                self.assertEqual(r.headers["x-content-type-options"], "nosniff")
                self.assertEqual(r.headers["referrer-policy"], "same-origin")
        self.assertEqual(self.client.get("/api/status").headers["cache-control"], "no-store")

    def test_the_page_says_there_is_no_login(self):
        self.assertIn("No login", self.HTML)
        self.assertIn("behind a VPN", self.HTML)
        self.assertIn("keeping-it-private", self.JS)


class Password(SecurityCase):
    def setUp(self):
        super().setUp()
        self.env(PS5PKG_PASSWORD="correct horse")

    def login(self, client=None, password="correct horse", **kw):
        return (client or self.client).post("/login", content="password=" + password.replace(" ", "+"),
                                            headers={"Content-Type": "application/x-www-form-urlencoded", **kw}, follow_redirects=False)

    def test_without_a_password_nothing_changes(self):
        self.env(PS5PKG_PASSWORD="")
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.assertIn('<script src="/static/app.js">', self.client.get("/").text)
        self.assertEqual(self.login().status_code, 303)                                           # nothing to log in to
        self.assertNotIn("set-cookie", self.login().headers)
        self.assertEqual(self.client.get("/api/status").json()["access"]["login"], False)

    def test_everything_but_the_health_check_needs_a_session(self):
        self.assertEqual(self.client.get("/api/health").json(), {"ok": True})
        for method, path in [("GET", "/api/status"), ("GET", "/api/config"), ("GET", "/api/plan"), ("GET", "/api/export"), ("GET", "/api/backup.zip"),
                             ("GET", "/api/logs"), ("GET", "/api/logs/pkg_sync.log"), ("POST", "/api/run"), ("POST", "/api/scan"), ("PUT", "/api/config"),
                             ("PUT", "/api/overrides/PPSA01234"), ("POST", "/api/import"), ("GET", "/api/nothing-here")]:
            with self.subTest(path):
                r = self.client.request(method, path, content="{}")
                self.assertEqual(r.status_code, 401, r.text)
                self.assertEqual(r.json(), {"detail": "Log in first."})
        self.scan.assert_not_called()
        self.start.assert_not_called()

    def test_the_login_address_itself_goes_to_the_front_page(self):
        r = self.client.get("/login", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/"))

    def test_the_page_is_the_login_form_until_there_is_a_session(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("type=password", r.text)
        self.assertNotIn("app.js", r.text)
        self.assertNotIn("<script", r.text)
        self.assertNotIn("Dashboard", r.text)

    def test_a_wrong_password_gets_no_cookie_a_right_one_gets_a_strict_http_only_cookie(self):
        r = self.login(password="wrong")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Wrong password", r.text)
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(self.client.get("/api/status").status_code, 401)
        r = self.login()
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/"))
        cookie = r.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("path=/", cookie)
        self.assertIn("max-age=", cookie)
        self.assertNotIn("secure", cookie.replace("samesite", ""))                                 # plain http on a LAN must work
        self.assertNotIn("correct", cookie)
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.assertIn("app.js", self.client.get("/").text)
        self.assertEqual(self.client.post("/api/scan").status_code, 200)

    def test_the_cookie_is_secure_when_the_request_came_over_https(self):
        self.assertIn("secure", self.login(**{"X-Forwarded-Proto": "https"}).headers["set-cookie"].lower().replace("samesite", ""))

    def test_logging_out_ends_the_session(self):
        self.login()
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        r = self.client.post("/api/logout")
        self.assertEqual(r.status_code, 200)
        self.assertIn("max-age=0", r.headers["set-cookie"].lower())
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_a_forged_or_old_cookie_does_not_work_and_a_new_password_ends_every_session(self):
        self.login()
        good = self.client.cookies.get("ps5pkg_session")
        self.assertTrue(good)
        self.client.cookies.clear()
        use = lambda value: self.client.get("/api/status", headers={"Cookie": f"ps5pkg_session={value}"}).status_code
        for bad in ("v1.9999999999.deadbeef", good[:-1] + ("0" if good[-1] != "0" else "1"), "garbage", ""):
            self.assertEqual(use(bad), 401, bad)
        self.assertEqual(use(good), 200)
        self.env(PS5PKG_PASSWORD="another one")
        self.assertEqual(use(good), 401)

    def test_other_sites_cannot_log_in_or_use_the_session(self):
        self.assertEqual(self.login(Origin="http://evil.example").status_code, 403)
        self.login()
        self.assertEqual(self.client.post("/api/scan", headers=EVIL).status_code, 403)             # the cookie alone is not enough
        self.assertEqual(self.client.post("/login", json={"password": "correct horse"}).status_code, 415)     # JSON is not the form
        self.scan.assert_not_called()

    def test_wrong_passwords_are_slowed_down_and_then_refused_for_a_while(self):
        for _ in range(8):
            self.assertEqual(self.login(password="nope").status_code, 401)
        r = self.login()
        self.assertEqual(r.status_code, 429)                                                      # even the right one waits
        self.assertNotIn("set-cookie", r.headers)
        self.assertEqual(as_client(self.srv, "10.1.1.1").post("/login", content="password=correct+horse", follow_redirects=False,
                                                              headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code, 303)

    def test_the_session_survives_a_restart_of_the_server(self):
        self.login()
        self.srv.WS._secrets.clear()
        self.assertEqual(self.client.get("/api/status").status_code, 200)

    def test_the_password_is_not_stored_or_shown_anywhere(self):
        self.login()
        for r in (self.client.get("/api/status"), self.client.get("/api/config"), self.client.get("/api/export"), self.client.get("/api/storage")):
            self.assertNotIn("correct horse", r.text)
        for f in self.state.rglob("*"):
            if f.is_file():
                self.assertNotIn(b"correct horse", f.read_bytes(), f.name)

    def test_the_login_state_is_in_the_status(self):
        self.login()
        a = self.client.get("/api/status").json()["access"]
        self.assertEqual((a["login"], a["warn"]), (True, False))


class Exposure(SecurityCase):
    def access(self, client=None, **headers):
        r = (client or self.client).get("/api/status", headers=headers)
        self.assertEqual(r.status_code, 200)
        return r.json()["access"]

    def test_a_request_from_the_home_network_has_no_warning(self):
        for ip in ("192.168.1.20", "10.0.0.7", "172.17.0.1", "127.0.0.1", "100.101.102.103", "fd00::5"):
            self.assertEqual(self.access(as_client(self.srv, ip)), dict(login=False, warn=False, reason=None, client=None), ip)
        self.assertEqual(self.access()["warn"], False)

    def test_a_request_from_a_public_address_warns(self):
        a = self.access(as_client(self.srv, "8.8.8.8"))
        self.assertEqual((a["warn"], a["reason"], a["client"], a["login"]), (True, "remote", "8.8.8.8", False))

    def test_a_proxy_that_adds_x_forwarded_for_warns(self):
        for h in ({"X-Forwarded-For": "203.0.113.9"}, {"X-Forwarded-For": "192.168.1.20"}, {"Forwarded": "for=203.0.113.9;proto=https"}, {"X-Real-IP": "203.0.113.9"}):
            a = self.access(**h)
            self.assertEqual((a["warn"], a["reason"]), (True, "proxy"), h)

    def test_a_password_or_the_switch_turns_it_off(self):
        self.env(PS5PKG_EXPOSURE_WARNING="0")
        self.assertFalse(self.access(**{"X-Forwarded-For": "203.0.113.9"})["warn"])
        self.assertFalse(self.access(as_client(self.srv, "8.8.8.8"))["warn"])
        self.env(PS5PKG_EXPOSURE_WARNING="1", PS5PKG_PASSWORD="pw")
        c = as_client(self.srv, "8.8.8.8")
        c.post("/login", content="password=pw", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        a = self.access(c, **{"X-Forwarded-For": "203.0.113.9"})
        self.assertEqual((a["warn"], a["login"]), (False, True))

    def test_the_page_has_a_place_for_the_banner_and_for_the_note(self):
        html = (WEB_DIR / "static" / "index.html").read_text()
        js = (WEB_DIR / "static" / "app.js").read_text()
        self.assertIn('id="accessBanner"', html)
        self.assertIn('id="accessNote"', html)
        self.assertIn("appears to be reachable from outside your network", js)
        self.assertIn("renderAccess(s.access)", js)


class PatchFolder(SecurityCase):
    def setUp(self):
        super().setUp()
        self.root = Path(os.path.commonpath([str(self.state), str(self.logs)])) / "patches"
        (self.root / "PPSA01234").mkdir(parents=True)

    def put(self, patch, tid="PPSA01234"):
        return self.client.put(f"/api/overrides/{tid}", json={"patch": patch})

    def stored(self, tid="PPSA01234"):
        return (json.loads(self.cfg_path.read_text()).get("overrides") or {}).get(tid, {}).get("patch")

    def test_a_folder_below_the_patches_folder_is_accepted(self):
        for p in (self.root / "PPSA01234", self.root / "PPSA01234" / "sub", self.root / "not-made-yet"):
            with self.subTest(p):
                r = self.put(str(p))
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(self.stored(), str(p))

    def test_anywhere_else_is_refused_and_nothing_is_saved(self):
        before = self.cfg_path.read_bytes()
        outside = self.dir / "elsewhere"
        outside.mkdir()
        (self.root / "link").symlink_to(outside)
        for p in ("/etc", "/", "/config", str(self.root), str(self.root) + "/../state", str(self.root / "PPSA01234" / ".." / ".." / "state"),
                  str(self.root / "link"), str(self.root / "link" / "x"), "relative/patches", "patches", "~", "file:///etc", str(self.state), str(outside),
                  str(self.root) + "-other/PPSA01234"):
            with self.subTest(p):
                r = self.put(p)
                self.assertEqual(r.status_code, 400, (p, r.text))
                self.assertIn("must be inside", r.json()["detail"])
        self.assertEqual(self.cfg_path.read_bytes(), before)

    def test_the_message_says_where_the_folder_belongs_on_the_nas(self):
        self.env(PS5PKG_HOST_CONFIG="/srv/docker/ps5/config")
        r = self.put("/etc")
        self.assertIn("/srv/docker/ps5/config/patches", r.json()["detail"])
        self.assertIn("PPSA01234", r.json()["detail"])

    def test_a_host_path_that_maps_to_the_patches_folder_is_accepted(self):
        self.env(PS5PKG_HOST_CONFIG="/srv/docker/ps5/config")
        r = self.put("/srv/docker/ps5/config/patches/PPSA01234")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.stored(), str(self.root / "PPSA01234"))
        self.assertEqual(self.put("/srv/docker/ps5/config/state").status_code, 400)

    def test_a_value_that_is_already_stored_is_kept_so_other_options_can_still_be_saved(self):
        self.write_cfg(legacy_config(overrides={"PPSA01234": {"patch": "/mnt/old-place/patches"}}))
        r = self.client.put("/api/overrides/PPSA01234", json={"patch": "/mnt/old-place/patches", "note": "still works", "skip": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.put("/mnt/some/other/place").status_code, 400)                      # but a new one is judged
        self.assertEqual(self.client.put("/api/overrides/PPSA01234", json={"patch": ""}).status_code, 200)        # and it can be cleared
        self.assertIsNone(self.stored())

    def test_import_checks_the_patch_folders_of_the_overrides(self):
        doc = dict(format=1, overrides={"PPSA01234": {"patch": "/etc"}, "PPSA07777": {"patch": str(self.root / "PPSA07777")}})
        for q in ("", "?dry_run=1"):
            r = self.client.post("/api/import" + q, content=json.dumps(doc))
            self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(len(r.json()["problems"]), 1)
            self.assertIn("overrides.PPSA01234.patch: A patch folder must be inside", r.json()["problems"][0])
        self.assertNotIn("PPSA07777", json.loads(self.cfg_path.read_text()).get("overrides", {}))
        ok = dict(format=1, overrides={"PPSA07777": {"patch": str(self.root / "PPSA07777")}})
        self.assertEqual(self.client.post("/api/import", content=json.dumps(ok)).status_code, 200)
        # importing only settings is not held up by the overrides in the same file
        self.assertEqual(self.client.post("/api/import?parts=settings", content=json.dumps(dict(doc, settings={"nice": 7}))).status_code, 200)

    def test_the_engine_still_builds_with_a_patch_folder_set_by_hand(self):
        self.write_cfg(legacy_config(overrides={"PPSA01234": {"patch": "/anywhere/the/user/put/it"}}))
        cfg = E.load_cfg()
        self.assertEqual(cfg["overrides"]["PPSA01234"]["patch"], "/anywhere/the/user/put/it")       # a hand-edited file is trusted as before


class NotifyUrl(SecurityCase):
    def test_only_web_addresses_are_accepted(self):
        for url in ("file:///etc/passwd", "ftp://example.org/x", "gopher://x", "javascript:alert(1)", "ntfy.sh/topic", "//example.org", "http:/x", "https://", "data:text/plain,x"):
            with self.subTest(url):
                r = self.client.put("/api/config", json={"notify_url": url})
                self.assertEqual(r.status_code, 400, (url, r.text))
                self.assertIn("http://", r.json()["detail"])
        for url in ("https://ntfy.sh/my-topic", "http://192.168.1.5:8080/topic", "HTTPS://Ntfy.sh/x", "", "  https://ntfy.sh/x  "):
            with self.subTest(url):
                self.assertEqual(self.client.put("/api/config", json={"notify_url": url}).status_code, 200, url)
        self.assertEqual(json.loads(self.cfg_path.read_text())["notify_url"], "https://ntfy.sh/x")

    def test_an_import_with_another_kind_of_address_is_refused(self):
        r = self.client.post("/api/import", content=json.dumps(dict(format=1, settings={"notify_url": "file:///etc/passwd"})))
        self.assertEqual(r.status_code, 400)
        self.assertIn("settings.notify_url", r.json()["problems"][0])

    def test_a_file_that_has_one_anyway_is_reported_and_never_used(self):
        self.write_cfg(legacy_config(notify_url="file:///etc/passwd"))
        info = E.load_cfg_info()
        self.assertEqual(info.cfg["notify_url"], "")
        self.assertTrue([p for p in info.problems if "notify_url" in p])
        with mock.patch("urllib.request.urlopen") as op:
            E.notify({"notify_url": "file:///etc/passwd"}, "t", "m")
            E.notify({"notify_url": "ftp://x/y"}, "t", "m")
            op.assert_not_called()
            E.notify({"notify_url": "https://ntfy.example/t"}, "t", "m")
            op.assert_called_once()


if __name__ == "__main__":
    unittest.main()
