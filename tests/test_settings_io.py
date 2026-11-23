"""Exporting and importing the settings (ps5_settingsio): what an export holds, how an import is checked, the
preview and what applying does and doesn't touch. Standard library only."""
from __future__ import annotations

import copy
import json
import unittest

from support import E, StateCase, legacy_config

import ps5_settingsio as IO  # noqa: E402


def cfg_with(**over):
    cfg, _ = E.validate_cfg(legacy_config(**over))
    return cfg


class Export(StateCase):
    def test_it_holds_the_settings_the_page_can_change_and_the_overrides_and_nothing_else(self):
        cfg = cfg_with(notify_url="https://ntfy.example/topic-abc", from_the_future={"a": 1})
        doc = IO.export_doc(cfg, app_version="0.2.1", now=1_700_000_000)
        self.assertEqual(set(doc), {"format", "app_version", "exported_at", "includes_notify_url", "settings", "overrides"})
        self.assertEqual((doc["format"], doc["app_version"], doc["exported_at"], doc["includes_notify_url"]),
                         (1, "0.2.1", "2023-11-14T22:13:20Z", True))
        self.assertEqual(set(doc["settings"]), set(E.SETTINGS))
        for private in ("ultrapack_src", "python", "from_the_future", "overrides"):
            self.assertNotIn(private, doc["settings"])
        self.assertEqual(doc["settings"]["notify_url"], "https://ntfy.example/topic-abc")
        self.assertEqual(doc["overrides"], cfg["overrides"])
        json.dumps(doc)                                                          # plain JSON

    def test_the_notification_url_can_be_left_out(self):
        doc = IO.export_doc(cfg_with(notify_url="https://ntfy.example/topic-abc"), include_notify_url=False)
        self.assertNotIn("notify_url", doc["settings"])
        self.assertFalse(doc["includes_notify_url"])
        self.assertNotIn("topic-abc", json.dumps(doc))

    def test_it_is_made_of_the_effective_values(self):
        cfg, _ = E.validate_cfg(legacy_config(compression_level=99))              # an invalid value in the file
        self.assertEqual(IO.export_doc(cfg)["settings"]["compression_level"], E.DEFAULT_CFG["compression_level"])

    def test_it_is_a_copy(self):
        cfg = cfg_with()
        doc = IO.export_doc(cfg)
        doc["settings"]["schedule"]["times"].append("04:00")
        doc["overrides"]["PPSA01234"]["note"] = "changed"
        self.assertNotIn("04:00", cfg["schedule"]["times"])
        self.assertNotEqual(cfg["overrides"]["PPSA01234"]["note"], "changed")


class Parse(unittest.TestCase):
    GOOD = dict(settings=dict(compression_level=5, console_fw="9.60", schedule=dict(enabled=True, times=["03:00"])),
                overrides={"PPSA01234": {"modified_exec": "image", "skip": True, "note": "x", "future": [1]}})

    def problems(self, doc):
        with self.assertRaises(IO.ImportProblem) as cm:
            IO.parse_import(doc)
        return cm.exception.problems

    def test_a_good_document(self):
        settings, overrides = IO.parse_import(dict(self.GOOD, format=1, app_version="0.2.0", exported_at="x"))
        self.assertEqual(settings["compression_level"], 5)
        self.assertEqual(overrides["PPSA01234"]["future"], [1])                  # a field this version doesn't know is kept

    def test_the_values_are_cleaned_like_the_settings_page_does(self):
        settings, _ = IO.parse_import({"settings": {"junk_file_patterns": ["a", "  "], "compression_level": "6", "library": "/lib"}})
        self.assertEqual((settings["junk_file_patterns"], settings["compression_level"]), (["a"], 6))

    def test_an_unknown_key_is_rejected_and_named(self):
        for key in ("from_the_future", "ultrapack_src", "python", "overrides"):
            with self.subTest(key):
                p = self.problems({"settings": {key: 1, "nice": 3}})
                self.assertEqual(len(p), 1)
                self.assertTrue(p[0].startswith(f"settings.{key}: not a setting"), p)

    def test_a_wrong_type_and_an_out_of_range_value_are_rejected_with_the_key(self):
        p = self.problems({"settings": {"compression_level": 99, "build_on_ssd": "yes", "schedule": {"times": ["3:00"]}, "nice": 4}})
        self.assertEqual(len(p), 3)
        self.assertIn("settings.compression_level: compression_level must be 1..9", p)
        self.assertIn("settings.build_on_ssd: build_on_ssd must be true or false", p)
        self.assertTrue(any(x.startswith("settings.schedule: schedule.times:") for x in p))

    def test_every_problem_is_listed_at_once(self):
        p = self.problems({"settings": {"nope": 1, "nice": 99}, "overrides": {"PPSA1": {}, "PPSA01234": {"skip": "yes", "modified_exec": "sideways", "source": 5}, "PPSA05678": 3}})
        self.assertEqual(len(p), 7, p)
        self.assertIn("overrides.PPSA1: not a title ID like PPSA01234", p)
        self.assertIn("overrides.PPSA01234.skip: must be true or false", p)
        self.assertIn("overrides.PPSA01234.source: must be text", p)
        self.assertIn("overrides.PPSA05678: must be an object", p)
        self.assertTrue(any(x.startswith('overrides.PPSA01234.modified_exec: must be "image" or "original"') for x in p))
        self.assertIn("Not imported: ", str(IO.ImportProblem(p)))

    def test_what_is_not_a_settings_export(self):
        self.assertEqual(len(self.problems([1, 2])), 1)
        self.assertEqual(len(self.problems("text")), 1)
        self.assertIn("neither settings nor overrides", self.problems({})[0])
        self.assertIn("neither settings nor overrides", self.problems({"app_version": "1"})[0])
        self.assertIn("settings: must be an object", self.problems({"settings": [1], "overrides": {}})[0])
        self.assertIn("overrides: must be an object", self.problems({"settings": {}, "overrides": []})[0])
        self.assertTrue(self.problems({"format": 2, "settings": {}})[0].startswith("format: 2 was made by a newer version"))

    def test_the_parts_choice(self):
        self.assertEqual(IO.parse_parts(""), ["settings", "overrides"])
        self.assertEqual(IO.parse_parts("overrides"), ["overrides"])
        for bad in ("everything", "settings,x", ","):
            with self.assertRaises(ValueError):
                IO.parse_parts(bad)


class Diff(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_with(console_fw="9.60", compression_level=5)
        self.cfg["overrides"] = {"PPSA01234": {"skip": True}, "PPSA05678": {"note": "same"}, "PPSA09999": {"skip": True}}

    def test_the_preview_counts_what_would_change(self):
        settings = {"compression_level": 4, "console_fw": "10.00", "nice": 9, "max_builds": self.cfg["max_builds"]}
        overrides = {"PPSA01234": {"modified_exec": "image"}, "PPSA05678": {"note": "same"}, "PPSA00001": {"skip": True}, "PPSA00002": {"skip": True}}
        d = IO.diff(self.cfg, settings, overrides, IO.PARTS)
        self.assertEqual(d["summary"], "3 settings change, 2 overrides added, 1 replaced")
        self.assertEqual({c["key"]: (c["from"], c["to"]) for c in d["settings"]["changed"]},
                         {"compression_level": (5, 4), "console_fw": ("9.60", "10.00"), "nice": (self.cfg["nice"], 9)})
        self.assertEqual((d["settings"]["unchanged"], d["overrides"]["unchanged"], d["changes"]), (1, 1, 6))
        self.assertEqual([a["title_id"] for a in d["overrides"]["added"]], ["PPSA00001", "PPSA00002"])
        self.assertEqual(d["overrides"]["replaced"][0], {"title_id": "PPSA01234", "from": {"skip": True}, "to": {"modified_exec": "image"}})
        self.assertEqual(d["overrides"]["removed"], [])                          # an import never removes

    def test_only_the_chosen_parts_count(self):
        settings, overrides = {"nice": 9}, {"PPSA00001": {"skip": True}}
        self.assertEqual(IO.diff(self.cfg, settings, overrides, ["settings"])["summary"], "1 setting change")
        self.assertEqual(IO.diff(self.cfg, settings, overrides, ["overrides"])["summary"], "1 override added")

    def test_nothing_to_change(self):
        d = IO.diff(self.cfg, {"console_fw": "9.60"}, {"PPSA05678": {"note": "same"}}, IO.PARTS)
        self.assertEqual((d["summary"], d["changes"]), ("Nothing would change.", 0))

    def test_a_folder_inside_the_container_is_flagged(self):
        d = IO.diff(self.cfg, {"library": "/elsewhere", "nice": 9}, {}, ["settings"])
        notes = {c["key"]: c.get("note") for c in d["settings"]["changed"]}
        self.assertIn("inside the container", notes["library"])
        self.assertIsNone(notes["nice"])

    def test_a_restore_also_lists_what_it_removes(self):
        d = IO.diff(self.cfg, {}, {"PPSA01234": {"skip": True}}, IO.PARTS, replace_all=True)
        self.assertEqual(sorted(r["title_id"] for r in d["overrides"]["removed"]), ["PPSA05678", "PPSA09999"])
        self.assertEqual(d["summary"], "2 removed")


class Apply(unittest.TestCase):
    def raw(self):
        return {"nice": 3, "from_the_future": {"a": 1}, "overrides": {"PPSA01234": {"skip": True, "future_field": "keep"},
                                                                       "PPSA05678": {"note": "untouched"}, "weird": 5}}

    def test_it_sets_what_the_document_says_and_nothing_else(self):
        raw = self.raw()
        IO.apply_import(raw, {"compression_level": 4}, {"PPSA01234": {"modified_exec": "image"}, "PPSA00001": {"skip": True}}, IO.PARTS)
        self.assertEqual(raw["nice"], 3)
        self.assertEqual(raw["compression_level"], 4)
        self.assertEqual(raw["from_the_future"], {"a": 1})                       # a key it doesn't know stays
        self.assertEqual(raw["overrides"]["PPSA05678"], {"note": "untouched"})   # overrides not in the file stay
        self.assertEqual(raw["overrides"]["weird"], 5)                            # even one it doesn't understand
        self.assertEqual(raw["overrides"]["PPSA00001"], {"skip": True})
        self.assertEqual(raw["overrides"]["PPSA01234"], {"modified_exec": "image", "future_field": "keep"})   # replaced; its unknown field stays

    def test_only_the_chosen_parts_are_applied(self):
        raw = self.raw()
        IO.apply_import(raw, {"nice": 9}, {"PPSA00001": {"skip": True}}, ["settings"])
        self.assertEqual((raw["nice"], "PPSA00001" in raw["overrides"]), (9, False))
        raw = self.raw()
        IO.apply_import(raw, {"nice": 9}, {"PPSA00001": {"skip": True}}, ["overrides"])
        self.assertEqual((raw["nice"], "PPSA00001" in raw["overrides"]), (3, True))

    def test_overrides_that_were_not_an_object_are_replaced_by_one(self):
        raw = {"overrides": "oops"}
        IO.apply_import(raw, {}, {"PPSA00001": {"skip": True}}, ["overrides"])
        self.assertEqual(raw["overrides"], {"PPSA00001": {"skip": True}})

    def test_the_document_is_copied_not_shared(self):
        settings, overrides = {"schedule": {"enabled": True, "times": ["03:00"]}}, {"PPSA00001": {"note": "n"}}
        raw = {}
        IO.apply_import(raw, settings, overrides, IO.PARTS)
        raw["schedule"]["times"].append("x")
        raw["overrides"]["PPSA00001"]["note"] = "changed"
        self.assertEqual((settings["schedule"]["times"], overrides["PPSA00001"]["note"]), (["03:00"], "n"))


class RoundTrip(StateCase):
    def test_export_from_one_instance_and_import_into_a_fresh_one_gives_the_same_settings_and_overrides(self):
        original = legacy_config(console_fw="9.60", compression_level=4, notify_url="https://ntfy.example/t", host_paths={"library": "/srv/games/ps5"},
                                 junk_file_patterns=[r"\.url$"], schedule={"enabled": True, "times": ["03:00", "15:30"]},
                                 overrides={"PPSA01234": {"source": "/library/x", "modified_exec": "image", "note": "café", "skip": True, "skip_verify": True},
                                            "PPSA05678": {"patch": "/config/patches/PPSA05678"}})
        self.write_cfg(original)
        text = json.dumps(IO.export_doc(E.load_cfg(), app_version="0.2.1"), ensure_ascii=False)
        # a fresh instance: no file, nothing in memory
        self.cfg_path.unlink()
        E._last_good = None
        fresh = E.load_cfg()
        self.assertNotEqual(fresh["console_fw"], "9.60")
        settings, overrides = IO.parse_import(json.loads(text))
        E.update_cfg(lambda raw: IO.apply_import(raw, settings, overrides, IO.PARTS))
        after = E.load_cfg()
        for k in E.SETTINGS:
            self.assertEqual(after[k], E.validate_cfg(original)[0][k], k)
        self.assertEqual(after["overrides"], original["overrides"])
        # and exporting that gives the same document again
        self.assertEqual(IO.export_doc(after, app_version="0.2.1", now=0)["settings"], json.loads(text)["settings"])
        self.assertEqual(IO.export_doc(after, now=0)["overrides"], json.loads(text)["overrides"])


if __name__ == "__main__":
    unittest.main()
