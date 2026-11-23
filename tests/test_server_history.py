"""The web server's History and Logs: the runs and the packages (ledger entries of every version of the app), Rebuild / Forget / replaced
packages, the log list with its games, and a log read by the end, by offset, before an offset, searched by the page and downloaded.
Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ENGINE_DIR, E, ServerCase, WEB_DIR, legacy_config, make_env

import ps5_failures as F  # noqa: E402
import ps5_history as PH  # noqa: E402

GIB = 2 ** 30
NOW = time.time()
ENV = {"PS5PKG_HOST_LIBRARY": "/srv/games/ps5", "PS5PKG_HOST_OUTPUT": "/srv/games/ps5/pkg", "PS5PKG_HOST_WORK": "/srv/scratch", "PS5PKG_HOST_CONFIG": "/srv/docker/ps5-pkg/config"}


def stamp(ts):
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))


class HistCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.env_dirs = make_env(self.dir / "env")
        self.out = Path(self.env_dirs["output"])
        self.write_cfg(legacy_config(**{k: v for k, v in self.env_dirs.items() if k in ("library", "downloads", "output", "temp")}, schedule={"enabled": False, "times": []}))

    def put(self, name, obj):
        (self.state / name).write_text(json.dumps(obj))

    def rows(self, rows):
        (self.state / "history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def log(self, name, text="x", age=0):
        p = self.logs / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode() if isinstance(text, str) else text)
        os.utime(p, (time.time() - age, time.time() - age))
        return p


class Packages(HistCase):
    def ledger(self):
        pkg1 = self.out / "Example Game [PPSA00001] [v01.004.000] [fw9.60].pkg"
        pkg1.write_bytes(b"p" * 100)
        pkg2 = self.out / "Old Game [PPSA00002] [v01.00].pkg"
        pkg2.write_bytes(b"q" * 50)
        ps4 = self.out / "PS4" / "Old PS4 [CUSA00001]"
        ps4.mkdir(parents=True)
        return {
            "PPSA00001": dict(version="01.004.000", output=str(pkg1), source="/library/x", built="2026-11-20 03:10", built_ts=NOW - 3600, prep_min=12.0, build_min=60.5, bytes=100,
                              source_bytes=9 * GIB, level=4, validate="summary: 40 passed, 0 warned, 0 failed", sha256="ab" * 32),
            "PPSA00002": dict(version="01.00", output=str(pkg2), validated="summary: 5 ok, 0 failed", seconds=1830, built="2026-01-02 03:04"),
            "CUSA00001": dict(version="01.00", output=str(ps4), ps4=True, files=["a.pkg"], bytes=3 * GIB, copy_secs=300, built_ts=NOW - 86400, validate="CRC verified by 7-Zip: 1 package(s)"),
            "PPSA00003": dict(version="01.00", output=str(self.out / "gone.pkg"), built_ts=NOW - 7200, bytes=7 * GIB, build_min=30.0),
            "PPSA00004": {}, "junk": "garbage", "PPSA00005": 7}

    def test_every_entry_is_one_set_of_fields_whatever_version_wrote_it(self):
        self.put("ledger.json", self.ledger())
        d = self.get("/api/history/packages")
        by = {p["tid"]: p for p in d["packages"]}
        self.assertEqual(sorted(by), ["CUSA00001", "PPSA00001", "PPSA00002", "PPSA00003", "PPSA00004"])      # what is not an object is not a package
        new, old, gone, empty = by["PPSA00001"], by["PPSA00002"], by["PPSA00003"], by["PPSA00004"]
        self.assertEqual((new["bytes"], new["level"], new["checks"], new["minutes"], new["exists"]), (100, 4, dict(passed=40, warned=0, failed=0), 72.5, True))
        self.assertEqual((old["checks"], old["level"], old["sha256"], old["prep_min"], old["build_min"], old["total_min"], old["minutes"], old["exists"]),
                         (dict(passed=5, failed=0), None, None, None, None, 30.5, 30.5, True))
        self.assertEqual(old["bytes"], 50)                                                  # an old entry has no size: the file's own
        self.assertEqual((gone["exists"], gone["bytes"]), (False, 7 * GIB))
        self.assertEqual((empty["bytes"], empty["built_ts"], empty["title"], empty["exists"], empty["minutes"]), (None, None, "PPSA00004", False, None))
        self.assertEqual((by["CUSA00001"]["ps4"], by["CUSA00001"]["exists"], by["CUSA00001"]["minutes"]), (True, True, 5.0))

    def test_titles_come_from_the_plan_the_scans_or_the_file_name(self):
        self.put("ledger.json", self.ledger())
        self.put("plan.json", [dict(title_id="PPSA00001", title="Example Game (from the plan)", action="done")])
        by = {p["tid"]: p for p in self.get("/api/history/packages")["packages"]}
        self.assertEqual(by["PPSA00001"]["title"], "Example Game (from the plan)")
        self.assertEqual(by["PPSA00002"]["title"], "Old Game")                                # from the name of the package
        self.assertEqual(by["PPSA00003"]["title"], "gone.pkg".split(" [")[0])

    def test_a_game_no_longer_in_the_library_is_still_named_by_what_the_history_recorded(self):
        self.put("ledger.json", {"PPSA00007": dict(version="01.00", output="/x.pkg", built_ts=NOW), "PPSA00008": dict(version="01.00", output="/y.pkg", built_ts=NOW)})
        self.rows([dict(k="job", run="r", at=NOW - 10, tid="PPSA00007", title="Older Name", status="built"), dict(k="job", run="r", at=NOW, tid="PPSA00007", title="Newer Name", status="built")])
        F.Store(self.state / "failures.json").update(lambda d: d.__setitem__("PPSA00008", dict(title_id="PPSA00008", title="Failing Game", state="failed")))
        by = {p["tid"]: p for p in self.get("/api/history/packages")["packages"]}
        self.assertEqual((by["PPSA00007"]["title"], by["PPSA00008"]["title"]), ("Newer Name", "Failing Game"))

    def test_totals_count_what_is_on_disk_and_the_time_that_is_known(self):
        self.put("ledger.json", self.ledger())
        t = self.get("/api/history/packages")["totals"]
        self.assertEqual((t["count"], t["present"], t["bytes"]), (5, 3, 100 + 50 + 3 * GIB))
        self.assertEqual((t["minutes"], t["minutes_known"]), (round(72.5 + 30.5 + 5.0 + 30.0, 1), 4))

    def test_a_package_finds_its_build_log_by_the_history_or_else_by_its_name_and_time(self):
        ledger = self.ledger()
        self.put("ledger.json", ledger)
        built = ledger["PPSA00001"]["built_ts"]
        self.log(f"pkg/{stamp(built - 4000)}_PPSA00001.log")                                  # the build that made it
        self.log(f"pkg/{stamp(built - 90000)}_PPSA00001.log")                                 # an earlier one
        self.log(f"pkg/{stamp(built + 3000)}_PPSA00001.log")                                  # a later attempt (not the one that made this package)
        by = {p["tid"]: p for p in self.get("/api/history/packages")["packages"]}
        self.assertEqual((by["PPSA00001"]["log"], by["PPSA00001"]["log_guessed"]), (f"pkg/{stamp(built - 4000)}_PPSA00001.log", True))
        self.log(f"pkg/{stamp(built - 4100)}_PPSA00001.log", "the recorded one")
        self.rows([dict(k="job", run="r", tid="PPSA00001", status="built", at=built, output=ledger["PPSA00001"]["output"], log=f"pkg/{stamp(built - 4100)}_PPSA00001.log")])
        by = {p["tid"]: p for p in self.get("/api/history/packages")["packages"]}
        self.assertEqual((by["PPSA00001"]["log"], by["PPSA00001"]["log_guessed"]), (f"pkg/{stamp(built - 4100)}_PPSA00001.log", False))
        self.assertIsNone(by["PPSA00002"]["log"])
        self.assertIsNone(by["CUSA00001"]["log"])

    def test_a_logged_name_that_is_not_a_log_is_not_passed_on(self):
        ledger = self.ledger()
        self.put("ledger.json", ledger)
        self.rows([dict(k="job", run="r", tid="PPSA00001", status="built", at=NOW, output=ledger["PPSA00001"]["output"], log="../../etc/passwd")])
        self.assertIsNone({p["tid"]: p for p in self.get("/api/history/packages")["packages"]}["PPSA00001"]["log"])

    def test_paths_have_host_twins_when_the_folders_have_host_locations(self):
        self.put("ledger.json", self.ledger())
        self.write_cfg(legacy_config(output="/output", library="/library", downloads="/downloads", temp="/work/tmp"))
        self.put("ledger.json", {"PPSA00001": dict(version="01", output="/output/x.pkg", source="/library/g", built_ts=NOW)})
        with mock.patch.dict(os.environ, ENV):
            p = self.get("/api/history/packages")["packages"][0]
        self.assertEqual((p["output_host"], p["source_host"]), ("/srv/games/ps5/pkg/x.pkg", "/srv/games/ps5/g"))

    def test_no_ledger_no_packages_and_a_broken_ledger_is_not_a_crash(self):
        self.assertEqual(self.get("/api/history/packages")["packages"], [])
        for text in ("{", "[]", '"x"'):
            (self.state / "ledger.json").write_text(text)
            self.assertEqual(self.get("/api/history/packages")["packages"], [])

    def test_the_ledger_endpoint_still_passes_entries_through_untouched(self):
        ledger = self.ledger()
        self.put("ledger.json", ledger)
        self.assertEqual(self.get("/api/ledger"), ledger)


class Runs(HistCase):
    def history(self):
        t0 = NOW - 86400
        self.log(f"run_{stamp(t0 + 2)}.stdout.log")
        self.log(f"pkg/{stamp(t0 + 30)}_PPSA00001.log", "x")
        rows = [dict(k="run", run=stamp(t0), trigger="scheduled", started=t0),
                dict(k="job", run=stamp(t0), at=t0 + 600, tid="PPSA00001", title="Example Game", name="Example Game.pkg", status="built", bytes=5 * GIB, prep_min=2.0, build_min=9.5,
                     level=4, validate="summary: 40 passed, 0 warned, 0 failed", log=f"pkg/{stamp(t0 + 30)}_PPSA00001.log", output="/output/x.pkg"),
                dict(k="job", run=stamp(t0), at=t0 + 700, tid="PPSA00002", title="Bad Game", status="failed", code="crc_mismatch", phase="prepare", why="1 extracted file(s) differ", summary="It differs from the original archive in 1 file."),
                dict(k="job", run=stamp(t0), at=t0 + 710, tid="PPSA00003", title="Later Game", status="cancelled", why="cancelled while building", code="cancelled"),
                dict(k="end", run=stamp(t0), finished=t0 + 800, interrupted=False),
                dict(k="run", run=stamp(NOW - 100), trigger="manual", started=NOW - 100)]
        self.rows(rows)
        return t0

    def test_the_runs_come_newest_first_with_counts_duration_and_size(self):
        t0 = self.history()
        d = self.get("/api/history/runs")
        self.assertEqual((d["total"], [r["trigger"] for r in d["runs"]]), (2, ["manual", "scheduled"]))
        sch = d["runs"][1]
        self.assertEqual((sch["seconds"], sch["counts"], sch["jobs"], sch["bytes"], sch["interrupted"]), (800, {"built": 1, "failed": 1, "cancelled": 1}, 3, 5 * GIB, False))
        self.assertTrue(d["runs"][0]["interrupted"])                                         # no end line: it did not finish (or is still going)
        self.assertEqual(sch["output_log"], f"run_{stamp(t0 + 2)}.stdout.log")
        self.assertIsNone(d["runs"][0]["output_log"])

    def test_paging(self):
        self.rows([dict(k="run", run=stamp(NOW - i * 3600), trigger="manual", started=NOW - i * 3600) for i in range(5)])
        a = self.get("/api/history/runs?limit=2&offset=0")
        b = self.get("/api/history/runs?limit=2&offset=2")
        self.assertEqual((a["total"], len(a["runs"]), len(b["runs"])), (5, 2, 2))
        self.assertEqual(len({r["run"] for r in a["runs"] + b["runs"]}), 4)
        self.assertEqual(len(self.get("/api/history/runs?limit=0")["runs"]), 1)               # never an empty page by accident
        self.assertEqual(self.get("/api/history/runs?limit=9999")["total"], 5)

    def test_a_run_lists_its_jobs_with_logs_checks_and_the_reason_a_game_failed(self):
        t0 = self.history()
        d = self.get(f"/api/history/runs/{stamp(t0)}")
        jobs = {j["tid"]: j for j in d["jobs"]}
        self.assertEqual([j["tid"] for j in d["jobs"]], ["PPSA00001", "PPSA00002", "PPSA00003"])
        self.assertEqual(jobs["PPSA00001"]["checks"], dict(passed=40, warned=0, failed=0))
        self.assertEqual(jobs["PPSA00001"]["log"], f"pkg/{stamp(t0 + 30)}_PPSA00001.log")
        self.assertEqual(jobs["PPSA00002"]["summary"], "It differs from the original archive in 1 file.")
        self.assertNotIn("log", jobs["PPSA00002"])
        self.assertEqual(jobs["PPSA00003"]["status"], "cancelled")

    def test_a_log_that_was_removed_is_not_linked(self):
        t0 = self.history()
        (self.logs / "pkg" / f"{stamp(t0 + 30)}_PPSA00001.log").unlink()
        self.assertNotIn("log", {j["tid"]: j for j in self.get(f"/api/history/runs/{stamp(t0)}")["jobs"]}["PPSA00001"])

    def test_bad_and_unknown_runs(self):
        self.history()
        self.assertEqual(self.client.get("/api/history/runs/not-a-run").status_code, 400)
        self.assertEqual(self.client.get("/api/history/runs/20200101-000000").status_code, 404)
        self.assertEqual(self.get("/api/history/runs")["since"], self.get("/api/history/runs")["runs"][-1]["started"])

    def test_no_history_yet(self):
        self.assertEqual(self.get("/api/history/runs"), dict(total=0, runs=[], since=None))

    def test_paths_in_a_job_have_host_twins(self):
        self.write_cfg(legacy_config(output="/output", library="/library", downloads="/downloads", temp="/work/tmp"))
        self.rows([dict(k="run", run="20261122-030000", started=NOW), dict(k="job", run="20261122-030000", at=NOW, tid="PPSA00001", status="built", output="/output/x.pkg", why="see /library/g")])
        with mock.patch.dict(os.environ, ENV):
            j = self.get("/api/history/runs/20261122-030000")["jobs"][0]
        self.assertEqual((j["output_host"], j["why_host"]), ("/srv/games/ps5/pkg/x.pkg", "see /srv/games/ps5/g"))


class Actions(HistCase):
    def setUp(self):
        super().setUp()
        self.pkg = self.out / "Example [PPSA00001].pkg"
        self.pkg.write_bytes(b"p")
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output=str(self.pkg), built_ts=NOW), "PPSA00002": dict(version="01.00", output="/x.pkg", built_ts=NOW)})

    def test_rebuild_marks_the_game_and_asks_for_a_new_plan_and_touches_no_file(self):
        r = self.client.post("/api/history/PPSA00001/rebuild")
        self.assertEqual((r.status_code, r.json()["ok"]), (200, True))
        self.assertEqual(list(json.loads((self.state / "rebuild.json").read_text())), ["PPSA00001"])
        self.assertEqual(len(self.replans), 1)
        self.assertTrue(self.pkg.exists())
        self.assertTrue({p["tid"]: p for p in self.get("/api/history/packages")["packages"]}["PPSA00001"]["marked"])
        self.assertEqual(json.loads((self.state / "ledger.json").read_text()).keys(), {"PPSA00001": 1, "PPSA00002": 1}.keys())     # the ledger is the planner's: untouched

    def test_a_rebuild_request_can_be_taken_back(self):
        self.client.post("/api/history/PPSA00001/rebuild")
        self.assertEqual(self.client.delete("/api/history/PPSA00001/rebuild").status_code, 200)
        self.assertEqual(json.loads((self.state / "rebuild.json").read_text()) if (self.state / "rebuild.json").exists() else {}, {})
        self.assertEqual(self.client.delete("/api/history/PPSA00001/rebuild").status_code, 200)

    def test_only_a_game_in_the_history_can_be_marked_and_the_id_is_checked(self):
        self.assertEqual(self.client.post("/api/history/PPSA09999/rebuild").status_code, 404)
        self.assertEqual(self.client.post("/api/history/not-an-id/rebuild").status_code, 400)
        self.assertEqual(self.client.post("/api/history/PPSA09999/forget").status_code, 404)
        self.assertFalse((self.state / "rebuild.json").exists())

    def test_a_marked_game_is_planned_again_by_the_next_scan(self):
        env = self.env_dirs
        self.write_cfg({**legacy_config(), **env, "overrides": {}})
        game = Path(env["library"]) / "Example Game [PPSA01234]"
        self.assertTrue(game.exists())
        pkg = self.out / "Example Game.pkg"
        pkg.write_bytes(b"p")
        self.put("ledger.json", {"PPSA01234": dict(version="01.002.003", output=str(pkg), built="2026-11-20 03:10", built_ts=NOW)})

        def plan():
            r = subprocess.run([sys.executable, "-I", str(ENGINE_DIR / "ps5_pkg_sync.py"), "plan"], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return json.loads((self.state / "plan.json").read_text())[0]
        self.assertEqual(plan()["action"], "done")
        self.assertEqual(self.client.post("/api/history/PPSA01234/rebuild").status_code, 200)
        self.assertEqual((plan()["action"], plan()["why"]), ("build", "rebuild requested"))
        self.client.delete("/api/history/PPSA01234/rebuild")
        self.assertEqual(plan()["action"], "done")

    def test_forget_removes_the_entry_and_keeps_the_file_and_the_others(self):
        r = self.client.post("/api/history/PPSA00001/forget")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(list(json.loads((self.state / "ledger.json").read_text())), ["PPSA00002"])
        self.assertTrue(self.pkg.exists())
        self.assertEqual(len(self.replans), 1)
        self.assertNotIn("PPSA00001", json.loads(self.cfg_path.read_text())["overrides"])

    def test_forget_can_also_say_never_build_it_again(self):
        self.client.post("/api/history/PPSA00001/rebuild")
        r = self.client.post("/api/history/PPSA00001/forget", json={"skip": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(json.loads(self.cfg_path.read_text())["overrides"]["PPSA00001"]["skip"], True)
        self.assertEqual(json.loads((self.state / "rebuild.json").read_text()) if (self.state / "rebuild.json").exists() else {}, {})     # a request for a forgotten game goes too

    def test_forget_waits_for_the_run_that_may_be_writing_the_ledger(self):
        (self.state / "run.lock").write_text(str(os.getpid()))
        r = self.client.post("/api/history/PPSA00001/forget")
        self.assertEqual(r.status_code, 409)
        self.assertIn("A run is active", r.json()["detail"])
        self.assertEqual(sorted(json.loads((self.state / "ledger.json").read_text())), ["PPSA00001", "PPSA00002"])


class Replaced(HistCase):
    def setUp(self):
        super().setUp()
        self.names = ["Game [PPSA01234] [v01] [fw9.00] (replaced 202611201430).pkg", "Game [PPSA01234] [v01] [fw9.00] (replaced 202611211430).pkg"]
        for n in self.names:
            (self.out / n).write_bytes(b"x" * 10)
        (self.out / "Game [PPSA01234] [v01] [fw9.00].pkg").write_bytes(b"current")

    def test_replaced_packages_are_listed_with_size_and_when_in_both_places(self):
        a = self.get("/api/replaced")
        self.assertEqual((a["count"], a["bytes"], [x["name"] for x in a["items"]]), (2, 20, [self.names[1], self.names[0]]))
        self.assertEqual(self.get("/api/history/packages")["replaced"]["count"], 2)

    def test_deleting_one_removes_that_file_only(self):
        r = self.client.delete("/api/replaced/" + self.names[0].replace(" ", "%20"))
        self.assertEqual((r.status_code, r.json()["freed"]), (200, 10))
        self.assertFalse((self.out / self.names[0]).exists())
        self.assertTrue((self.out / self.names[1]).exists())
        self.assertTrue((self.out / "Game [PPSA01234] [v01] [fw9.00].pkg").exists())

    def test_nothing_else_can_be_deleted_this_way(self):
        for bad in ("Game%20%5BPPSA01234%5D%20%5Bv01%5D%20%5Bfw9.00%5D.pkg", "..%2Fx%20(replaced%20202611201430).pkg", "x%20(replaced%202026).pkg"):
            r = self.client.delete("/api/replaced/" + bad)
            self.assertIn(r.status_code, (400, 404), bad)
        self.assertEqual(self.client.delete("/api/replaced/Nothing%20(replaced%20202611201430).pkg").status_code, 404)
        self.assertTrue((self.out / "Game [PPSA01234] [v01] [fw9.00].pkg").exists())
        self.assertEqual(self.get("/api/replaced")["count"], 2)


class LogList(HistCase):
    def setUp(self):
        super().setUp()
        self.log("pkg_sync.log", "a" * 100, -60)
        self.log("scan.stdout.log", "s")
        self.log("run_20261121-030000.stdout.log", "r", 5 * 86400)
        self.log("pkg_sync.20261001-000000.log", "o", 9 * 86400)
        self.b1 = "pkg/20261121-031500_PPSA00001.log"
        self.b2 = "pkg/20261121-041500_PPSA00002.log"
        self.log(self.b1, "b" * 300, 500)
        self.log(self.b2, "c" * 400, 400)
        self.put("plan.json", [dict(title_id="PPSA00001", title="Example Game", action="done")])

    def test_logs_are_listed_by_kind_and_a_build_has_its_game_title_and_outcome(self):
        self.rows([dict(k="job", run="r", at=NOW, tid="PPSA00001", status="built", log=self.b1)])
        F.Store(self.state / "failures.json").update(lambda d: d.__setitem__("PPSA00002", dict(title_id="PPSA00002", log=self.b2, state="failed")))
        by = {x["name"]: x for x in self.get("/api/logs")}
        self.assertEqual({n: x["kind"] for n, x in by.items()}, {"pkg_sync.log": "activity", "scan.stdout.log": "scan", "run_20261121-030000.stdout.log": "run", "pkg_sync.20261001-000000.log": "activity_old",
                                                                 self.b1: "build", self.b2: "build"})
        self.assertEqual((by[self.b1]["tid"], by[self.b1]["title"], by[self.b1]["outcome"], by[self.b1]["size"]), ("PPSA00001", "Example Game", "built", 300))
        self.assertEqual((by[self.b2]["title"], by[self.b2]["outcome"]), ("PPSA00002", "failed"))                           # no title known: the ID
        self.assertEqual(self.get("/api/logs")[0]["name"], "pkg_sync.log")                                                  # newest first

    def test_info_adds_the_room_the_logs_take_and_the_retention_settings(self):
        d = self.get("/api/logs?info=1")
        self.assertEqual(d["usage"]["bytes"], 100 + 1 + 1 + 1 + 300 + 400)
        self.assertEqual(d["usage"]["by_kind"]["build"], dict(files=2, bytes=700))
        self.assertEqual(d["retention"], dict(days=0, gb=0))
        self.client.put("/api/config", json={"keep_logs_days": 30, "keep_logs_gb": 2})
        self.assertEqual(self.get("/api/logs?info=1")["retention"], dict(days=30, gb=2))

    def test_a_long_history_of_builds_lists_the_newest(self):
        for i in range(520):
            self.log(f"pkg/2026{i // 28 % 12 + 1:02d}{i % 28 + 1:02d}-000000_PPSA{i:05d}.log", "x", i)
        n = sum(1 for x in self.get("/api/logs") if x["kind"] == "build")
        self.assertEqual(n, 500)

    def test_retention_removes_old_logs_asks_first_with_a_dry_run_and_keeps_what_a_failure_needs(self):
        self.assertIn("No retention is set", self.client.post("/api/logs/prune").json()["note"])
        self.assertTrue((self.logs / self.b1).exists())
        self.client.put("/api/config", json={"keep_logs_days": 1})
        F.Store(self.state / "failures.json").update(lambda d: d.__setitem__("PPSA00002", dict(title_id="PPSA00002", log=self.b2, state="failed")))
        os.utime(self.logs / self.b1, (NOW - 5 * 86400,) * 2)
        os.utime(self.logs / self.b2, (NOW - 5 * 86400,) * 2)
        dry = self.client.post("/api/logs/prune", json={"dry_run": True}).json()
        self.assertEqual(sorted(dry["removed"]), sorted([self.b1, "run_20261121-030000.stdout.log", "pkg_sync.20261001-000000.log"]))
        self.assertTrue((self.logs / self.b1).exists())                                       # a dry run removes nothing
        done = self.client.post("/api/logs/prune").json()
        self.assertEqual(sorted(done["removed"]), sorted(dry["removed"]))
        self.assertFalse((self.logs / self.b1).exists())
        self.assertTrue((self.logs / self.b2).exists())                                       # the log of a game that is still failing
        self.assertTrue((self.logs / "pkg_sync.log").exists() and (self.logs / "scan.stdout.log").exists())


class ReadingALog(HistCase):
    LINES = [f"line {i:03d} " + "x" * 90 for i in range(100)]

    def setUp(self):
        super().setUp()
        self.text = "\n".join(self.LINES) + "\n"
        self.path = self.log("pkg/20261121-031500_PPSA00001.log", self.text)
        self.url = "/api/logs/pkg/20261121-031500_PPSA00001.log"

    def offset_of(self, line):
        return len(("\n".join(self.LINES[:line]) + "\n").encode()) if line else 0

    def test_the_default_is_the_last_lines_as_before_and_says_where_they_start(self):
        r = self.client.get(self.url + "?lines=20")
        self.assertEqual(r.text, "\n".join(self.LINES[-20:]))
        self.assertEqual((int(r.headers["x-log-start"]), int(r.headers["x-log-next"]), int(r.headers["x-log-size"])), (self.offset_of(80), len(self.text), len(self.text)))
        self.assertNotIn("x-log-reset", r.headers)
        self.assertEqual(self.client.get(self.url + "?lines=5").text.count("\n") + 1, 10)       # never fewer than ten, as before

    def test_progress_lines_ending_in_a_carriage_return_are_lines(self):
        self.log("pkg/20261121-041500_PPSA00001.log", "start\n[#---] 5% a\r[##--] 10% a\r[###-] 15% a\ndone\n")
        self.assertEqual(self.client.get("/api/logs/pkg/20261121-041500_PPSA00001.log").text, "start\n[#---] 5% a\n[##--] 10% a\n[###-] 15% a\ndone")

    def test_offset_returns_what_came_after_and_where_to_go_on(self):
        r = self.client.get(self.url + f"?offset={self.offset_of(90)}")
        self.assertEqual(r.text, "\n".join(self.LINES[90:]) + "\n")
        self.assertEqual((int(r.headers["x-log-start"]), int(r.headers["x-log-next"])), (self.offset_of(90), len(self.text)))
        r = self.client.get(self.url + f"?offset={len(self.text)}")
        self.assertEqual((r.text, int(r.headers["x-log-next"])), ("", len(self.text)))                           # nothing new: nothing, and the same place
        with open(self.path, "ab") as fh:
            fh.write(b"a new line\n")
        r = self.client.get(self.url + f"?offset={len(self.text)}")
        self.assertEqual((r.text, int(r.headers["x-log-next"])), ("a new line\n", len(self.text) + 11))        # follow: only the new bytes

    def test_a_chunk_ends_at_a_line_end_when_the_file_goes_on_and_the_next_call_continues_there(self):
        got, off = [], 0
        for _ in range(40):
            r = self.client.get(self.url + f"?offset={off}&limit=4096")
            if not r.text:
                break
            got.append(r.text)
            self.assertTrue(r.text.endswith("\n"))
            off = int(r.headers["x-log-next"])
        self.assertGreater(len(got), 1)
        self.assertEqual("".join(got), self.text)                                           # nothing lost, nothing twice
        self.assertEqual(off, len(self.text))

    def test_a_chunk_never_ends_inside_a_character(self):
        self.log("pkg/20261121-051500_PPSA00001.log", ("é" * 50 + "\n") * 200)
        off, text = 0, ""
        for _ in range(100):
            r = self.client.get(f"/api/logs/pkg/20261121-051500_PPSA00001.log?offset={off}&limit=4097")
            text += r.text
            if int(r.headers["x-log-next"]) == off:
                break
            off = int(r.headers["x-log-next"])
        self.assertNotIn("�", text)
        self.assertEqual(text, ("é" * 50 + "\n") * 200)

    def test_one_long_line_is_read_in_pieces_that_never_split_a_character(self):
        line = "é" * 5000                                                                    # 10000 bytes and no line end: a chunk has to end somewhere
        self.log("pkg/20261121-071500_PPSA00001.log", line)
        name, off, text = "/api/logs/pkg/20261121-071500_PPSA00001.log", 0, ""
        for _ in range(20):
            r = self.client.get(f"{name}?offset={off}&limit=4097")
            text += r.text.rstrip("\n")
            nxt = int(r.headers["x-log-next"])
            if nxt == off:
                break
            self.assertNotIn("\ufffd", r.text)
            off = nxt
        self.assertEqual((text.replace("\n", ""), off), (line, 10000))

    def test_a_file_that_became_shorter_is_read_again_from_its_end_and_says_so(self):
        r = self.client.get(self.url + f"?offset={len(self.text) + 500}&lines=20")
        self.assertEqual(r.headers["x-log-reset"], "1")
        self.assertEqual(r.text, "\n".join(self.LINES[-20:]))

    def test_load_earlier_gives_the_lines_before_a_place(self):
        start = self.offset_of(80)
        r = self.client.get(self.url + f"?before={start}&lines=20")
        self.assertEqual(r.text, "\n".join(self.LINES[60:80]))
        self.assertEqual(int(r.headers["x-log-start"]), self.offset_of(60))
        r = self.client.get(self.url + f"?before={self.offset_of(5)}&lines=20")
        self.assertEqual((r.text, int(r.headers["x-log-start"])), ("\n".join(self.LINES[:5]), 0))                 # the start of the file
        self.assertEqual(self.client.get(self.url + "?before=0&lines=20").text, "")
        self.assertEqual(self.client.get(self.url + "?before=1&offset=1").status_code, 400)

    def test_reading_the_end_of_a_big_log_reads_only_the_end(self):
        big = self.logs / "pkg_sync.log"
        with open(big, "wb") as fh:
            for _ in range(40):
                fh.write((b"an engine log line, as long as they usually are, with a path /library/x\n" * 14000))
        size = big.stat().st_size
        self.assertGreater(size, 30 << 20)
        read = []

        class Counting:
            def __init__(self, fh):
                self.fh = fh

            def seek(self, *a):
                return self.fh.seek(*a)

            def read(self, n=-1):
                data = self.fh.read(n)
                read.append(len(data))
                return data
        with open(big, "rb") as fh:
            start, text = self.srv.log_lines_before(Counting(fh), size, 600)
        self.assertEqual(text.count("\n") + 1, 600)
        self.assertLess(sum(read), 3 << 20)
        t0 = time.time()
        r = self.client.get("/api/logs/pkg_sync.log?lines=600")
        self.assertLess(time.time() - t0, 3)
        self.assertEqual((r.text.count("\n") + 1, int(r.headers["x-log-size"])), (600, size))

    def test_download_is_the_file_exactly_as_it_is(self):
        raw = b"[#---] 5% a\r[##--] 10% a\r\nplain\n\xff\xfe odd bytes\n"
        self.log("pkg/20261121-061500_PPSA00001.log", raw)
        r = self.client.get("/api/logs/pkg/20261121-061500_PPSA00001.log?download=1")
        self.assertEqual((r.status_code, r.content), (200, raw))
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertIn("20261121-061500_PPSA00001.log", r.headers["content-disposition"])

    def test_host_paths_apply_to_every_way_of_reading(self):
        self.log("pkg_sync.log", "copied to /output/x.pkg\nsee /library/g\n")
        with mock.patch.dict(os.environ, ENV):
            self.assertEqual(self.client.get("/api/logs/pkg_sync.log?host=1").text, "copied to /srv/games/ps5/pkg/x.pkg\nsee /srv/games/ps5/g")
            self.assertEqual(self.client.get("/api/logs/pkg_sync.log?host=1&offset=0").text, "copied to /srv/games/ps5/pkg/x.pkg\nsee /srv/games/ps5/g\n")
            self.assertEqual(self.client.get("/api/logs/pkg_sync.log?offset=0").text, "copied to /output/x.pkg\nsee /library/g\n")

    def test_only_a_log_can_be_read_and_a_missing_one_is_not_found(self):
        for bad in ("../state/config.json", "pkg/../../state/config.json", "config.json", "pkg/x.txt", "..%2Fstate%2Fconfig.json.log"):
            self.assertIn(self.client.get("/api/logs/" + bad).status_code, (400, 404), bad)
        self.assertEqual(self.client.get("/api/logs/pkg/20200101-000000_PPSA00001.log").status_code, 404)
        self.assertEqual(self.client.get("/api/logs/pkg/20200101-000000_PPSA00001.log?download=1").status_code, 404)
        self.assertEqual(self.client.get("/api/logs/pkg").status_code, 400)


class SettingsPage(HistCase):
    def test_the_new_settings_are_on_the_settings_page_with_their_limits_and_checked_on_save(self):
        c = self.get("/api/config")
        by = {f["key"]: f for f in c["schema"]}
        self.assertEqual((by["keep_replaced"]["kind"], by["keep_replaced"]["default"], [x[0] for x in by["keep_replaced"]["choices"]]), ("choice", "all", ["all", "1", "never"]))
        self.assertEqual((by["keep_logs_days"]["min"], by["keep_logs_days"]["max"], by["keep_logs_days"]["default"]), (0, 3650, 0))
        self.assertEqual((by["keep_logs_gb"]["min"], by["keep_logs_gb"]["max"]), (0, 1000))
        self.assertTrue(all(by[k]["group"] == "cleanup" for k in ("keep_replaced", "keep_logs_days", "keep_logs_gb")))
        self.assertEqual(self.client.put("/api/config", json={"keep_replaced": "never", "keep_logs_days": 30, "keep_logs_gb": 5}).status_code, 200)
        r = self.client.put("/api/config", json={"keep_replaced": "some", "keep_logs_days": -1})
        self.assertEqual((r.status_code, sorted(e["field"] for e in r.json()["errors"])), (400, ["keep_logs_days", "keep_replaced"]))
        self.assertIn("Choose keep all", [e["message"] for e in r.json()["errors"] if e["field"] == "keep_replaced"][0])


class Page(unittest.TestCase):
    """The page (no browser here: the interactions were checked in a real one)."""
    JS = (WEB_DIR / "static" / "app.js").read_text()
    HTML = (WEB_DIR / "static" / "index.html").read_text()

    def body(self, name, until):
        start = self.JS.index(name)
        return self.JS[start:self.JS.index(until, start)]

    def test_history_has_two_views_and_the_packages_view_can_be_searched_filtered_and_sorted(self):
        for needle in ('data-hv="runs"', 'data-hv="pkgs"', 'id="pkgFind"', 'id="pkgStatus"', 'id="pkgSort"', 'id="replCard"'):
            self.assertIn(needle, self.HTML)
        sort = self.body("function renderPkgs", "async function loadPkgs")
        for needle in ("new:", "old:", "size:", "time:", "name:", "p.title,p.tid,p.name"):
            self.assertIn(needle, sort)
        self.assertIn("Packages (", sort)                                                  # the totals in the heading

    def test_what_an_old_entry_does_not_say_is_said_to_be_not_recorded(self):
        body = self.body("function pkgRow", "function failedPkgRow")
        for needle in ("not recorded", "preparation time not recorded", "build time not recorded", "compression level not recorded", "checks not recorded"):
            self.assertIn(needle, body + self.JS)
        self.assertNotIn("prep —", self.JS)

    def test_rebuild_forget_and_delete_ask_first_in_the_page_and_forget_offers_never_build(self):
        for fn, until in (("async function rebuildGame", "async function forgetGame"), ("async function deleteReplaced", "$(\"#histPkgs\")")):
            body = self.body(fn, until)
            self.assertIn("if(!(await askConfirm({title:", body)                              # nothing goes on unless the answer is yes
            self.assertLess(body.index("askConfirm("), body.index("api("))
        self.assertIn('if(!r||r.key!=="go")return;', self.body("async function forgetGame", "async function deleteReplaced"))
        forget = self.body("async function forgetGame", "async function deleteReplaced")
        self.assertIn("Also never build this game again", forget)
        self.assertLess(forget.index("showDialog("), forget.index("api("))

    def test_the_sha_can_be_copied_whole_and_a_row_links_to_its_logs(self):
        row = self.body("function pkgRow", "function failedPkgRow")
        self.assertIn('data-copy="${esc(p.sha256)}"', row)
        self.assertIn("Copy SHA-256", row)
        self.assertIn("Validation report", row)
        self.assertIn("?find=validate", row)
        self.assertIn('href="#logs/${esc(j.log)}"', self.body("function jobRow", "function runBody"))

    def test_the_logs_page_has_the_viewer_controls(self):
        for needle in ('id="logDl"', 'id="logSearch"', 'id="logWarn"', 'id="logCollapse"', 'id="logWrap"', 'id="logFollow"', 'id="logEarlier"', 'id="logAll"', 'id="logFind"', 'id="bLogPrune"'):
            self.assertIn(needle, self.HTML)
        self.assertNotIn('id="logSel"', self.HTML)                                         # a list, not a dropdown of file names

    def test_following_asks_only_for_what_is_new_and_does_not_move_the_view_of_someone_who_scrolled_up(self):
        follow = self.body("async function followLog", "$(\"#logSearch\")")
        self.assertIn("offset=${logSt.next}", follow)
        self.assertNotIn("lines=600", follow)                                              # never the whole tail again
        render = self.body("function renderLog", "function gotoHit")
        self.assertIn("atEnd", render)
        self.assertIn('how==="keep"&&atEnd&&$("#logFollow").checked', render)              # only someone at the end is taken to the end
        self.assertIn("p.scrollTop=t0", render)                                            # everyone else stays where they were
        self.assertNotIn("showLog", self.JS)

    def test_search_highlights_and_there_is_a_download_and_deep_links(self):
        self.assertIn("<mark>", self.body("function logHtml", "// draw the log"))
        self.assertIn("?download=1", self.body("async function openLog", "async function loadAllLog"))
        route = self.body("function routeHash", "window.addEventListener")
        self.assertIn("#logs", route)
        self.assertIn('qs.get("game")', route)
        self.assertIn("routeHash()", self.JS)
        self.assertRegex(self.JS, r"""document\.addEventListener\("click",e=>\{const a=e\.target\.closest\('a\[href\^="#logs"\]'\);if\(!a\)return;e\.preventDefault\(\)""")     # a link works even when the address already says so
        self.assertIn('if(logSt.q)gotoHit(1)', self.JS)                                    # the first match, not the last
        self.assertIn('href="#logs?game=${tid}"', self.body("function libRow", "function unrecBox"))      # a game's logs, from the Library

    def test_progress_lines_collapse_and_warnings_can_be_the_only_thing_shown(self):
        body = self.body("function logHtml", "// draw the log")
        self.assertIn("progress lines hidden", body)
        self.assertIn("WARN_RE.test", body)
        sample = ["[#-------------------] 5% staged in place", "[##------------------] 10% inner image (Kraken) @ 12.3 MB/s ETA 3s"]
        prog = re.compile(r"^\s*\[[#\-=. ]{8,}\]\s*\d+%|\d+% [^@]*@ [\d.]+ MB/s")                               # the same pattern as the page's
        self.assertTrue(all(prog.search(x) for x in sample))
        self.assertIn(r"/^\s*\[[#\-=. ]{8,}\]\s*\d+%|\d+% [^@]*@ [\d.]+ MB\/s/", self.JS)


if __name__ == "__main__":
    unittest.main()
