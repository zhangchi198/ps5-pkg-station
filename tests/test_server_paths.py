"""Host paths in the web API: /api/plan, /api/ledger, /api/status (results, log tails), /api/overrides and the log
viewer carry `*_host` twins next to the container paths, and nothing is translated that has no host location.
Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import E, ServerCase, legacy_config

ENV = {"PS5PKG_HOST_LIBRARY": "/srv/games/ps5", "PS5PKG_HOST_DOWNLOADS": "/srv/games/dl", "PS5PKG_HOST_OUTPUT": "/srv/games/ps5/pkg",
       "PS5PKG_HOST_WORK": "/srv/scratch", "PS5PKG_HOST_CONFIG": "/srv/docker/ps5-pkg/config"}
NAME = "Pokémon Example [PPSA01234]"
EXFAT = f"/library/{NAME}.exfat"
RAR = "/downloads/Example.Game.PPSA01234/example.part1.rar"
PLAN = [dict(title_id="PPSA01234", title=NAME, version="01.002.003", fw="9.00", source=EXFAT, kind="exfat", size=5, action="build",
             why="new", name=f"{NAME} [v01.002.003].pkg", patch="/config/patches/PPSA01234", verify_archive=RAR,
             candidates=[dict(source=EXFAT, kind="exfat", version="01.002.003", packed="", size=5),
                         dict(source=RAR, kind="rar", version="01.002.003", packed="", size=5)],
             skipped_duplicates=[f"{RAR} (v01.002.003)", "/elsewhere/Example.exfat (v01.000.000, LZ4)"]),
        dict(title_id="PPSA05678", title="Broken", action="error", why="override source not found: /library/Missing.exfat")]
LEDGER = {"PPSA01234": dict(version="01.002.003", output=f"/output/{NAME} [v01.002.003].pkg", source=EXFAT, built="2026-11-20 03:10",
                            prep_min=12.5, build_min=40.1, bytes=5 << 30, level=7, validate="summary: ok", sha256="ab" * 32)}
# written by older versions: other field names, gaps, even a stray non-object
OLD_LEDGER = {"PPSA00001": dict(output="/output/Old Game [PPSA00001].pkg", validated="summary: ok", seconds=1800, built="2026-01-02 03:04"),
              "PPSA00002": dict(source="/library/Old Two.exfat"), "PPSA00003": dict(output=None, source=5), "PPSA00004": "garbage",
              "PPSA00005": None, "PPSA00006": dict(output="/output/Elsewhere/old.pkg")}


class PathsCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output="/output", temp="/work/tmp"))

    def env(self, **kw):
        p = mock.patch.dict(os.environ, kw)
        p.start()
        self.addCleanup(p.stop)

    def put(self, name, obj):
        (self.state / name).write_text(json.dumps(obj, ensure_ascii=False))


class Plan(PathsCase):
    def test_sources_candidates_patch_and_duplicates_get_host_twins(self):
        self.env(**ENV)
        self.put("plan.json", PLAN)
        p = self.get("/api/plan")[0]
        self.assertEqual(p["source"], EXFAT)                                           # the real field is untouched
        self.assertEqual(p["source_host"], f"/srv/games/ps5/{NAME}.exfat")
        self.assertEqual(p["patch_host"], "/srv/docker/ps5-pkg/config/patches/PPSA01234")
        self.assertEqual(p["verify_archive_host"], "/srv/games/dl/Example.Game.PPSA01234/example.part1.rar")
        self.assertEqual([c["source_host"] for c in p["candidates"]],
                         [f"/srv/games/ps5/{NAME}.exfat", "/srv/games/dl/Example.Game.PPSA01234/example.part1.rar"])
        self.assertEqual([c["source"] for c in p["candidates"]], [EXFAT, RAR])
        self.assertEqual(p["skipped_duplicates_host"], ["/srv/games/dl/Example.Game.PPSA01234/example.part1.rar (v01.002.003)",
                                                        "/elsewhere/Example.exfat (v01.000.000, LZ4)"])
        self.assertEqual(self.get("/api/plan")[1]["why_host"], "override source not found: /srv/games/ps5/Missing.exfat")

    def test_without_a_host_location_nothing_is_added(self):
        self.put("plan.json", PLAN)
        self.assertEqual(self.get("/api/plan"), PLAN)

    def test_a_folder_that_only_has_a_detected_hint_gets_no_twin(self):
        self.put("plan.json", PLAN)
        (self.dir / "mountinfo").write_text("24 22 253:1 /@syno/share/games/ps5 /library ro - btrfs /dev/mapper/pool_1 rw\n")
        self.assertEqual(self.get("/api/plan"), PLAN)
        self.assertEqual(self.get("/api/status")["folders"]["library"]["source"], "detected")

    def test_only_the_folders_that_are_mapped_get_twins(self):
        self.env(PS5PKG_HOST_LIBRARY="/srv/games/ps5")
        self.put("plan.json", PLAN)
        p = self.get("/api/plan")[0]
        self.assertIn("source_host", p)
        self.assertNotIn("verify_archive_host", p)                                    # /downloads is not mapped
        self.assertNotIn("patch_host", p)
        self.assertNotIn("source_host", p["candidates"][1])

    def test_an_empty_or_odd_plan_does_not_break_anything(self):
        self.env(**ENV)
        for odd in ([], {}, [None, 5, "x"], [{"candidates": "nope", "source": 3}]):
            self.put("plan.json", odd)
            self.assertEqual(self.get("/api/plan"), odd)


class Ledger(PathsCase):
    def test_the_package_and_its_source_get_host_twins(self):
        self.env(**ENV)
        self.put("ledger.json", LEDGER)
        v = self.get("/api/ledger")["PPSA01234"]
        self.assertEqual(v["output"], f"/output/{NAME} [v01.002.003].pkg")
        self.assertEqual(v["output_host"], f"/srv/games/ps5/pkg/{NAME} [v01.002.003].pkg")
        self.assertEqual(v["source_host"], f"/srv/games/ps5/{NAME}.exfat")

    def test_entries_from_older_versions_do_not_crash_it(self):
        self.env(**ENV)
        self.put("ledger.json", {**OLD_LEDGER, **LEDGER})
        got = self.get("/api/ledger")
        self.assertEqual(got["PPSA00001"], dict(OLD_LEDGER["PPSA00001"], output_host="/srv/games/ps5/pkg/Old Game [PPSA00001].pkg"))
        self.assertEqual(got["PPSA00002"], dict(OLD_LEDGER["PPSA00002"], source_host="/srv/games/ps5/Old Two.exfat"))
        self.assertEqual(got["PPSA00003"], OLD_LEDGER["PPSA00003"])
        self.assertEqual((got["PPSA00004"], got["PPSA00005"]), ("garbage", None))
        self.assertEqual(got["PPSA00006"]["output_host"], "/srv/games/ps5/pkg/Elsewhere/old.pkg")
        self.assertIn("output_host", got["PPSA01234"])

    def test_not_even_an_object_is_passed_through(self):
        self.env(**ENV)
        for odd in ([], "x", None):
            self.put("ledger.json", odd)
            self.assertEqual(self.get("/api/ledger"), odd)

    def test_without_a_host_location_it_is_exactly_the_file(self):
        self.put("ledger.json", {**OLD_LEDGER, **LEDGER})
        self.assertEqual(self.get("/api/ledger"), {**OLD_LEDGER, **LEDGER})


class StatusAndRuns(PathsCase):
    def test_this_runs_reasons_show_host_paths_and_the_raw_reason_stays(self):
        self.env(**ENV)
        self.put("status.json", dict(updated=1, results=[
            dict(title_id="PPSA01234", name="x.pkg", status="failed", why="fpkg-validate failed (summary: bad); package left in /output/.staging/PPSA01234"),
            dict(title_id="PPSA05678", name="y.pkg", status="built"), dict(name="z.pkg", status="failed", why=None)]))
        r = self.get("/api/status")["status"]["results"]
        self.assertTrue(r[0]["why"].endswith("package left in /output/.staging/PPSA01234"))
        self.assertTrue(r[0]["why_host"].endswith("package left in /srv/games/ps5/pkg/.staging/PPSA01234"))
        self.assertNotIn("why_host", r[1])
        self.assertNotIn("why_host", r[2])

    def test_a_failed_scans_log_tail_is_translated_for_display(self):
        self.env(**ENV)
        self.srv._scan.update(finished=1.0, rc=1, error="boom", log="scan.stdout.log", log_tail=["PREPARE <- /library/Game.exfat", "plain"])
        scan = self.get("/api/status")["scan"]
        self.assertEqual(scan["log_tail"], ["PREPARE <- /library/Game.exfat", "plain"])
        self.assertEqual(scan["log_tail_host"], ["PREPARE <- /srv/games/ps5/Game.exfat", "plain"])

    def test_without_a_host_location_status_has_no_twins(self):
        self.put("status.json", dict(updated=1, results=[dict(name="x.pkg", status="failed", why="left in /output/x")]))
        st = self.get("/api/status")
        self.assertNotIn("why_host", st["status"]["results"][0])
        self.assertNotIn("log_tail_host", st["scan"])


class Overrides(PathsCase):
    def test_the_stored_overrides_show_where_they_are_on_the_host(self):
        self.env(**ENV)
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output="/output", temp="/work/tmp",
                                     overrides={"PPSA01234": {"source": EXFAT, "patch": "/config/patches/PPSA01234", "note": "n"}}))
        o = self.get("/api/overrides")["PPSA01234"]
        self.assertEqual((o["source"], o["source_host"], o["patch_host"]),
                         (EXFAT, f"/srv/games/ps5/{NAME}.exfat", "/srv/docker/ps5-pkg/config/patches/PPSA01234"))

    def test_a_host_path_typed_in_is_stored_as_the_container_path_when_it_maps_cleanly(self):
        self.env(**ENV)
        r = self.client.put("/api/overrides/PPSA01234", json={
            "patch": "/srv/docker/ps5-pkg/config/patches/PPSA01234", "source": f"/srv/games/ps5/{NAME}.exfat", "note": "/srv/games/ps5"})
        self.assertEqual(r.status_code, 200, r.text)
        o = r.json()
        cfg_root = os.path.commonpath([str(self.state), str(self.logs)])               # /config in the container
        self.assertEqual((o["patch"], o["source"]), (cfg_root + "/patches/PPSA01234", EXFAT))
        self.assertEqual(o["patch_host"], "/srv/docker/ps5-pkg/config/patches/PPSA01234")
        self.assertEqual(o["note"], "/srv/games/ps5")                                   # a note is text, not a path
        stored = json.loads(self.cfg_path.read_text())["overrides"]["PPSA01234"]
        self.assertEqual((stored["patch"], stored["source"]), (cfg_root + "/patches/PPSA01234", EXFAT))
        self.assertNotIn("patch_host", stored)

    def test_a_container_path_or_one_that_does_not_map_is_kept_as_typed(self):
        self.env(**ENV)
        for typed in ("/config/patches/PPSA01234", "/srv/unmapped/place", "patches/rel"):
            with self.subTest(typed):
                o = self.client.put("/api/overrides/PPSA01234", json={"patch": typed}).json()
                self.assertEqual(o["patch"], typed)
        self.assertNotIn("patch_host", self.client.put("/api/overrides/PPSA01234", json={"patch": "/srv/unmapped/place"}).json())

    def test_without_a_mapping_a_typed_path_is_stored_as_it_is(self):
        o = self.client.put("/api/overrides/PPSA01234", json={"patch": "/srv/docker/ps5-pkg/config/patches/PPSA01234"}).json()
        self.assertEqual(o, {"patch": "/srv/docker/ps5-pkg/config/patches/PPSA01234"})


class Logs(PathsCase):
    LINES = ["[2026-11-22 03:00:01] PREPARE Example  <- /library/Example Game_PPSA01234.exfat",
             "[2026-11-22 03:40:02]   copied to /output/Example [PPSA01234].pkg (verified)",
             "[2026-11-22 03:40:03]   see /output2/other and /library-old/x", "[2026-11-22 03:40:04]   café /work/tmp/PPSA01234"]

    def setUp(self):
        super().setUp()
        self.log = self.logs / "pkg_sync.log"
        self.log.write_text("\n".join(self.LINES) + "\n", encoding="utf-8")

    def test_the_log_viewer_can_show_host_paths_and_the_file_is_never_changed(self):
        self.env(**ENV)
        before = self.log.read_bytes()
        raw = self.client.get("/api/logs/pkg_sync.log").text
        host = self.client.get("/api/logs/pkg_sync.log?host=1").text
        self.assertEqual(raw, "\n".join(self.LINES))                                    # the default is exactly the text on disk
        self.assertEqual(host.split("\n"), [
            "[2026-11-22 03:00:01] PREPARE Example  <- /srv/games/ps5/Example Game_PPSA01234.exfat",
            "[2026-11-22 03:40:02]   copied to /srv/games/ps5/pkg/Example [PPSA01234].pkg (verified)",
            "[2026-11-22 03:40:03]   see /output2/other and /library-old/x",              # whole segments only
            "[2026-11-22 03:40:04]   café /srv/scratch/tmp/PPSA01234"])
        self.assertEqual(self.log.read_bytes(), before)

    def test_host_equals_zero_and_no_mapping_are_both_raw(self):
        self.assertEqual(self.client.get("/api/logs/pkg_sync.log?host=1").text, "\n".join(self.LINES))
        self.env(**ENV)
        self.assertEqual(self.client.get("/api/logs/pkg_sync.log?host=0").text, "\n".join(self.LINES))


if __name__ == "__main__":
    unittest.main()
