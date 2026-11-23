"""Storage & backup in the web API: where the settings live, export / import with a preview, the automatic backups
and restore, the full backup zip, the per-game overrides list and removal, and the versions. Needs fastapi and
httpx (skipped without them)."""
from __future__ import annotations

import io
import json
import os
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from support import E, ServerCase, legacy_config

OVERRIDES = {"PPSA01234": {"source": "/library/Example Game [PPSA01234].exfat", "modified_exec": "image", "note": "backport", "skip_verify": True},
             "PPSA05678": {"patch": "/config/patches/PPSA05678", "skip": True},
             "PPSA09999": {"note": "game left the library", "weird_future_field": [1]}}
PLAN = [dict(title_id="PPSA01234", title="Example Game", action="build", source="/library/x", candidates=[]),
        dict(title_id="PPSA05678", title="Other Game", action="done", source="/library/y", candidates=[])]
HOST_ENV = {"PS5PKG_HOST_CONFIG": "/srv/docker/ps5-pkg/config", "PS5PKG_HOST_LIBRARY": "/srv/games/ps5"}


class StorageCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config(overrides=json.loads(json.dumps(OVERRIDES)), console_fw="9.60", nice=3,
                                     notify_url="https://ntfy.example/private-topic"))
        (self.state / "plan.json").write_text(json.dumps(PLAN))
        # the fixtures name patch folders as the container does (/config/patches/...): so is the folder that is checked against
        p = mock.patch.object(self.srv, "patch_root", return_value=Path("/config/patches"))
        p.start()
        self.addCleanup(p.stop)

    def backups(self):
        return [b["name"] for b in E.list_backups()]

    def raw(self):
        return json.loads(self.cfg_path.read_text())


class Location(StorageCase):
    def test_it_says_where_everything_is_in_the_container_and_on_the_host(self):
        (self.state / "ledger.json").write_text(json.dumps({"PPSA01234": {"output": "/output/x.pkg"}, "PPSA05678": {}, "old": "garbage"}))
        (self.state / "failures.json").write_text(json.dumps({"PPSA09999": {"state": "failed"}}))
        cfg_root = os.path.commonpath([str(self.state), str(self.logs)])
        s = self.get("/api/storage")
        self.assertEqual(s["settings"]["path"], str(self.cfg_path))
        self.assertTrue(s["settings"]["exists"])
        self.assertIsNone(s["settings"]["error"])
        self.assertAlmostEqual(s["settings"]["changed"], self.cfg_path.stat().st_mtime, places=2)
        self.assertEqual((s["history"]["path"], s["history"]["packages"]), (str(self.state / "ledger.json"), 2))
        self.assertEqual((s["failures"]["path"], s["failures"]["count"]), (str(self.state / "failures.json"), 1))
        self.assertIn("state, not settings", s["state_note"])
        self.assertEqual(cfg_root, str(self.dir))

    def test_the_host_path_is_shown_when_the_config_folder_has_one(self):
        with mock.patch.dict(os.environ, HOST_ENV):
            s = self.get("/api/storage")
        root = os.path.commonpath([str(self.state), str(self.logs)])
        rel = str(self.cfg_path)[len(root):]
        self.assertEqual(s["settings"]["host"], "/srv/docker/ps5-pkg/config" + rel)
        self.assertEqual(s["history"]["host"], "/srv/docker/ps5-pkg/config" + str(self.state / "ledger.json")[len(root):])
        self.assertEqual(s["backups"]["folder"]["host"], "/srv/docker/ps5-pkg/config" + str(self.state / "backups")[len(root):])

    def test_without_a_host_location_only_the_container_path_is_given(self):
        s = self.get("/api/storage")
        self.assertIsNone(s["settings"]["host"])
        self.assertIsNone(s["history"]["host"])

    def test_missing_files_are_reported_as_such(self):
        s = self.get("/api/storage")
        self.assertEqual((s["history"]["exists"], s["history"]["packages"], s["failures"]["count"]), (False, 0, 0))

    def test_the_versions_are_in_storage_and_in_status(self):
        with mock.patch.dict(os.environ, {"PS5PKG_ULTRAPACK_VERSION": "2.7.1"}):
            s, st = self.get("/api/storage"), self.get("/api/status")
        self.assertEqual(s["versions"], st["version"])
        self.assertEqual(st["version"]["ultrapack"], "2.7.1")
        self.assertEqual(st["version"]["app"], (E.ROOT.parent / "VERSION").read_text().strip())
        with mock.patch.dict(os.environ):
            os.environ.pop("PS5PKG_ULTRAPACK_VERSION", None)
            self.assertIsNone(self.get("/api/status")["version"]["ultrapack"])

    def test_reading_never_writes_a_backup_or_touches_the_file(self):
        before = self.snapshot()
        for _ in range(5):
            for path in ("/api/storage", "/api/status", "/api/config", "/api/export", "/api/backups", "/api/overrides", "/api/backup.zip"):
                self.assertEqual(self.client.get(path).status_code, 200, path)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.state / "backups").exists())


class Overrides(StorageCase):
    def test_every_override_is_listed_including_for_games_that_are_not_in_the_library(self):
        rows = {r["title_id"]: r for r in self.get("/api/storage")["overrides"]}
        self.assertEqual(sorted(rows), ["PPSA01234", "PPSA05678", "PPSA09999"])
        self.assertEqual((rows["PPSA01234"]["title"], rows["PPSA01234"]["in_library"]), ("Example Game", True))
        self.assertEqual((rows["PPSA05678"]["title"], rows["PPSA05678"]["in_library"]), ("Other Game", True))
        self.assertEqual((rows["PPSA09999"]["title"], rows["PPSA09999"]["in_library"]), ("", False))
        self.assertEqual(rows["PPSA01234"]["what"], ["source: Example Game [PPSA01234].exfat", "keep the backport", "no archive check"])
        self.assertEqual(rows["PPSA05678"]["what"], ["patch folder", "skipped"])
        self.assertEqual(rows["PPSA09999"]["what"], ["other: weird_future_field"])
        self.assertEqual(rows["PPSA01234"]["note"], "backport")
        self.assertEqual(rows["PPSA01234"]["override"], OVERRIDES["PPSA01234"])

    def test_the_patch_folder_has_a_host_twin_when_known(self):
        with mock.patch.dict(os.environ, HOST_ENV):
            self.write_cfg(legacy_config(overrides={"PPSA05678": {"patch": os.path.commonpath([str(self.state), str(self.logs)]) + "/patches/PPSA05678"}}))
            row = self.get("/api/storage")["overrides"][0]
        self.assertEqual(row["patch_host"], "/srv/docker/ps5-pkg/config/patches/PPSA05678")

    def test_remove_deletes_just_that_entry(self):
        before = self.raw()
        r = self.client.delete("/api/overrides/PPSA05678")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "removed": "PPSA05678"}))
        after = self.raw()
        self.assertEqual(sorted(after["overrides"]), ["PPSA01234", "PPSA09999"])
        self.assertEqual(after["overrides"]["PPSA01234"], OVERRIDES["PPSA01234"])
        self.assertEqual({k: v for k, v in after.items() if k != "overrides"}, {k: v for k, v in before.items() if k != "overrides"})
        self.assertEqual(len(self.backups()), 1)                                         # the change was backed up
        self.assertEqual(json.loads((self.state / "backups" / self.backups()[0]).read_text())["overrides"].keys(), before["overrides"].keys())

    def test_an_override_for_a_game_that_is_gone_can_be_removed_and_an_unknown_one_is_a_404(self):
        self.assertEqual(self.client.delete("/api/overrides/PPSA09999").status_code, 200)
        before = self.snapshot()
        self.assertEqual(self.client.delete("/api/overrides/PPSA09999").status_code, 404)
        self.assertEqual(self.client.delete("/api/overrides/PPSA11111").status_code, 404)
        self.assertEqual(self.snapshot(), before)                                         # a 404 writes nothing, not even a backup

    def test_remove_is_refused_while_the_file_is_broken(self):
        self.write_cfg('{"nice": 3\n "x": 1}')
        before = self.snapshot()
        self.assertEqual(self.client.delete("/api/overrides/PPSA05678").status_code, 409)
        self.assertEqual(self.snapshot(), before)


class Export(StorageCase):
    def test_it_is_the_documented_json_as_a_download(self):
        r = self.client.get("/api/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment; filename=\"ps5-pkg-station-settings-", r.headers["content-disposition"])
        doc = r.json()
        self.assertEqual(set(doc), {"format", "app_version", "exported_at", "includes_notify_url", "settings", "overrides"})
        self.assertEqual((doc["settings"]["console_fw"], doc["settings"]["nice"], doc["overrides"]), ("9.60", 3, OVERRIDES))
        self.assertEqual(doc["settings"]["notify_url"], "https://ntfy.example/private-topic")
        for k in ("ultrapack_src", "python"):
            self.assertNotIn(k, doc["settings"])

    def test_the_notification_url_can_be_left_out(self):
        r = self.client.get("/api/export?notify_url=0")
        self.assertNotIn("private-topic", r.text)
        self.assertFalse(r.json()["includes_notify_url"])

    def test_nothing_is_exported_from_a_file_that_cannot_be_read(self):
        self.write_cfg('{"nice": 3\n "x": 1}')
        r = self.client.get("/api/export")
        self.assertEqual(r.status_code, 409)
        self.assertIn("Fix the file first", r.json()["detail"])


class Import(StorageCase):
    def doc(self, settings=None, overrides=None, **kw):
        d = dict(format=1, settings=settings if settings is not None else {}, overrides=overrides if overrides is not None else {})
        return dict(d, **kw)

    def post(self, doc, query=""):
        return self.client.post("/api/import" + query, content=doc if isinstance(doc, str) else json.dumps(doc), headers={"Content-Type": "application/json"})

    def test_a_dry_run_says_what_would_change_and_writes_nothing(self):
        before = self.snapshot()
        r = self.post(self.doc(settings={"console_fw": "10.00", "nice": 9, "max_builds": 4}, overrides={"PPSA01234": {"skip": True}, "PPSA00001": {"skip": True}, "PPSA00002": {"note": "n"}}),
                      "?dry_run=1")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["ok"], d["dry_run"]), (True, True))
        self.assertEqual(d["summary"], "3 settings change, 2 overrides added, 1 replaced")
        self.assertEqual({c["key"] for c in d["settings"]["changed"]}, {"console_fw", "nice", "max_builds"})
        self.assertEqual(self.snapshot(), before)                                         # nothing written
        self.assertFalse((self.state / "backups").exists())

    def test_applying_writes_atomically_backs_up_the_old_file_and_returns_the_new_config(self):
        old = self.cfg_path.read_bytes()
        r = self.post(self.doc(settings={"console_fw": "10.00", "nice": 9}, overrides={"PPSA00001": {"skip": True}}))
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual((d["dry_run"], d["summary"], d["config"]["console_fw"], d["config"]["nice"]), (False, "2 settings change, 1 override added", "10.00", 9))
        self.assertEqual(d["overrides"]["PPSA00001"], {"skip": True})
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(d["backup"], self.backups()[0])
        self.assertEqual((self.state / "backups" / d["backup"]).read_bytes(), old)
        self.assertEqual((self.raw()["console_fw"], self.raw()["nice"]), ("10.00", 9))
        self.assertEqual([p.name for p in self.state.iterdir() if p.suffix == ".part"], [])

    def test_a_rejected_import_names_the_key_and_writes_nothing(self):
        before = self.snapshot()
        for doc, key in [(self.doc(settings={"from_the_future": 1}), "settings.from_the_future"), (self.doc(settings={"compression_level": 99}), "settings.compression_level"),
                         (self.doc(settings={"build_on_ssd": "yes"}), "settings.build_on_ssd"), (self.doc(overrides={"nope": {}}), "overrides.nope"),
                         (self.doc(overrides={"PPSA00001": {"skip": "yes"}}), "overrides.PPSA00001.skip")]:
            for query in ("", "?dry_run=1"):
                with self.subTest(key=key, query=query):
                    r = self.post(doc, query)
                    self.assertEqual(r.status_code, 400, r.text)
                    self.assertTrue(r.json()["detail"].startswith("Not imported: "))
                    self.assertTrue(any(p.startswith(key) for p in r.json()["problems"]), r.json())
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.state / "backups").exists())

    def test_one_bad_value_blocks_the_whole_file(self):
        before = self.snapshot()
        r = self.post(self.doc(settings={"nice": 9, "compression_level": 99}, overrides={"PPSA00001": {"skip": True}}))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(r.json()["problems"]), 1)
        self.assertEqual(self.snapshot(), before)                                         # not even the valid nice

    def test_not_json_and_not_an_export(self):
        before = self.snapshot()
        self.assertEqual(self.post("{nope").status_code, 400)
        self.assertEqual(self.post("[1]").status_code, 400)
        self.assertEqual(self.post({}).status_code, 400)
        self.assertEqual(self.post(self.doc(settings={"nice": 1}), "?parts=everything").status_code, 400)
        self.assertEqual(self.snapshot(), before)

    def test_unknown_keys_and_other_overrides_are_never_dropped(self):
        raw = self.raw()
        raw["from_the_future"] = {"a": [1, 2]}
        self.write_cfg(raw)
        r = self.post(self.doc(settings={"nice": 9}, overrides={"PPSA01234": {"skip": True}}))
        self.assertEqual(r.status_code, 200, r.text)
        after = self.raw()
        self.assertEqual(after["from_the_future"], {"a": [1, 2]})
        self.assertEqual(after["ultrapack_src"], raw["ultrapack_src"])
        self.assertEqual(after["overrides"]["PPSA05678"], OVERRIDES["PPSA05678"])
        self.assertEqual(after["overrides"]["PPSA09999"], OVERRIDES["PPSA09999"])         # its unknown field too
        self.assertEqual(after["overrides"]["PPSA01234"], {"skip": True})                  # replaced, as the file says

    def test_only_the_chosen_parts_are_imported(self):
        self.assertEqual(self.post(self.doc(settings={"nice": 9}, overrides={"PPSA00001": {"skip": True}}), "?parts=overrides").status_code, 200)
        self.assertEqual((self.raw()["nice"], "PPSA00001" in self.raw()["overrides"]), (3, True))
        self.assertEqual(self.post(self.doc(settings={"nice": 9}, overrides={"PPSA00002": {"skip": True}}), "?parts=settings").status_code, 200)
        self.assertEqual((self.raw()["nice"], "PPSA00002" in self.raw()["overrides"]), (9, False))

    def test_a_file_that_changes_nothing_writes_nothing(self):
        before = self.snapshot()
        r = self.post(self.doc(settings={"console_fw": "9.60"}))
        self.assertEqual((r.status_code, r.json()["summary"], r.json()["backup"]), (200, "Nothing would change.", None))
        self.assertEqual(self.snapshot(), before)

    def test_it_is_refused_while_the_current_file_is_unreadable(self):
        self.write_cfg('{"nice": 3\n "x": 1}')
        before = self.snapshot()
        r = self.post(self.doc(settings={"nice": 1}))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.snapshot(), before)

    def test_the_container_folders_in_a_file_are_flagged_in_the_preview(self):
        d = self.post(self.doc(settings={"library": "/mnt/other"}), "?dry_run=1").json()
        self.assertIn("inside the container", d["settings"]["changed"][0]["note"])

    def test_export_from_one_instance_and_import_into_a_fresh_one_reproduces_settings_and_overrides(self):
        exported = self.client.get("/api/export").json()
        want_cfg, want_ovr = self.get("/api/config"), self.get("/api/overrides")
        self.cfg_path.unlink()                                                             # a fresh instance
        E._last_good = None
        fresh = self.get("/api/config")
        self.assertNotEqual((fresh["console_fw"], fresh["nice"]), (want_cfg["console_fw"], want_cfg["nice"]))
        self.assertEqual(self.get("/api/overrides"), {})
        self.assertEqual(self.post(exported).status_code, 200)
        got_cfg, got_ovr = self.get("/api/config"), self.get("/api/overrides")
        self.assertEqual({k: got_cfg[k] for k in E.SETTINGS}, {k: want_cfg[k] for k in E.SETTINGS})
        self.assertEqual(got_ovr, want_ovr)
        self.assertEqual(self.client.get("/api/export").json()["settings"], exported["settings"])
        self.assertEqual(self.client.get("/api/export").json()["overrides"], exported["overrides"])


class AutomaticBackups(StorageCase):
    def test_every_save_makes_a_backup_and_only_the_newest_ten_stay(self):
        stamps = iter(f"202611{d:02d}-030000" for d in range(1, 40))
        with mock.patch.object(E.time, "strftime", lambda fmt, *a: next(stamps) if fmt == "%Y%m%d-%H%M%S" else "x"):
            for n in range(1, 15):
                self.assertEqual(self.client.put("/api/config", json={"cpu_count": n}).status_code, 200)
        listing = self.get("/api/backups")
        self.assertEqual((listing["keep"], len(listing["items"])), (10, 10))
        self.assertEqual(listing["items"][0]["name"], "config-20261114-030000.json")
        self.assertEqual(self.get("/api/storage")["backups"]["count"], 10)

    def test_a_save_of_the_same_values_makes_none(self):
        self.assertEqual(self.client.put("/api/config", json={"nice": 3}).status_code, 200)
        self.assertEqual(self.backups(), [])

    def test_overrides_saved_from_the_library_are_backed_up_too(self):
        self.client.put("/api/overrides/PPSA00001", json={"skip": True})
        self.assertEqual(len(self.backups()), 1)

    def test_restore_previews_then_makes_the_backup_current_and_backs_up_what_it_replaced(self):
        self.client.put("/api/config", json={"nice": 4, "console_fw": "10.00"})
        self.client.delete("/api/overrides/PPSA05678")
        names = self.backups()
        self.assertEqual(len(names), 2)
        oldest = names[-1]                                                                  # the file as it was at the start
        current = self.cfg_path.read_bytes()
        d = self.client.post(f"/api/backups/{oldest}/restore?dry_run=1").json()
        self.assertEqual((d["dry_run"], d["ok"]), (True, True))
        self.assertEqual({c["key"]: (c["from"], c["to"]) for c in d["settings"]["changed"]}, {"nice": (4, 3), "console_fw": ("10.00", "9.60")})
        self.assertEqual([a["title_id"] for a in d["overrides"]["added"]], ["PPSA05678"])
        self.assertEqual(self.cfg_path.read_bytes(), current)                                # the preview wrote nothing
        r = self.client.post(f"/api/backups/{oldest}/restore")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((self.raw()["nice"], self.raw()["console_fw"]), (3, "9.60"))
        self.assertIn("PPSA05678", self.raw()["overrides"])
        self.assertEqual(r.json()["backup"], self.backups()[0])
        self.assertEqual((self.state / "backups" / r.json()["backup"]).read_bytes(), current)   # what it replaced is the newest backup

    def test_restore_reports_what_it_would_remove(self):
        self.client.put("/api/overrides/PPSA11111", json={"skip": True})
        oldest = self.backups()[-1]
        d = self.client.post(f"/api/backups/{oldest}/restore?dry_run=1").json()
        self.assertEqual([r["title_id"] for r in d["overrides"]["removed"]], ["PPSA11111"])

    def test_restore_over_an_unreadable_file(self):
        self.client.put("/api/config", json={"nice": 4})
        name = self.backups()[0]
        self.write_cfg('{"nice": 4\n "x": 1}')
        d = self.client.post(f"/api/backups/{name}/restore?dry_run=1").json()
        self.assertTrue(d["replaces_unreadable_file"])
        self.assertEqual(self.client.post(f"/api/backups/{name}/restore").status_code, 200)
        self.assertEqual(self.get("/api/status")["config_error"], None)

    def test_bad_names_and_missing_backups(self):
        for name, status in (("config-oops.json", 400), ("config-1.json.bak", 400), ("config-20200101-000000.json", 404)):
            for q in ("", "?dry_run=1"):
                self.assertEqual(self.client.post(f"/api/backups/{name}/restore{q}").status_code, status, (name, q))
        (self.state / "backups").mkdir(exist_ok=True)
        (self.state / "backups" / "config-20200101-000000.json").write_text("[1]")
        before = self.snapshot()
        self.assertEqual(self.client.post("/api/backups/config-20200101-000000.json/restore").status_code, 400)
        self.assertEqual(self.snapshot(), before)


class FullBackup(StorageCase):
    def zip(self, query=""):
        r = self.client.get("/api/backup.zip" + query)
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "application/zip"))
        self.assertIn("attachment; filename=\"ps5-pkg-station-backup-", r.headers["content-disposition"])
        return zipfile.ZipFile(io.BytesIO(r.content))

    def test_it_holds_the_settings_file_the_history_and_a_list_of_patch_folders(self):
        (self.state / "ledger.json").write_text(json.dumps({"PPSA01234": {"output": "/output/x.pkg"}}))
        (self.state / "failures.json").write_text(json.dumps({"PPSA09999": {"state": "failed"}}))
        patches = os.path.commonpath([str(self.state), str(self.logs)]) + "/patches"
        os.makedirs(patches + "/PPSA05678")
        open(patches + "/PPSA05678/overlay.bin", "w").write("x")
        z = self.zip()
        self.assertEqual(sorted(z.namelist()), ["README.txt", "config.json", "ledger.json", "manifest.json", "patch-folders.json"])
        self.assertEqual(z.read("config.json"), self.cfg_path.read_bytes())                  # exactly the file
        self.assertEqual(json.loads(z.read("ledger.json")), {"PPSA01234": {"output": "/output/x.pkg"}})
        pf = json.loads(z.read("patch-folders.json"))
        self.assertEqual(pf["referenced_by_overrides"][0]["title_id"], "PPSA05678")
        self.assertEqual(pf["folders_in_patches_dir"], ["PPSA05678"])
        self.assertNotIn("overlay.bin", " ".join(z.namelist()))                               # names only, not their files
        self.assertEqual(json.loads(z.read("manifest.json"))["contains"], ["config.json", "ledger.json"])
        self.assertIn("failures.json", self.zip("?failures=1").namelist())
        self.assertNotIn("failures.json", z.namelist())                                        # a separate, explicit option

    def test_it_works_when_there_is_no_history_and_when_the_settings_file_is_broken(self):
        self.assertEqual(sorted(self.zip().namelist()), ["README.txt", "config.json", "manifest.json", "patch-folders.json"])
        broken = '{"nice": 3\n "x": 1}'
        self.write_cfg(broken)
        self.assertEqual(self.zip().read("config.json").decode(), broken)                     # the raw bytes, worth keeping


if __name__ == "__main__":
    unittest.main()
