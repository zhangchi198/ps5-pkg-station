"""What the Settings page shows and checks (app/engine/ps5_settingsmeta.py): one definition for labels, help, defaults and
ranges; plain-language errors per field; the cross-field rules; warnings; what a cleanup pattern would match; what needs a
rescan. Standard library only."""
from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from support import ENGINE_DIR, E, StateCase, make_env, GAME_PARAM

import ps5_settingsmeta as M  # noqa: E402

GIB = 2 ** 30
MACHINE = dict(cpus=8, work_total=500 * GIB)


def current(**kw):
    return {**copy.deepcopy(E.DEFAULT_CFG), **kw}


class Definition(unittest.TestCase):
    def test_every_field_has_words_and_a_group_and_the_groups_come_in_the_order_of_the_page(self):
        groups = [g[0] for g in M.GROUPS]
        self.assertEqual([g[1] for g in M.GROUPS], ["Console", "Performance", "Cleanup", "Schedule", "Notifications", "Folders"])
        for f in M.FIELDS:
            self.assertIn(f["group"], groups, f["key"])
            self.assertTrue(f["label"] and f["help"], f["key"])
            self.assertTrue(len(f["help"]) > 30, f["key"])                                         # a sentence, not a restated label
        self.assertEqual(M.FIELDS[0]["key"], "console_fw")                                          # the most important setting comes first
        self.assertEqual(M.FIELDS[0]["group"], "console")
        self.assertEqual(len({f["key"] for f in M.FIELDS}), len(M.FIELDS))

    def test_every_setting_the_old_page_edited_is_still_on_it(self):
        edited = {f["key"].split(".")[0] for f in M.FIELDS}
        self.assertEqual(edited, {"console_fw", "ps4_mode", "max_builds", "max_compress", "cpu_count", "compression_level", "nice", "ssd_reserve_gb", "build_on_ssd",
                                  "skip_download_prefixes", "junk_file_patterns", "junk_title_patterns", "schedule", "notify_url"})
        for key in edited:
            self.assertIn(key, E.SETTINGS)

    def test_ranges_and_defaults_come_from_the_engine(self):
        by = {f["key"]: f for f in M.schema()}
        for key, spec in E.SETTINGS.items():
            if spec[0] is int and key in by:
                self.assertEqual((by[key]["min"], by[key]["max"], by[key]["step"]), (spec[1], spec[2], 1), key)
        for f in M.schema():
            self.assertEqual(f["default"], M.default_of(f["key"]), f["key"])
        self.assertEqual(by["compression_level"]["default"], E.DEFAULT_CFG["compression_level"])
        self.assertEqual(by["schedule.times"]["default"], E.DEFAULT_CFG["schedule"]["times"])
        self.assertEqual(by["nice"]["choices"], M.NICE_CHOICES)
        self.assertEqual([c[0] for c in M.NICE_CHOICES], [0, 10, 19])                              # normal / low / lowest

    def test_the_words_explain_the_traps_the_old_labels_hid(self):
        by = {f["key"]: f["help"] for f in M.FIELDS}
        self.assertIn("can't be more than the games in progress", by["max_compress"])
        self.assertIn("1 is fastest", by["compression_level"])
        self.assertIn("slow or on the network", by["build_on_ssd"])
        self.assertIn("more room on the work drive", by["build_on_ssd"])
        self.assertIn("13.20", M.BY_KEY["console_fw"]["example"])
        self.assertIn("backport", by["console_fw"])


class Errors(unittest.TestCase):
    def check(self, body, **cfg):
        return M.check(current(**cfg), body, MACHINE)

    def fields(self, body, **cfg):
        return [(e["field"], e["message"]) for e in self.check(body, **cfg)[1]]

    def test_valid_settings_pass_and_are_cleaned(self):
        clean, errors, _ = self.check({"compression_level": "5", "console_fw": "13.20", "nice": 10, "build_on_ssd": True, "notify_url": "  https://ntfy.sh/x "})
        self.assertEqual((errors, clean["compression_level"], clean["notify_url"]), ([], 5, "https://ntfy.sh/x"))

    def test_messages_are_plain_and_name_the_field_not_the_key(self):
        for body, field, text in [({"compression_level": 99}, "compression_level", "Compression level must be a whole number from 1 to 9."),
                                  ({"max_builds": 0}, "max_builds", "Games in progress at once (max) must be a whole number from 1 to 6."),
                                  ({"cpu_count": "many"}, "cpu_count", "CPU threads per game must be a whole number from 1 to 64."),
                                  ({"nice": 20}, "nice", "Run builds at low priority must be a whole number from 0 to 19."),
                                  ({"ssd_reserve_gb": -1}, "ssd_reserve_gb", "Keep this much free on the work drive must be a whole number from 0 to 10000."),
                                  ({"build_on_ssd": "yes"}, "build_on_ssd", "must be on or off"),
                                  ({"console_fw": "9"}, "console_fw", "Enter the firmware like 13.20"),
                                  ({"notify_url": "ntfy.sh/topic"}, "notify_url", "Enter a web address that starts with http:// or https://"),
                                  ({"ps4_mode": "x"}, "ps4_mode", "Choose copy or skip."),
                                  ({"library": 3}, "library", "Game library folder: enter text."),
                                  ({"zzz": 1}, "zzz", "'zzz' is not a setting this version has.")]:
            with self.subTest(body):
                (got_field, got_msg), = self.fields(body)
                self.assertEqual(got_field, field)
                self.assertIn(text, got_msg)
                self.assertNotRegex(got_msg, r"\b(max_builds|max_compress|cpu_count|compression_level|ssd_reserve_gb|build_on_ssd|console_fw|notify_url|ps4_mode)\b")

    def test_every_problem_is_reported_not_just_the_first(self):
        errors = self.fields({"compression_level": 99, "nice": 40, "console_fw": "x", "schedule": {"enabled": True, "times": ["3:00"]}})
        self.assertEqual(sorted(f for f, _ in errors), ["compression_level", "console_fw", "nice", "schedule.times"])

    def test_a_schedule_time_that_is_not_hh_mm_is_an_error_naming_it(self):
        errors = self.fields({"schedule": {"enabled": True, "times": ["3:00", "3am", "25:00", "03:00", "14:30"]}})
        self.assertEqual([f for f, _ in errors], ["schedule.times"] * 3)
        self.assertEqual([m.split(" ")[0] for _, m in errors], ["'3:00'", "'3am'", "'25:00'"])
        self.assertIn("Use 24-hour HH:MM, like 03:00 or 14:30.", errors[0][1])

    def test_a_schedule_that_is_on_needs_a_time_and_one_that_is_off_does_not(self):
        (field, msg), = self.fields({"schedule": {"enabled": True, "times": []}})
        self.assertEqual(field, "schedule.times")
        self.assertIn("Add at least one time, or turn it off", msg)
        self.assertEqual(self.fields({"schedule": {"enabled": False, "times": []}}), [])
        self.assertEqual(self.fields({"schedule": {"enabled": True, "times": ["03:00"]}}), [])

    def test_games_compressing_cannot_exceed_games_in_progress(self):
        (field, msg), = self.fields({"max_compress": 3, "max_builds": 2})
        self.assertEqual(field, "max_compress")
        self.assertIn("Games compressing at once (3) can't be more than games in progress at once (2)", msg)
        self.assertEqual(self.fields({"max_compress": 2, "max_builds": 2}), [])
        # changing only one of them is judged against the saved other
        self.assertEqual([f for f, _ in self.fields({"max_builds": 1}, max_compress=2)], ["max_compress"])
        self.assertEqual([f for f, _ in self.fields({"max_compress": 4}, max_builds=3)], ["max_compress"])
        self.assertEqual(self.fields({"nice": 5}, max_compress=4, max_builds=1), [])                  # an unrelated change is not blocked by it

    def test_a_pattern_with_a_comma_is_a_valid_pattern_and_round_trips(self):
        patterns = [r"\d{1,3}", r"a{2,}b", r"x,y"]
        clean, errors, _ = self.check({"junk_file_patterns": patterns, "junk_title_patterns": patterns})
        self.assertEqual((errors, clean["junk_file_patterns"], clean["junk_title_patterns"]), ([], patterns, patterns))

    def test_a_bad_regex_shows_the_compile_error_and_its_line(self):
        errors = self.fields({"junk_title_patterns": ["ok", "(", "[a-"]})
        self.assertEqual([f for f, _ in errors], ["junk_title_patterns"] * 2)
        self.assertRegex(errors[0][1], r"^Line 2: '\(' is not a valid pattern: missing \)")
        self.assertRegex(errors[1][1], r"^Line 3: '\[a-' is not a valid pattern: ")

    def test_the_notification_url_is_empty_or_http(self):
        for url, ok in [("", True), ("http://192.168.1.5:8080/t", True), ("https://ntfy.sh/x", True), ("ftp://x/y", False), ("file:///etc/passwd", False), ("ntfy.sh/x", False), ("javascript:alert(1)", False)]:
            self.assertEqual(not self.fields({"notify_url": url}), ok, url)

    def test_host_paths_problems_are_in_words_too(self):
        (field, msg), = self.fields({"host_paths": {"library": "games/ps5"}})
        self.assertEqual(field, "host_paths.library")
        self.assertIn("Game library folder: must be an absolute path", msg)
        self.assertEqual(self.fields({"host_paths": ["/x"]})[0], ("host_paths", "Folder locations must be an object."))


class Warnings(unittest.TestCase):
    def test_over_committing_the_cpu_is_a_warning_that_does_not_block(self):
        clean, errors, warns = M.check(current(), {"max_compress": 2, "max_builds": 3, "cpu_count": 16}, dict(cpus=8, work_total=500 * GIB))
        self.assertEqual(errors, [])
        self.assertEqual(clean["cpu_count"], 16)
        (w,) = [w for w in warns if w["field"] == "cpu_count"]
        self.assertIn("This machine has 8 CPU threads. With these values up to 2 × 16 = 32 threads compress at once", w["message"])
        self.assertIn("4.0 times what it has", w["message"])
        self.assertIn("twice what it has", M.warnings(current(max_compress=2, cpu_count=8), dict(cpus=8))[0]["message"])

    def test_exactly_the_cpu_count_is_fine(self):
        self.assertEqual(M.warnings(current(max_compress=2, cpu_count=4), dict(cpus=8)), [])
        self.assertEqual(M.warnings(current(max_compress=1, cpu_count=8), dict(cpus=8)), [])
        self.assertEqual([w["field"] for w in M.warnings(current(max_compress=2, cpu_count=8), dict(cpus=8))], ["cpu_count"])
        self.assertEqual(M.warnings(current(max_compress=2, cpu_count=8), dict(cpus=None)), [])           # an unknown machine says nothing

    def test_a_reserve_bigger_than_the_work_drive_is_a_warning_with_the_consequence(self):
        w = M.warnings(current(ssd_reserve_gb=600, max_compress=1), dict(cpus=8, work_total=500 * GIB))
        self.assertEqual([x["field"] for x in w], ["ssd_reserve_gb"])
        self.assertIn("Keeping 600 GB free is more than the whole work drive (500 GB)", w[0]["message"])
        self.assertIn("every game would be deferred", w[0]["message"])
        self.assertEqual(M.warnings(current(ssd_reserve_gb=80, max_compress=1), dict(cpus=8, work_total=500 * GIB)), [])
        self.assertEqual(M.warnings(current(ssd_reserve_gb=600, max_compress=1), dict(cpus=8, work_total=None)), [])


class Preview(StateCase):
    def test_titles_the_last_scan_found_that_a_pattern_would_change(self):
        (self.state / "ident_cache.json").write_text(json.dumps({"__version__": 3, "a": {"title": "Example Game [SiteTag]"}, "b": {"title": "Other Game"},
                                                                  "c": {"error": "x"}, "d": {"title": "Example Game [SiteTag]"}, "e": None}))
        r = M.preview_patterns(self.state, "title", [r"\[SiteTag\]"])
        self.assertEqual((r["errors"], r["total"], r["looked_at"]), ([], 1, 2))
        self.assertEqual(r["matches"], [dict(text="Example Game [SiteTag]", after="Example Game")])
        self.assertIn("Looked at 2 titles found by the last scan", r["note"])

    def test_file_names_inside_the_archives_the_scans_listed(self):
        lf = self.state / "listing_cache"
        lf.mkdir()
        (lf / "a.txt").write_text("Path = Game/eboot.bin\nFolder = -\nSize = 5\n\nPath = Game/readme.url\nFolder = -\nSize = 1\n\nPath = Game/Folder\nFolder = +\nSize = 0\n")
        (lf / "b.txt").write_text("Path = x/other.URL\nFolder = -\nSize = 1\n\nPath = x/a.txt\nFolder = -\nSize = 1\n")
        r = M.preview_patterns(self.state, "file", [r"\.url$"])
        self.assertEqual((r["errors"], r["total"], r["looked_at"]), ([], 2, 4))
        self.assertEqual(sorted(m["text"] for m in r["matches"]), ["other.URL", "readme.url"])                  # case-insensitive, like the cleanup
        self.assertIn("inside 2 archives", r["note"])
        self.assertIn("extracted folders and images are checked when a game is built", r["note"])

    def test_a_bad_pattern_is_reported_by_line_and_nothing_is_matched(self):
        r = M.preview_patterns(self.state, "file", ["ok", "("])
        self.assertEqual((r["matches"], r["total"]), ([], 0))
        self.assertRegex(r["errors"][0]["message"], r"^Line 2: '\(' is not a valid pattern")

    def test_nothing_to_test_and_nothing_scanned(self):
        self.assertEqual(M.preview_patterns(self.state, "title", [])["note"], "Add a pattern to test.")
        r = M.preview_patterns(self.state, "title", ["x"])
        self.assertEqual((r["total"], r["looked_at"], r["errors"]), (0, 0, []))
        r = M.preview_patterns(self.state, "file", ["x"])
        self.assertEqual((r["total"], r["looked_at"]), (0, 0))

    def test_the_matches_are_capped(self):
        (self.state / "ident_cache.json").write_text(json.dumps({str(i): {"title": f"Game {i} [x]"} for i in range(200)}))
        r = M.preview_patterns(self.state, "title", [r"\[x\]"])
        self.assertEqual((r["total"], len(r["matches"])), (200, 60))


class Rescan(StateCase):
    def test_what_changed_since_the_plan_was_made(self):
        snap = E.plan_settings(current())
        self.assertEqual(M.rescan_needed(snap, current()), [])
        cfg = current(junk_title_patterns=["x"], ps4_mode="skip", console_fw="13.20", nice=3, library="/other")
        self.assertEqual(M.rescan_needed(snap, cfg), ["Game library folder", "PS4 titles", "Title cleanup patterns"])      # not the console firmware or the priority
        self.assertEqual(M.rescan_needed(None, cfg), [])
        self.assertEqual(M.rescan_needed("odd", cfg), [])

    def test_the_settings_a_plan_depends_on(self):
        self.assertEqual(set(E.RESCAN_KEYS), {"library", "downloads", "output", "ps4_mode", "skip_download_prefixes", "junk_file_patterns", "junk_title_patterns"})
        self.assertEqual(set(E.plan_settings(current())), set(E.RESCAN_KEYS))

    def test_a_scan_records_them_next_to_the_plan(self):
        env = make_env(self.dir / "env", game=False)
        game = Path(env["library"]) / "g"
        (game / "sce_sys").mkdir(parents=True)
        (game / "eboot.bin").write_bytes(b"\0" * 16)
        (game / "sce_sys" / "param.json").write_text(json.dumps(GAME_PARAM))
        self.write_cfg({**E.DEFAULT_CFG, **env, "junk_title_patterns": ["zz,top"], "ps4_mode": "skip"})
        r = subprocess.run([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), "plan"], capture_output=True, text=True, env=os.environ.copy(), timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        info = json.loads((self.state / "plan_settings.json").read_text())
        self.assertEqual((info["format"], info["settings"]["junk_title_patterns"], info["settings"]["ps4_mode"], info["settings"]["library"]), (E.PLAN_FORMAT, ["zz,top"], "skip", env["library"]))
        self.assertGreater(info["at"], 0)


class Helpers(unittest.TestCase):
    def test_the_junk_regexes_are_the_ones_the_engine_uses(self):
        cfg = current(junk_file_patterns=[r"\.url$", "readme"], junk_title_patterns=[r"\[Site\]", r"-\s*Repack"])
        E.set_junk(cfg)
        try:
            self.assertEqual(E.JUNK_FILE.pattern, E.junk_file_regex(cfg["junk_file_patterns"]).pattern)
            self.assertEqual(E.JUNK_TITLE.pattern, E.junk_title_regex(cfg["junk_title_patterns"]).pattern)
            self.assertEqual(E.clean_title("Example Game [Site] - Repack"), "Example Game")
            self.assertEqual(E.clean_title("Example Game [Site]", junk=None), "Example Game [Site]")                       # junk=None: nothing is cut
        finally:
            E.set_junk(current())
        self.assertIsNone(E.junk_file_regex([]))
        self.assertIsNone(E.junk_title_regex(["", None]))

    def test_clean_title_with_another_regex_leaves_the_configured_one_alone(self):
        E.set_junk(current(junk_title_patterns=["AAA"]))
        try:
            self.assertEqual(E.clean_title("x AAA y BBB", junk=E.junk_title_regex(["BBB"])), "x AAA y")
            self.assertEqual(E.clean_title("x AAA y BBB"), "x y BBB")
        finally:
            E.set_junk(current())


if __name__ == "__main__":
    unittest.main()
