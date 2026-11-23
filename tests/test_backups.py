"""Automatic backups of config.json (ps5_pkg_sync): one before every real change, the newest 10 kept, none on read,
poll or a save that changes nothing, and restore. Standard library only."""
from __future__ import annotations

import json
import os
from unittest import mock

from support import E, StateCase, legacy_config


class Backups(StateCase):
    def names(self):
        return [b["name"] for b in E.list_backups()]

    def contents(self):
        return {b["name"]: json.loads((self.state / "backups" / b["name"]).read_text()) for b in E.list_backups()}

    def test_a_change_backs_up_the_previous_version_first(self):
        text = self.write_cfg(legacy_config(nice=3))
        before_bytes = self.cfg_path.read_bytes()
        E.update_cfg(lambda raw: raw.update(nice=4))
        names = self.names()
        self.assertEqual(len(names), 1)
        self.assertRegex(names[0], r"^config-\d{8}-\d{6}\.json$")
        self.assertEqual((self.state / "backups" / names[0]).read_bytes(), before_bytes)       # exactly the old file
        self.assertEqual(json.loads((self.state / "backups" / names[0]).read_text())["nice"], 3)
        self.assertEqual(json.loads(self.cfg_path.read_text())["nice"], 4)
        self.assertEqual(text.count("nice"), 1)

    def test_a_hand_edited_file_is_backed_up_byte_for_byte(self):
        odd = '{"nice":   3 ,\n\n  "from_the_future": {"a": [1,2]}}\n'
        self.write_cfg(odd)
        E.update_cfg(lambda raw: raw.update(nice=4))
        self.assertEqual((self.state / "backups" / self.names()[0]).read_text(), odd)

    def test_reading_polling_and_the_first_start_never_write_a_backup(self):
        for _ in range(20):
            E.load_cfg()
            E.load_cfg_info()
        self.assertTrue(self.cfg_path.exists())                                # created from the defaults
        self.assertFalse((self.state / "backups").exists())
        os.utime(self.cfg_path, ns=(10**18, 10**18))
        E.update_cfg(lambda raw: raw.update(nice=4))                           # the first real change backs up the defaults
        self.assertEqual(len(self.names()), 1)

    def test_a_save_that_changes_nothing_writes_nothing(self):
        self.write_cfg(legacy_config(nice=3))
        before = self.snapshot()
        E.update_cfg(lambda raw: raw.update(nice=3))
        E.update_cfg(lambda raw: None)
        E.update_cfg(lambda raw: raw.setdefault("nice", 99))
        self.assertEqual(self.snapshot(), before)                              # bytes and mtime
        self.assertEqual(self.names(), [])

    def test_the_newest_ten_are_kept(self):
        self.write_cfg(legacy_config(nice=0))
        stamps = iter(f"202611{d:02d}-0300{d:02d}" for d in range(1, 40))
        with mock.patch.object(E.time, "strftime", lambda fmt, *a: next(stamps) if fmt == "%Y%m%d-%H%M%S" else "x"):
            for n in range(1, 15):
                E.update_cfg(lambda raw, n=n: raw.update(cpu_count=n))
        got = self.contents()
        self.assertEqual(len(got), E.BACKUP_KEEP)
        # 14 saves made 14 backups of the file as it was before each: cpu_count 8(default via legacy), 1, 2, ..., 13
        self.assertEqual(sorted(v["cpu_count"] for v in got.values()), [4, 5, 6, 7, 8, 9, 10, 11, 12, 13])
        self.assertEqual(self.names()[0], "config-20261114-030014.json")        # newest first

    def test_two_changes_in_the_same_second_do_not_overwrite_each_other(self):
        self.write_cfg(legacy_config(nice=0))
        with mock.patch.object(E.time, "strftime", lambda fmt, *a: "20261122-030000" if fmt == "%Y%m%d-%H%M%S" else "x"):
            for n in (1, 2, 3):
                E.update_cfg(lambda raw, n=n: raw.update(nice=n))
        self.assertEqual(self.names(), ["config-20261122-030000-3.json", "config-20261122-030000-2.json", "config-20261122-030000.json"])
        self.assertEqual([self.contents()[n]["nice"] for n in self.names()], [2, 1, 0])

    def test_a_backup_that_cannot_be_written_does_not_stop_the_save(self):
        self.write_cfg(legacy_config(nice=3))
        (self.state / "backups").write_text("a file where the folder should be")
        E.update_cfg(lambda raw: raw.update(nice=4))
        self.assertEqual(E.load_cfg()["nice"], 4)
        self.assertIn("can't write the backup", E.backup_status["error"])
        (self.state / "backups").unlink()
        E.update_cfg(lambda raw: raw.update(nice=5))
        self.assertIsNone(E.backup_status["error"])
        self.assertEqual(len(self.names()), 1)

    def test_other_files_in_the_folder_are_not_listed_or_pruned(self):
        self.write_cfg(legacy_config(nice=3))
        E.update_cfg(lambda raw: raw.update(nice=4))
        (self.state / "backups" / "notes.txt").write_text("mine")
        (self.state / "backups" / "config-oops.json").write_text("{}")
        self.assertEqual(len(self.names()), 1)
        for n in range(5, 25):
            E.update_cfg(lambda raw, n=n: raw.update(nice=n % 19))
        self.assertTrue((self.state / "backups" / "notes.txt").exists())
        self.assertTrue((self.state / "backups" / "config-oops.json").exists())


class Restore(StateCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config(nice=3, console_fw="9.60"))
        E.update_cfg(lambda raw: raw.update(nice=4, console_fw="10.00"))        # backup #1 holds nice=3
        self.old = E.list_backups()[0]["name"]

    def test_restoring_makes_it_current_and_backs_up_what_it_replaced_first(self):
        E.update_cfg(lambda raw: raw.update(nice=7))                             # backup #2 holds nice=4
        replaced = self.cfg_path.read_bytes()
        cfg = E.restore_backup(self.old)
        self.assertEqual((cfg["nice"], cfg["console_fw"]), (3, "9.60"))
        self.assertEqual(json.loads(self.cfg_path.read_text())["nice"], 3)
        self.assertEqual((self.state / "backups" / self.old).read_bytes(), self.cfg_path.read_bytes())    # exactly that file
        newest = (self.state / "backups" / E.list_backups()[0]["name"]).read_bytes()
        self.assertEqual(newest, replaced)                                        # the replaced version is the newest backup
        self.assertEqual(E.load_cfg()["nice"], 3)

    def test_restoring_over_a_broken_file_is_the_way_out_and_keeps_the_broken_one(self):
        self.write_cfg('{"nice": 4\n "oops": 1}')
        broken = self.cfg_path.read_bytes()
        E.restore_backup(self.old)
        self.assertIsNone(E.load_cfg_info().error)
        self.assertEqual((self.state / "backups" / E.list_backups()[0]["name"]).read_bytes(), broken)

    def test_what_cannot_be_restored(self):
        with self.assertRaises(ValueError):
            E.restore_backup("../config.json")
        with self.assertRaises(ValueError):
            E.restore_backup("config-oops.json")
        with self.assertRaises(FileNotFoundError):
            E.restore_backup("config-20200101-000000.json")
        (self.state / "backups" / "config-20200101-000000.json").write_text("[1, 2]")
        with self.assertRaises(ValueError) as cm:
            E.restore_backup("config-20200101-000000.json")
        self.assertIn("not a JSON object", str(cm.exception))
        (self.state / "backups" / "config-20200102-000000.json").write_text("{nope")
        with self.assertRaises(ValueError) as cm:
            E.restore_backup("config-20200102-000000.json")
        self.assertIn("not valid JSON", str(cm.exception))
        self.assertEqual(E.load_cfg()["nice"], 4)                                # nothing changed

    def test_the_settings_a_restored_file_has_are_reported_as_they_load(self):
        (self.state / "backups" / "config-20200101-000000.json").write_text(json.dumps({"compression_level": 99, "keep_me": 1}))
        cfg = E.restore_backup("config-20200101-000000.json")
        self.assertEqual(cfg["compression_level"], E.DEFAULT_CFG["compression_level"])
        self.assertEqual(json.loads(self.cfg_path.read_text()), {"compression_level": 99, "keep_me": 1})   # the file is as it was


if __name__ == "__main__":
    import unittest
    unittest.main()
