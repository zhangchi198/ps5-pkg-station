"""The Settings page's endpoints: the definition it builds itself from, validation field by field (before and at the save),
the warnings that don't block, what a cleanup pattern would match, what needs a rescan, and the corrected backup note.
Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import re

from support import E, ServerCase, WEB_DIR, legacy_config

import ps5_settingsmeta as M  # noqa: E402


class Definition(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config())

    def test_the_page_gets_its_fields_groups_defaults_and_the_machine(self):
        c = self.get("/api/config")
        keys = [f["key"] for f in c["schema"]]
        self.assertEqual(keys[0], "console_fw")
        self.assertEqual([g[1] for g in c["groups"]][:4], ["Console", "Performance", "Cleanup", "Schedule"])
        by = {f["key"]: f for f in c["schema"]}
        self.assertEqual((by["max_builds"]["min"], by["max_builds"]["max"], by["max_builds"]["step"]), (1, E.SETTINGS["max_builds"][2], 1))
        self.assertEqual(by["compression_level"]["default"], E.DEFAULT_CFG["compression_level"])
        self.assertEqual(c["machine"]["cpus"], os.cpu_count())
        self.assertIn("rescan", c["rescan_note"].lower())
        self.assertEqual(c["rescan_keys"], list(E.RESCAN_KEYS))
        self.assertIs(c["run_active"], False)
        self.assertEqual(c["compression_level"], 5)                         # the values are still at the top level, as before

    def test_a_hand_edited_combination_that_would_be_refused_is_shown_at_its_field(self):
        self.write_cfg(legacy_config(max_compress=4, max_builds=2, schedule={"enabled": True, "times": []}))
        c = self.get("/api/config")
        self.assertEqual(sorted(p["field"] for p in c["problems"]), ["max_compress", "schedule.times"])
        self.write_cfg(legacy_config())
        self.assertEqual(self.get("/api/config")["problems"], [])

    def test_get_warns_about_an_overcommitted_saved_config(self):
        with mock.patch.object(os, "cpu_count", return_value=4):
            self.client.put("/api/config", json={"max_compress": 2, "max_builds": 2, "cpu_count": 8})
            w = self.get("/api/config")["warnings"]
        self.assertEqual([x["field"] for x in w], ["cpu_count"])
        self.assertIn("4 CPU threads", w[0]["message"])


class Saving(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config())

    def test_a_pattern_with_a_comma_is_saved_and_comes_back_unchanged(self):
        patterns = [r"\d{1,3}", r"a{2,}b", "x,y"]
        r = self.client.put("/api/config", json={"junk_file_patterns": patterns, "junk_title_patterns": patterns})
        self.assertEqual(r.status_code, 200, r.text)
        c = self.get("/api/config")
        self.assertEqual((c["junk_file_patterns"], c["junk_title_patterns"]), (patterns, patterns))
        self.assertEqual(json.loads(self.cfg_path.read_text())["junk_file_patterns"], patterns)

    def test_an_invalid_regex_names_the_field_the_line_and_the_compile_error_and_saves_nothing(self):
        before = self.snapshot()
        r = self.client.put("/api/config", json={"junk_title_patterns": ["ok", "(["], "nice": 3})
        self.assertEqual(r.status_code, 400)
        (err,) = r.json()["errors"]
        self.assertEqual(err["field"], "junk_title_patterns")
        self.assertRegex(err["message"], r"^Line 2: '\(\[' is not a valid pattern: ")
        self.assertEqual(r.json()["detail"], err["message"])
        self.assertEqual(self.snapshot(), before)                            # the valid part (nice) was not saved either

    def test_a_schedule_time_of_3_00_is_refused_with_an_inline_message_and_nothing_is_saved(self):
        before = self.snapshot()
        r = self.client.put("/api/config", json={"schedule": {"enabled": True, "times": ["03:00", "3:00"]}})
        self.assertEqual(r.status_code, 400)
        (err,) = r.json()["errors"]
        self.assertEqual(err["field"], "schedule.times")
        self.assertEqual(err["message"], "'3:00' is not a time. Use 24-hour HH:MM, like 03:00 or 14:30.")
        self.assertEqual(self.snapshot(), before)

    def test_an_enabled_schedule_with_no_valid_time_cannot_be_saved(self):
        before = self.snapshot()
        for times in ([], ["3am"], ["24:00"]):
            with self.subTest(times):
                r = self.client.put("/api/config", json={"schedule": {"enabled": True, "times": times}})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual({e["field"] for e in r.json()["errors"]}, {"schedule.times"})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.client.put("/api/config", json={"schedule": {"enabled": False, "times": []}}).status_code, 200)

    def test_compressing_at_once_cannot_exceed_in_progress_on_the_server_either(self):
        before = self.snapshot()
        r = self.client.put("/api/config", json={"max_compress": 4, "max_builds": 2})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"][0]["field"], "max_compress")
        self.assertIn("can't be more than games in progress at once (2)", r.json()["detail"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.client.put("/api/config", json={"max_compress": 2, "max_builds": 3}).status_code, 200)
        self.assertEqual(self.client.put("/api/config", json={"max_builds": 1}).status_code, 400)           # now max_compress is 2: refused
        self.assertEqual(self.get("/api/config")["max_builds"], 3)

    def test_over_committing_the_cpu_is_saved_with_a_warning(self):
        with mock.patch.object(os, "cpu_count", return_value=4):
            r = self.client.put("/api/config", json={"max_compress": 2, "max_builds": 2, "cpu_count": 8})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.get("/api/config")["cpu_count"], 8)
        (w,) = r.json()["warnings"]
        self.assertEqual(w["field"], "cpu_count")
        self.assertIn("2 × 8 = 16 threads", w["message"])

    def test_the_notification_url_is_empty_or_http(self):
        for url in ("ftp://x", "ntfy.sh/topic", "file:///etc/passwd"):
            r = self.client.put("/api/config", json={"notify_url": url})
            self.assertEqual((r.status_code, r.json()["errors"][0]["field"]), (400, "notify_url"), url)
        for url in ("", "https://ntfy.sh/x", "http://192.168.1.5/t"):
            self.assertEqual(self.client.put("/api/config", json={"notify_url": url}).status_code, 200, url)

    def test_every_problem_comes_back_not_only_the_first(self):
        r = self.client.put("/api/config", json={"compression_level": 99, "nice": 40, "schedule": {"enabled": True, "times": []}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(sorted(e["field"] for e in r.json()["errors"]), ["compression_level", "nice", "schedule.times"])

    def test_the_save_says_whether_a_run_is_active(self):
        r = self.client.put("/api/config", json={"nice": 4})
        self.assertIs(r.json()["run_active"], False)
        with mock.patch.object(self.srv, "run_active", return_value=True):
            r = self.client.put("/api/config", json={"nice": 5})
            self.assertEqual((r.status_code, r.json()["run_active"]), (200, True))
            self.assertIs(self.get("/api/config")["run_active"], True)

    def test_a_valid_save_keeps_what_the_page_does_not_edit(self):
        self.client.put("/api/config", json={"nice": 7})
        raw = json.loads(self.cfg_path.read_text())
        self.assertEqual(raw["overrides"], legacy_config()["overrides"])
        self.assertEqual(raw["nice"], 7)


class Validate(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config())

    def test_it_reports_without_saving_anything(self):
        before = self.snapshot()
        r = self.client.post("/api/config/validate", json={"nice": 99, "compression_level": 3, "max_compress": 5, "max_builds": 2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(e["field"] for e in r.json()["errors"]), ["max_compress", "nice"])
        r = self.client.post("/api/config/validate", json={"nice": 5})
        self.assertEqual(r.json()["errors"], [])
        self.assertEqual(self.snapshot(), before)

    def test_a_body_that_is_not_an_object_is_refused(self):
        self.assertEqual(self.client.post("/api/config/validate", json=[1]).status_code, 400)


class Preview(ServerCase):
    def test_titles_and_file_names_of_the_last_scan(self):
        self.write_cfg(legacy_config())
        (self.state / "ident_cache.json").write_text(json.dumps({"a": {"title": "Example Game [Site]"}, "b": {"title": "Other"}}))
        r = self.client.post("/api/config/preview-patterns", json={"kind": "title", "patterns": [r"\[Site\]"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["total"], r.json()["matches"][0]["after"]), (1, "Example Game"))
        r = self.client.post("/api/config/preview-patterns", json={"kind": "file", "patterns": ["("]})
        self.assertRegex(r.json()["errors"][0]["message"], r"^Line 1: '\(' is not a valid pattern")

    def test_it_does_not_change_the_titles_the_engine_will_use(self):
        self.write_cfg(legacy_config())
        before = (E.JUNK_TITLE, E.JUNK_FILE)
        self.client.post("/api/config/preview-patterns", json={"kind": "title", "patterns": ["zzz"]})
        self.assertEqual((E.JUNK_TITLE, E.JUNK_FILE), before)

    def test_bad_requests(self):
        for body in ({"kind": "x", "patterns": []}, {"kind": "title", "patterns": "a"}, {"patterns": []}, []):
            self.assertEqual(self.client.post("/api/config/preview-patterns", json=body).status_code, 400, body)


class Rescan(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config())
        (self.state / "plan.json").write_text("[]")

    def snapshot_plan(self, **settings):
        cfg = E.validate_cfg(json.loads(self.cfg_path.read_text()))[0]
        (self.state / "plan_settings.json").write_text(json.dumps(dict(format=E.PLAN_FORMAT, settings={**E.plan_settings(cfg), **settings})))

    def test_nothing_changed_means_nothing_to_rescan(self):
        self.snapshot_plan()
        self.assertEqual(self.get("/api/status")["rescan"], dict(needed=[], stale=False))

    def test_a_changed_library_setting_is_named_and_a_console_change_is_not(self):
        self.snapshot_plan()
        self.client.put("/api/config", json={"ps4_mode": "skip", "junk_title_patterns": ["zz"], "console_fw": "12.00", "nice": 2})
        r = self.get("/api/status")["rescan"]
        self.assertEqual((r["needed"], r["stale"]), (["PS4 titles", "Title cleanup patterns"], False))
        saved = self.client.put("/api/config", json={"nice": 3}).json()
        self.assertEqual(saved["rescan"]["needed"], ["PS4 titles", "Title cleanup patterns"])

    def test_changing_it_back_clears_the_note(self):
        self.snapshot_plan()
        self.client.put("/api/config", json={"ps4_mode": "skip"})
        self.assertEqual(self.get("/api/status")["rescan"]["needed"], ["PS4 titles"])
        self.client.put("/api/config", json={"ps4_mode": "copy"})
        self.assertEqual(self.get("/api/status")["rescan"]["needed"], [])

    def test_a_plan_from_an_older_version_is_stale_and_not_judged(self):
        (self.state / "plan_settings.json").unlink(missing_ok=True)
        self.assertEqual(self.get("/api/status")["rescan"], dict(needed=[], stale=True))
        (self.state / "plan_settings.json").write_text(json.dumps(dict(format=E.PLAN_FORMAT - 1, settings={})))
        self.assertTrue(self.get("/api/status")["rescan"]["stale"])
        # its fields (the firmware check, the verdicts) are not recomputed from settings the plan was not made with
        (self.state / "plan.json").write_text(json.dumps([dict(title_id="PPSA01234", action="build", name="Example Game", size=5, fw="9.00", candidates=[])]))
        (p,) = self.get("/api/plan")
        self.assertNotIn("fw_check", p)
        self.snapshot_plan()
        (p,) = self.get("/api/plan")
        self.assertIn("fw_check", p)

    def test_no_plan_no_note(self):
        (self.state / "plan.json").unlink()
        self.assertEqual(self.get("/api/status")["rescan"], dict(needed=[], stale=False))


class BackupNote(ServerCase):
    def test_the_note_says_what_the_full_backup_really_contains(self):
        self.write_cfg(legacy_config())
        note = self.get("/api/storage")["state_note"]
        self.assertIn("The full backup (.zip) includes the build history", note)
        self.assertIn("failed-game records only if you tick", note)
        self.assertNotIn("not in a full backup", note)

    def test_it_matches_what_the_backup_does(self):
        """The claim is true: ledger.json is in a plain full backup, failures.json only when asked."""
        import io
        import zipfile
        self.write_cfg(legacy_config())
        (self.state / "ledger.json").write_text(json.dumps({"PPSA01234": {"output": "/output/x.pkg"}}))
        (self.state / "failures.json").write_text(json.dumps({"PPSA09999": {"state": "failed"}}))
        plain = self.client.get("/api/backup.zip")
        self.assertEqual(plain.status_code, 200)
        names = zipfile.ZipFile(io.BytesIO(plain.content)).namelist()
        self.assertIn("ledger.json", names)
        self.assertNotIn("failures.json", names)
        asked = zipfile.ZipFile(io.BytesIO(self.client.get("/api/backup.zip?failures=1").content)).namelist()
        self.assertIn("failures.json", asked)


class Page(unittest.TestCase):
    """The page's script and markup (no browser here: the interactions were checked in a real one)."""
    JS = (WEB_DIR / "static" / "app.js").read_text()
    HTML = (WEB_DIR / "static" / "index.html").read_text()

    def body(self, name, until):
        start = self.JS.index(name)
        return self.JS[start:self.JS.index(until, start)]

    def test_lists_are_one_per_line_never_split_on_commas(self):
        collect = self.body("function readCtl", "function collectForm")
        self.assertIn('split("\\n")', collect)
        self.assertNotIn('split(",")', collect)
        self.assertNotIn('split(",")', self.body("function renderGroups", "function showSetBar"))
        self.assertNotIn('join(",")', self.body("function fieldHtml", "const timeRow"))                # a list is shown with newlines, so a comma stays in its pattern

    def test_numbers_get_their_limits_from_the_servers_definition(self):
        field = self.body("function fieldHtml", "const timeRow")
        for needle in ('min="${num(f.min)}"', 'max="${num(f.max)}"', 'step="${num(f.step)||1}"'):
            self.assertIn(needle, field)
        self.assertNotRegex(field, r'min="\d')                                                         # no limit is written into the page

    def test_the_sticky_bar_has_save_and_discard_and_leaving_asks(self):
        for needle in ('id="saveBar"', 'id="bSave"', 'id="bDiscard"'):
            self.assertIn(needle, self.HTML)
        self.assertIn(".savebar{position:sticky;bottom:0", self.HTML)
        nav = self.body('document.querySelectorAll("nav button")', "\n")
        self.assertRegex(nav, r'settingsDirty\(\)&&!\(await askConfirm\(\{title:"Discard your changes\?",html:"<p>You have unsaved changes on the Settings page')
        self.assertIn('window.addEventListener("beforeunload"', self.JS)
        self.assertIn("settingsDirty()", self.body('window.addEventListener("beforeunload"', "\n"))

    def test_a_refused_save_is_put_at_the_field_and_the_message_is_not_lost(self):
        self.assertIn("e.errors=d.errors", self.body("async function api", "\n"))
        save = self.body("async function saveSettings", 'window.addEventListener("beforeunload"')
        self.assertIn("showFieldMessages(e.errors", save)
        self.assertIn("Not saved: ", save)
        self.assertIn('setMessage("Saved."+(r.run_active?" Applies to the next run.":"")', save)                   # on the bar...
        self.assertIn('toast("Saved."+(r.run_active?" Applies to the next run.":"")', save)                        # ...and as a toast
        self.assertIn("r.run_active", save)

    def test_save_is_off_while_a_problem_is_showing(self):
        self.assertRegex(self.body("function showSetBar", "function setMessage"), r'\$\("#bSave"\)\.disabled=!dirty\|\|bad>0')

    def test_every_field_is_checked_by_the_server_as_it_is_edited(self):
        self.assertIn('api("/api/config/validate"', self.JS)
        self.assertIn('api("/api/config/preview-patterns"', self.JS)
        self.assertRegex(self.JS, r'\$\("#set"\)\.addEventListener\("input"')

    def test_the_rescan_note_has_a_button_on_settings_and_in_the_library(self):
        self.assertIn('id="rescanBox"', self.HTML)
        self.assertIn('id="libNote"', self.HTML)
        self.assertEqual(self.JS.count('data-do="rescan"'), 2)
        self.assertIn('b.dataset.do==="rescan")doScan()', self.JS)                                       # and unlike the Dashboard's Scan it stays on the page

    def test_what_a_change_applies_to_is_said_on_the_page(self):
        self.assertIn("Build settings are read when a run starts, so a change applies from the next run", self.HTML)
        self.assertRegex(self.HTML, r'id="rescanNote"')


if __name__ == "__main__":
    unittest.main()
