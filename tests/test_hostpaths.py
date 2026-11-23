"""Where the container's folders are on the host (ps5_hostpaths): the environment variables, the precedence
between them, the mount-table hint and the `host_paths` setting. Standard library only."""
from __future__ import annotations

import json
import unittest

from support import E, StateCase, legacy_config

import ps5_hostpaths as H  # noqa: E402

MOUNTINFO = r"""
22 1 0:21 / / rw,relatime - overlay overlay rw,lowerdir=/x,upperdir=/y
23 22 0:5 / /proc rw,nosuid - proc proc rw
24 22 253:1 /games/ps5 /library ro,relatime shared:8 - btrfs /dev/mapper/vg-volume_1 rw,space_cache=v2
25 22 253:1 /games/ps5/pkg /output rw,relatime shared:8 master:2 - btrfs /dev/mapper/vg-volume_1 rw
26 22 8:17 /scratch /work rw,noatime - ext4 /dev/sdb1 rw
27 22 253:1 /my\040files/cfg /config rw,relatime - btrfs /dev/mapper/vg-volume_1 rw
28 22 0:60 / /downloads rw,relatime - tmpfs tmpfs rw
"""
CONTAINERS = {"library": "/library", "downloads": "/downloads", "output": "/output", "temp": "/work/tmp",
              "config": "/config"}


def resolve(env=None, settings=None, mounts=None, containers=CONTAINERS):
    found, _ = H.read_env(env or {})
    return H.resolve(containers, found, settings or {}, H.parse_mountinfo(MOUNTINFO) if mounts is None else mounts)


class MountInfo(unittest.TestCase):
    def test_parsing_handles_optional_fields_and_escaped_spaces(self):
        mounts = {m["mount_point"]: m for m in H.parse_mountinfo(MOUNTINFO)}
        self.assertEqual(mounts["/library"]["root"], "/games/ps5")
        self.assertEqual(mounts["/library"]["options"][0], "ro")
        self.assertEqual(mounts["/output"]["fstype"], "btrfs")          # after "shared:8 master:2 -"
        self.assertEqual(mounts["/config"]["root"], "/my files/cfg")
        self.assertEqual(H.parse_mountinfo("garbage\n\n1 2 3\n"), [])
        self.assertEqual(H.read_mountinfo("/nonexistent/mountinfo"), [])

    def test_a_bind_mount_gives_the_tail_of_the_host_path_and_the_mode(self):
        mounts = H.parse_mountinfo(MOUNTINFO)
        self.assertEqual(H.detect("/library", mounts), dict(tail="/games/ps5", mode="ro", mount_point="/library"))
        self.assertEqual(H.detect("/output", mounts)["mode"], "rw")
        self.assertEqual(H.detect("/config", mounts)["tail"], "/my files/cfg")

    def test_a_folder_below_the_mount_adds_its_sub_path(self):
        self.assertEqual(H.detect("/work/tmp", H.parse_mountinfo(MOUNTINFO))["tail"], "/scratch/tmp")

    def test_the_most_specific_mount_wins(self):
        mounts = H.parse_mountinfo(MOUNTINFO)
        self.assertEqual(H.detect("/output/sub", mounts)["tail"], "/games/ps5/pkg/sub")

    def test_the_containers_own_filesystem_and_pseudo_filesystems_tell_nothing(self):
        mounts = H.parse_mountinfo(MOUNTINFO)
        self.assertIsNone(H.detect("/nowhere", mounts))                 # only the overlay root holds it
        self.assertIsNone(H.detect("/downloads", mounts))               # a tmpfs
        self.assertIsNone(H.detect("/library", []))

    def test_a_whole_filesystem_tells_nothing(self):
        whole = H.parse_mountinfo("30 22 8:1 / /library rw - ext4 /dev/sda1 rw\n")
        self.assertIsNone(H.detect("/library", whole))


class Environment(unittest.TestCase):
    def test_nothing_set(self):
        self.assertEqual(H.read_env({}), ({}, []))
        self.assertEqual(H.read_env({"PS5PKG_HOST_LIBRARY": "", "PS5PKG_HOST_PATHS": "  "}), ({}, []))   # compose's empty ${VAR}

    def test_the_per_folder_variables(self):
        found, problems = H.read_env({"PS5PKG_HOST_LIBRARY": "/srv/games/ps5/", "PS5PKG_HOST_DOWNLOADS": "/srv/dl",
                                      "PS5PKG_HOST_OUTPUT": "/srv/games/ps5/pkg", "PS5PKG_HOST_WORK": "/srv/scratch",
                                      "PS5PKG_HOST_CONFIG": "/srv/cfg"})
        self.assertEqual(problems, [])
        self.assertEqual({k: v["path"] for k, v in found.items()},
                         {"library": "/srv/games/ps5", "downloads": "/srv/dl", "output": "/srv/games/ps5/pkg",
                          "temp": "/srv/scratch", "config": "/srv/cfg"})
        self.assertEqual(found["temp"]["via"], "PS5PKG_HOST_WORK")

    def test_the_original_json_variable_still_works_with_its_four_keys(self):
        live = json.dumps({"library": "/srv/games/ps5", "downloads": "/srv/games/dl", "output": "/srv/games/ps5/pkg",
                           "temp": "/srv/scratch/tmp"})
        found, problems = H.read_env({"PS5PKG_HOST_PATHS": live})
        self.assertEqual(problems, [])
        self.assertEqual({k: v["path"] for k, v in found.items()}, json.loads(live))
        self.assertTrue(all(v["via"] == "PS5PKG_HOST_PATHS" and not v["mount"] for v in found.values()))

    def test_work_is_accepted_as_the_json_name_of_the_work_folder(self):
        found, _ = H.read_env({"PS5PKG_HOST_PATHS": '{"work": "/srv/scratch/tmp"}'})
        self.assertEqual(found["temp"]["path"], "/srv/scratch/tmp")

    def test_a_per_folder_variable_beats_the_json_entry_for_the_same_folder(self):
        found, problems = H.read_env({"PS5PKG_HOST_PATHS": '{"library": "/old", "output": "/old/pkg"}',
                                      "PS5PKG_HOST_LIBRARY": "/new"})
        self.assertEqual((found["library"]["path"], found["library"]["via"]), ("/new", "PS5PKG_HOST_LIBRARY"))
        self.assertEqual(found["output"]["path"], "/old/pkg")
        self.assertEqual(problems, [])

    def test_malformed_values_are_reported_and_ignored_not_dropped_silently(self):
        found, problems = H.read_env({"PS5PKG_HOST_PATHS": '{"library": "/srv/games/ps5", }'})
        self.assertEqual(found, {})
        self.assertEqual(len(problems), 1)
        self.assertIn("PS5PKG_HOST_PATHS is not valid JSON", problems[0])
        found, problems = H.read_env({"PS5PKG_HOST_PATHS": '["/srv"]'})
        self.assertIn("expected a JSON object", problems[0])
        found, problems = H.read_env({"PS5PKG_HOST_PATHS": '{"library": "/srv", "pkg": "/x", "output": "pkg", "temp": 5}',
                                      "PS5PKG_HOST_CONFIG": "relative/dir"})
        self.assertEqual({k: v["path"] for k, v in found.items()}, {"library": "/srv"})
        self.assertEqual(len(problems), 4, problems)
        self.assertTrue(any("unknown folder 'pkg'" in p for p in problems))
        self.assertTrue(any("PS5PKG_HOST_PATHS.output must be an absolute path" in p for p in problems))
        self.assertTrue(any("PS5PKG_HOST_PATHS.temp must be text" in p for p in problems))
        self.assertTrue(any("PS5PKG_HOST_CONFIG must be an absolute path" in p for p in problems))

    def test_windows_style_host_paths_are_fine(self):
        found, problems = H.read_env({"PS5PKG_HOST_LIBRARY": "D:\\Games\\PS5\\", "PS5PKG_HOST_OUTPUT": "\\\\server\\share",
                                      "PS5PKG_HOST_WORK": "E:\\"})
        self.assertEqual(problems, [])
        self.assertEqual((found["library"]["path"], found["output"]["path"], found["temp"]["path"]),
                         ("D:\\Games\\PS5", "\\\\server\\share", "E:\\"))


class Precedence(unittest.TestCase):
    def test_the_documented_order_env_then_json_then_settings_then_the_detected_hint(self):
        settings = {"library": "/from/settings", "output": "/from/settings/pkg", "temp": "/from/settings/tmp"}
        r = resolve(env={"PS5PKG_HOST_LIBRARY": "/from/env", "PS5PKG_HOST_PATHS": '{"library": "/from/json", "output": "/from/json/pkg"}'},
                    settings=settings)
        self.assertEqual((r["library"]["host"], r["library"]["source"], r["library"]["via"]),
                         ("/from/env", "env", "PS5PKG_HOST_LIBRARY"))
        self.assertEqual((r["output"]["host"], r["output"]["source"], r["output"]["via"]),
                         ("/from/json/pkg", "env", "PS5PKG_HOST_PATHS"))
        self.assertEqual((r["temp"]["host"], r["temp"]["source"]), ("/from/settings/tmp", "settings"))
        self.assertEqual((r["config"]["host"], r["config"]["source"], r["config"]["detected"]),
                         ("/my files/cfg", "detected", "/my files/cfg"))
        self.assertEqual((r["downloads"]["host"], r["downloads"]["source"], r["downloads"]["via"]), (None, "none", None))

    def test_the_work_variable_is_the_volume_so_the_sub_folder_is_added_but_the_json_value_is_taken_as_is(self):
        self.assertEqual(resolve(env={"PS5PKG_HOST_WORK": "/srv/scratch"})["temp"]["host"], "/srv/scratch/tmp")
        self.assertEqual(resolve(env={"PS5PKG_HOST_PATHS": '{"temp": "/srv/scratch/tmp"}'})["temp"]["host"], "/srv/scratch/tmp")
        self.assertEqual(resolve(settings={"temp": "/srv/scratch/tmp"})["temp"]["host"], "/srv/scratch/tmp")
        # a folder the app uses outside the standard mount: the value is used as given
        odd = dict(CONTAINERS, temp="/scratch/jobs")
        self.assertEqual(resolve(env={"PS5PKG_HOST_WORK": "/srv/scratch"}, containers=odd)["temp"]["host"], "/srv/scratch")

    def test_every_entry_has_the_documented_fields(self):
        for f in resolve().values():
            self.assertEqual(set(f), {"key", "container", "host", "source", "via", "detected", "mismatch", "mode", "setting"})
            self.assertIn(f["source"], ("env", "settings", "detected", "none"))

    def test_nothing_configured_and_no_mount_table(self):
        r = resolve(mounts=[])
        self.assertEqual({f["source"] for f in r.values()}, {"none"})
        self.assertEqual({f["mismatch"] for f in r.values()}, {False})


class Mismatch(unittest.TestCase):
    def test_a_host_path_whose_tail_agrees_with_the_mount_is_fine_whatever_the_prefix(self):
        r = resolve(env={"PS5PKG_HOST_LIBRARY": "/mnt/disk1/games/ps5", "PS5PKG_HOST_OUTPUT": "/srv/other/games/ps5/pkg/",
                         "PS5PKG_HOST_WORK": "/mnt/scratch"})
        self.assertEqual([r[k]["mismatch"] for k in ("library", "output", "temp")], [False, False, False])

    def test_a_host_path_that_disagrees_is_flagged(self):
        r = resolve(env={"PS5PKG_HOST_LIBRARY": "/mnt/disk1/games/ps4", "PS5PKG_HOST_OUTPUT": "/mnt/disk1/games/ps5"})
        self.assertTrue(r["library"]["mismatch"])
        self.assertTrue(r["output"]["mismatch"])           # a prefix of the tail is not a match
        self.assertEqual(r["library"]["detected"], "/games/ps5")

    def test_settings_values_are_checked_too_and_case_and_slashes_do_not_matter(self):
        r = resolve(settings={"library": "D:\\Volume1\\Games\\PS5", "output": "/x/GAMES/ps5/PKG"})
        self.assertFalse(r["library"]["mismatch"])
        self.assertFalse(r["output"]["mismatch"])
        self.assertTrue(resolve(settings={"library": "/srv/elsewhere"})["library"]["mismatch"])

    def test_no_hint_no_mismatch(self):
        self.assertFalse(resolve(env={"PS5PKG_HOST_LIBRARY": "/anything"}, mounts=[])["library"]["mismatch"])


class SettingValue(StateCase):
    def test_validation(self):
        self.assertEqual(H.validate_setting({"library": " /srv/games/ps5/ ", "output": "", "temp": "  "}),
                         {"library": "/srv/games/ps5"})
        for bad, text in [({"pkg": "/x"}, "unknown folder 'pkg'"), ({"library": "games"}, "host_paths.library must be an absolute path"),
                          ({"library": 3}, "host_paths.library must be text")]:
            with self.subTest(bad), self.assertRaises(ValueError) as cm:
                H.validate_setting(bad)
            self.assertIn(text, str(cm.exception))

    def test_a_config_file_from_before_this_setting_loads_with_an_empty_one_and_is_not_touched(self):
        old = legacy_config()
        del old["host_paths"]
        self.write_cfg(old)
        before = self.snapshot()
        info = E.load_cfg_info()
        self.assertEqual((info.cfg["host_paths"], info.problems, info.error), ({}, [], None))
        self.assertEqual(self.snapshot(), before)

    def test_a_bad_value_in_the_file_is_reported_and_the_default_used(self):
        self.write_cfg({"host_paths": {"library": "/ok", "output": "relative"}})
        info = E.load_cfg_info()
        self.assertEqual(info.cfg["host_paths"], {})
        self.assertEqual(len(info.problems), 1)
        self.assertIn("host_paths.output must be an absolute path", info.problems[0])

    def test_a_save_keeps_it_in_the_file(self):
        self.write_cfg(legacy_config())
        E.update_cfg(lambda raw: raw.update(host_paths=E.validate_setting("host_paths", {"library": "/srv/games/ps5"})))
        self.assertEqual(json.loads(self.cfg_path.read_text())["host_paths"], {"library": "/srv/games/ps5"})
        E._last_good = None                                  # a container recreate: nothing in memory
        self.assertEqual(E.load_cfg()["host_paths"], {"library": "/srv/games/ps5"})


if __name__ == "__main__":
    unittest.main()
