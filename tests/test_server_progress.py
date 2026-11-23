"""The Dashboard's data: counts and the Library that follow a run, when the next scheduled run is, what the last
run did, and why a run was started. Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from support import ServerCase, legacy_config

NOW = time.time()


def plan_entry(tid, action="build", version="01.00", **kw):
    return dict(title_id=tid, title=f"Game {tid}", version=version, fw="9.00", source=f"/library/{tid}", kind="folder", size=5, action=action,
                why="new", candidates=[], **kw)


class ProgressCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config(overrides={}))

    def put(self, name, obj, age=None):
        p = self.state / name
        p.write_text(json.dumps(obj))
        if age is not None:
            os.utime(p, (NOW - age, NOW - age))

    def counts(self):
        return self.get("/api/status")["plan_counts"]


class LiveCounts(ProgressCase):
    def test_a_game_built_while_the_run_goes_is_done_in_the_next_poll_and_in_the_library(self):
        self.put("plan.json", [plan_entry("PPSA00001"), plan_entry("PPSA00002"), plan_entry("PPSA00003")], age=600)
        self.assertEqual((self.counts()["build"], self.counts()["done"]), (3, 0))
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output="/output/Game 1.pkg", built="2026-11-23 03:12", built_ts=NOW - 60)})
        c = self.counts()
        self.assertEqual((c["build"], c["done"]), (2, 1))                                  # the very next poll
        lib = {p["title_id"]: p for p in self.get("/api/plan")}
        self.assertEqual((lib["PPSA00001"]["action"], lib["PPSA00002"]["action"]), ("done", "build"))
        self.assertEqual(lib["PPSA00001"]["why"], "built 2026-11-23 03:12: Game 1.pkg")
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output="/output/a.pkg", built_ts=NOW - 60),
                                 "PPSA00002": dict(version="01.00", output="/output/b.pkg", built_ts=NOW - 30)})
        self.assertEqual((self.counts()["build"], self.counts()["done"]), (1, 2))

    def test_a_build_from_before_the_plan_does_not_make_a_newer_version_done(self):
        self.put("plan.json", [plan_entry("PPSA00001", version="01.10")], age=600)
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output="/output/a.pkg", built_ts=NOW - 86400)})
        self.assertEqual(self.counts()["build"], 1)
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output="/output/a.pkg", built_ts=NOW - 60)})      # after the plan, but an older version
        self.assertEqual((self.counts()["build"], self.counts()["done"]), (1, 0))
        self.put("ledger.json", {"PPSA00001": dict(version="01.10", output="/output/a.pkg", built_ts=NOW - 60)})
        self.assertEqual((self.counts()["build"], self.counts()["done"]), (0, 1))

    def test_an_entry_from_an_older_version_without_a_precise_time_goes_by_its_minute(self):
        self.put("plan.json", [plan_entry("PPSA00001")], age=7200)
        minute = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output="/output/a.pkg", built=minute(NOW - 86400))})
        self.assertEqual(self.counts()["build"], 1)
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output="/output/a.pkg", built=minute(NOW - 600))})
        self.assertEqual(self.counts()["done"], 1)
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output="/output/a.pkg", built="not a time")})
        self.assertEqual(self.counts()["build"], 1)

    def test_odd_ledgers_and_plans_do_not_break_it(self):
        self.put("plan.json", [plan_entry("PPSA00001"), "junk", None, plan_entry("PPSA00002", version="")], age=600)
        for ledger in ([], "x", {"PPSA00001": "garbage", "PPSA00002": dict(built_ts="soon")}, {"PPSA00002": dict(built_ts=True)}):
            self.put("ledger.json", ledger)
            self.assertEqual(self.counts()["build"], 2)
            self.assertEqual(self.get("/api/plan")[1], "junk")
        self.put("ledger.json", {"PPSA00002": dict(built_ts=NOW - 5, version="01.00", output="/o/x.pkg")})
        self.assertEqual(self.counts()["done"], 1)                                          # a plan entry without a version has nothing to compare

    def test_a_ps4_copy_and_a_retried_failure_count_too(self):
        self.put("plan.json", [plan_entry("CUSA12345", action="copy", ps4=True), plan_entry("PPSA00002", action="failed")], age=600)
        self.put("ledger.json", {"CUSA12345": dict(version="01.00", output="/output/PS4/x", built_ts=NOW - 10, ps4=True),
                                 "PPSA00002": dict(version="01.00", output="/output/b.pkg", built_ts=NOW - 10)})
        c = self.counts()
        self.assertEqual((c["copy"], c["failed"], c["done"]), (0, 0, 2))

    def test_without_a_plan_or_with_the_plan_after_the_build_nothing_changes(self):
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output="/o/a.pkg", built_ts=NOW - 60)})
        self.assertEqual(self.counts()["done"], 0)
        self.put("plan.json", [plan_entry("PPSA00001")], age=0)                              # a scan after the build: it is its own truth
        self.assertEqual(self.counts()["build"], 1)

    def test_the_ledger_is_only_parsed_again_when_it_changes(self):
        self.put("plan.json", [plan_entry("PPSA00001")], age=600)
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output="/o/a.pkg", built_ts=NOW - 60)})
        with mock.patch.object(self.srv, "read_json", wraps=self.srv.read_json) as rj:
            self.counts()
            first = sum(1 for c in rj.call_args_list if str(c.args[0]).endswith("ledger.json"))
            self.counts()
            self.counts()
            second = sum(1 for c in rj.call_args_list if str(c.args[0]).endswith("ledger.json"))
        self.assertLessEqual(second - first, 0)


class NextRun(ProgressCase):
    def at(self, hm):
        return time.mktime(time.strptime("2030-06-15 " + hm, "%Y-%m-%d %H:%M"))

    def test_the_next_time_is_today_or_tomorrow(self):
        sch = {"enabled": True, "times": ["14:00", "03:00"]}
        n = self.srv.next_run(sch, self.at("01:00"))
        self.assertEqual((n["label"], n["day"], n["time"]), ("today 03:00", "today", "03:00"))
        self.assertEqual(n["at"], self.at("03:00"))
        self.assertEqual(self.srv.next_run(sch, self.at("03:00"))["label"], "today 14:00")           # at the minute: that one has begun
        self.assertEqual(self.srv.next_run(sch, self.at("13:59"))["label"], "today 14:00")
        n = self.srv.next_run(sch, self.at("14:00"))
        self.assertEqual((n["label"], n["at"]), ("tomorrow 03:00", self.at("03:00") + 86400))
        self.assertEqual(self.srv.next_run(sch, self.at("23:59"))["label"], "tomorrow 03:00")

    def test_nothing_scheduled(self):
        for sch in (None, {}, {"enabled": False, "times": ["03:00"]}, {"enabled": True, "times": []}, {"enabled": True, "times": ["x", 5, None, "3:00"]}, "nope"):
            self.assertIsNone(self.srv.next_run(sch, self.at("01:00")), sch)

    def test_an_impossible_time_is_skipped(self):
        self.assertEqual(self.srv.next_run({"enabled": True, "times": ["99:99", "14:00"]}, self.at("01:00"))["label"], "today 14:00")

    def test_the_status_carries_it(self):
        self.write_cfg(legacy_config(schedule={"enabled": True, "times": ["03:00", "14:00"]}))
        n = self.get("/api/status")["next_run"]
        self.assertIn(n["day"], ("today", "tomorrow"))
        self.assertIn(n["time"], ("03:00", "14:00"))
        self.assertGreater(n["at"], time.time() - 1)
        self.write_cfg(legacy_config(schedule={"enabled": False, "times": ["03:00"]}))
        self.assertIsNone(self.get("/api/status")["next_run"])


class LastRun(ProgressCase):
    RUN = dict(started=NOW - 7200, finished=NOW - 600, trigger="scheduled", total=5, done=5, built=4, copied=0, failed=1, deferred=0)

    def test_the_last_finished_run_is_summarised_with_its_date(self):
        self.put("status.json", dict(updated=NOW - 600, pid=None, finished=True, run=self.RUN, results=[dict(status="built")] * 4 + [dict(status="failed")]))
        lr = self.get("/api/status")["last_run"]
        self.assertEqual((lr["total"], lr["built"], lr["failed"], lr["trigger"], lr["interrupted"]), (5, 4, 1, "scheduled", False))
        self.assertEqual((lr["started"], lr["finished"]), (self.RUN["started"], self.RUN["finished"]))

    def test_while_a_run_is_active_there_is_no_last_run(self):
        self.put("status.json", dict(updated=NOW, pid=1, run=dict(self.RUN, finished=None), results=[]))
        (self.state / "run.lock").write_text(str(os.getpid()))
        st = self.get("/api/status")
        self.assertIsNone(st["last_run"])
        self.assertTrue(st["engine_running"])
        self.assertEqual(st["status"]["run"]["trigger"], "scheduled")                       # the live run is in `status`, as the engine wrote it

    def test_a_run_that_did_not_finish_is_not_a_success(self):
        self.put("status.json", dict(updated=NOW - 100, pid=4242, run=dict(self.RUN, finished=None, done=2, built=2, failed=0), results=[dict(status="built")] * 2))
        lr = self.get("/api/status")["last_run"]
        self.assertTrue(lr["interrupted"])
        self.assertEqual((lr["done"], lr["total"], lr["built"]), (2, 5, 2))
        self.put("status.json", dict(updated=NOW - 100, pid=None, finished=True, run=dict(self.RUN, interrupted=True), results=[]))
        self.assertTrue(self.get("/api/status")["last_run"]["interrupted"])

    def test_a_status_file_from_before_runs_were_described_still_gives_a_summary(self):
        self.put("status.json", dict(updated=NOW - 3000, pid=None, finished=True, results=[dict(status="built"), dict(status="copied"), dict(status="failed"), dict(status="deferred")]))
        lr = self.get("/api/status")["last_run"]
        self.assertEqual((lr["total"], lr["built"], lr["copied"], lr["failed"], lr["deferred"], lr["started"]), (4, 1, 1, 1, 1, None))
        self.assertEqual(lr["finished"], NOW - 3000)

    def test_nothing_to_say_when_there_has_been_no_run(self):
        self.assertIsNone(self.get("/api/status")["last_run"])
        self.put("status.json", dict(updated=NOW, pid=None, finished=True, results=[]))
        self.assertIsNone(self.get("/api/status")["last_run"])
        self.put("status.json", "garbage")
        self.assertIsNone(self.get("/api/status")["last_run"])


class WhyItStarted(ProgressCase):
    def popen(self):
        proc = mock.Mock(pid=4321, wait=lambda: 0)
        proc.poll.return_value = 0
        return mock.patch.object(self.srv.subprocess, "Popen", return_value=proc)

    def test_a_manual_run_says_so_and_a_scheduled_one_too(self):
        for kwargs, want in (({}, "manual"), ({"trigger": "scheduled"}, "scheduled")):
            with self.popen() as p:
                self.assertTrue(self.srv.start_engine(**kwargs))
            argv = p.call_args.args[0]
            self.assertEqual(argv[argv.index("--trigger") + 1], want)
            self.assertLess(argv.index("run"), argv.index("--trigger"))
            self.srv._run.update(pid=None)

    def test_only_titles_keep_their_place_after_it(self):
        with self.popen() as p:
            self.srv.start_engine("--only", "PPSA01234")
        argv = p.call_args.args[0]
        self.assertEqual(argv[argv.index("--only"):], ["--only", "PPSA01234"])

    def test_the_scheduler_starts_runs_as_scheduled(self):
        self.write_cfg(legacy_config(schedule={"enabled": True, "times": ["03:00"]}))
        with mock.patch.object(self.srv, "start_engine", return_value=True) as start:
            self.srv.scheduler_tick(set(), time.strptime("2030-01-01 03:00", "%Y-%m-%d %H:%M"))
        start.assert_called_once_with(trigger="scheduled")

    def test_the_run_button_starts_a_manual_one(self):
        with mock.patch.object(self.srv, "start_engine", return_value=True) as start:
            self.assertEqual(self.client.post("/api/run", json={}).status_code, 200)
        start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
