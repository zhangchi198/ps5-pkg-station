"""The pure parts of the web safety layer (app/web/ps5_websec.py): which Host names are accepted, what counts as the
same origin, which requests may change something, the password and its session cookie, and when a page looks open
to the world. Standard library only: no server is started."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import StateCase, WEB_DIR

sys.path.insert(0, str(WEB_DIR))
import ps5_websec as W  # noqa: E402


class Hosts(unittest.TestCase):
    def test_the_names_the_app_is_meant_to_be_reached_by_are_accepted(self):
        for host in ["localhost", "localhost:8765", "127.0.0.1:8765", "192.168.1.20", "10.0.0.5:8765", "[::1]:8765", "[fe80::1]", "8.8.8.8",
                     "nas", "my-nas:8765", "testserver", "nas.local", "nas.local:8765", "NAS.LOCAL.", "box.lan", "ps5.home.arpa",
                     "nas.tail1234.ts.net:8765", "diskstation.internal", "x.localhost", "pi.localdomain", None]:
            with self.subTest(host):
                self.assertTrue(W.host_allowed(host), host)

    def test_a_name_on_the_internet_is_refused_whatever_it_points_at(self):
        for host in ["evil.example.com", "evil.example.com:8765", "ps5.com", "localhost.evil.com", "nas.local.evil.com", "192.168.1.5.evil.com",
                     "nas.ts.net.evil.com", "xn--80ak6aa92e.com", "", " ", "[", "evil.com\r\nx"]:
            with self.subTest(host):
                self.assertFalse(W.host_allowed(host), host)

    def test_names_listed_by_the_owner_are_added(self):
        extra = W.allowed_hosts({"PS5PKG_ALLOWED_HOSTS": "Ps5.Example.com, other.org:8080  third.net;*.corp.example"})
        self.assertEqual(extra, ["ps5.example.com", "other.org", "third.net", "*.corp.example"])
        for host in ["ps5.example.com", "PS5.example.com:8765", "other.org", "third.net", "a.corp.example", "a.b.corp.example"]:
            with self.subTest(host):
                self.assertTrue(W.host_allowed(host, extra), host)
        for host in ["corp.example", "evil.com", "ps5.example.com.evil.com", "xps5.example.com"]:
            with self.subTest(host):
                self.assertFalse(W.host_allowed(host, extra), host)
        self.assertEqual(W.allowed_hosts({}), [])

    def test_host_name(self):
        for raw, want in [("nas.local:8765", "nas.local"), ("[::1]:8765", "::1"), ("NAS.", "nas"), ("a.b", "a.b"), ("::1", "::1")]:
            self.assertEqual(W.host_name(raw), want)


class Origins(unittest.TestCase):
    def test_same_origin(self):
        yes = [("http://nas.local:8765", "nas.local:8765"), ("http://192.168.1.20:8765", "192.168.1.20:8765"), ("http://NAS.local:8765", "nas.local:8765"),
               ("http://testserver", "testserver"), ("https://ps5.example.com", "ps5.example.com"), ("http://[::1]:8765", "[::1]:8765"),
               ("http://nas.local:8765/some/page?x=1", "nas.local:8765"), ("https://ps5.example.com:443", "ps5.example.com")]
        no = [("http://evil.example:8765", "nas.local:8765"), ("http://nas.local:9000", "nas.local:8765"), ("http://nas.local", "nas.local:8765"),
              ("null", "nas.local:8765"), ("", "nas.local:8765"), ("file:///etc/passwd", "nas.local:8765"), ("http://nas.local:8765.evil.com", "nas.local:8765"),
              ("http://nas.local:99999", "nas.local:99999"), ("ftp://nas.local:8765", "nas.local:8765"), ("http://nas.local:8765", ""), ("http://nas.local:8765", None),
              ("http://nas.local:8765@evil.com", "nas.local:8765")]
        for origin, host in yes:
            with self.subTest(origin, host=host):
                self.assertTrue(W.same_origin(origin, host), (origin, host))
        for origin, host in no:
            with self.subTest(origin, host=host):
                self.assertFalse(W.same_origin(origin, host), (origin, host))

    def test_a_proxy_that_rewrites_the_host_header_works_once_its_name_is_listed(self):
        self.assertFalse(W.same_origin("https://ps5.example.com", "10.0.0.5:8765"))
        self.assertTrue(W.same_origin("https://ps5.example.com", "10.0.0.5:8765", ["ps5.example.com"]))
        self.assertFalse(W.same_origin("https://evil.example.com", "10.0.0.5:8765", ["ps5.example.com"]))
        self.assertTrue(W.same_origin("https://x.corp.example", "10.0.0.5:8765", ["*.corp.example"]))

    def test_json_content_type(self):
        for ct, want in [("application/json", True), ("application/json; charset=utf-8", True), ("Application/JSON", True), ("text/plain", False),
                         ("application/x-www-form-urlencoded", False), ("multipart/form-data; boundary=x", False), (None, False), ("", False),
                         ("application/jsonp", False)]:
            self.assertEqual(W.json_content(ct), want, ct)


class Requests(unittest.TestCase):
    H = {"host": "nas.local:8765", "content-type": "application/json"}

    def check(self, method="POST", path="/api/run", **over):
        headers = {**self.H, **over}
        return W.check_request(method, path, {k: v for k, v in headers.items() if v is not None})

    def test_a_request_from_this_page_goes_through(self):
        self.assertIsNone(self.check(origin="http://nas.local:8765"))
        self.assertIsNone(self.check(referer="http://nas.local:8765/"))
        self.assertIsNone(self.check(origin="http://nas.local:8765", **{"sec-fetch-site": "same-origin"}))

    def test_a_script_or_curl_without_origin_or_referer_goes_through_with_json(self):
        self.assertIsNone(self.check())
        self.assertIsNone(self.check("DELETE", "/api/overrides/PPSA01234"))

    def test_a_request_from_another_site_is_refused(self):
        for over in [dict(origin="http://evil.example"), dict(origin="null"), dict(origin="http://nas.local:9000"), dict(referer="http://evil.example/x"),
                     dict(origin="http://nas.local:8765", **{"sec-fetch-site": "cross-site"}), dict(origin="http://evil.example", **{"content-type": "text/plain"})]:
            for method in W.UNSAFE:
                with self.subTest(method, **over):
                    self.assertEqual(self.check(method, **over), (403, "cross-site"))

    def test_origin_wins_over_a_matching_referer(self):
        self.assertEqual(self.check(origin="http://evil.example", referer="http://nas.local:8765/"), (403, "cross-site"))

    def test_a_form_post_or_plain_text_is_refused_for_the_content_type(self):
        for ct in ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", None]:
            with self.subTest(ct):
                self.assertEqual(self.check(**{"content-type": ct}), (415, "content-type"))

    def test_reading_needs_neither(self):
        for method in ("GET", "HEAD", "OPTIONS"):
            self.assertIsNone(self.check(method, "/api/status", origin="http://evil.example", **{"content-type": None}))

    def test_the_host_is_checked_first_and_for_every_method(self):
        for method in ("GET", "POST"):
            self.assertEqual(self.check(method, host="evil.example.com"), (403, "host"))
        self.assertIsNone(W.check_request("POST", "/api/run", {"host": "ps5.example.com", "content-type": "application/json"}, ["ps5.example.com"]))

    def test_the_login_form_is_a_plain_form_and_still_origin_checked(self):
        form = {"content-type": "application/x-www-form-urlencoded"}
        self.assertIsNone(self.check("POST", "/login", **form))
        self.assertEqual(self.check("POST", "/login", origin="http://evil.example", **form), (403, "cross-site"))
        self.assertEqual(self.check("POST", "/login"), (415, "content-type"))           # JSON is for the API, the form for the form


class Session(StateCase):
    def test_a_session_is_valid_until_it_expires_and_for_this_password_only(self):
        c = W.make_session(self.state, "secret", now=1000)
        self.assertTrue(W.session_valid(c, self.state, "secret", now=1001))
        self.assertTrue(W.session_valid(c, self.state, "secret", now=1000 + W.SESSION_DAYS * 86400 - 1))
        self.assertFalse(W.session_valid(c, self.state, "secret", now=1000 + W.SESSION_DAYS * 86400 + 1))
        self.assertFalse(W.session_valid(c, self.state, "another", now=1001))                     # a new password ends every session

    def test_forged_and_broken_cookies_are_refused(self):
        c = W.make_session(self.state, "secret", now=time.time())
        ver, exp, sig = c.split(".")
        for bad in ["", None, "x", "v1.1.2", f"v1.{int(exp) + 99}.{sig}", f"v2.{exp}.{sig}", f"v1.{exp}.{sig[:-1]}{'1' if sig[-1] == '0' else '0'}", f"v1.{exp}.", ".".join([ver, exp, sig, "x"]), f"v1.abc.{sig}"]:
            with self.subTest(bad):
                self.assertFalse(W.session_valid(bad, self.state, "secret"))

    def test_the_secret_is_kept_in_the_state_folder_and_survives_a_restart(self):
        c = W.make_session(self.state, "secret")
        f = self.state / "session.secret"
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        W._secrets.clear()                                                                          # a new process
        self.assertTrue(W.session_valid(c, self.state, "secret"))
        other = Path(tempfile.mkdtemp(dir=self.dir))
        self.assertFalse(W.session_valid(c, other, "secret"))                                       # another install has another secret

    def test_an_unwritable_state_folder_still_gives_a_working_session(self):
        gone = self.dir / "does-not-exist"
        c = W.make_session(gone, "secret")
        self.assertTrue(W.session_valid(c, gone, "secret"))

    def test_password_ok(self):
        self.assertTrue(W.password_ok("secret", "secret"))
        self.assertTrue(W.password_ok("päss wörd", "päss wörd"))
        for bad in ["Secret", "secret ", "", None, "secre"]:
            self.assertFalse(W.password_ok(bad, "secret"), bad)
        self.assertFalse(W.password_ok("", ""))                                                       # no password set: nothing logs in

    def test_the_cookie_is_found_among_others(self):
        self.assertEqual(W.cookie_value("a=1; ps5pkg_session=abc.def; b=2"), "abc.def")
        self.assertIsNone(W.cookie_value("a=1"))
        self.assertIsNone(W.cookie_value(None))

    def test_the_password_comes_from_the_environment(self):
        self.assertEqual(W.password({}), "")
        self.assertEqual(W.password({"PS5PKG_PASSWORD": "x y"}), "x y")


class Throttle(unittest.TestCase):
    def test_too_many_wrong_passwords_wait(self):
        t = W.LoginThrottle(limit=3, window=60)
        for i in range(3):
            self.assertFalse(t.blocked("a", now=100 + i))
            t.fail("a", now=100 + i)
        self.assertTrue(t.blocked("a", now=110))
        self.assertFalse(t.blocked("b", now=110))                                                     # someone else is not held up
        self.assertFalse(t.blocked("a", now=100 + 61 + 2))                                            # a while later: again
        t.fail("a", now=200)
        t.ok("a")
        self.assertFalse(t.blocked("a", now=200))


class Exposure(unittest.TestCase):
    def test_inside_the_network_there_is_nothing_to_say(self):
        for ip in ["192.168.1.20", "10.1.2.3", "172.17.0.1", "127.0.0.1", "::1", "fd00::5", "fe80::1%eth0", "100.101.102.103", "169.254.1.1", "testclient", None, ""]:
            with self.subTest(ip):
                self.assertEqual(W.exposure(ip, {}, login=False), dict(login=False, warn=False, reason=None, client=None))

    def test_a_global_address_means_the_port_is_open_to_the_world(self):
        for ip in ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"]:
            with self.subTest(ip):
                e = W.exposure(ip, {}, login=False)
                self.assertEqual((e["warn"], e["reason"], e["client"]), (True, "remote", ip))

    def test_a_pc_and_a_nas_with_ipv6_addresses_from_the_provider_are_on_one_link(self):
        pc, nas = "2606:4700:1234:5678::20", "2606:4700:1234:5678:aaaa::5"
        self.assertEqual(W.exposure(pc, {}, login=False, server_ip=nas)["warn"], False)               # the same /64
        self.assertEqual(W.exposure("2606:4700:1234:9999::20", {}, login=False, server_ip=nas)["warn"], True)    # another network
        self.assertEqual(W.exposure(pc, {}, login=False)["warn"], True)                                # no idea where the server is
        self.assertEqual(W.exposure("8.8.8.8", {}, login=False, server_ip="192.168.1.5")["warn"], True)
        self.assertEqual(W.exposure("8.8.8.8", {}, login=False, server_ip=nas)["warn"], True)
        self.assertEqual(W.exposure(pc, {"x-forwarded-for": "1.2.3.4"}, login=False, server_ip=nas)["reason"], "proxy")

    def test_the_headers_of_a_reverse_proxy_are_enough(self):
        for h in ["x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-proto", "x-forwarded-host", "cf-connecting-ip"]:
            with self.subTest(h):
                e = W.exposure("172.18.0.2", {h: "203.0.113.9"}, login=False)
                self.assertEqual((e["warn"], e["reason"]), (True, "proxy"))

    def test_a_password_or_the_switch_silences_it(self):
        self.assertFalse(W.exposure("8.8.8.8", {"x-forwarded-for": "1.2.3.4"}, login=True)["warn"])
        self.assertTrue(W.exposure("8.8.8.8", {}, login=True)["login"])
        self.assertFalse(W.exposure("8.8.8.8", {"x-forwarded-for": "1.2.3.4"}, login=False, enabled=False)["warn"])
        for v, want in [("0", False), ("off", False), ("False", False), ("no", False), ("1", True), ("", True)]:
            self.assertEqual(W.warning_enabled({"PS5PKG_EXPOSURE_WARNING": v}), want, v)
        self.assertTrue(W.warning_enabled({}))


class Pages(unittest.TestCase):
    def test_the_host_page_names_the_variable_and_escapes_what_it_was_sent(self):
        ctype, body = W.blocked_host_page("<script>alert(1)</script>.example.com:8765")
        self.assertTrue(ctype.startswith("text/html"))
        self.assertIn("PS5PKG_ALLOWED_HOSTS", body)
        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;script&gt;", body)
        ctype, body = W.blocked_host_page("ps5.example.com:8765", accept_html=False)
        self.assertEqual(ctype, "application/json")
        self.assertIn("PS5PKG_ALLOWED_HOSTS=ps5.example.com", body)

    def test_the_login_page_is_a_form_and_escapes_its_message(self):
        page = W.login_page('<b onmouseover="x">no</b>')
        self.assertIn('type=password', page)
        self.assertIn('action="/login"', page)
        self.assertNotIn("<b onmouseover", page)
        self.assertNotIn("<script", page)
        self.assertNotIn(" onclick", page)


if __name__ == "__main__":
    unittest.main()
