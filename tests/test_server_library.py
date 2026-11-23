"""The Library's data: what each row needs from the current settings (firmware against the console, the backport decision),
the sources that could not be identified, planning again when an option changes, and how long a build takes on this
machine. Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import re
import time
import unittest
from unittest import mock

from support import WEB_DIR, ServerCase, legacy_config

NOW = time.time()
GIB = 2 ** 30


def entry(tid="PPSA01234", fw="9.00", **kw):
    return dict(title_id=tid, title=f"Game {tid}", version="01.00", fw=fw, source=f"/library/{tid}", kind="exfat", size=5, action="build", why="new", candidates=[], **kw)


def backport(image_fw="7.00", original_fw="9.60", **kw):
    return dict(present=True, confirmed=False, image_fw=image_fw, original_fw=original_fw, console_fw="", decision=None, decided_by="", **kw)


class LibraryCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config(console_fw="10.01", overrides={}))

    def put(self, name, obj, age=None):
        p = self.state / name
        p.write_text(json.dumps(obj))
        if age is not None:
            import os
            os.utime(p, (NOW - age, NOW - age))

    def plan(self):
        return {p["title_id"]: p for p in self.get("/api/plan")}


class Firmware(LibraryCase):
    def test_a_game_that_asks_for_more_than_the_console_has_is_flagged(self):
        self.put("plan.json", [entry("PPSA00001", "9.00"), entry("PPSA00002", "10.01"), entry("PPSA00003", "13.60")], age=60)
        p = self.plan()
        self.assertEqual(p["PPSA00001"]["fw_check"], dict(need="9.00", console="10.01", ok=True))
        self.assertEqual(p["PPSA00002"]["fw_check"], dict(need="10.01", console="10.01", ok=True))
        self.assertEqual(p["PPSA00003"]["fw_check"], dict(need="13.60", console="10.01", ok=False))

    def test_it_follows_the_settings_without_a_new_scan(self):
        self.put("plan.json", [entry("PPSA00003", "13.60")], age=60)
        self.assertFalse(self.plan()["PPSA00003"]["fw_check"]["ok"])
        self.client.put("/api/config", json={"console_fw": "13.60"})
        self.assertTrue(self.plan()["PPSA00003"]["fw_check"]["ok"])

    def test_no_console_firmware_set_is_unknown_not_ok(self):
        self.client.put("/api/config", json={"console_fw": ""})
        self.put("plan.json", [entry("PPSA00003", "13.60")], age=60)
        self.assertEqual(self.plan()["PPSA00003"]["fw_check"], dict(need="13.60", console="", ok=None))

    def test_a_game_without_firmware_and_a_ps4_title_get_no_check(self):
        self.put("plan.json", [entry("PPSA00001", ""), dict(entry("CUSA12345", "9.00"), ps4=True)], age=60)
        p = self.plan()
        self.assertNotIn("fw_check", p["PPSA00001"])
        self.assertNotIn("fw_check", p["CUSA12345"])

    def test_odd_entries_pass_through(self):
        self.put("plan.json", [entry("PPSA00001"), "junk", None, dict(entry("PPSA00002"), backport="odd", fw=5)], age=60)
        got = self.get("/api/plan")
        self.assertEqual(got[1], "junk")
        self.assertEqual(len(got), 4)


class BackportDecision(LibraryCase):
    def setUp(self):
        super().setUp()
        self.put("plan.json", [dict(entry("PPSA00001", "7.00"), backport=backport())], age=60)

    def bp(self):
        return self.plan()["PPSA00001"]

    def test_the_original_runs_on_the_console_so_it_is_used_and_it_is_the_firmware_checked(self):
        p = self.bp()
        self.assertEqual((p["backport"]["decision"], p["backport"]["decided_by"], p["backport"]["console_fw"]), ("original", "your console's firmware", "10.01"))
        self.assertEqual(p["fw_check"], dict(need="9.60", console="10.01", ok=True))                         # the original's firmware, not the image's

    def test_an_older_console_keeps_the_image(self):
        self.client.put("/api/config", json={"console_fw": "8.00"})
        p = self.bp()
        self.assertEqual((p["backport"]["decision"], p["fw_check"]["need"], p["fw_check"]["ok"]), ("image", "7.00", True))

    def test_a_console_neither_runs_on_is_flagged(self):
        self.client.put("/api/config", json={"console_fw": "6.00"})
        p = self.bp()
        self.assertEqual((p["backport"]["decision"], p["backport"]["decided_by"]), (None, "neither copy is known to run on the console"))
        self.assertEqual(p["fw_check"], dict(need="7.00", console="6.00", ok=False))

    def test_your_option_beats_the_console(self):
        self.client.put("/api/overrides/PPSA00001", json={"modified_exec": "original"})
        self.client.put("/api/config", json={"console_fw": "8.00"})
        p = self.bp()
        self.assertEqual((p["backport"]["decision"], p["backport"]["decided_by"]), ("original", "your option"))
        self.assertEqual((p["fw_check"]["need"], p["fw_check"]["ok"]), ("9.60", False))


class Outcome(LibraryCase):
    def test_what_the_last_build_did_is_shown_on_a_finished_game(self):
        outcome = dict(differs=True, used="original", files=3, image_fw="7.00", original_fw="9.60", decided_by="your console's firmware")
        self.put("plan.json", [dict(entry("PPSA00001"), action="done"), dict(entry("PPSA00002"), action="build")], age=60)
        self.put("ledger.json", {"PPSA00001": dict(version="01.00", output="/o/a.pkg", backport=outcome), "PPSA00002": dict(version="01.00", output="/o/b.pkg", backport=outcome)})
        p = self.plan()
        self.assertEqual(p["PPSA00001"]["last_backport"], outcome)
        self.assertNotIn("last_backport", p["PPSA00002"])                                                    # it is going to be built again: not the last build's story


class NotRecognised(LibraryCase):
    def test_the_sources_that_failed_are_returned_with_their_error(self):
        self.put("unrecognised.json", [dict(source="/library/Broken", origin="library", error="JSONDecodeError: boom"),
                                       dict(source="/downloads/a.rar", origin="download", error="BadRarFile")])
        got = self.get("/api/unrecognised")
        self.assertEqual([(x["source"], x["origin"], x["error"]) for x in got], [("/library/Broken", "library", "JSONDecodeError: boom"), ("/downloads/a.rar", "download", "BadRarFile")])

    def test_they_get_host_paths_when_the_folder_has_one(self):
        import os
        with mock.patch.dict(os.environ, {"PS5PKG_HOST_LIBRARY": "/srv/games/ps5"}):
            self.write_cfg(legacy_config(library="/library", downloads="/downloads"))
            self.put("unrecognised.json", [dict(source="/library/Broken", origin="library", error="x")])
            self.assertEqual(self.get("/api/unrecognised")[0]["source_host"], "/srv/games/ps5/Broken")

    def test_nothing_or_nonsense_is_an_empty_list(self):
        self.assertEqual(self.get("/api/unrecognised"), [])
        for odd in ({}, "x", [None, 5, "y"], None):
            self.put("unrecognised.json", odd)
            self.assertEqual(self.get("/api/unrecognised"), [])


class PlanAgain(LibraryCase):
    """A changed option is planned at once (a second with the cache warm), or when the run or scan is over."""

    def test_when_nothing_runs_it_scans_now(self):
        with mock.patch.object(self.srv, "run_scan", return_value=True) as scan, mock.patch.object(self.srv, "engine_pid", return_value=None):
            self.assertEqual(self.real_replan(), "now")
            scan.assert_called_once()
        self.assertFalse(self.srv._replan["pending"])

    def test_during_a_run_it_waits_for_the_end(self):
        with mock.patch.object(self.srv, "run_scan", return_value=False), mock.patch.object(self.srv, "engine_pid", return_value=4242):
            self.assertEqual(self.real_replan(), "after_run")
        self.assertTrue(self.srv._replan["pending"])
        with mock.patch.object(self.srv, "run_scan", return_value=True) as scan, mock.patch.object(self.srv, "engine_pid", return_value=4242):
            self.srv.replan_tick()
            scan.assert_not_called()                                                                             # the run is still on
        with mock.patch.object(self.srv, "run_scan", return_value=True) as scan, mock.patch.object(self.srv, "engine_pid", return_value=None):
            self.srv.replan_tick()
            scan.assert_called_once()
        self.assertFalse(self.srv._replan["pending"])
        with mock.patch.object(self.srv, "run_scan") as scan:
            self.srv.replan_tick()
            scan.assert_not_called()                                                                             # once

    def test_during_a_scan_it_waits_for_that_too(self):
        with mock.patch.object(self.srv, "run_scan", return_value=False), mock.patch.object(self.srv, "engine_pid", return_value=None):
            self.assertEqual(self.real_replan(), "after_scan")
        self.assertTrue(self.srv._replan["pending"])

    def test_the_changes_that_matter_ask_for_it(self):
        self.client.put("/api/overrides/PPSA01234", json={"note": "x"})
        self.client.put("/api/overrides/PPSA01234", json={"skip": True})
        self.assertEqual(len(self.replans), 2)
        self.assertEqual(self.client.put("/api/overrides/PPSA01234", json={"note": "y"}).json()["replan"], "now")
        self.assertEqual(self.client.delete("/api/overrides/PPSA01234").json()["replan"], "now")
        n = len(self.replans)
        self.client.delete("/api/overrides/PPSA01234")                                                           # nothing there: nothing changed
        self.client.put("/api/overrides/NOPE", json={})
        self.assertEqual(len(self.replans), n)

    def test_a_refused_change_does_not_plan(self):
        n = len(self.replans)
        self.assertEqual(self.client.put("/api/overrides/PPSA01234", json={"patch": "/etc"}).status_code, 400)
        self.assertEqual(len(self.replans), n)

    def test_import_and_restore_plan_too_but_a_preview_does_not(self):
        doc = json.dumps(dict(format=1, overrides={"PPSA01234": {"skip": True}}))
        self.client.post("/api/import?dry_run=1", content=doc)
        self.assertEqual(self.replans, [])
        self.assertEqual(self.client.post("/api/import", content=doc).status_code, 200)
        self.assertEqual(len(self.replans), 1)
        name = self.client.get("/api/backups").json()["items"][0]["name"]
        self.client.post(f"/api/backups/{name}/restore?dry_run=1")
        self.assertEqual(len(self.replans), 1)
        self.assertEqual(self.client.post(f"/api/backups/{name}/restore").status_code, 200)
        self.assertEqual(len(self.replans), 2)


class Pace(LibraryCase):
    def test_how_long_a_build_takes_comes_from_earlier_builds(self):
        self.assertEqual(self.get("/api/status")["pace"], dict(build_per_gib=None, prep_per_gib=None, builds=0))
        self.put("ledger.json", {"PPSA00001": dict(stage_secs={"1": 1200, "2": 20, "3": 400, "4": 20, "5": 160, "validate": 100, "copy": 100}, source_bytes=100 * GIB, level=5, prep_min=10)})
        p = self.get("/api/status")["pace"]
        self.assertEqual((p["builds"], round(p["build_per_gib"], 1), round(p["prep_per_gib"], 1)), (1, 20.0, 6.0))

    def test_an_odd_ledger_is_no_pace(self):
        for odd in ([], "x", {"a": "b"}):
            self.put("ledger.json", odd)
            self.assertEqual(self.get("/api/status")["pace"]["builds"], 0)


class Page(LibraryCase):
    JS = (WEB_DIR / "static" / "app.js").read_text()

    def body(self, name, until):
        start = self.JS.index(name)
        return self.JS[start:self.JS.index(until, start)]

    def test_filtering_asks_the_server_for_nothing_and_does_not_rebuild_the_table(self):
        body = self.body("function applyLibFilter", "$(\"#libFilter\").oninput")
        self.assertNotRegex(body, r"\b(api|fetch|loadLib|fetchLib|renderLib|innerHTML)\b")
        self.assertIn('$("#libFilter").oninput=applyLibFilter', self.JS)
        self.assertIn('$("#libAct").onchange=applyLibFilter', self.JS)

    def test_open_panels_and_edits_are_kept_across_a_rebuild(self):
        for needle in ("libUi.open", "libUi.edits", "function restoreLibUi", 'addEventListener("toggle"', 'addEventListener("input"'):
            self.assertIn(needle, self.JS)
        self.assertRegex(self.body("function renderLib", "function restoreLibUi"), r"restoreLibUi\(\);applyLibFilter\(\)")

    def test_the_library_follows_changes_but_not_while_someone_types_in_it(self):
        body = self.body("async function refreshLibIfChanged", "async function saveOv")
        self.assertIn("ovr", body)                                                                                # the active element check
        self.assertIn("refreshLibIfChanged(s)", self.body("async function poll", "async function refresh"))

    def test_saving_says_when_it_applies_and_checks_for_the_new_plan(self):
        body = self.body("async function saveOv", "// a scan is about a second")
        self.assertIn("applies from the next run", body)
        self.assertIn("Planning again", body)
        self.assertNotIn("rescan to apply", self.JS)
        self.assertIn("afterReplan", body)

    def test_build_only_this_asks_with_the_size_and_an_estimate(self):
        body = self.body("async function buildOne", "function alertBox")
        self.assertIn("confirm(", body)
        self.assertIn("buildEstimate", body)
        self.assertLess(body.index("confirm("), body.index('api("/api/run"'))                                      # asked before anything starts

    def test_history_shows_what_a_build_did_about_a_backport(self):
        body = self.body("function builtRow", "async function loadHist")
        self.assertIn("v.backport", body)
        self.assertIn("was used", body)

    def test_the_wording(self):
        for text in ("Copy to use", "If a backport exists", "decide by console firmware", "keep the backport (the image)", "use the original",
                     "Extra files folder (copied over the game)", "never build this game", "Not recognised", "Copies found"):
            self.assertIn(text, self.JS)
        self.assertNotIn("Modified executables", self.JS)

    def test_the_status_filter_and_the_summary_have_every_state(self):
        html = (WEB_DIR / "static" / "index.html").read_text()
        for value in ("build", "copy", "failed", "done", "skip", "error"):
            self.assertIn(f'<option value="{value}">', html)
        for word in ("to build", "done", "failed", "skipped", "problem", "not recognised", "PS4 to copy"):
            self.assertIn(word, self.body("function renderLib", "function restoreLibUi"))

    def test_the_packed_help_links_to_the_readme_section_that_exists(self):
        readme = (WEB_DIR.parent.parent / "README.md").read_text()
        self.assertRegex(readme, r"(?m)^### Packed copies \(LZ4 / Zstd\)$")
        self.assertIn("#packed-copies", self.JS)


if __name__ == "__main__":
    unittest.main()
