"""The web server's side of first-run setup: GET /api/setup, and a scan or run that fails being reported in
/api/status instead of leaving the Dashboard at "Idle". Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import os
import shutil
import time
import unittest
from unittest import mock

from support import E, ServerCase, make_env

ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0
GIB = 2**30


class SetupCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.dir / "env")
        self.write_cfg({**E.DEFAULT_CFG, **self.env})

    def wait_for(self, what, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            st = self.get("/api/status")
            if what(st):
                return st
            time.sleep(0.05)
        self.fail("timed out waiting; last status: " + json.dumps(st)[:400])

    def scan(self):
        self.assertEqual(self.client.post("/api/scan").status_code, 200)
        return self.wait_for(lambda st: not st["scan"]["running"] and st["scan"]["finished"])

    def fake_engine(self, body):
        path = self.dir / "fake_engine.py"
        path.write_text(body)
        p = mock.patch.object(self.srv, "ENGINE", path)
        p.start()
        self.addCleanup(p.stop)


class SetupApi(SetupCase):
    def test_returns_a_list_of_checks_with_id_level_message_and_fix(self):
        checks = self.get("/api/setup")
        self.assertIsInstance(checks, list)
        self.assertEqual([c["id"] for c in checks], ["library", "downloads", "output", "work", "config", "tools",
                                                     "console_fw", "time", "host_paths"])
        for c in checks:
            self.assertTrue({"id", "level", "message", "fix"} <= set(c))
            self.assertIn(c["level"], ("ok", "warn", "fail"))
        self.assertEqual([c["id"] for c in checks if c["level"] != "ok"], ["host_paths"])

    def test_checking_does_not_touch_settings_and_leaves_no_files_in_the_folders(self):
        before_cfg = self.snapshot()
        listing = {k: sorted(os.listdir(self.env[k])) for k in ("library", "output")}
        for _ in range(3):
            self.get("/api/setup")
        self.assertEqual(self.snapshot(), before_cfg)
        self.assertEqual({k: sorted(os.listdir(self.env[k])) for k in ("library", "output")}, listing)

    def test_no_downloads_volume_is_a_warning_and_the_rest_is_fine(self):
        shutil.rmtree(self.env["downloads"])
        rows = {c["id"]: c for c in self.get("/api/setup")}
        self.assertEqual(rows["downloads"]["level"], "warn")
        self.assertIn("not mounted (optional)", rows["downloads"]["message"])
        self.assertNotIn("fail", {c["level"] for c in rows.values()})

    @unittest.skipIf(ROOT_USER, "root can write everywhere")
    def test_a_folder_that_is_not_writable_shows_as_fail_in_one_call(self):
        os.chmod(self.env["output"], 0o555)
        self.addCleanup(os.chmod, self.env["output"], 0o755)
        rows = {c["id"]: c for c in self.get("/api/setup")}
        self.assertEqual(rows["output"]["level"], "fail")
        self.assertIn(f"not writable by uid {os.getuid()}", rows["output"]["message"])
        self.assertTrue(rows["output"]["fix"])

    def test_the_biggest_game_in_the_last_scan_is_checked_against_the_work_drive(self):
        plan = [dict(title_id="PPSA01234", title="Example Game", action="build", size=300 * GIB)]
        (self.state / "plan.json").write_text(json.dumps(plan))
        self.write_cfg({**E.DEFAULT_CFG, **self.env, "ssd_reserve_gb": 80})
        with mock.patch.object(self.srv.S, "_free", return_value=200 * GIB):
            work = {c["id"]: c for c in self.get("/api/setup")}["work"]
        self.assertEqual(work["level"], "warn")
        self.assertIn("Example Game", work["message"])

    def test_the_host_paths_row_follows_the_env_setting(self):
        with mock.patch.object(self.srv, "HOST_PATHS", {"library": "/srv/games/ps5"}):
            self.assertEqual({c["id"]: c for c in self.get("/api/setup")}["host_paths"]["level"], "ok")

    def test_status_carries_the_server_clock_next_to_the_schedule(self):
        st = self.get("/api/status")
        self.assertEqual(set(st["clock"]), {"time", "tz", "utc_offset", "text"})
        self.assertIn("schedule", st)


class ScanFailures(SetupCase):
    def test_a_scan_without_the_downloads_volume_succeeds_and_finds_the_games(self):
        shutil.rmtree(self.env["downloads"])
        st = self.scan()
        self.assertEqual(st["scan"]["rc"], 0)
        self.assertEqual((st["scan"]["error"], st["scan"]["log_tail"]), (None, []))
        self.assertEqual([p["title_id"] for p in self.get("/api/plan")], ["PPSA01234"])
        self.assertEqual(st["plan_counts"]["build"], 1)

    def test_a_scan_with_the_library_gone_reports_why(self):
        shutil.rmtree(self.env["library"])
        st = self.scan()
        scan = st["scan"]
        self.assertFalse(scan["running"])
        self.assertEqual(scan["rc"], 2)
        self.assertIn("game library folder", scan["error"])
        self.assertIn(self.env["library"], scan["error"])
        self.assertTrue(any(f"not mounted: {self.env['library']}" in ln for ln in scan["log_tail"]), scan["log_tail"])
        self.assertEqual(scan["log"], "scan.stdout.log")
        self.assertIn("scan.stdout.log", [x["name"] for x in self.get("/api/logs")])

    def test_the_next_good_scan_clears_the_error(self):
        shutil.rmtree(self.env["library"])
        self.assertIsNotNone(self.scan()["scan"]["error"])
        os.makedirs(self.env["library"])
        scan = self.scan()["scan"]
        self.assertEqual((scan["rc"], scan["error"], scan["log_tail"]), (0, None, []))

    def test_a_scan_that_crashes_is_reported_with_the_end_of_its_log(self):
        self.fake_engine("import sys\nprint('Traceback (most recent call last):')\nprint('RuntimeError: boom', flush=True)\nsys.exit(1)\n")
        scan = self.scan()["scan"]
        self.assertEqual(scan["rc"], 1)
        self.assertIn("exit code 1", scan["error"])
        self.assertEqual(scan["log_tail"][-1], "RuntimeError: boom")
        self.assertFalse(scan["running"])

    def test_a_scan_that_cannot_even_start_does_not_stay_running_forever(self):
        with mock.patch.object(self.srv.subprocess, "run", side_effect=OSError("no such python")):
            scan = self.scan()["scan"]
        self.assertFalse(scan["running"])
        self.assertNotEqual(scan["rc"], 0)
        self.assertTrue(scan["error"])
        self.assertEqual(scan["log_tail"], ["could not start the scan: OSError: no such python"])


class RunFailures(SetupCase):
    @unittest.skipIf(ROOT_USER, "root can write everywhere")
    def test_a_run_with_an_unwritable_output_does_not_start_and_says_why(self):
        os.chmod(self.env["output"], 0o555)
        self.addCleanup(os.chmod, self.env["output"], 0o755)
        r = self.client.post("/api/run", json={})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("The run did not start. Setup problem: Output:", r.json()["detail"])
        run = self.get("/api/status")["run"]
        self.assertEqual(run["rc"], E.EXIT_SETUP)
        self.assertIn("Setup problem: Output", run["error"])
        self.assertTrue(any("setup problem: Output" in ln for ln in run["log_tail"]), run["log_tail"])
        self.assertTrue(run["log"].startswith("run_") and run["log"].endswith(".stdout.log"))
        self.assertFalse((self.state / "run.lock").exists())

    def test_a_run_with_the_library_gone_does_not_start_and_says_why(self):
        shutil.rmtree(self.env["library"])
        r = self.client.post("/api/run", json={})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("game library folder", r.json()["detail"])
        self.assertIn("game library folder", self.get("/api/status")["run"]["error"])

    def test_an_engine_that_dies_right_away_for_any_reason_is_a_failed_start(self):
        self.fake_engine("import sys\nprint('something broke', flush=True)\nsys.exit(9)\n")
        r = self.client.post("/api/run", json={})
        self.assertEqual(r.status_code, 500, r.text)
        self.assertIn("exit code 9", r.json()["detail"])
        run = self.get("/api/status")["run"]
        self.assertEqual((run["rc"], run["log_tail"]), (9, ["something broke"]))

    def test_an_engine_that_dies_after_it_started_is_noticed_later(self):
        self.fake_engine("import os, sys, time\n"
                         "lock = os.path.join(os.environ['PS5PKG_STATE'], 'run.lock')\n"
                         "open(lock, 'w').write(str(os.getpid()))\n"
                         "time.sleep(1.2)\n"
                         "print('RuntimeError: out of cheese', flush=True)\n"
                         "os.remove(lock)\n"
                         "sys.exit(1)\n")
        self.assertEqual(self.client.post("/api/run", json={}).status_code, 200)
        st = self.get("/api/status")
        self.assertTrue(st["engine_running"])
        self.assertIsNone(st["run"]["finished"])
        st = self.wait_for(lambda st: st["run"]["finished"])
        self.assertFalse(st["engine_running"])
        self.assertEqual(st["run"]["rc"], 1)
        self.assertIn("exit code 1", st["run"]["error"])
        self.assertEqual(st["run"]["log_tail"][-1], "RuntimeError: out of cheese")

    def test_a_clean_run_leaves_no_error_and_a_new_run_resets_the_old_one(self):
        self.srv._run.update(rc=2, error="old", finished=1.0, log_tail=["old"])
        self.fake_engine("import os, time\n"
                         "lock = os.path.join(os.environ['PS5PKG_STATE'], 'run.lock')\n"
                         "open(lock, 'w').write(str(os.getpid()))\ntime.sleep(0.5)\nos.remove(lock)\n")
        self.assertEqual(self.client.post("/api/run", json={}).status_code, 200)
        self.assertIsNone(self.get("/api/status")["run"]["error"])
        st = self.wait_for(lambda st: st["run"]["finished"])
        self.assertEqual((st["run"]["rc"], st["run"]["error"], st["run"]["log_tail"]), (0, None, []))

if __name__ == "__main__":
    unittest.main()
