"""The web server's side of notifications: the settings and their defaults, the Send test button (the form's values, the answer in words, nothing
kept, never the address), what the last delivery was, and the things only the server can say: a scheduled run skipped or stopped, a settings file
that can't be read. Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from support import E, ServerCase, WEB_DIR, legacy_config

from test_notify import SECRET, serve  # noqa: E402

import ps5_notify as PN  # noqa: E402

BROKEN = '{\n  "compression_level": 5,\n  "nice": 3\n  "max_builds": 2\n}\n'


class Settings(ServerCase):
    def test_the_new_settings_are_on_the_page_with_their_defaults(self):
        self.write_cfg(legacy_config())
        c = self.get("/api/config")
        by = {f["key"]: f for f in c["schema"]}
        self.assertEqual([k for k in by if k.startswith("notify")], ["notify_url", "notify_type", "notify_summary", "notify_attention", "notify_each_game", "notify_link"])
        self.assertEqual((by["notify_type"]["default"], by["notify_summary"]["default"], by["notify_attention"]["default"], by["notify_each_game"]["default"]), ("ntfy", True, True, False))
        self.assertEqual([x[0] for x in by["notify_type"]["choices"]], ["ntfy", "webhook"])
        self.assertTrue(all(by[k]["group"] == "notifications" for k in by if k.startswith("notify")))

    def test_an_install_with_an_address_from_before_the_toggles_keeps_a_message_per_game_and_one_without_does_not(self):
        raw = legacy_config(notify_url="https://ntfy.example/t")
        for k in ("notify_each_game", "notify_summary", "notify_attention", "notify_type", "notify_link"):
            raw.pop(k, None)
        self.write_cfg(raw)
        self.assertIs(self.get("/api/config")["notify_each_game"], True)
        raw.pop("notify_url")
        raw["notify_url"] = ""
        self.write_cfg(raw)
        self.assertIs(self.get("/api/config")["notify_each_game"], False)

    def test_the_settings_are_checked_with_words_at_their_fields(self):
        self.write_cfg(legacy_config())
        r = self.client.put("/api/config", json={"notify_type": "gotify", "notify_link": "nas.local", "notify_url": "ntfy.sh/x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(sorted(e["field"] for e in r.json()["errors"]), ["notify_link", "notify_type", "notify_url"])
        r = self.client.put("/api/config", json={"notify_type": "webhook", "notify_link": "http://nas.local:8765", "notify_each_game": True, "notify_summary": False})
        self.assertEqual(r.status_code, 200, r.text)
        saved = json.loads(self.cfg_path.read_text())
        self.assertEqual((saved["notify_type"], saved["notify_each_game"], saved["notify_summary"]), ("webhook", True, False))

    def test_an_export_without_the_address_keeps_the_other_notification_settings(self):
        self.write_cfg(legacy_config(notify_url=f"https://ntfy.example/{SECRET}", notify_type="webhook", notify_link="http://nas.local:8765"))
        doc = self.get("/api/export?notify_url=0")
        self.assertNotIn("notify_url", doc["settings"])
        self.assertNotIn(SECRET, json.dumps(doc))
        self.assertEqual((doc["settings"]["notify_type"], doc["settings"]["notify_link"]), ("webhook", "http://nas.local:8765"))
        self.assertEqual(self.get("/api/export")["settings"]["notify_url"], f"https://ntfy.example/{SECRET}")


class SendTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config())

    def test_it_sends_what_is_on_the_form_not_what_is_saved_and_says_so(self):
        sink, other = serve(self), serve(self)
        self.client.put("/api/config", json={"notify_url": other.url})                      # saved: somewhere else
        r = self.client.post("/api/notify/test", json={"notify_url": sink.url, "notify_type": "webhook", "notify_link": "http://nas.local:8765"})
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "status": 200, "error": None, "type": "webhook"}))
        self.assertEqual((len(sink.got), len(other.got)), (1, 0))
        doc = json.loads(sink.got[0]["body"].decode())
        self.assertEqual((doc["event"], doc["url"]), ("test", "http://nas.local:8765"))
        self.assertIn("If you can read it, notifications work", doc["message"])
        self.assertEqual(sink.got[0]["headers"]["Content-Type"], "application/json; charset=utf-8")

    def test_the_saved_values_fill_in_what_the_form_leaves_out(self):
        sink = serve(self)
        self.client.put("/api/config", json={"notify_url": sink.url, "notify_type": "webhook"})
        r = self.client.post("/api/notify/test", json={})
        self.assertEqual((r.json()["ok"], r.json()["type"], len(sink.got)), (True, "webhook", 1))

    def test_a_refusal_is_shown_with_its_status_and_no_address(self):
        for code, text in ((403, "HTTP 403 Forbidden"), (404, "HTTP 404 Not Found"), (500, "HTTP 500 Internal Server Error")):
            sink = serve(self, code=code)
            r = self.client.post("/api/notify/test", json={"notify_url": sink.url})
            self.assertEqual((r.status_code, r.json()["ok"], r.json()["status"], r.json()["error"]), (200, False, code, text))
            for secret in (SECRET, "abcdef123456", "127.0.0.1"):
                self.assertNotIn(secret, r.text)

    def test_an_address_nobody_answers_is_an_error_in_words(self):
        r = self.client.post("/api/notify/test", json={"notify_url": f"http://127.0.0.1:1/{SECRET}?token=abcdef123456"})
        self.assertEqual((r.status_code, r.json()["ok"], r.json()["status"]), (200, False, None))
        self.assertRegex(r.json()["error"], r"(?i)refused|connect")
        self.assertNotIn(SECRET, r.text)

    def test_a_missing_or_bad_value_is_refused_at_its_field_and_nothing_is_sent(self):
        r = self.client.post("/api/notify/test", json={})
        self.assertEqual((r.status_code, r.json()["errors"][0]["field"]), (400, "notify_url"))
        self.assertIn("Enter the address", r.json()["detail"])
        sink = serve(self)
        for body, field in (({"notify_url": "ftp://x/y"}, "notify_url"), ({"notify_url": sink.url, "notify_type": "gotify"}, "notify_type"), ({"notify_url": sink.url, "notify_link": "nas"}, "notify_link"),
                            ({"notify_url": ""}, "notify_url")):
            r = self.client.post("/api/notify/test", json=body)
            self.assertEqual((r.status_code, r.json()["errors"][0]["field"]), (400, field), body)
        self.assertEqual(self.client.post("/api/notify/test", json=[1]).status_code, 400)
        self.assertEqual(sink.got, [])

    def test_a_test_is_not_a_delivery_it_changes_what_the_saved_address_looks_like_to_nobody(self):
        bad = serve(self, code=403)
        self.client.post("/api/notify/test", json={"notify_url": bad.url})
        self.assertIsNone(PN.read_last(self.state))
        saved = serve(self)
        self.client.put("/api/config", json={"notify_url": saved.url})
        self.assertIsNone(self.get("/api/status")["notify_last"])                            # nothing has been sent to it yet

    def test_the_test_goes_through_when_every_kind_of_message_is_off_and_is_logged_without_the_address(self):
        sink = serve(self)
        self.client.put("/api/config", json={"notify_summary": False, "notify_attention": False, "notify_each_game": False})
        self.assertTrue(self.client.post("/api/notify/test", json={"notify_url": sink.url}).json()["ok"])
        log = (self.logs / "pkg_sync.log").read_text()
        self.assertIn("notification test (ntfy): delivered (HTTP 200)", log)
        self.assertNotIn(SECRET, log)


class LastDelivery(ServerCase):
    def test_the_status_says_what_the_last_delivery_was_only_while_there_is_an_address(self):
        sink = serve(self, code=403)
        self.write_cfg(legacy_config(notify_url=sink.url))
        self.assertIsNone(self.get("/api/status")["notify_last"])
        PN.send(self.srv.load_cfg(), "summary", "t", "m", state=self.state)
        last = self.get("/api/status")["notify_last"]
        self.assertEqual((last["ok"], last["status"], last["error"], last["event"]), (False, 403, "HTTP 403 Forbidden", "summary"))
        self.assertEqual(self.get("/api/config")["notify_last"]["ok"], False)
        self.client.put("/api/config", json={"notify_url": ""})
        self.assertIsNone(self.get("/api/status")["notify_last"])                            # nowhere to send: nothing to report
        self.assertIsNone(self.get("/api/config")["notify_last"])

    def test_the_status_never_has_the_address_in_it(self):
        sink = serve(self, code=500)
        self.write_cfg(legacy_config(notify_url=sink.url))
        PN.send(self.srv.load_cfg(), "attention", "t", "m", state=self.state)
        text = json.dumps(self.get("/api/status")["notify_last"])
        for secret in (SECRET, "abcdef123456", "127.0.0.1"):
            self.assertNotIn(secret, text)


class FakeProc:
    def __init__(self, rc):
        self.pid, self.rc = 99999, rc

    def wait(self):
        return self.rc

    def poll(self):
        return self.rc


class FromTheServer(ServerCase):
    AT_3 = time.strptime("2030-01-01 03:00", "%Y-%m-%d %H:%M")

    def setUp(self):
        super().setUp()
        self.sink = serve(self)
        self.write_cfg(legacy_config(notify_url=self.sink.url, schedule={"enabled": True, "times": ["03:00"]}))
        self.srv.load_cfg()

    def titles(self):
        return [g["headers"].get("Title") for g in self.sink.got]

    def test_a_scheduled_run_skipped_because_another_is_active_is_told(self):
        with mock.patch.object(self.srv, "start_engine", return_value=False):
            self.srv.scheduler_tick(set(), self.AT_3)
        self.assertEqual(self.titles(), ["PS5 pkg: scheduled run skipped"])
        self.assertEqual(self.sink.got[0]["headers"]["Priority"], "high")
        self.assertIn("03:00", self.sink.got[0]["body"].decode())

    def test_a_run_that_started_or_a_paused_schedule_says_nothing(self):
        with mock.patch.object(self.srv, "start_engine", return_value=True):
            self.srv.scheduler_tick(set(), self.AT_3)
        self.client.post("/api/schedule/pause", json={})
        with mock.patch.object(self.srv, "start_engine", return_value=False):
            self.srv.scheduler_tick(set(), self.AT_3)
        self.assertEqual(self.sink.got, [])                                                  # a pause is the owner's own choice

    def test_with_attention_off_nothing_is_sent(self):
        self.client.put("/api/config", json={"notify_attention": False})
        with mock.patch.object(self.srv, "start_engine", return_value=False):
            self.srv.scheduler_tick(set(), self.AT_3)
        self.assertEqual(self.sink.got, [])

    def wait_for(self, cond, secs=5):
        end = time.time() + secs
        while time.time() < end and not cond():
            time.sleep(0.05)

    def start(self, rc, trigger):
        with mock.patch.object(self.srv.subprocess, "Popen", return_value=FakeProc(rc)):
            try:
                self.srv.start_engine(trigger=trigger)
            except self.srv.EngineStartError:
                pass

    def test_a_scheduled_run_that_ends_badly_is_told_what_the_engine_could_not_say_itself(self):
        for rc, want in ((E.EXIT_BAD_CONFIG, "settings file (config.json) could not be read"), (1, "stopped unexpectedly (exit code 1)"), (-9, "stopped (signal 9)")):
            before = len(self.sink.got)
            self.start(rc, "scheduled")
            self.wait_for(lambda: len(self.sink.got) > before)
            self.assertEqual(self.titles()[before:], ["PS5 pkg: scheduled run did not finish"], rc)
            self.assertIn(want, self.sink.got[before]["body"].decode())

    def test_what_the_engine_tells_itself_is_not_told_twice_and_a_manual_run_or_a_good_one_says_nothing(self):
        for rc, trigger in ((E.EXIT_NOT_MOUNTED, "scheduled"), (E.EXIT_SETUP, "scheduled"), (E.EXIT_LOCKED, "scheduled"), (1, "manual"), (0, "scheduled")):
            self.start(rc, trigger)
        time.sleep(0.5)
        self.assertEqual(self.sink.got, [])

    def test_a_settings_file_that_can_t_be_read_is_told_once_and_again_if_it_breaks_again(self):
        self.srv.config_tick()
        self.assertEqual(self.sink.got, [])
        self.write_cfg(BROKEN)
        self.srv.config_tick()
        self.srv.config_tick()
        self.assertEqual(self.titles(), ["PS5 pkg: settings file can't be read"])
        body = self.sink.got[0]["body"].decode()
        self.assertIn("line 4", body)
        self.assertIn("The last settings that worked are used", body)
        self.write_cfg(legacy_config(notify_url=self.sink.url))
        self.srv.config_tick()
        self.write_cfg(BROKEN)
        self.srv.config_tick()
        self.assertEqual(len(self.sink.got), 2)

    def test_a_failure_to_deliver_from_the_server_is_logged_and_does_not_raise(self):
        bad = serve(self, code=500)
        self.client.put("/api/config", json={"notify_url": bad.url})
        with mock.patch.object(self.srv, "start_engine", return_value=False):
            self.srv.scheduler_tick(set(), self.AT_3)
        self.assertIn("NOT delivered: HTTP 500", (self.logs / "pkg_sync.log").read_text())
        self.assertFalse(PN.read_last(self.state)["ok"])


class Page(unittest.TestCase):
    """The page (no browser here: the interactions were checked in a real one)."""
    JS = (WEB_DIR / "static" / "app.js").read_text()

    def body(self, name, until):
        start = self.JS.index(name)
        return self.JS[start:self.JS.index(until, start)]

    def test_the_address_has_a_send_test_button_that_sends_the_form_and_answers_beside_it(self):
        field = self.body("function fieldHtml", "const timeRow")
        self.assertIn("data-notify-test", field)
        self.assertIn('id="notifyLast"', field)
        test = self.body("async function testNotify", '$("#setGroups")')
        self.assertIn('api("/api/notify/test"', test)
        for key in ("notify_url", "notify_type", "notify_link"):
            self.assertIn(f'{key}:v("{key}")', test)                                         # the values on the form, saved or not
        self.assertIn("Not delivered:", test)
        self.assertIn("e.errors", test)                                                      # a refused value is shown at its field
        self.assertIn("[data-notify-test]", self.JS)

    def test_the_last_delivery_is_shown_in_words_and_follows_the_status(self):
        words = self.body("function notifyLastText", "// \"Send test\"")
        for needle in ("No message has been sent yet.", "Last delivery: ok", "Last delivery: failed"):
            self.assertIn(needle, words)
        self.assertIn("notifyLastText(s.notify_last)", self.body("async function poll", "// ---- the Dashboard"))

    def test_a_destination_that_fails_is_a_row_in_needs_attention_with_a_way_to_the_setting(self):
        row = self.body("const notifyRow", "function renderAttention")
        self.assertIn("Notifications can't be delivered", row)
        self.assertIn('data-fa="goto" data-field="notify_url"', row)
        self.assertIn("notifyBad=s.notify_last&&s.notify_last.ok===false", self.JS)


if __name__ == "__main__":
    unittest.main()
