"""Host folders on the server: /api/status `folders`, the Settings fallback (`host_paths`), the old
PS5PKG_HOST_PATHS variable, and what a malformed one does. Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import E, ServerCase, legacy_config

MOUNTINFO = r"""
22 1 0:21 / / rw,relatime - overlay overlay rw,lowerdir=/x
24 22 253:1 /games/ps5 /library ro,relatime shared:8 - btrfs /dev/mapper/vg-volume_1 rw
25 22 253:1 /games/ps5/pkg /output rw,relatime - btrfs /dev/mapper/vg-volume_1 rw
26 22 8:17 /scratch /work rw,noatime - ext4 /dev/sdb1 rw
"""
LIVE_JSON = json.dumps({"library": "/srv/games/ps5", "downloads": "/srv/games/dl", "output": "/srv/games/ps5/pkg",
                        "temp": "/srv/scratch/tmp"})


class HostCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output="/output", temp="/work/tmp"))

    def env(self, **kw):
        p = mock.patch.dict(os.environ, kw)
        p.start()
        self.addCleanup(p.stop)

    def mounts(self, text=MOUNTINFO):
        (self.dir / "mountinfo").write_text(text)

    def folders(self):
        return self.get("/api/status")["folders"]


class StatusFolders(HostCase):
    def test_each_folder_reports_container_path_host_path_source_and_mismatch(self):
        self.env(PS5PKG_HOST_LIBRARY="/srv/games/ps5", PS5PKG_HOST_OUTPUT="/srv/games/ps5/pkg")
        f = self.folders()
        self.assertEqual(list(f), ["library", "downloads", "output", "temp", "config"])
        for k, row in f.items():
            self.assertTrue({"container", "host", "source", "mismatch"} <= set(row), k)
        self.assertEqual((f["library"]["container"], f["library"]["host"], f["library"]["source"], f["library"]["mismatch"]),
                         ("/library", "/srv/games/ps5", "env", False))
        self.assertEqual(f["library"]["via"], "PS5PKG_HOST_LIBRARY")
        self.assertEqual((f["downloads"]["host"], f["downloads"]["source"]), (None, "none"))

    def test_the_config_folder_is_a_folder_too(self):
        self.env(PS5PKG_HOST_CONFIG="/srv/ps5-pkg-station/config")
        st = self.get("/api/status")
        self.assertEqual(st["folders"]["config"]["host"], "/srv/ps5-pkg-station/config")
        self.assertEqual(st["folders"]["config"]["container"], os.path.commonpath([str(self.state), str(self.logs)]))
        self.assertIn("config", st["disks"])
        self.assertEqual(st["disks"]["config"]["path"], st["folders"]["config"]["container"])

    def test_the_old_json_variable_works_unchanged(self):
        self.env(PS5PKG_HOST_PATHS=LIVE_JSON)
        st = self.get("/api/status")
        f = st["folders"]
        self.assertEqual({k: (f[k]["host"], f[k]["source"], f[k]["via"]) for k in ("library", "downloads", "output", "temp")},
                         {k: (v, "env", "PS5PKG_HOST_PATHS") for k, v in json.loads(LIVE_JSON).items()})
        self.assertEqual(f["temp"]["container"], "/work/tmp")          # the work temp dir, as before
        self.assertEqual(st["host_paths"], json.loads(LIVE_JSON))      # the old field keeps its meaning
        self.assertEqual(st["host_problems"], [])
        self.assertEqual(f["config"]["source"], "none")

    def test_precedence_env_then_json_then_settings_then_detected(self):
        self.mounts()
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output="/output", temp="/work/tmp",
                                     host_paths={"library": "/from/settings", "output": "/from/settings/pkg",
                                                 "temp": "/from/settings/tmp"}))
        self.env(PS5PKG_HOST_LIBRARY="/from/env", PS5PKG_HOST_PATHS=json.dumps({"library": "/from/json", "output": "/from/json/pkg"}))
        f = self.folders()
        self.assertEqual([(f[k]["host"], f[k]["source"]) for k in ("library", "output", "temp")],
                         [("/from/env", "env"), ("/from/json/pkg", "env"), ("/from/settings/tmp", "settings")])
        self.assertEqual(f["downloads"]["source"], "none")

    def test_the_work_variable_is_the_volume_and_gets_the_sub_folder(self):
        self.env(PS5PKG_HOST_WORK="/srv/scratch")
        self.assertEqual(self.folders()["temp"]["host"], "/srv/scratch/tmp")

    def test_with_nothing_configured_the_detected_tail_is_the_hint(self):
        self.mounts()
        f = self.folders()
        self.assertEqual([(f[k]["source"], f[k]["host"], f[k]["detected"]) for k in ("library", "output", "temp")],
                         [("detected", "/games/ps5", "/games/ps5"), ("detected", "/games/ps5/pkg", "/games/ps5/pkg"),
                          ("detected", "/scratch/tmp", "/scratch/tmp")])
        self.assertEqual([f[k]["mode"] for k in ("library", "output")], ["ro", "rw"])
        self.assertEqual(f["downloads"]["source"], "none")

    def test_nothing_configured_and_nothing_detected(self):
        self.assertEqual({r["source"] for r in self.folders().values()}, {"none"})

    def test_a_configured_path_that_disagrees_with_the_mount_is_flagged(self):
        self.mounts()
        self.env(PS5PKG_HOST_LIBRARY="/srv/games/ps4", PS5PKG_HOST_OUTPUT="/mnt/disk1/games/ps5/pkg")
        f = self.folders()
        self.assertTrue(f["library"]["mismatch"])
        self.assertEqual(f["library"]["detected"], "/games/ps5")
        self.assertFalse(f["output"]["mismatch"])

    def test_a_malformed_json_variable_is_reported_and_the_other_variables_still_work(self):
        self.env(PS5PKG_HOST_PATHS='{"library": "/srv/games/ps5", }', PS5PKG_HOST_OUTPUT="/srv/pkg")
        st = self.get("/api/status")
        self.assertEqual(len(st["host_problems"]), 1)
        self.assertIn("PS5PKG_HOST_PATHS is not valid JSON", st["host_problems"][0])
        self.assertEqual(st["folders"]["library"]["source"], "none")
        self.assertEqual(st["folders"]["output"]["host"], "/srv/pkg")
        row = {c["id"]: c for c in self.get("/api/setup")}["host_paths"]
        self.assertEqual((row["level"], row["advisory"]), ("warn", False))
        self.assertIn("not valid JSON", row["message"])

    def test_the_setup_row_for_folder_locations(self):
        row = lambda: {c["id"]: c for c in self.get("/api/setup")}["host_paths"]
        self.assertEqual((row()["level"], row()["advisory"]), ("warn", True))
        self.assertEqual(row()["link"], {"tab": "set", "field": "host_paths.library"})
        self.env(PS5PKG_HOST_LIBRARY="/srv/games/ps5")
        self.assertEqual(row()["level"], "ok")


class SettingsFallback(HostCase):
    def test_labels_can_be_saved_from_settings_and_survive_a_container_recreate(self):
        r = self.client.put("/api/config", json={"host_paths": {"library": "/srv/games/ps5/", "output": " /srv/games/ps5/pkg ",
                                                                "temp": ""}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["host_paths"], {"library": "/srv/games/ps5", "output": "/srv/games/ps5/pkg"})
        self.assertEqual(json.loads(self.cfg_path.read_text())["host_paths"],
                         {"library": "/srv/games/ps5", "output": "/srv/games/ps5/pkg"})
        E._last_good = None                                              # a new container: nothing in memory
        cfg, f = self.get("/api/config"), self.folders()
        self.assertEqual(cfg["host_paths"], {"library": "/srv/games/ps5", "output": "/srv/games/ps5/pkg"})
        self.assertEqual((f["library"]["host"], f["library"]["source"]), ("/srv/games/ps5", "settings"))
        self.assertEqual(cfg["folders"]["output"]["source"], "settings")

    def test_an_environment_variable_wins_over_the_setting_which_stays_stored(self):
        self.client.put("/api/config", json={"host_paths": {"library": "/from/settings"}})
        self.env(PS5PKG_HOST_LIBRARY="/from/env")
        f = self.get("/api/config")["folders"]["library"]
        self.assertEqual((f["host"], f["source"], f["via"], f["setting"]), ("/from/env", "env", "PS5PKG_HOST_LIBRARY", "/from/settings"))
        self.assertEqual(self.get("/api/config")["host_paths"], {"library": "/from/settings"})

    def test_bad_values_are_refused_and_nothing_is_saved(self):
        before = self.snapshot()
        for body, text in [({"host_paths": {"library": "games/ps5"}}, "host_paths.library must be an absolute path"),
                           ({"host_paths": {"pkg": "/x"}}, "unknown folder 'pkg'"), ({"host_paths": {"library": 5}}, "must be text"),
                           ({"host_paths": ["/x"]}, "host_paths must be an object")]:
            with self.subTest(body):
                r = self.client.put("/api/config", json=body)
                self.assertEqual(r.status_code, 400, r.text)
                self.assertIn(text, r.json()["detail"])
        self.assertEqual(self.snapshot(), before)

    def test_the_page_is_told_whether_container_paths_are_fixed_by_docker(self):
        with mock.patch.object(E, "IN_DOCKER", True):
            self.assertIs(self.get("/api/config")["in_docker"], True)
        with mock.patch.object(E, "IN_DOCKER", False):
            self.assertIs(self.get("/api/status")["in_docker"], False)

    def test_the_container_paths_are_still_part_of_the_settings(self):
        self.assertEqual(self.client.put("/api/config", json={"temp": "/work/scratch"}).status_code, 200)
        self.assertEqual(self.get("/api/status")["folders"]["temp"]["container"], "/work/scratch")


if __name__ == "__main__":
    unittest.main()
