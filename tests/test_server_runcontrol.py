"""The web server's side of run control: stop after the current games / stop now (and taking the first back), cancelling one game,
what a run would do (for the confirmation), the schedule pause, and the new status fields. Needs fastapi and httpx (skipped
without them)."""
from __future__ import annotations

import json
import os
import re
import time
import unittest
from unittest import mock

from support import E, ServerCase, WEB_DIR, legacy_config

import ps5_runctl as RC  # noqa: E402

GIB = 2 ** 30


class RunningCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config(schedule={"enabled": True, "times": ["03:00"]}, compression_level=7))

    def run_active(self, status=None):
        """A run is "active": the lock names a live process (this one), and status.json is of a run in progress."""
        (self.state / "run.lock").write_text(str(os.getpid()))
        self.put_status(status or dict(builds=[dict(title_id="PPSA01234", name="A")], preparing=[dict(title_id="PPSA05678", name="B")],
                                       queue=[dict(title_id="PPSA09999", name="C", size=5)], results=[], run=dict(started=time.time(), phase="running")))

    def put_status(self, st):
        (self.state / "status.json").write_text(json.dumps(st))


class Stop(RunningCase):
    def test_nothing_to_stop_when_no_run_is_active(self):
        for call in (lambda: self.client.post("/api/stop"), lambda: self.client.delete("/api/stop")):
            r = call()
            self.assertEqual((r.status_code, r.json()["detail"]), (409, "no run is active"))
        self.assertFalse((self.state / "stop.request").exists())

    def test_the_default_is_the_old_behaviour_after_the_current_games(self):
        self.run_active()
        r = self.client.post("/api/stop")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "mode": "after_current"}))
        self.assertEqual(RC.stop_mode(self.state), "after_current")
        self.assertEqual(self.get("/api/status")["stop_mode"], "after_current")
        self.assertIs(self.get("/api/status")["stop_requested"], True)

    def test_stop_now_and_it_is_never_weakened(self):
        self.run_active()
        self.assertEqual(self.client.post("/api/stop", json={"mode": "now"}).json()["mode"], "now")
        self.assertEqual(self.client.post("/api/stop", json={"mode": "after_current"}).json()["mode"], "now")
        self.assertEqual(self.get("/api/status")["stop_mode"], "now")

    def test_a_bad_mode_is_refused(self):
        self.run_active()
        for body in ({"mode": "later"}, {"mode": 1}, [1], "now"):
            r = self.client.post("/api/stop", json=body)
            self.assertEqual(r.status_code, 400, body)
        self.assertFalse((self.state / "stop.request").exists())

    def test_an_after_current_request_can_be_withdrawn_but_stop_now_cannot(self):
        self.run_active()
        self.assertEqual(self.client.delete("/api/stop").status_code, 409)                  # nothing was asked
        self.client.post("/api/stop")
        r = self.client.delete("/api/stop")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertIsNone(self.get("/api/status")["stop_mode"])
        self.client.post("/api/stop", json={"mode": "now"})
        r = self.client.delete("/api/stop")
        self.assertEqual(r.status_code, 409)
        self.assertIn("already under way", r.json()["detail"])
        self.assertEqual(RC.stop_mode(self.state), "now")


class Cancel(RunningCase):
    def test_a_game_in_the_run_is_asked_to_stop(self):
        self.run_active()
        for tid in ("PPSA01234", "PPSA05678", "PPSA09999"):                                  # building, preparing, queued: the same call
            r = self.client.post(f"/api/builds/{tid}/cancel")
            self.assertEqual((r.status_code, r.json()), (200, {"ok": True}), tid)
        self.assertEqual(RC.pending_cancels(self.state), ["PPSA01234", "PPSA05678", "PPSA09999"])
        self.assertEqual(self.get("/api/status")["cancelling"], ["PPSA01234", "PPSA05678", "PPSA09999"])

    def test_the_engines_own_list_is_shown_until_the_game_is_out_of_the_run(self):
        self.run_active(dict(builds=[dict(title_id="PPSA01234")], queue=[], preparing=[], results=[], cancelling=["PPSA01234"], run=dict(started=1)))
        self.assertEqual(self.get("/api/status")["cancelling"], ["PPSA01234"])
        (self.state / "run.lock").unlink()
        self.assertEqual(self.get("/api/status")["cancelling"], [])                         # no run: nothing to show

    def test_nothing_without_a_run_a_title_id_or_a_game_in_the_run(self):
        r = self.client.post("/api/builds/PPSA01234/cancel")
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "no run is active"))
        self.run_active()
        self.assertEqual(self.client.post("/api/builds/not-an-id/cancel").status_code, 400)
        self.assertEqual(self.client.post("/api/builds/PPSA00000/cancel").status_code, 404)  # not in this run
        self.assertEqual(RC.pending_cancels(self.state), [])
        self.put_status(dict(finished=True, results=[], run=dict(finished=1)))               # a finished run's status is not this run
        self.assertEqual(self.client.post("/api/builds/PPSA01234/cancel").status_code, 404)
        for path in ("/api/builds/PPSA01234%2F..%2F..%2Fx/cancel", "/api/builds/../cancel"):
            self.assertNotEqual(self.client.post(path).status_code, 200)
        self.assertEqual(RC.pending_cancels(self.state), [])

    def test_a_queued_ps4_title_can_be_taken_out_too(self):
        self.run_active(dict(builds=[], preparing=[], queue=[dict(title_id="CUSA00001", name="Old", ps4=True)], results=[], run=dict(started=1)))
        self.assertEqual(self.client.post("/api/builds/CUSA00001/cancel").status_code, 200)

    def test_a_cancelled_result_is_counted_in_the_last_run(self):
        self.put_status(dict(finished=True, updated=1, run=dict(started=1, finished=2, trigger="manual", total=3, done=3, built=1, copied=0, failed=0,
                                                                  deferred=0, cancelled=2), results=[]))
        self.assertEqual(self.get("/api/status")["last_run"]["cancelled"], 2)
        self.put_status(dict(finished=True, updated=1, results=[dict(status="cancelled"), dict(status="built")]))     # a status from before runs were described
        lr = self.get("/api/status")["last_run"]
        self.assertEqual((lr["cancelled"], lr["built"]), (1, 1))


class Preview(RunningCase):
    def plan(self, *entries):
        (self.state / "plan.json").write_text(json.dumps(list(entries)))

    def entry(self, tid, size, action="build", **kw):
        return dict(title_id=tid, title=tid, name=tid, size=size, action=action, why="new", source="/x", kind="folder", version="01", fw="9.00", candidates=[], **kw)

    def history(self, n=3):
        (self.state / "ledger.json").write_text(json.dumps({
            f"PPSA0000{i}": dict(version="01", output="/o", source_bytes=100 * GIB, level=7, prep_min=10, built_ts=time.time(),
                                 stage_secs={"1": 3600, "2": 10, "3": 300, "4": 10, "5": 100, "validate": 100, "copy": 50}) for i in range(n)}))

    def test_counts_and_sizes_of_what_a_run_would_do(self):
        self.plan(self.entry("PPSA00010", 50 * GIB), self.entry("PPSA00011", 150 * GIB), self.entry("PPSA00012", 9 * GIB, action="done"),
                  self.entry("PPSA00013", 7 * GIB, action="skip"), self.entry("CUSA00001", 20 * GIB, action="copy", ps4=True))
        t = self.get("/api/status")["todo"]
        self.assertEqual((t["builds"], t["copies"], t["bytes"]), (2, 1, 220 * GIB))
        self.assertIsNone(t["seconds"])                                                     # no build has finished here: no estimate, sizes only

    def test_an_estimate_from_what_earlier_builds_took_here_follows_the_pipeline(self):
        self.history()
        self.plan(self.entry("PPSA00010", 100 * GIB), self.entry("PPSA00011", 100 * GIB), self.entry("PPSA00012", 100 * GIB))
        one = self.srv.run_preview(self.srv.load_cfg(), [self.entry("PPSA00010", 100 * GIB)])["seconds"]
        three = self.get("/api/status")["todo"]["seconds"]
        self.assertGreater(one, 3600)
        self.assertGreater(three, one)                                                      # more games take longer...
        self.assertLess(three, 3 * one)                                                     # ...but they overlap (3 games at once)
        self.assertEqual(self.srv.run_preview(self.srv.load_cfg(), [])["seconds"], None)

    def test_ps4_copies_without_a_known_pace_leave_the_estimate_out_rather_than_guess(self):
        self.history()
        t = self.srv.run_preview(self.srv.load_cfg(), [self.entry("PPSA00010", 100 * GIB), self.entry("CUSA00001", 5 * GIB, action="copy", ps4=True)])
        self.assertIsNone(t["seconds"])
        self.assertEqual((t["builds"], t["copies"]), (1, 1))

    def test_nothing_planned(self):
        self.assertEqual(self.get("/api/status")["todo"], dict(builds=0, copies=0, bytes=0, seconds=None))


class SchedulePause(RunningCase):
    AT_3 = time.strptime("2030-01-01 03:00", "%Y-%m-%d %H:%M")

    def test_pause_for_ever_and_resume(self):
        r = self.client.post("/api/schedule/pause", json={})
        self.assertEqual((r.status_code, r.json()["pause"]["until"]), (200, None))
        s = self.get("/api/status")
        self.assertEqual((s["schedule_pause"]["until"], s["next_run"]), (None, None))       # the next run is not announced while paused
        self.assertEqual(self.client.delete("/api/schedule/pause").status_code, 200)
        s = self.get("/api/status")
        self.assertIsNone(s["schedule_pause"])
        self.assertIsNotNone(s["next_run"])
        self.assertEqual(self.client.delete("/api/schedule/pause").status_code, 200)        # resuming twice is fine

    def test_pause_until_a_time_on_the_servers_clock(self):
        until = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 3 * 86400))
        r = self.client.post("/api/schedule/pause", json={"until": until})
        self.assertEqual(r.status_code, 200, r.text)
        want = time.mktime(time.strptime(until, "%Y-%m-%dT%H:%M"))
        self.assertEqual(self.get("/api/status")["schedule_pause"]["until"], want)
        r = self.client.post("/api/schedule/pause", json={"until": want + 1000})              # epoch seconds work too
        self.assertEqual(r.json()["pause"]["until"], want + 1000)

    def test_a_time_in_the_past_or_nonsense_is_refused_and_changes_nothing(self):
        self.client.post("/api/schedule/pause", json={})
        for body in ({"until": "2001-01-01T08:00"}, {"until": "tomorrow"}, {"until": True}, {"until": [1]}, [1]):
            r = self.client.post("/api/schedule/pause", json=body)
            self.assertEqual(r.status_code, 400, body)
        self.assertEqual(self.get("/api/status")["schedule_pause"]["until"], None)            # the earlier pause stands
        self.assertIn("already passed", self.client.post("/api/schedule/pause", json={"until": "2001-01-01T08:00"}).json()["detail"])

    def test_a_pause_that_has_ended_is_over(self):
        RC.pause_write(self.state, time.time() + 1)
        with mock.patch.object(RC.time, "time", return_value=time.time() + 5):
            self.assertIsNone(self.get("/api/status")["schedule_pause"])

    def test_a_paused_schedule_starts_nothing_and_says_so_and_does_not_make_the_run_up_later(self):
        RC.pause_write(self.state, None)
        fired = set()
        with mock.patch.object(self.srv, "start_engine", return_value=True) as start:
            self.srv.scheduler_tick(fired, self.AT_3)
            self.assertEqual(start.call_count, 0)
            self.assertIn("scheduled run 03:00: skipped (the schedule is paused)", (self.logs / "pkg_sync.log").read_text())
            RC.pause_clear(self.state)
            self.srv.scheduler_tick(fired, self.AT_3)                                          # resumed in the same minute: the slot is spent
            self.assertEqual(start.call_count, 0)
            self.srv.scheduler_tick(fired, time.strptime("2030-01-02 03:00", "%Y-%m-%d %H:%M"))
            self.assertEqual(start.call_count, 1)                                              # the next day it runs as usual

    def test_a_pause_with_an_end_lets_the_runs_after_it_start(self):
        end = time.mktime(time.strptime("2030-01-01 12:00", "%Y-%m-%d %H:%M"))
        RC.pause_write(self.state, end, now=end - 3600)
        with mock.patch.object(self.srv, "start_engine", return_value=True) as start:
            self.srv.scheduler_tick(set(), self.AT_3)
            self.assertEqual(start.call_count, 0)
            self.srv.scheduler_tick(set(), time.strptime("2030-01-02 03:00", "%Y-%m-%d %H:%M"))
            self.assertEqual(start.call_count, 1)                                              # the pause is over by then


class Page(unittest.TestCase):
    """The page (no browser here: the interactions were checked in a real one)."""
    JS = (WEB_DIR / "static" / "app.js").read_text()
    HTML = (WEB_DIR / "static" / "index.html").read_text()

    def body(self, name, until):
        start = self.JS.index(name)
        return self.JS[start:self.JS.index(until, start)]

    def test_no_browser_dialog_is_used_anywhere(self):
        self.assertEqual(re.findall(r"(?<![\w.])(?:confirm|alert|prompt)\(", self.JS), [])
        self.assertIn("function askConfirm(", self.JS)
        self.assertIn("function showDialog(", self.JS)
        self.assertIn('role="dialog"', self.HTML)
        self.assertIn('aria-modal="true"', self.HTML)
        self.assertIn('"Escape"', self.body("function showDialog", "let dlgDone"))                 # it can be dismissed from the keyboard

    def test_every_question_the_page_asks_is_asked_in_the_page(self):
        self.assertIn("askConfirm(", self.body("async function buildOne", "function alertBox"))
        self.assertIn("askConfirm(", self.body("async function cancelGame", "document.addEventListener"))
        self.assertIn("showDialog(", self.body("async function startRun", '$("#bRun")'))
        self.assertIn("showDialog(", self.body("async function stopDialog", "async function doStop"))
        for title in ("Discard your changes?", "Restore this backup?", "Remove this override?"):
            self.assertIn(f'askConfirm({{title:"{title}"', self.JS)

    def test_run_now_asks_first_with_the_size_the_time_and_the_room_and_can_be_silenced_in_this_browser(self):
        run = self.body("async function startRun", '$("#bRun")')
        self.assertLess(run.index("showDialog("), run.index('api("/api/run"'))                       # asked before anything starts
        self.assertLess(run.index('if(!r||r.key!=="go")return;'), run.index('api("/api/run"'))       # and only "Start run" goes on
        for needle in ("s.todo", "of sources", "Free on the work drive", "dontAskRun()", "Don't ask me again"):
            self.assertIn(needle, run)
        ask = self.body("const dontAskRun", "\n")
        self.assertIn("localStorage", ask)
        self.assertIn("try", ask)                                                                  # a browser without storage still works
        self.assertIn("data-askagain", self.HTML)                                                  # and the question can be turned back on

    def test_stop_offers_both_choices_and_stop_now_says_what_is_lost_and_asks_again(self):
        body = self.body("async function stopDialog", "async function doStop")
        self.assertIn("Finish current games, then stop", body)
        self.assertIn("Stop now", body)
        self.assertIn("Resume: carry on with the queue", body)
        self.assertIn("will be discarded", body)
        self.assertIn("kept for next time", body)
        self.assertLess(body.index("Stop now…"), body.index("askConfirm("))
        self.assertIn('r.key==="now"&&!(await askConfirm({title:"Stop now?"', body)                 # "Stop now" is asked about a second time
        self.assertIn('"/api/stop"', self.body("async function doStop", "\n$"))
        self.assertIn('method:"DELETE"', self.body("async function doStop", "\n$"))                   # Resume takes the request back

    def test_building_and_preparing_rows_have_cancel_and_queued_rows_remove_from_this_run(self):
        self.assertIn("cancelBtn(x,canc,\"building\")", self.body("function buildRow", "function prepRow"))
        self.assertIn("cancelBtn(x,canc,\"preparing\")", self.body("function prepRow", "let resOpen"))
        self.assertIn("cancelBtn(x,canc,\"queue\",\"Remove from this run\")", self.JS)
        self.assertIn('data-cancel="${esc(x.title_id)}"', self.body("const cancelBtn", "function buildRow"))
        cancel = self.body("async function cancelGame", "document.addEventListener")
        self.assertIn('phase!=="queue"&&!(await askConfirm(', cancel)                               # a build or a prepare is only discarded after asking
        self.assertIn("/cancel", cancel)
        self.assertLess(cancel.index("askConfirm("), cancel.index('api(`/api/builds'))                  # a build is only discarded after asking

    def test_buttons_say_why_they_are_disabled(self):
        why = self.body("function whyNot", "function syncRun")
        for text in ("A run is active.", "A scan is running.", "No run is active.", "FAIL items under Setup"):
            self.assertIn(text, why)
        sync = self.body("function syncRun", "// the schedule can be paused")
        self.assertIn(".title=", sync)
        self.assertIn("Run now and Scan are off while a run is active.", sync)
        self.assertIn("Run now and Scan are off while a scan is running.", sync)

    def test_the_header_and_the_run_line_show_a_paused_schedule_and_a_stop_in_progress(self):
        head = self.body("function headerText", "function renderRun")
        self.assertIn("schedule ${pauseLabel(", head)
        self.assertIn("Stopping now", head)
        self.assertIn("Schedule ${esc(pauseLabel(", self.body("function renderRun", "const cancelBtn"))
        for needle in ('id="bPause"', 'id="pauseUntil"', 'type="datetime-local"'):
            self.assertIn(needle, self.HTML)
        self.assertIn('"/api/schedule/pause"', self.JS)
        self.assertIn("Resume schedule", self.JS)

    def test_a_cancelled_game_has_its_own_tag_and_count(self):
        self.assertIn(".t-cancelled", self.HTML)
        self.assertIn("cancelled`", self.body("const countsText", "\n"))


if __name__ == "__main__":
    unittest.main()
