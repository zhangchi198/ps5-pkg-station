"""The first-run setup checks (ps5_setup) and what the engine does about a missing or unwritable folder.
Standard library only."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest import mock

from support import ENGINE_DIR, E, StateCase, make_env   # (puts app/engine on sys.path)

import ps5_setup as S  # noqa: E402

ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0     # root ignores file permissions: those tests can't run
GIB = 2**30


class SetupCase(StateCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.dir / "env")
        self.cfg = {**copy.deepcopy(E.DEFAULT_CFG), **self.env}

    def checks(self, **kw):
        kw.setdefault("state_dir", self.state)
        return {r["id"]: r for r in S.run_checks(self.cfg, **kw)}

    def lock_down(self, path):
        """Make a folder read-only for this user; put it back before the temp dir is removed."""
        os.chmod(path, 0o555)
        self.addCleanup(os.chmod, path, 0o755)


class Checks(SetupCase):
    def test_a_set_up_install_is_all_green(self):
        rows = self.checks(host_paths={"library": "/srv/games/ps5"})
        self.assertEqual(list(rows), ["library", "downloads", "output", "work", "config", "tools", "console_fw",
                                      "time", "host_paths"])
        self.assertEqual({r["id"]: r["level"] for r in rows.values()}, {k: "ok" for k in rows})
        self.assertEqual(S.blocking(rows.values()), [])

    def test_every_row_has_the_documented_fields_and_a_fix_only_when_something_is_wrong(self):
        for r in self.checks().values():
            self.assertEqual(set(r), {"id", "level", "title", "message", "fix", "advisory", "link"})
            self.assertIn(r["level"], ("ok", "warn", "fail"))
            self.assertTrue(r["message"])
            self.assertEqual(bool(r["fix"]), r["level"] != "ok", r)

    def test_item_counts_and_paths_are_in_the_message(self):
        self.assertIn(f"{self.env['library']} · readable · 1 item", self.checks()["library"]["message"])

    def test_missing_library_fails_with_a_fix_naming_the_path(self):
        shutil.rmtree(self.env["library"])
        r = self.checks()["library"]
        self.assertEqual(r["level"], "fail")
        self.assertIn("not found", r["message"])
        self.assertIn(self.env["library"], r["fix"])
        self.assertIn("docker-compose.yml", r["fix"])

    def test_a_file_where_a_folder_should_be(self):
        shutil.rmtree(self.env["library"])
        open(self.env["library"], "w").close()
        self.assertIn("is not a folder", self.checks()["library"]["message"])

    @unittest.skipIf(ROOT_USER, "root can read everything")
    def test_unreadable_library(self):
        os.chmod(self.env["library"], 0o000)
        self.addCleanup(os.chmod, self.env["library"], 0o755)
        r = self.checks()["library"]
        self.assertEqual(r["level"], "fail")
        self.assertIn(f"not readable by uid {os.getuid()}", r["message"])

    def test_missing_downloads_is_a_warning_that_blocks_nothing(self):
        shutil.rmtree(self.env["downloads"])
        rows = self.checks()
        r = rows["downloads"]
        self.assertEqual((r["level"], r["advisory"]), ("warn", True))
        self.assertIn("not mounted (optional)", r["message"])
        self.assertIn("only the library is scanned", r["message"])
        self.assertEqual(S.blocking(rows.values()), [])

    def test_output_that_does_not_exist_fails(self):
        shutil.rmtree(self.env["output"])
        r = self.checks()["output"]
        self.assertEqual(r["level"], "fail")
        self.assertIn("not found", r["message"])

    @unittest.skipIf(ROOT_USER, "root can write everywhere")
    def test_output_that_is_not_writable_fails_naming_the_user_and_the_fix(self):
        self.lock_down(self.env["output"])
        rows = self.checks()
        r = rows["output"]
        self.assertEqual(r["level"], "fail")
        self.assertIn(f"not writable by uid {os.getuid()}", r["message"])
        self.assertIn("write access", r["fix"])
        self.assertIn("group_add", r["fix"])
        self.assertEqual([x["id"] for x in S.blocking(rows.values())], ["output"])

    def test_the_write_test_leaves_nothing_behind(self):
        before = sorted(os.listdir(self.env["output"]))
        self.assertEqual(self.checks()["output"]["level"], "ok")
        self.assertEqual(sorted(os.listdir(self.env["output"])), before)
        self.assertEqual(sorted(os.listdir(self.env["library"])), ["Example Game [PPSA01234]"])   # read-only: not written to

    def test_a_work_folder_that_does_not_exist_yet_is_fine_when_its_parent_is_writable(self):
        self.assertFalse(os.path.exists(self.env["temp"]))
        self.assertEqual(self.checks()["work"]["level"], "ok")
        self.assertFalse(os.path.exists(self.env["temp"]))               # the check doesn't create it

    @unittest.skipIf(ROOT_USER, "root can write everywhere")
    def test_a_work_folder_whose_parent_is_not_writable_fails(self):
        self.lock_down(os.path.dirname(self.env["temp"]))
        r = self.checks()["work"]
        self.assertEqual(r["level"], "fail")
        self.assertIn("not writable", r["message"])

    @unittest.skipIf(ROOT_USER, "root can write everywhere")
    def test_a_settings_folder_that_is_not_writable_fails(self):
        self.lock_down(self.state)
        self.assertEqual(self.checks()["config"]["level"], "fail")

    def test_work_drive_space_against_the_biggest_game_plus_the_reserve(self):
        self.cfg["ssd_reserve_gb"] = 80
        plan = [dict(title_id="PPSA01234", title="Example Game", action="build", size=300 * GIB),
                dict(title_id="PPSA00002", title="Small One", action="build", size=20 * GIB),
                dict(title_id="PPSA00003", title="Huge But Done", action="done", size=900 * GIB)]
        cases = [(2000 * GIB, "ok", "biggest game needs about 410 GiB incl. reserve"),
                 (200 * GIB, "warn", "Example Game"),
                 (60 * GIB, "fail", "no game can be extracted")]
        for free, level, text in cases:
            with self.subTest(free=free), mock.patch.object(S, "_free", return_value=free):
                r = self.checks(plan=plan)["work"]
                self.assertEqual(r["level"], level, r)
                self.assertIn(text, r["message"])
                if level != "ok":
                    self.assertIn("Keep free on work drive", r["fix"])

    def test_work_drive_before_the_first_scan(self):
        with mock.patch.object(S, "_free", return_value=500 * GIB):
            r = self.checks(plan=None)["work"]
        self.assertEqual(r["level"], "ok")
        self.assertIn("run a scan to check that the biggest game fits", r["message"])

    def test_output_space_against_the_biggest_game(self):
        plan = [dict(title_id="PPSA01234", title="Example Game", action="build", size=300 * GIB)]
        with mock.patch.object(S, "_free", return_value=100 * GIB):
            r = self.checks(plan=plan)["output"]
        self.assertEqual(r["level"], "warn")
        self.assertIn("may not fit", r["message"])

    def test_console_firmware(self):
        self.assertEqual(self.checks()["console_fw"]["level"], "ok")
        self.cfg["console_fw"] = ""
        r = self.checks()["console_fw"]
        self.assertEqual((r["level"], r["advisory"], r["link"]), ("warn", False, {"tab": "set", "field": "console_fw"}))
        self.assertIn("backport", r["message"])

    def test_ultrapack_files(self):
        os.remove(os.path.join(self.env["ultrapack_src"], "backend/native/7zz"))
        r = self.checks()["tools"]
        self.assertEqual(r["level"], "fail")
        self.assertIn("backend/native/7zz", r["message"])
        self.cfg["python"] = str(self.dir / "no-python")
        self.assertIn(str(self.dir / "no-python"), self.checks()["tools"]["message"])

    @unittest.skipIf(ROOT_USER, "root can run anything")
    def test_ultrapack_files_that_are_not_executable(self):
        os.chmod(os.path.join(self.env["ultrapack_src"], "backend/native/ffpfsc-pkg-tool"), 0o644)
        r = self.checks()["tools"]
        self.assertEqual(r["level"], "fail")
        self.assertIn("not executable", r["message"])

    def test_the_shared_7zip_path_is_the_one_the_engine_uses(self):
        self.assertEqual(E.SEVENZ, S.SEVENZ)
        self.assertIn(S.SEVENZ, S.ULTRAPACK_FILES)

    def test_server_time_and_zone(self):
        with mock.patch.dict(os.environ, {"TZ": "Etc/UTC"}):
            c = S.clock_info()
        self.assertEqual(c["tz"], "Etc/UTC")
        self.assertRegex(c["time"], r"^\d\d:\d\d$")
        self.assertRegex(c["utc_offset"], r"^[+-]\d\d:\d\d$")
        self.assertIn("Etc/UTC", c["text"])
        r = self.checks()["time"]
        self.assertEqual(r["level"], "ok")
        self.assertIn("schedule times use this clock", r["message"])

    def test_host_paths_not_set_is_only_a_note(self):
        r = self.checks(host_paths={})["host_paths"]
        self.assertEqual((r["level"], r["advisory"]), ("warn", True))

    def test_sizes(self):
        self.assertEqual((S.fmt_size(310 * GIB), S.fmt_size(1.8 * 2**40), S.fmt_size(5 * 2**20)),
                         ("310 GiB", "1.8 TiB", "5 MiB"))


if __name__ == "__main__":
    unittest.main()
