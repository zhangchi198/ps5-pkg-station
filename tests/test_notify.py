"""Notifications, the engine's side (app/engine/ps5_notify.py and its use in the engine): which messages the settings want, what an ntfy message
and a webhook message look like on the wire, what a failed delivery says (never the URL), the run summary, and the things that need a person.
Standard library only; the messages go to a small HTTP server in the test."""
from __future__ import annotations

import copy
import email.header
import http.server
import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import E, StateCase

import ps5_failures as F  # noqa: E402
import ps5_notify as PN  # noqa: E402
import test_engine_failures as TEF  # noqa: E402

GIB = 2 ** 30
SECRET = "k7Qz9-private-topic-token"


class Sink(http.server.BaseHTTPRequestHandler):
    """Remembers every POST; answers with the code (and after the delay) its server was given."""

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.got.append(dict(path=self.path, headers={k: v for k, v in self.headers.items()}, body=body))
        time.sleep(self.server.delay)
        self.send_response(self.server.code)
        self.end_headers()

    def log_message(self, *a):
        pass


def serve(case, code=200, delay=0.0):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    srv.got, srv.code, srv.delay = [], code, delay
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    case.addCleanup(srv.server_close)
    case.addCleanup(srv.shutdown)
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}/{SECRET}?token=abcdef123456"
    return srv


class Wants(unittest.TestCase):
    def test_a_new_install_sends_the_summary_and_what_needs_attention_and_not_every_game(self):
        cfg = copy.deepcopy(E.DEFAULT_CFG)
        got = {e: PN.wants(cfg, e) for e in ("summary", "attention", "built", "copied", "failed", "test")}
        self.assertEqual(got, dict(summary=True, attention=True, built=False, copied=False, failed=True, test=True))

    def test_each_toggle_turns_its_own_messages_off(self):
        cfg = {"notify_summary": False, "notify_attention": False, "notify_each_game": False}
        self.assertEqual([PN.wants(cfg, e) for e in ("summary", "attention", "built", "failed")], [False] * 4)
        self.assertTrue(PN.wants(cfg, "test"))                                              # the test button always sends
        self.assertTrue(PN.wants({**cfg, "notify_each_game": True}, "built"))
        self.assertTrue(PN.wants({**cfg, "notify_each_game": True}, "failed"))              # every finished game includes the ones that failed
        self.assertTrue(PN.wants({**cfg, "notify_attention": True}, "failed"))
        self.assertFalse(PN.wants({**cfg, "notify_attention": True}, "built"))

    def test_a_config_from_before_the_toggles_keeps_a_message_per_game(self):
        self.assertTrue(PN.wants({"notify_url": "https://ntfy.example/t"}, "built"))
        self.assertFalse(PN.wants({}, "built"))
        cfg, _ = E.validate_cfg({"notify_url": "https://ntfy.example/t"})
        self.assertIs(cfg["notify_each_game"], True)
        cfg, _ = E.validate_cfg({"notify_url": "https://ntfy.example/t", "notify_each_game": False})
        self.assertIs(cfg["notify_each_game"], False)                                        # once someone chose, that stands
        cfg, _ = E.validate_cfg({})
        self.assertIs(cfg["notify_each_game"], False)

    def test_the_settings_are_validated(self):
        self.assertEqual(E.validate_setting("notify_type", "webhook"), "webhook")
        self.assertEqual(E.validate_setting("notify_link", " http://nas.local:8765 "), "http://nas.local:8765")
        self.assertEqual(E.validate_setting("notify_link", ""), "")
        for key, bad in (("notify_type", "gotify"), ("notify_link", "ftp://x"), ("notify_link", "nas.local"), ("notify_summary", "yes"), ("notify_each_game", 1)):
            with self.assertRaises(ValueError, msg=(key, bad)):
                E.validate_setting(key, bad)


class Redacting(unittest.TestCase):
    URL = f"https://ntfy.example.net/{SECRET}?token=abcdef123456&user=me"

    def test_the_url_its_topic_and_its_token_never_survive(self):
        for text in (f"Connection to {self.URL} failed", f"HTTP 403 for {self.URL}", f"bad {SECRET} here", "url was https://other.example/hook?key=zzz999 ok",
                     "retry at /api?access_token=hunter2hunter2&x=1"):
            out = PN.redact(text, self.URL)
            for secret in (SECRET, "abcdef123456", "hunter2hunter2", "zzz999", "ntfy.example.net/", "https://"):
                self.assertNotIn(secret, out, (text, out))

    def test_ordinary_words_are_left_alone(self):
        self.assertEqual(PN.redact("HTTP 403 Forbidden", self.URL), "HTTP 403 Forbidden")
        self.assertEqual(PN.redact("timed out", ""), "timed out")
        self.assertEqual(PN.redact("Connection refused", self.URL), "Connection refused")

    def test_a_password_in_the_address_goes_too(self):
        url = "https://user:s3cr3tpass@host.example/t"
        self.assertNotIn("s3cr3tpass", PN.redact(f"failed for {url}", url))


    def test_a_bad_address_in_the_settings_file_is_not_quoted_back_in_the_problems(self):
        cfg, problems = E.validate_cfg({"notify_url": f"ntfy.sh/{SECRET}", "notify_link": "nas-without-scheme"})
        self.assertEqual(cfg["notify_url"], "")
        self.assertTrue(problems)
        self.assertNotIn(SECRET, " ".join(problems))
        self.assertTrue(any("notify_link" in x and "nas-without-scheme" in x for x in problems))      # the link back is not a secret: it is shown


class OnTheWire(unittest.TestCase):
    CFG = {"notify_url": "http://x.example/t", "notify_link": "http://nas.local:8765"}

    def test_ntfy_gets_the_text_with_a_title_a_priority_tags_and_the_link(self):
        h, body = PN.build(self.CFG, "attention", "PS5 pkg: run did not start", "The library folder is not available.")
        self.assertEqual((h["Title"], h["Priority"], h["Tags"], h["Click"]), ("PS5 pkg: run did not start", "high", "warning", "http://nas.local:8765"))
        self.assertEqual(body, b"The library folder is not available.")
        self.assertEqual(h["Content-Type"], "text/plain; charset=utf-8")

    def test_priorities_and_tags_follow_the_event_and_the_link_is_optional(self):
        got = {e: PN.build({"notify_url": "http://x/t"}, e, "t", "m")[0] for e in ("summary", "built", "copied", "failed", "attention", "test")}
        self.assertEqual({e: h["Priority"] for e, h in got.items()}, dict(summary="default", built="default", copied="default", failed="high", attention="high", test="default"))
        self.assertTrue(all(h["Tags"] for h in got.values()))
        self.assertTrue(all("Click" not in h for h in got.values()))

    def test_a_game_title_in_any_language_arrives_intact_in_the_body_and_the_title(self):
        title = "Pokémon Écarlate 红色 ゲーム"
        h, body = PN.build(self.CFG, "built", "PS5 pkg: " + title, f"{title} [PPSA01234].pkg")
        self.assertEqual(body.decode("utf-8"), f"{title} [PPSA01234].pkg")
        part, charset = email.header.decode_header(h["Title"])[0]
        self.assertEqual(part.decode(charset), "PS5 pkg: " + title)                         # RFC 2047, what ntfy reads
        self.assertTrue(h["Title"].isascii())                                               # a header the HTTP library accepts
        self.assertEqual(PN.build(self.CFG, "built", "PS5 pkg: built", "x")[0]["Title"], "PS5 pkg: built")      # as it always was

    def test_a_header_is_one_line(self):
        self.assertEqual(PN.header_text("a\r\nInjected: yes"), "a Injected: yes")

    def test_a_webhook_gets_json_the_same_for_every_event(self):
        games = [dict(title_id="PPSA01234", title="Pokémon", status="built")]
        h, body = PN.build({**self.CFG, "notify_type": "webhook"}, "summary", "PS5 pkg: run finished", "Run finished: 1 built.", games=games,
                           run=dict(counts={"built": 1}, seconds=60), now=1_795_000_000)
        self.assertEqual(h, {"Content-Type": "application/json; charset=utf-8"})
        doc = json.loads(body.decode("utf-8"))
        self.assertEqual(set(doc), {"schema", "event", "title", "message", "priority", "url", "time", "games", "run"})
        self.assertEqual((doc["schema"], doc["event"], doc["title"], doc["message"], doc["priority"], doc["url"]),
                         (1, "summary", "PS5 pkg: run finished", "Run finished: 1 built.", "default", "http://nas.local:8765"))
        self.assertEqual(doc["games"], games)
        self.assertRegex(doc["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4}$")
        self.assertIn("Pokémon".encode("utf-8"), body)                                      # not \u00e9 escapes: the text itself
        h, body = PN.build({"notify_url": "http://x/t", "notify_type": "webhook"}, "test", "t", "m")
        doc = json.loads(body)
        self.assertEqual((doc["url"], doc["games"], "run" in doc), (None, [], False))


class Delivering(StateCase):
    def cfg(self, srv, **kw):
        return {**copy.deepcopy(E.DEFAULT_CFG), "notify_url": srv.url, "notify_link": "http://nas.local:8765", **kw}

    def test_an_ntfy_message_reaches_the_server_as_the_text_with_its_headers(self):
        srv = serve(self)
        res = PN.send(self.cfg(srv), "attention", "PS5 pkg: waiting for room", "Waiting for room: Example Game.", state=self.state)
        self.assertEqual((res["ok"], res["status"], res["error"]), (True, 200, None))
        (got,) = srv.got
        self.assertEqual(got["path"], f"/{SECRET}?token=abcdef123456")                       # the address as given
        self.assertEqual((got["headers"]["Title"], got["headers"]["Priority"], got["headers"]["Click"]), ("PS5 pkg: waiting for room", "high", "http://nas.local:8765"))
        self.assertEqual(got["body"], b"Waiting for room: Example Game.")

    def test_a_webhook_message_is_valid_json_with_the_right_content_type(self):
        srv = serve(self)
        PN.send(self.cfg(srv, notify_type="webhook", notify_each_game=True), "built", "PS5 pkg: built", "Pokémon Écarlate [PPSA01234].pkg", state=self.state, games=[dict(title_id="PPSA01234", status="built")])
        (got,) = srv.got
        self.assertEqual(got["headers"]["Content-Type"], "application/json; charset=utf-8")
        doc = json.loads(got["body"].decode("utf-8"))
        self.assertEqual((doc["event"], doc["message"], doc["games"][0]["title_id"]), ("built", "Pokémon Écarlate [PPSA01234].pkg", "PPSA01234"))

    def test_a_game_title_in_any_language_arrives_intact_by_either_type(self):
        title = "Pokémon Écarlate 红色 ゲーム"
        for kind in ("ntfy", "webhook"):
            srv = serve(self)
            PN.send(self.cfg(srv, notify_type=kind, notify_each_game=True), "built", "PS5 pkg: built", f"{title} [PPSA01234].pkg", state=self.state,
                    games=[dict(title_id="PPSA01234", title=title, status="built")])
            (got,) = srv.got
            if kind == "ntfy":
                self.assertEqual(got["body"].decode("utf-8"), f"{title} [PPSA01234].pkg")
            else:
                doc = json.loads(got["body"].decode("utf-8"))
                self.assertEqual((doc["message"], doc["games"][0]["title"]), (f"{title} [PPSA01234].pkg", title))

    def test_the_outcome_is_kept_and_a_failure_says_why_in_words_and_never_has_the_url(self):
        srv = serve(self, code=403)
        lines = []
        res = PN.send(self.cfg(srv), "summary", "PS5 pkg: run finished", "Run finished: 1 built.", state=self.state, log=lines.append)
        self.assertEqual((res["ok"], res["status"], res["error"]), (False, 403, "HTTP 403 Forbidden"))
        last = PN.read_last(self.state)
        self.assertEqual((last["ok"], last["status"], last["error"], last["event"], last["type"]), (False, 403, "HTTP 403 Forbidden", "summary", "ntfy"))
        self.assertAlmostEqual(last["at"], time.time(), delta=5)
        self.assertEqual(lines, ["notification summary (ntfy): NOT delivered: HTTP 403 Forbidden"])
        self.assertEqual(PN.send(self.cfg(serve(self)), "summary", "t", "m", state=self.state).get("ok"), True)
        self.assertTrue(PN.read_last(self.state)["ok"])                                      # the latest delivery is the one kept

    def test_a_server_that_is_not_there_and_one_that_is_too_slow_are_errors_in_words(self):
        srv = serve(self)
        cfg = self.cfg(srv)
        srv.shutdown()
        srv.server_close()
        res = PN.send(cfg, "summary", "t", "m", state=self.state, log=lambda m: None)
        self.assertFalse(res["ok"])
        self.assertIsNone(res["status"])
        self.assertRegex(res["error"], r"(?i)refused|connect")
        slow = serve(self, delay=2)
        with mock.patch.object(PN, "TIMEOUT", 0.3):
            res = PN.send(self.cfg(slow), "summary", "t", "m", state=self.state)
        self.assertEqual((res["ok"], res["error"]), (False, "timed out"))

    def test_no_error_text_has_the_address_or_its_secret_in_it_and_neither_has_the_log(self):
        for code in (400, 403, 404, 500):
            srv = serve(self, code=code)
            lines = []
            res = PN.send(self.cfg(srv), "summary", "t", "m", state=self.state, log=lines.append)
            blob = json.dumps(res) + "".join(lines) + (self.state / "notify_last.json").read_text()
            for secret in (SECRET, "abcdef123456", "127.0.0.1"):
                self.assertNotIn(secret, blob, code)
        res = PN.post("http://[::1", {}, b"")                                                # an address urllib itself rejects, with a message that quotes it
        self.assertNotIn("[::1", json.dumps(res))

    def test_nothing_is_sent_when_there_is_no_address_or_the_kind_is_off_and_the_test_always_is(self):
        srv = serve(self)
        self.assertIsNone(PN.send({**self.cfg(srv), "notify_url": ""}, "summary", "t", "m", state=self.state))
        self.assertIsNone(PN.send({**self.cfg(srv), "notify_url": "file:///etc/passwd"}, "summary", "t", "m", state=self.state))
        self.assertIsNone(PN.send(self.cfg(srv, notify_summary=False), "summary", "t", "m", state=self.state))
        self.assertEqual(srv.got, [])
        self.assertIsNone(PN.read_last(self.state))
        self.assertTrue(PN.send(self.cfg(srv, notify_summary=False, notify_attention=False), "test", "t", "m", state=self.state, record=False)["ok"])
        self.assertIsNone(PN.read_last(self.state))                                          # a test is not "the last delivery"

    def test_a_condition_that_lasts_is_said_once_and_again_after_it_cleared(self):
        self.assertEqual([PN.once(self.state, "low_output", v) for v in (True, True, True, False, False, True, True)], [True, False, False, False, False, True, False])
        self.assertTrue(PN.once(self.state, "other", True))                                  # each condition has its own memory


class Words(unittest.TestCase):
    def results(self):
        return [dict(title_id="PPSA00001", title="Example Game", name="Example Game.pkg", status="built", build_secs=3600, bytes=5 * GIB, output="/output/x.pkg"),
                dict(title_id="PPSA00002", title="Pokémon Écarlate", status="built"), dict(title_id="PPSA00003", title="Third", status="built"),
                dict(title_id="PPSA00004", title="Fourth", status="built"),
                dict(title_id="PPSA00005", title="Bad Game", status="failed", code="crc_mismatch", why="1 extracted file(s) differ from the archive CRCs",
                     data={"files": ["sce_sys/param.json"], "count": 1}),
                dict(title_id="PPSA00006", title="Dropped", status="cancelled", why="cancelled while building"),
                dict(title_id="PPSA00007", title="Waiting", status="deferred", code="no_space", why="needs room", data={"need": 5 * GIB, "usable": 1 * GIB})]

    def test_the_summary_in_one_line(self):
        text, rows, counts = PN.summary_text(self.results(), 3 * 3600 + 12 * 60, free=int(1.3 * 2**40))
        why = F.short_reason(dict(code="crc_mismatch", data={"files": ["sce_sys/param.json"], "count": 1}, message="x"))
        self.assertIn("sce_sys/param.json", why)                                            # the words of #6's failure explanations
        self.assertEqual(text, f"Run finished: 4 built, 1 failed (Bad Game: {why}), 1 cancelled, 1 waiting for space. 3 h 12 min. 1.3 TiB free in the output folder.")
        self.assertEqual(counts, dict(built=4, copied=0, failed=1, cancelled=1, deferred=1))
        self.assertEqual(rows[0], dict(title_id="PPSA00001", title="Example Game", name="Example Game.pkg", status="built", output="/output/x.pkg", bytes=5 * GIB, seconds=3600))

    def test_several_failures_are_named_up_to_three(self):
        res = [dict(title_id=f"PPSA0000{i}", title=f"Game {i}", status="failed", code="unknown", why="boom") for i in range(1, 6)]
        text, _, _ = PN.summary_text(res, 60)
        self.assertIn("5 failed (Game 1: ", text)
        self.assertIn("; and 2 more)", text)
        self.assertNotIn("Game 4", text)

    def test_a_run_of_copies_and_a_short_run(self):
        text, _, _ = PN.summary_text([dict(title_id="CUSA00001", title="Old", status="copied")], 45)
        self.assertEqual(text, "Run finished: 1 copied. 45 s.")
        self.assertEqual(PN.summary_text([], 5)[0], "Run finished: nothing to do. 5 s.")

    def test_durations_and_sizes(self):
        self.assertEqual([PN.duration(s) for s in (5, 89, 90, 3600, 3660, 11520)], ["5 s", "89 s", "2 min", "1 h", "1 h 01 min", "3 h 12 min"])
        self.assertEqual([PN.size_text(b) for b in (5 * GIB, 50 * GIB, int(1.5 * 2**40))], ["5.0 GiB", "50 GiB", "1.5 TiB"])

    def test_a_folder_is_low_when_the_biggest_game_would_not_fit_or_under_a_floor(self):
        cfg = {"ssd_reserve_gb": 80}
        ok = PN.space_findings(cfg, 500 * GIB, 900 * GIB, 100 * GIB)
        self.assertEqual(ok, dict(output=None, work=None))
        low = PN.space_findings(cfg, 60 * GIB, 900 * GIB, 100 * GIB)
        self.assertIn("output folder is nearly full: 60 GiB free, and the biggest game still to build is 100 GiB", low["output"])
        self.assertIsNone(low["work"])
        low = PN.space_findings(cfg, 500 * GIB, 150 * GIB, 100 * GIB)                        # 150 free - 80 kept < 110 needed
        self.assertIn("work drive is nearly full", low["work"])
        self.assertIsNone(PN.space_findings(cfg, 11 * GIB, 900 * GIB, 0)["output"])           # nothing waiting: only the floor counts
        self.assertIsNotNone(PN.space_findings(cfg, 9 * GIB, 900 * GIB, 0)["output"])
        self.assertEqual(PN.space_findings(cfg, None, None, 100 * GIB), dict(output=None, work=None))


class EngineEvents(TEF.EngineCase):
    """What the engine's own steps send, with the messages going to a real (local) server."""

    def setUp(self):
        super().setUp()
        self.srv = serve(self)
        self.cfg.update(notify_url=self.srv.url, notify_each_game=False)

    def titles(self):
        return [g["headers"].get("Title") for g in self.srv.got]

    def test_a_run_that_did_nothing_says_nothing_and_one_that_did_says_it_once(self):
        E.notify_run_end(self.cfg, [], time.time() - 5, [], "scheduled")
        self.assertEqual(self.srv.got, [])
        E.notify_run_end(self.cfg, [dict(title_id="PPSA01234", title="Example Game", status="built")] * 5, time.time() - 60, [], "scheduled")
        self.assertEqual(self.titles(), ["PS5 pkg: run finished"])                            # five games, one message
        self.assertTrue(self.srv.got[0]["body"].decode().startswith("Run finished: 5 built."))

    def test_only_the_summary_on_means_one_message_for_a_run_of_many(self):
        self.cfg.update(notify_attention=False)
        res = [dict(title_id=f"PPSA0000{i}", title=f"Game {i}", status="built", at=i) for i in range(1, 6)]
        with mock.patch.object(E.shutil, "disk_usage", return_value=mock.Mock(free=2**40)):
            for r in res:
                E.notify_game(self.cfg, "PS5 pkg: built", "x.pkg", r)                        # what finish_build does per game
            E.notify_run_end(self.cfg, res, time.time() - 600, [], "manual")
        self.assertEqual(self.titles(), ["PS5 pkg: run finished"])

    def test_every_finished_game_is_a_message_only_when_asked(self):
        self.cfg.update(notify_each_game=True)
        E.notify_game(self.cfg, "PS5 pkg: built", "Example.pkg", dict(title_id="PPSA01234", title="Example Game", status="built"))
        E.notify_game(self.cfg, "PS4: copied", "Old [CUSA00001]", dict(title_id="CUSA00001", title="Old", status="copied"))
        self.assertEqual(self.titles(), ["PS5 pkg: built", "PS4: copied"])

    def test_a_webhook_lists_the_game_a_message_is_about(self):
        self.cfg.update(notify_each_game=True, notify_type="webhook")
        E.notify_game(self.cfg, "PS5 pkg: built", "Pokémon.pkg", dict(title_id="PPSA01234", title="Pokémon", name="Pokémon.pkg", status="built", output="/output/x.pkg"))
        doc = json.loads(self.srv.got[0]["body"].decode("utf-8"))
        self.assertEqual((doc["event"], doc["games"]), ("built", [dict(title_id="PPSA01234", title="Pokémon", name="Pokémon.pkg", status="built", output="/output/x.pkg")]))

    def test_a_failure_is_high_priority_and_comes_with_only_attention_on(self):
        E.notify_game(self.cfg, "PS5 pkg: failed", "Bad Game.pkg: It differs", dict(title_id="PPSA01234", title="Bad Game", status="failed", code="crc_mismatch"))
        (g,) = self.srv.got
        self.assertEqual((g["headers"]["Title"], g["headers"]["Priority"]), ("PS5 pkg: failed", "high"))
        self.cfg.update(notify_attention=False)
        E.notify_game(self.cfg, "PS5 pkg: failed", "x", dict(title_id="PPSA01234", status="failed"))
        self.assertEqual(len(self.srv.got), 1)                                               # off: nothing more

    def test_a_folder_that_is_nearly_full_is_said_once_until_it_has_been_fixed(self):
        plan = [dict(title_id="PPSA01234", action="build", size=100 * GIB)]
        low, fine = mock.Mock(free=40 * GIB), mock.Mock(free=2**40)
        with mock.patch.object(E.shutil, "disk_usage", return_value=low):
            E.notify_run_end(self.cfg, [], time.time(), plan, "scheduled")
            E.notify_run_end(self.cfg, [], time.time(), plan, "scheduled")
        self.assertEqual(self.titles(), ["PS5 pkg: output folder nearly full", "PS5 pkg: work drive nearly full"])
        self.assertIn("40 GiB free", self.srv.got[0]["body"].decode())
        with mock.patch.object(E.shutil, "disk_usage", return_value=fine):
            E.notify_run_end(self.cfg, [], time.time(), plan, "scheduled")
        with mock.patch.object(E.shutil, "disk_usage", return_value=low):
            E.notify_run_end(self.cfg, [], time.time(), plan, "scheduled")
        self.assertEqual(len(self.srv.got), 4)                                               # low again after it was fine: told again

    def test_a_game_that_was_built_in_this_run_no_longer_counts_as_waiting_for_room(self):
        plan = [dict(title_id="PPSA01234", action="build", size=300 * GIB)]
        done = [dict(title_id="PPSA01234", title="Example Game", status="built")]
        with mock.patch.object(E.shutil, "disk_usage", return_value=mock.Mock(free=100 * GIB)):
            E.notify_run_end(self.cfg, done, time.time(), plan, "manual")
        self.assertEqual(self.titles(), ["PS5 pkg: run finished"])

    def test_a_game_waiting_for_room_is_told_once_not_every_night(self):
        def run():
            with mock.patch.object(E, "usable_ssd", return_value=1 * GIB), mock.patch.object(E.shutil, "disk_usage", return_value=mock.Mock(free=2**40)):
                return E.run_queue(self.cfg, [dict(self.plan(), size=300 * GIB)], lambda m: None, self.state / "ledger.json", {})
        self.assertEqual(run()[0]["status"], "deferred")
        waiting = [g for g in self.srv.got if g["headers"]["Title"] == "PS5 pkg: waiting for room"]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["headers"]["Priority"], "high")
        body = waiting[0]["body"].decode()
        self.assertIn("Example Game", body)
        self.assertIn("Needs about 330 GiB on the work drive; 1 GiB usable. Will be retried automatically.", body)      # the failure explanation's words
        run()                                                                                # the next night: still waiting, nothing new to say
        self.assertEqual(len([g for g in self.srv.got if g["headers"]["Title"] == "PS5 pkg: waiting for room"]), 1)

    def test_a_failing_destination_does_not_stop_anything_and_is_remembered(self):
        bad = serve(self, code=500)
        self.cfg["notify_url"] = bad.url
        E.notify_attention(self.cfg, "PS5 pkg: x", "y")
        E.notify_run_end(self.cfg, [dict(title_id="PPSA01234", title="G", status="built")], time.time(), [], "manual")
        self.assertFalse(PN.read_last(self.state)["ok"])
        log = (self.logs / "pkg_sync.log").read_text() if (self.logs / "pkg_sync.log").exists() else ""
        self.assertIn("NOT delivered: HTTP 500 Internal Server Error", log)
        self.assertNotIn(SECRET, log)


if __name__ == "__main__":
    unittest.main()
