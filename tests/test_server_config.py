"""The web server's side of config.json: status polls never write, saves can't be reverted by them, a broken
file keeps every endpoint (and the scheduler) alive and is reported, unknown keys survive a save, and a hand
edit shows up without a restart. Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from support import E, StateCase, legacy_config, server_module

BROKEN = '{\n  "compression_level": 5,\n  "nice": 3\n  "max_builds": 2\n}\n'      # missing comma, error on line 4


class ServerCase(StateCase):
    def setUp(self):
        super().setUp()
        self.srv = server_module()
        from fastapi.testclient import TestClient
        for name, val in (("STATE", self.state), ("LOGS", self.logs)):
            p = mock.patch.object(self.srv, name, val)
            p.start()
            self.addCleanup(p.stop)
        self.srv._scan.update(running=False, started=None, finished=None, rc=None)
        self.client = TestClient(self.srv.app)

    def get(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class PollingNeverWrites(ServerCase):
    def test_dashboard_polls_do_not_touch_the_file(self):
        self.write_cfg(legacy_config())
        before = self.snapshot()
        for _ in range(25):                          # what an open Dashboard does, plus the scheduler tick
            self.get("/api/status")
            self.get("/api/config")
            self.srv.load_cfg()
        self.assertEqual(self.snapshot(), before)

    def test_polling_while_saving_never_reverts_a_save(self):
        self.write_cfg(legacy_config())
        from fastapi.testclient import TestClient
        stop, bad = threading.Event(), []

        def poll(path):
            c = TestClient(self.srv.app)
            while not stop.is_set():
                try:
                    r = c.get(path)       # (the test client re-raises what would have been a 500)
                except Exception as ex:
                    bad.append((path, repr(ex)[:100]))
                    continue
                if r.status_code != 200:
                    bad.append((path, r.status_code, r.text[:80]))

        pollers = [threading.Thread(target=poll, args=(p,)) for p in
                   ("/api/status", "/api/config", "/api/status", "/api/overrides")]
        for t in pollers:
            t.start()
        try:
            for i in range(120):
                want = i % 9 + 1
                r = self.client.put("/api/config", json={"compression_level": want})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(self.get("/api/config")["compression_level"], want, f"save #{i} was reverted")
        finally:
            stop.set()
            for t in pollers:
                t.join()
        self.assertEqual(bad, [])
        self.assertEqual(json.loads(self.cfg_path.read_text())["compression_level"], want)

    def test_a_healthy_file_reports_no_problems(self):
        self.write_cfg(legacy_config())
        st = self.get("/api/status")
        self.assertIsNone(st["config_error"])
        self.assertEqual(st["config_problems"], [])
        self.assertTrue(st["config_rev"])


class BrokenFile(ServerCase):
    def test_a_syntax_error_keeps_the_endpoints_up_on_the_last_good_settings(self):
        self.write_cfg(legacy_config())
        self.get("/api/status")                      # the server has seen the good file
        self.write_cfg(BROKEN)
        before = self.snapshot()
        st = self.get("/api/status")
        err = st["config_error"]
        self.assertEqual(err["line"], 4)
        self.assertGreater(err["column"], 0)
        self.assertIn("Expecting ','", err["message"])
        self.assertEqual(err["using"], "last_good")
        self.assertEqual(st["schedule"], {"enabled": True, "times": ["03:00", "15:30"]})
        cfg = self.get("/api/config")
        self.assertEqual((cfg["compression_level"], cfg["console_fw"]), (5, "9.60"))
        self.assertEqual(set(self.get("/api/overrides")), {"PPSA01234", "PPSA05678"})
        self.assertEqual(self.snapshot(), before)

    def test_a_syntax_error_from_the_start_serves_defaults_and_says_so(self):
        self.write_cfg(BROKEN)
        st = self.get("/api/status")
        self.assertEqual((st["config_error"]["line"], st["config_error"]["using"]), (4, "defaults"))
        self.assertEqual(self.get("/api/config")["compression_level"], E.DEFAULT_CFG["compression_level"])

    def test_fixing_the_file_clears_the_report(self):
        self.write_cfg(BROKEN)
        self.assertIsNotNone(self.get("/api/status")["config_error"])
        self.write_cfg({"compression_level": 2})
        st = self.get("/api/status")
        self.assertIsNone(st["config_error"])
        self.assertEqual(self.get("/api/config")["compression_level"], 2)

    def test_scheduled_runs_keep_being_attempted_on_the_last_good_schedule(self):
        self.write_cfg(legacy_config(schedule={"enabled": True, "times": ["03:00"]}))
        self.srv.load_cfg()
        self.write_cfg(BROKEN)
        at_3 = time.strptime("2030-01-01 03:00", "%Y-%m-%d %H:%M")
        with mock.patch.object(self.srv, "start_engine", return_value=True) as start:
            fired = set()
            self.srv.scheduler_tick(fired, at_3)
            self.srv.scheduler_tick(fired, at_3)                # same minute: only once
            self.srv.scheduler_tick(fired, time.strptime("2030-01-01 04:00", "%Y-%m-%d %H:%M"))
        self.assertEqual(start.call_count, 1)
        self.assertIn("scheduled run 03:00: started", (self.logs / "pkg_sync.log").read_text())

    def test_a_scheduled_run_that_cannot_start_is_logged(self):
        self.write_cfg(legacy_config(schedule={"enabled": True, "times": ["03:00"]}))
        boom = self.srv.EngineStartError(409, "The settings file (config.json) could not be read")
        with mock.patch.object(self.srv, "start_engine", side_effect=boom):
            self.srv.scheduler_tick(set(), time.strptime("2030-01-01 03:00", "%Y-%m-%d %H:%M"))
        self.assertIn("scheduled run 03:00: did not start: The settings file", (self.logs / "pkg_sync.log").read_text())

    def test_saving_is_refused_while_the_file_is_unreadable_and_the_file_is_left_alone(self):
        self.write_cfg(BROKEN)
        before = self.snapshot()
        for r in (self.client.put("/api/config", json={"compression_level": 4}),
                  self.client.put("/api/overrides/PPSA01234", json={"skip": True})):
            self.assertEqual(r.status_code, 409, r.text)
            self.assertIn("line 4", r.json()["detail"])
            self.assertIn("Nothing was saved", r.json()["detail"])
        self.assertEqual(self.snapshot(), before)

    def test_a_run_started_on_an_unreadable_file_is_reported_as_failed_to_start(self):
        self.write_cfg(BROKEN)
        before = self.snapshot()
        r = self.client.post("/api/run", json={})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("could not be read", r.json()["detail"])
        self.assertFalse((self.state / "run.lock").exists())
        text = (self.logs / "pkg_sync.log").read_text()
        self.assertIn("not starting: config.json can't be used (line 4", text)
        for log in self.logs.glob("run_*.stdout.log"):
            self.assertNotIn("Traceback", log.read_text())
        self.assertEqual(self.snapshot(), before)

    def test_an_engine_that_exits_right_away_for_another_reason_is_reported_too(self):
        self.write_cfg(legacy_config(library=str(self.dir / "nowhere")))
        r = self.client.post("/api/run", json={})
        self.assertEqual(r.status_code, 500, r.text)
        self.assertIn("exited with code 2", r.json()["detail"])


class OutOfRangeValueInTheFile(ServerCase):
    def test_reported_replaced_in_memory_and_the_file_not_modified(self):
        self.write_cfg(legacy_config(compression_level=99, max_builds=2))
        before = self.snapshot()
        st = self.get("/api/status")
        self.assertEqual(len(st["config_problems"]), 1)
        self.assertIn("compression_level", st["config_problems"][0])
        self.assertIsNone(st["config_error"])
        cfg = self.get("/api/config")
        self.assertEqual(cfg["compression_level"], E.DEFAULT_CFG["compression_level"])
        self.assertEqual(cfg["max_builds"], 2)
        self.assertEqual(self.snapshot(), before)


class UnknownKeysAndFormat(ServerCase):
    def test_unknown_keys_survive_a_save_from_the_settings_page_and_from_the_library_page(self):
        raw = legacy_config()
        raw["from_the_future"] = {"a": [1, 2, {"b": None}]}
        raw["overrides"]["PPSA01234"]["future_field"] = "x"
        self.write_cfg(raw)
        self.assertEqual(self.client.put("/api/config", json={"compression_level": 4, "nice": 2}).status_code, 200)
        self.assertEqual(self.client.put("/api/overrides/PPSA09999", json={"note": "n"}).status_code, 200)
        after = json.loads(self.cfg_path.read_text())
        self.assertEqual(after["from_the_future"], raw["from_the_future"])
        self.assertEqual((after["compression_level"], after["nice"]), (4, 2))
        self.assertEqual(after["overrides"]["PPSA01234"], raw["overrides"]["PPSA01234"])
        self.assertEqual(after["overrides"]["PPSA09999"], {"note": "n"})
        self.assertEqual(after["overrides"]["PPSA05678"], raw["overrides"]["PPSA05678"])
        self.assertEqual(after["ultrapack_src"], raw["ultrapack_src"])

    def test_clearing_an_override_removes_only_that_title(self):
        self.write_cfg(legacy_config())
        r = self.client.put("/api/overrides/PPSA01234", json={})
        self.assertEqual((r.status_code, r.json()), (200, {}))
        self.assertEqual(list(json.loads(self.cfg_path.read_text())["overrides"]), ["PPSA05678"])

    def test_the_file_stays_pretty_json_with_real_characters(self):
        self.write_cfg(legacy_config())
        self.client.put("/api/overrides/PPSA01234", json={"note": "Préféré"})
        text = self.cfg_path.read_text(encoding="utf-8")
        self.assertIn("Préféré", text)
        self.assertIn('\n  "overrides": {\n    "PPSA01234": {', text)

    def test_the_put_checks_are_the_same_as_before(self):
        self.write_cfg(legacy_config())
        before = self.snapshot()
        for body, msg in [({"compression_level": 99}, "compression_level must be 1..9"),
                          ({"nice": 20}, "nice must be 0..19"),
                          ({"console_fw": "nine"}, "console_fw looks like 13.20"),
                          ({"library": 3}, "library must be text"),
                          ({"junk_file_patterns": ["("]}, "junk_file_patterns: bad pattern '('"),
                          ({"schedule": {"enabled": True, "times": ["3:00"]}}, "schedule.times"),
                          ({"not_a_setting": 1}, "unknown setting not_a_setting"),
                          ({"nice": 1, "compression_level": 0}, "compression_level must be 1..9")]:
            with self.subTest(body):
                r = self.client.put("/api/config", json=body)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn(msg, r.json()["detail"])
        self.assertEqual(self.client.put("/api/config", json=[1]).status_code, 400)
        self.assertEqual(self.snapshot(), before)                # a rejected save changes nothing, not even the valid part

    def test_the_settings_page_round_trip(self):
        self.write_cfg(legacy_config())
        form = {k: v for k, v in self.get("/api/config").items() if k != "config_rev"}
        form.update(compression_level=7, schedule={"enabled": False, "times": ["01:00"]})
        r = self.client.put("/api/config", json=form)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["schedule"], {"enabled": False, "times": ["01:00"]})
        self.assertEqual(self.get("/api/config")["compression_level"], 7)


class HandEdits(ServerCase):
    def test_a_hand_edit_shows_up_without_a_restart_and_changes_the_revision(self):
        self.write_cfg(legacy_config())
        rev1 = self.get("/api/status")["config_rev"]
        self.assertEqual(self.get("/api/config")["config_rev"], rev1)
        self.assertEqual(self.get("/api/config")["compression_level"], 5)
        self.write_cfg(legacy_config(compression_level=3), age=False)   # same size; only the mtime differs
        st = self.get("/api/status")
        self.assertNotEqual(st["config_rev"], rev1)
        cfg = self.get("/api/config")
        self.assertEqual((cfg["compression_level"], cfg["config_rev"]), (3, st["config_rev"]))

    def test_our_own_save_changes_the_revision_too(self):
        self.write_cfg(legacy_config())
        rev1 = self.get("/api/status")["config_rev"]
        self.client.put("/api/config", json={"nice": 1})
        self.assertNotEqual(self.get("/api/status")["config_rev"], rev1)


if __name__ == "__main__":
    unittest.main()
