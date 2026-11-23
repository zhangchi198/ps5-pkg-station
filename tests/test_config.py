"""config.json handling in the engine module: reading never writes, bad values and bad files are reported
instead of raised, saves are atomic and can't lose each other, and the engine refuses to start on a file it
can't read. Standard library only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

from support import ENGINE_DIR, E, StateCase, legacy_config


class ReadNeverWrites(StateCase):
    def test_loading_leaves_an_existing_file_alone(self):
        self.write_cfg(legacy_config())
        before = self.snapshot()
        for _ in range(30):
            E.load_cfg()
            E.load_cfg_info()
        self.assertEqual(self.snapshot(), before)

    def test_defaults_are_merged_in_memory_not_into_the_file(self):
        text = self.write_cfg('{"compression_level": 5}\n')          # a hand-written, sparse file
        before = self.snapshot()
        cfg = E.load_cfg()
        self.assertEqual(cfg["compression_level"], 5)
        self.assertEqual(cfg["max_builds"], E.DEFAULT_CFG["max_builds"])
        self.assertEqual(cfg["schedule"], E.DEFAULT_CFG["schedule"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.cfg_path.read_text(), text)

    def test_first_start_writes_the_defaults_once(self):
        self.assertFalse(self.cfg_path.exists())
        cfg = E.load_cfg()
        self.assertEqual(json.loads(self.cfg_path.read_text()), E.DEFAULT_CFG)
        self.assertEqual(cfg, E.DEFAULT_CFG)
        os.utime(self.cfg_path, ns=(1_000_000_000_000_000_000,) * 2)
        before = self.snapshot()
        for _ in range(10):
            E.load_cfg()
        self.assertEqual(self.snapshot(), before)

    def test_a_missing_state_dir_is_created(self):
        self.state.rmdir()
        E.load_cfg()
        self.assertTrue(self.cfg_path.exists())

    def test_returned_settings_are_private_copies(self):
        E.load_cfg()["overrides"]["PPSA00001"] = {"skip": True}      # must not leak into the defaults
        E.load_cfg()["schedule"]["times"].append("04:00")
        self.assertEqual(E.DEFAULT_CFG["overrides"], {})
        self.assertEqual(E.DEFAULT_CFG["schedule"]["times"], ["03:00"])
        self.assertEqual(E.load_cfg()["overrides"], {})

    def test_a_file_written_by_the_previous_version_loads_clean(self):
        old = legacy_config()
        # what the old load_cfg wrote: no trailing newline, tight everything
        self.write_cfg(json.dumps(old, indent=2, ensure_ascii=False))
        info = E.load_cfg_info()
        self.assertIsNone(info.error)
        self.assertEqual(info.problems, [])
        self.assertEqual(info.cfg, old)
        self.assertEqual(info.cfg["overrides"]["PPSA01234"]["modified_exec"], "image")

    def test_a_byte_order_mark_from_a_windows_editor_is_fine(self):
        self.cfg_path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"nice": 4}).encode())
        info = E.load_cfg_info()
        self.assertIsNone(info.error)
        self.assertEqual(info.cfg["nice"], 4)


class Validation(StateCase):
    def test_out_of_range_value_is_replaced_in_memory_and_reported(self):
        self.write_cfg({"compression_level": 99, "nice": 3})
        before = self.snapshot()
        info = E.load_cfg_info()
        self.assertEqual(info.cfg["compression_level"], E.DEFAULT_CFG["compression_level"])
        self.assertEqual(info.cfg["nice"], 3)                        # valid neighbours are kept
        self.assertIsNone(info.error)
        self.assertEqual(len(info.problems), 1)
        self.assertIn("compression_level", info.problems[0])
        self.assertIn("1..9", info.problems[0])
        self.assertIn("99", info.problems[0])
        self.assertEqual(self.snapshot(), before)                    # the file is not modified

    def test_bad_values_of_every_kind_fall_back_to_their_default(self):
        bad = {"build_on_ssd": "yes", "max_builds": "many", "cpu_count": 8.5, "nice": True, "max_compress": None,
               "console_fw": "nine", "library": 5, "skip_download_prefixes": "PC_",
               "junk_file_patterns": ["ok", "("], "junk_title_patterns": [1],
               "schedule": {"enabled": True, "times": ["3:00"]}, "ultrapack_src": []}
        self.write_cfg(bad)
        info = E.load_cfg_info()
        for k in bad:
            with self.subTest(k):
                self.assertEqual(info.cfg[k], E.DEFAULT_CFG[k])
                self.assertEqual(sum(p.startswith(k) for p in info.problems), 1, info.problems)
        self.assertEqual(len(info.problems), len(bad))

    def test_numbers_may_be_written_as_text_or_with_a_zero_fraction(self):
        self.write_cfg({"compression_level": "6", "max_builds": 4.0})
        info = E.load_cfg_info()
        self.assertEqual((info.cfg["compression_level"], info.cfg["max_builds"], info.problems), (6, 4, []))

    def test_blank_list_entries_are_dropped(self):
        self.write_cfg({"skip_download_prefixes": ["PC_", "", "  "], "junk_file_patterns": ["a", ""]})
        cfg = E.load_cfg()
        self.assertEqual(cfg["skip_download_prefixes"], ["PC_"])
        self.assertEqual(cfg["junk_file_patterns"], ["a"])

    def test_bad_override_fields_are_ignored_the_rest_kept(self):
        self.write_cfg({"overrides": {
            "PPSA00001": {"source": "/x", "skip": "yes", "modified_exec": "sideways", "future": [1]},
            "PPSA00002": "nope"}})
        info = E.load_cfg_info()
        self.assertEqual(info.cfg["overrides"], {"PPSA00001": {"source": "/x", "future": [1]}})
        self.assertEqual(len(info.problems), 3)
        self.assertTrue(all("overrides." in p for p in info.problems))

    def test_overrides_that_is_not_an_object(self):
        self.write_cfg({"overrides": ["PPSA00001"]})
        info = E.load_cfg_info()
        self.assertEqual(info.cfg["overrides"], {})
        self.assertEqual(len(info.problems), 1)

    def test_unknown_keys_are_kept_in_the_settings(self):
        self.write_cfg({"from_the_future": {"a": 1}})
        self.assertEqual(E.load_cfg()["from_the_future"], {"a": 1})

    def test_validate_setting_messages_match_the_settings_page_checks(self):
        for key, val, msg in [("compression_level", 99, "compression_level must be 1..9"),
                              ("max_builds", 0, "max_builds must be 1..6"),
                              ("console_fw", "9", "console_fw looks like 13.20"),
                              ("library", 3, "library must be text"),
                              ("nope", 1, "unknown setting nope")]:
            with self.subTest(key), self.assertRaises(ValueError) as cm:
                E.validate_setting(key, val)
            self.assertEqual(str(cm.exception), msg)
        self.assertEqual(E.validate_setting("junk_file_patterns", ["a", " ", "b"]), ["a", "b"])
        self.assertEqual(E.validate_setting("schedule", {"enabled": True, "times": ["03:00"], "x": 1}),
                         {"enabled": True, "times": ["03:00"]})


class BrokenFile(StateCase):
    BROKEN = '{\n  "compression_level": 5,\n  "nice": 3\n  "max_builds": 2\n}\n'     # missing comma after line 3

    def test_unreadable_file_serves_the_last_good_settings_and_says_why(self):
        self.write_cfg(legacy_config())
        good = E.load_cfg_info()
        self.assertIsNone(good.error)
        self.write_cfg(self.BROKEN)
        before = self.snapshot()
        info = E.load_cfg_info()
        self.assertEqual(info.cfg, good.cfg)
        self.assertEqual(info.fallback, "last_good")
        self.assertEqual((info.error.line, info.error.column > 0), (4, True))
        self.assertIn("Expecting ','", info.error.message)
        self.assertEqual(info.error.describe(), "line 4: " + info.error.message)
        self.assertEqual(set(info.error.as_dict()), {"message", "line", "column"})
        self.assertEqual(E.load_cfg(), good.cfg)                     # load_cfg() never raises either
        self.assertEqual(self.snapshot(), before)                    # and the broken file is left for the user

    def test_cold_start_with_a_broken_file_gives_the_defaults_and_the_error(self):
        self.write_cfg(self.BROKEN)
        info = E.load_cfg_info()
        self.assertEqual(info.cfg, E.DEFAULT_CFG)
        self.assertEqual(info.fallback, "defaults")
        self.assertEqual(info.error.line, 4)

    def test_fixing_the_file_clears_the_error(self):
        self.write_cfg(legacy_config())
        E.load_cfg()
        self.write_cfg(self.BROKEN)
        self.assertIsNotNone(E.load_cfg_info().error)
        self.write_cfg({"compression_level": 3})
        info = E.load_cfg_info()
        self.assertIsNone(info.error)
        self.assertEqual(info.cfg["compression_level"], 3)

    def test_other_ways_a_file_can_be_unusable(self):
        for text, line in [("", 1), ("[1, 2]", 1), ("not json", 1), ('{"a": 1,}', 1)]:
            with self.subTest(text):
                self.write_cfg(text)
                info = E.load_cfg_info()
                self.assertIsNotNone(info.error)
                self.assertEqual(info.error.line, line)
        self.cfg_path.write_bytes(b'{"note": "\xff\xfe"}')
        info = E.load_cfg_info()
        self.assertIn("UTF-8", info.error.message)
        self.assertIsNone(info.error.line)
        self.assertEqual(info.error.describe(), info.error.message)

    def test_an_editor_that_rewrites_in_place_is_not_reported_as_an_error(self):
        # the first read lands in the middle of a truncate-and-write; the retry sees the finished file
        self.write_cfg('{"nice": ')
        real, calls = E.Path.read_text, []

        def flaky(path, *a, **kw):
            calls.append(1)
            if len(calls) == 2:                                      # the retry: the editor has finished
                self.write_cfg('{"nice": 4}')
            return real(path, *a, **kw)

        with mock.patch.object(E.Path, "read_text", flaky):
            info = E.load_cfg_info()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(info.error)
        self.assertEqual(info.cfg["nice"], 4)


class Saving(StateCase):
    def test_a_save_changes_only_what_it_touches(self):
        raw = legacy_config()
        raw["from_the_future"] = {"keep": ["me"]}
        raw["compression_level"] = 99                                # invalid, but not this save's business
        raw["overrides"]["PPSA01234"]["future_field"] = 1
        self.write_cfg(raw)
        cfg = E.update_cfg(lambda r: r.update(nice=7))
        after = json.loads(self.cfg_path.read_text())
        self.assertEqual(after, {**raw, "nice": 7})
        self.assertEqual(cfg["nice"], 7)
        self.assertEqual(cfg["compression_level"], E.DEFAULT_CFG["compression_level"])   # still reported/replaced
        self.assertEqual(E.load_cfg(), cfg)

    def test_file_format_is_pretty_json_with_real_characters(self):
        self.write_cfg({"note_for_me": "Café"})
        E.update_cfg(lambda r: r.update(console_fw="9.60"))
        text = self.cfg_path.read_text(encoding="utf-8")
        self.assertIn('\n  "console_fw": "9.60"', text)               # indent=2
        self.assertIn("Café", text)                              # ensure_ascii=False
        self.assertTrue(text.endswith("}\n"))

    def test_a_save_refuses_to_overwrite_a_file_it_cannot_read(self):
        text = self.write_cfg(BrokenFile.BROKEN)
        before = self.snapshot()
        with self.assertRaises(E.ConfigError) as cm:
            E.update_cfg(lambda r: r.update(nice=7))
        self.assertEqual(cm.exception.line, 4)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.cfg_path.read_text(), text)

    def test_saving_with_no_file_yet_starts_from_the_defaults(self):
        E.update_cfg(lambda r: r.update(nice=7))
        self.assertEqual(json.loads(self.cfg_path.read_text()), {**E.DEFAULT_CFG, "nice": 7})

    def test_a_failed_save_leaves_the_old_file_and_no_temp_file(self):
        text = self.write_cfg(legacy_config())
        before = self.snapshot()
        with self.assertRaises(TypeError):
            E.update_cfg(lambda r: r.update(nice=object()))          # can't be serialised
        with self.assertRaises(RuntimeError):
            E.update_cfg(lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.cfg_path.read_text(), text)
        self.assertEqual(self.stray_files(), [])
        E.update_cfg(lambda r: r.update(nice=1))                     # and the lock was released
        self.assertEqual(E.load_cfg()["nice"], 1)

    def test_permissions_of_the_file_survive_a_save(self):
        self.write_cfg(legacy_config())
        os.chmod(self.cfg_path, 0o664)
        E.update_cfg(lambda r: r.update(nice=2))
        self.assertEqual(self.cfg_path.stat().st_mode & 0o777, 0o664)
        self.assertEqual(self.stray_files(), [])


class Concurrency(StateCase):
    def test_polling_readers_never_see_a_broken_file_and_never_revert_a_save(self):
        self.write_cfg(legacy_config())
        stop, problems = threading.Event(), []

        def reader():
            while not stop.is_set():
                info = E.load_cfg_info()
                if info.error or not 1 <= info.cfg["compression_level"] <= 9:
                    problems.append(info.error.describe() if info.error else info.cfg["compression_level"])

        readers = [threading.Thread(target=reader) for _ in range(4)]
        for t in readers:
            t.start()
        try:
            for i in range(150):
                want = i % 9 + 1
                E.update_cfg(lambda r: r.update(compression_level=want))
                self.assertEqual(E.load_cfg()["compression_level"], want, f"save #{i} was reverted")
        finally:
            stop.set()
            for t in readers:
                t.join()
        self.assertEqual(problems, [])
        self.assertEqual(json.loads(self.cfg_path.read_text())["compression_level"], want)

    def test_concurrent_saves_in_one_process_lose_nothing(self):
        self.write_cfg({"counter": 0})

        def bump():
            for _ in range(25):
                E.update_cfg(lambda r: r.update(counter=r["counter"] + 1))

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(json.loads(self.cfg_path.read_text())["counter"], 100)

    def test_concurrent_saves_from_two_processes_lose_nothing(self):
        # the web server and the engine are separate processes sharing one file
        self.write_cfg({"counter": 0})
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import ps5_pkg_sync as E\n"
                "for _ in range(20): E.update_cfg(lambda r: r.update(counter=r['counter'] + 1))\n")
        procs = [subprocess.Popen([sys.executable, "-I", "-c", code, str(ENGINE_DIR)], env=os.environ.copy())
                 for _ in range(3)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        self.assertEqual(json.loads(self.cfg_path.read_text())["counter"], 60)
        self.assertEqual(self.stray_files(), [])


class EngineStartsOnlyWithAReadableFile(StateCase):
    def run_engine(self, mode):
        return subprocess.run([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), mode],
                              capture_output=True, text=True, env=os.environ.copy(), timeout=120)

    def test_a_broken_file_is_a_clear_log_line_and_exit_code_not_a_traceback(self):
        self.write_cfg(BrokenFile.BROKEN)
        before = self.snapshot()
        for mode in ("run", "plan"):
            with self.subTest(mode):
                r = self.run_engine(mode)
                self.assertEqual(r.returncode, E.EXIT_BAD_CONFIG, r.stdout + r.stderr)
                self.assertNotIn("Traceback", r.stdout + r.stderr)
                for text in (r.stdout, (self.logs / "pkg_sync.log").read_text()):
                    self.assertIn("not starting: config.json can't be used (line 4: Expecting ','", text)
                self.assertFalse((self.state / "run.lock").exists())
        self.assertEqual(self.snapshot(), before)

    def test_invalid_values_are_logged_and_the_defaults_used(self):
        self.write_cfg({"compression_level": 99, "library": str(self.dir / "nowhere")})
        r = self.run_engine("plan")
        self.assertIn("config: compression_level must be 1..9", r.stdout)
        self.assertIn("not mounted", r.stdout)                       # got past the config check
        self.assertNotIn("Traceback", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
