"""Translating container paths into host paths for display (ps5_hostpaths.display_path / display_text /
to_container) and what the engine's notifications say. Standard library only."""
from __future__ import annotations

import http.server
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import E, StateCase, make_env

import ps5_hostpaths as H  # noqa: E402

MAP = [("/work/tmp", "/srv/scratch/tmp"), ("/library", "/srv/games/ps5"), ("/output", "/srv/games/ps5/pkg"),
       ("/downloads", "/srv/games/dl"), ("/config", "/srv/docker/ps5-pkg/config"), ("/work", "/srv/scratch")]
MAP.sort(key=lambda kv: -len(kv[0]))


class DisplayPath(unittest.TestCase):
    def test_no_mapping_means_no_host_path(self):
        self.assertIsNone(H.display_path("/library/Example Game [PPSA01234].exfat", []))
        self.assertIsNone(H.display_path("/somewhere/else", MAP))

    def test_the_root_of_a_folder_itself(self):
        self.assertEqual(H.display_path("/library", MAP), "/srv/games/ps5")
        self.assertEqual(H.display_path("/output", MAP), "/srv/games/ps5/pkg")

    def test_a_nested_path(self):
        self.assertEqual(H.display_path("/library/Example Game [PPSA01234].exfat", MAP),
                         "/srv/games/ps5/Example Game [PPSA01234].exfat")
        self.assertEqual(H.display_path("/output/.staging/PPSA01234", MAP), "/srv/games/ps5/pkg/.staging/PPSA01234")
        self.assertEqual(H.display_path("/config/patches/PPSA01234", MAP), "/srv/docker/ps5-pkg/config/patches/PPSA01234")

    def test_a_sibling_whose_name_starts_the_same_is_not_rewritten(self):
        for p in ("/output2/x.pkg", "/outputs", "/library-old/x", "/library.bak", "/workshop/x"):
            with self.subTest(p):
                self.assertIsNone(H.display_path(p, MAP))

    def test_the_longest_prefix_wins(self):
        self.assertEqual(H.display_path("/work/tmp/PPSA01234/app", MAP), "/srv/scratch/tmp/PPSA01234/app")
        self.assertEqual(H.display_path("/work/other", MAP), "/srv/scratch/other")          # the /work volume itself
        self.assertEqual(H.display_path("/work", MAP), "/srv/scratch")

    def test_trailing_slashes(self):
        self.assertEqual(H.display_path("/library/", MAP), "/srv/games/ps5")
        self.assertEqual(H.display_path("/library/Game/", MAP), "/srv/games/ps5/Game")
        self.assertEqual(H.display_path("/library", [("/library", "/srv/games/ps5/")]), "/srv/games/ps5/")   # host as configured
        self.assertEqual(H.display_path("/library/x", [("/library", "/srv/games/ps5/")]), "/srv/games/ps5/x")

    def test_non_ascii_file_names(self):
        self.assertEqual(H.display_path("/library/Pok\u00e9mon \u30b2\u30fc\u30e0 [PPSA01234] \U0001f3ae.exfat", MAP),
                         "/srv/games/ps5/Pok\u00e9mon \u30b2\u30fc\u30e0 [PPSA01234] \U0001f3ae.exfat")
        self.assertEqual(H.display_path("/output/\u00dcber \u2013 caf\u00e9 (v1).pkg", MAP), "/srv/games/ps5/pkg/\u00dcber \u2013 caf\u00e9 (v1).pkg")

    def test_not_a_path_at_all(self):
        for v in (None, "", 5, ["/library"]):
            self.assertIsNone(H.display_path(v, MAP))

    def test_a_windows_host_gets_its_own_separators(self):
        m = [("/library", "D:\\Games\\PS5"), ("/output", "\\\\server\\share\\pkg")]
        self.assertEqual(H.display_path("/library/Example/game.exfat", m), "D:\\Games\\PS5\\Example\\game.exfat")
        self.assertEqual(H.display_path("/output", m), "\\\\server\\share\\pkg")

    def test_a_partial_detected_hint_never_becomes_a_path(self):
        folders = H.resolve({"library": "/library", "downloads": "/downloads", "output": "/output", "temp": "/work/tmp",
                             "config": "/config"}, {}, {}, H.parse_mountinfo(
            "24 22 253:1 /@syno/share/games/ps5 /library ro - btrfs /dev/mapper/pool_1 rw\n"))
        self.assertEqual(folders["library"]["source"], "detected")
        self.assertEqual(H.path_map(folders, {}), [])
        self.assertIsNone(H.display_path("/library/Game.exfat", H.path_map(folders, {})))


class PathMap(unittest.TestCase):
    CONTAINERS = {"library": "/library", "downloads": "/downloads", "output": "/output", "temp": "/work/tmp", "config": "/config"}

    def test_only_configured_folders_count_and_the_volume_is_included(self):
        env, _ = H.read_env({"PS5PKG_HOST_LIBRARY": "/srv/games/ps5", "PS5PKG_HOST_WORK": "/srv/scratch"})
        folders = H.resolve(self.CONTAINERS, env, {"output": "/srv/pkg"}, [])
        pm = dict(H.path_map(folders, env))
        self.assertEqual(pm, {"/work/tmp": "/srv/scratch/tmp", "/work": "/srv/scratch", "/library": "/srv/games/ps5",
                              "/output": "/srv/pkg"})
        self.assertEqual(list(pm)[0], "/work/tmp")                                  # longest first

    def test_the_legacy_json_values_are_the_folders_exactly_as_shown(self):
        env, _ = H.read_env({"PS5PKG_HOST_PATHS": json.dumps({"temp": "/srv/scratch/tmp", "library": "/srv/games/ps5"})})
        pm = H.path_map(H.resolve(self.CONTAINERS, env, {}, []), env)
        self.assertEqual(dict(pm), {"/work/tmp": "/srv/scratch/tmp", "/library": "/srv/games/ps5"})


class DisplayText(unittest.TestCase):
    def test_log_lines(self):
        self.assertEqual(H.display_text("PREPARE Example Game  <- /library/Example Game_PPSA01234.exfat", MAP),
                         "PREPARE Example Game  <- /srv/games/ps5/Example Game_PPSA01234.exfat")
        self.assertEqual(H.display_text("  copied to /output/Example Game [PPSA01234].pkg (verified)", MAP),
                         "  copied to /srv/games/ps5/pkg/Example Game [PPSA01234].pkg (verified)")
        self.assertEqual(H.display_text("fpkg-validate failed (summary: bad); package left in /output/.staging/PPSA01234", MAP),
                         "fpkg-validate failed (summary: bad); package left in /srv/games/ps5/pkg/.staging/PPSA01234")

    def test_several_paths_in_one_line_and_every_kind_of_delimiter(self):
        got = H.display_text("'/library/a.exfat' -> (/output/a.pkg), /work/tmp/PPSA01234: \"/downloads/x.rar\" [/config]", MAP)
        self.assertEqual(got, "'/srv/games/ps5/a.exfat' -> (/srv/games/ps5/pkg/a.pkg), /srv/scratch/tmp/PPSA01234: "
                              "\"/srv/games/dl/x.rar\" [/srv/docker/ps5-pkg/config]")

    def test_whole_segments_only_and_not_inside_other_paths(self):
        for line in ("see /output2/x and /outputs/y", "in /mnt/output/x", "at /library-old/x", "file /library.bak",
                     "a/library/b", "x=/workshop/y"):
            with self.subTest(line):
                self.assertEqual(H.display_text(line, MAP), line)

    def test_a_sentence_ending_in_a_path_and_a_trailing_slash(self):
        self.assertEqual(H.display_text("left in /output.", MAP), "left in /srv/games/ps5/pkg.")
        self.assertEqual(H.display_text("scanning /library/", MAP), "scanning /srv/games/ps5/")

    def test_the_longest_prefix_wins_and_non_ascii_survives(self):
        self.assertEqual(H.display_text("rm /work/tmp/PPSA01234 and /work/x \u00e9\u30c6", MAP),
                         "rm /srv/scratch/tmp/PPSA01234 and /srv/scratch/x \u00e9\u30c6")

    def test_nothing_to_do(self):
        self.assertEqual(H.display_text("no paths here", MAP), "no paths here")
        self.assertEqual(H.display_text("/library/x", []), "/library/x")
        self.assertEqual(H.display_text("", MAP), "")


class ToContainer(unittest.TestCase):
    def test_a_host_path_that_maps_cleanly_becomes_the_container_path(self):
        self.assertEqual(H.to_container("/srv/docker/ps5-pkg/config/patches/PPSA01234", MAP), "/config/patches/PPSA01234")
        self.assertEqual(H.to_container("/srv/games/ps5/Game.exfat", MAP), "/library/Game.exfat")
        self.assertEqual(H.to_container("/srv/games/ps5/pkg/", MAP), "/output")
        self.assertEqual(H.to_container("/srv/scratch/tmp/PPSA01234", MAP), "/work/tmp/PPSA01234")

    def test_a_container_path_stays_as_it_is(self):
        self.assertEqual(H.to_container("/config/patches/PPSA01234", MAP), "/config/patches/PPSA01234")
        self.assertEqual(H.to_container("/library/Game.exfat", MAP), "/library/Game.exfat")

    def test_what_does_not_map_cleanly_is_left_alone(self):
        for p in ("/srv/games/ps5x/Game", "/srv/other/place", "relative/dir", "/srv/games", ""):
            with self.subTest(p):
                self.assertIsNone(H.to_container(p, MAP))

    def test_windows_host_paths_compare_without_case_and_separator_trouble(self):
        m = [("/library", "D:\\Games\\PS5")]
        self.assertEqual(H.to_container("d:\\games\\ps5\\Example\\game.exfat", m), "/library/Example/game.exfat")
        self.assertEqual(H.to_container("D:/Games/PS5", m), "/library")
        self.assertIsNone(H.to_container("D:\\Games\\PS5x\\a", m))


class FoldersFor(StateCase):
    def test_everything_from_the_environment_and_the_settings(self):
        cfg = {**E.DEFAULT_CFG, "library": "/library", "downloads": "/downloads", "output": "/output", "temp": "/work/tmp",
               "host_paths": {"downloads": "/srv/games/dl"}}
        env = {"PS5PKG_HOST_LIBRARY": "/srv/games/ps5", "PS5PKG_HOST_WORK": "/srv/scratch", "PS5PKG_HOST_CONFIG": "/srv/cfg"}
        v = H.folders_for(cfg, state=Path("/config/state"), logs=Path("/config/logs"), environ=env, mountinfo="/nonexistent")
        self.assertEqual(v["problems"], [])
        self.assertEqual(v["configured"], {"library": "/srv/games/ps5", "downloads": "/srv/games/dl", "temp": "/srv/scratch/tmp",
                                           "config": "/srv/cfg"})
        self.assertEqual(H.display_path("/config/patches/PPSA01234", v["mapping"]), "/srv/cfg/patches/PPSA01234")
        self.assertIsNone(H.display_path("/output/x.pkg", v["mapping"]))             # output has no host location


class NotificationText(StateCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.dir / "env")
        self.cfg = {**E.DEFAULT_CFG, **self.env}
        p = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if not k.startswith("PS5PKG_HOST_")}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_a_built_package_says_which_host_folder_it_is_in(self):
        os.environ["PS5PKG_HOST_OUTPUT"] = "/srv/games/ps5/pkg"
        out = str(Path(self.cfg["output"]) / "Example Game [PPSA01234] [v01.002.003].pkg")
        self.assertEqual(E.notification_text(self.cfg, "Example Game [PPSA01234] [v01.002.003].pkg", "built", None, out),
                         "Example Game [PPSA01234] [v01.002.003].pkg\nSaved in /srv/games/ps5/pkg")

    def test_a_reason_is_shown_with_host_paths(self):
        os.environ["PS5PKG_HOST_OUTPUT"] = "/srv/games/ps5/pkg"
        why = f"fpkg-validate failed (summary: bad); package left in {self.cfg['output']}/.staging/PPSA01234"
        self.assertEqual(E.notification_text(self.cfg, "Example Game.pkg", "failed", why),
                         "Example Game.pkg: fpkg-validate failed (summary: bad); package left in /srv/games/ps5/pkg/.staging/PPSA01234")

    def test_without_a_host_location_the_text_is_as_before_and_no_container_path_is_invented(self):
        out = str(Path(self.cfg["output"]) / "Example Game.pkg")
        self.assertEqual(E.notification_text(self.cfg, "Example Game.pkg", "built", None, out), "Example Game.pkg")
        self.assertEqual(E.notification_text(self.cfg, "Example Game.pkg", "failed", "UltraPack exit 1, 0 package(s)"),
                         "Example Game.pkg: UltraPack exit 1, 0 package(s)")

    def test_a_broken_environment_never_breaks_a_notification(self):
        os.environ["PS5PKG_HOST_PATHS"] = "{not json"
        self.assertEqual(E.notification_text(self.cfg, "Example Game.pkg", "built", None, "/output/x.pkg"), "Example Game.pkg")

    def test_what_is_posted_to_the_notification_url(self):
        os.environ["PS5PKG_HOST_OUTPUT"] = "/srv/games/ps5/pkg"
        got = {}

        class H_(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got["title"], got["body"] = self.headers["Title"], self.rfile.read(int(self.headers["Content-Length"])).decode()
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H_)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        cfg = {**self.cfg, "notify_url": f"http://127.0.0.1:{srv.server_address[1]}/topic"}
        out = str(Path(cfg["output"]) / "Example Game.pkg")
        E.notify(cfg, "PS5 pkg: built", E.notification_text(cfg, "Example Game.pkg", "built", None, out))
        self.assertEqual(got, {"title": "PS5 pkg: built", "body": "Example Game.pkg\nSaved in /srv/games/ps5/pkg"})

    def test_finish_build_sends_it(self):
        os.environ["PS5PKG_HOST_OUTPUT"] = "/srv/games/ps5/pkg"
        up = Path(self.cfg["ultrapack_src"]) / "backend" / "cli.py"
        up.write_text("print('summary: ok')\n")
        stage = Path(self.cfg["output"]) / ".staging" / "PPSA01234"
        stage.mkdir(parents=True)
        (stage / "game.pkg").write_bytes(b"x" * 64)
        Path(self.cfg["temp"]).mkdir(parents=True)
        job = dict(title="Example Game", title_id="PPSA01234", name="Example Game [PPSA01234] [v01.002.003].pkg",
                   source=str(Path(self.cfg["library"]) / "Example Game [PPSA01234]"), version="01.002.003")
        b = dict(job=job, prep=dict(clean=None, prep_min=1), stage=stage, logf=self.logs / "b.log", t0=time.time(),
                 proc=mock.Mock(wait=lambda: 0))
        sent = []
        with mock.patch.object(E, "notify", lambda cfg, title, msg: sent.append((title, msg))):
            res = E.finish_build(self.cfg, b, lambda m: None, self.state / "ledger.json", {}, threading.Lock())
        self.assertEqual(res["status"], "built", res)
        self.assertEqual(sent, [("PS5 pkg: built", "Example Game [PPSA01234] [v01.002.003].pkg\nSaved in /srv/games/ps5/pkg")])
        self.assertTrue((Path(self.cfg["output"]) / job["name"]).exists())


if __name__ == "__main__":
    unittest.main()
