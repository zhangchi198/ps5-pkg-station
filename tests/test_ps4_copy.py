"""PS4 titles: ready-made packages in an archive are copied as they are, never converted. A small synthetic archive
(a zip with the shape of the real RAR sets: a base game, a patch in a non-ASCII folder, a nested DLC.zip, a junk
.url) and a stand-in for 7-Zip. Standard library only."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from support import ENGINE_DIR, E, StateCase, install_fake_7zz, make_env, make_ps4_pkg, make_zip

import ps5_failures as F  # noqa: E402

TID = "CUSA12345"
BASE = make_ps4_pkg(f"UP9000-{TID}_00-EXAMPLEGAME00000", "Example Game", "01.00", "gd", body=b"B" * 5000)
PATCH = make_ps4_pkg(f"UP9000-{TID}_00-EXAMPLEGAME00000", "Example Game", "01.01", "gp", body=b"P" * 3000)
DLC_PKG = make_ps4_pkg(f"UP9000-{TID}_00-EXAMPLEDLC0000000", "Example Game Extra", "01.00", "ac", body=b"D" * 2000)
BASE_NAME = f"{TID}/Example Game-{TID}.v1.0[SiteTag].pkg"
PATCH_NAME = f"{TID}/日本語フォルダ/HP0177-{TID}_00-ZZZZZZZZZZZZZZZZ-A0101-V0100.pkg"
ARCHIVE_NAME = f"Example.Game.{TID}.part1.rar"                    # a zip inside: the stand-in reads zips whatever they are called


def dlc_zip(path, *, with_pkg):
    members = {"readme.txt": b"nothing to see"}
    if with_pkg:
        members["Extra DLC [SiteTag].pkg"] = DLC_PKG
    return make_zip(path, members)


class Ps4Case(StateCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.dir / "env", game=False)
        install_fake_7zz(self.env)
        self.cfg = {**copy.deepcopy(E.DEFAULT_CFG), **self.env, "junk_file_patterns": [r"SiteTag", r"SiteName"], "junk_title_patterns": [r"\[SiteTag\]"],
                    "console_fw": "9.60"}
        self.write_cfg(self.cfg)
        self.downloads = Path(self.env["downloads"])
        self.output = Path(self.env["output"])
        self.make_archive()

    def make_archive(self, *, dlc_pkg=False, members=None, **kw):
        dlc = self.dir / "DLC.zip"
        dlc_zip(dlc, with_pkg=dlc_pkg)
        top = self.downloads / "Example Game"
        top.mkdir(exist_ok=True)
        self.archive = top / ARCHIVE_NAME
        make_zip(self.archive, members or {BASE_NAME: BASE, PATCH_NAME: PATCH, f"{TID}/DLC.zip": dlc.read_bytes(), f"{TID}/SiteName.url": b"[InternetShortcut]\nURL=x\n"}, **kw)

    def scan(self, cfg=None, cache=None):
        E.set_junk(cfg or self.cfg)
        lines = []
        cands = E.discover(cfg or self.cfg, lines.append, cache if cache is not None else {})
        return cands, E.choose(cfg or self.cfg, cands, {}), lines

    def entry(self, cfg=None):
        return {p["title_id"]: p for p in self.scan(cfg)[1]}[TID]

    def run_engine(self, *args):
        return subprocess.run([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), *args], capture_output=True, text=True,
                              env=os.environ.copy(), timeout=180)


class Planning(Ps4Case):
    def test_a_cusa_title_in_an_archive_is_planned_as_copy_never_build(self):
        cands, plan, _ = self.scan()
        self.assertEqual([c["kind"] for c in cands], ["archive-ps4"])
        p = {x["title_id"]: x for x in plan}[TID]
        self.assertEqual((p["action"], p["why"], p["ps4"]), ("copy", "PS4: copy packages as they are", True))
        self.assertNotIn("build", [x["action"] for x in plan])
        self.assertEqual((p["title_id"], p["version"], p["kind"]), (TID, "01.01", "archive-ps4"))            # the newest package's version
        self.assertEqual(p["name"], f"Example Game [{TID}]")
        self.assertEqual(p["dest_dir"], str(self.output / "PS4" / f"Example Game [{TID}]"))

    def test_the_packages_are_named_and_classified_by_their_headers(self):
        files = {f["path"]: f for f in self.entry()["files"]}
        self.assertEqual(set(files), {BASE_NAME, PATCH_NAME})
        self.assertEqual((files[BASE_NAME]["kind"], files[BASE_NAME]["dest"], files[BASE_NAME]["version"]),
                         ("game", f"Example Game [{TID}] [v01.00].pkg", "01.00"))
        self.assertEqual((files[PATCH_NAME]["kind"], files[PATCH_NAME]["dest"], files[PATCH_NAME]["version"]),
                         ("patch", f"HP0177-{TID}_00-ZZZZZZZZZZZZZZZZ-A0101-V0100.pkg", "01.01"))
        self.assertEqual((files[BASE_NAME]["size"], files[PATCH_NAME]["size"]), (len(BASE), len(PATCH)))
        self.assertEqual(files[BASE_NAME]["content_id"], f"UP9000-{TID}_00-EXAMPLEGAME00000")

    def test_names_come_from_the_header_not_from_the_file_name_and_use_the_configured_junk_patterns(self):
        # a base game named after a site: the clean name comes from the header title, never from the file name
        self.make_archive(members={f"{TID}/Whatever [SiteTag] release.pkg": BASE})
        self.assertEqual([f["dest"] for f in self.entry()["files"]], [f"Example Game [{TID}] [v01.00].pkg"])

    def test_a_dlc_zip_with_no_pkg_in_it_copies_nothing_and_says_so(self):
        e = self.entry()
        self.assertEqual(len(e["files"]), 2)
        self.assertIn("DLC.zip: no .pkg inside, nothing is copied from it", e["notes"])
        self.assertFalse(any("readme" in f["name"] or f["name"].endswith(".url") for f in e["files"]))     # junk files are not copied

    def test_a_dlc_zip_with_pkgs_in_it_adds_them_with_cleaned_names(self):
        self.make_archive(dlc_pkg=True)
        e = self.entry()
        dlc = [f for f in e["files"] if f["kind"] == "dlc"]
        self.assertEqual([(f["dest"], f["via"], f["path"]) for f in dlc], [("Extra DLC.pkg", f"{TID}/DLC.zip", "Extra DLC [SiteTag].pkg")])
        self.assertEqual(e["notes"], [])

    def test_the_cleaning_uses_the_configured_patterns_and_nothing_hard_coded(self):
        self.make_archive(dlc_pkg=True)
        names = [f["dest"] for f in self.entry()["files"] if f["kind"] == "dlc"]
        self.assertEqual(names, ["Extra DLC.pkg"])
        cfg = {**self.cfg, "junk_file_patterns": [], "junk_title_patterns": []}                      # the owner took the patterns away
        self.assertEqual([f["dest"] for f in self.entry(cfg)["files"] if f["kind"] == "dlc"], ["Extra DLC [SiteTag].pkg"])

    def test_a_big_nested_archive_is_noted_and_opened_when_copying(self):
        self.make_archive(dlc_pkg=True)
        with mock.patch.object(E, "PS4_NESTED_MAX", 10):
            e = self.entry()
        self.assertEqual([f["kind"] for f in e["files"]], ["game", "patch", "nested archive"])
        self.assertTrue(any("DLC.zip" in n and "is opened when the packages are copied" in n for n in e["notes"]))

    def test_a_package_that_is_not_a_ps4_one_does_not_make_it_a_ps4_title(self):
        ps5 = b"\x7fFIH" + b"\0" * 0x200
        self.make_archive(members={f"PPSA01234/Game-PPSA01234.pkg": ps5})
        cands, plan, _ = self.scan()
        self.assertEqual(cands, [])                                                                      # left to the existing handling

    def test_a_header_that_cannot_be_read_falls_back_to_names_and_the_biggest_package_is_the_game(self):
        self.make_archive(members={f"{TID}/Example Game {TID}.pkg": b"too short to have a header" * 3, f"{TID}/Patch-{TID}-A0101-V0100.pkg": b"tiny"})
        e = self.entry()
        kinds = {f["path"]: f["kind"] for f in e["files"]}
        self.assertEqual(kinds, {f"{TID}/Example Game {TID}.pkg": "game", f"{TID}/Patch-{TID}-A0101-V0100.pkg": "patch"})
        self.assertTrue(any("could not be read" in n for n in e["notes"]))

    def test_patches_and_dlc_only_is_not_a_game(self):
        self.make_archive(members={PATCH_NAME: PATCH})
        self.assertEqual(self.scan()[0], [])


ASIAN = "\u7bc4\u4f8b\u904a\u6232"                               # a Traditional-Chinese TITLE
KOREAN = "\uc608\uc2dc \uac8c\uc784"


def asian_pkg(**extra):
    """The real shape: TITLE in Chinese, TITLE_09 in Korean, no English title."""
    return make_ps4_pkg(f"UP9000-{TID}_00-EXAMPLEGAME00000", ASIAN, "01.00", "gd", body=b"B" * 5000, extra={"TITLE_09": KOREAN, **extra})


class Titles(Ps4Case):
    """The PS5 side names games from the English title: so does the PS4 copy."""

    def make(self, pkg, member=BASE_NAME, folder="Example-Game"):
        top = self.downloads / folder
        top.mkdir(parents=True, exist_ok=True)
        for old in self.downloads.glob("*/*.part1.rar"):
            old.unlink()
        self.archive = top / ARCHIVE_NAME
        make_zip(self.archive, {member: pkg})

    def named(self, cfg=None):
        e = self.entry(cfg)
        return e["name"], [f["dest"] for f in e["files"] if f["kind"] == "game"][0], e

    def test_an_asian_release_is_named_after_the_english_title_in_the_file_name(self):
        self.make(asian_pkg())                                                      # member "Example Game-CUSA12345.v1.0[SiteTag].pkg"
        folder, game, e = self.named()
        self.assertEqual((folder, game), (f"Example Game [{TID}]", f"Example Game [{TID}] [v01.00].pkg"))
        self.assertEqual((e["title"], e["title_in_package"], e["title_source"]), ("Example Game", ASIAN, "file name"))
        self.assertTrue(e["dest_dir"].endswith(f"PS4/Example Game [{TID}]"))

    def test_the_title_in_the_package_stays_visible_in_the_plan(self):
        self.make(asian_pkg())
        e = self.entry()
        self.assertEqual(e["title_in_package"], ASIAN)
        self.assertNotIn("\u7bc4", e["name"])

    def test_an_english_title_in_the_package_wins_over_everything(self):
        self.make(asian_pkg(TITLE_01="Real English Title"))
        folder, game, e = self.named()
        self.assertEqual((folder, game, e["title_source"]), (f"Real English Title [{TID}]", f"Real English Title [{TID}] [v01.00].pkg", "package (English)"))
        self.assertEqual(e["title_in_package"], ASIAN)

    def test_title_18_when_there_is_no_title_01(self):
        self.make(asian_pkg(TITLE_18="British English Title"))
        self.assertEqual(self.named()[0], f"British English Title [{TID}]")
        self.make(asian_pkg(TITLE_18="British English Title", TITLE_01="American English Title"))
        self.assertEqual(self.named()[0], f"American English Title [{TID}]")

    def test_a_latin_script_title_is_kept_even_when_it_is_not_english(self):
        self.make(make_ps4_pkg(f"UP9000-{TID}_00-EXAMPLEGAME00000", "\u00c9cho des \u00c2mes", "01.00", "gd", extra={"TITLE_02": "x"}))
        folder, _, e = self.named()
        self.assertEqual((folder, e["title_source"]), (f"\u00c9cho des \u00c2mes [{TID}]", "package"))

    def test_without_a_title_id_next_to_a_title_in_the_file_name_the_source_folder_is_used(self):
        self.make(asian_pkg(), member=f"{TID}/release.pkg")
        folder, game, e = self.named()
        self.assertEqual((folder, e["title_source"]), (f"Example Game [{TID}]", "folder name"))       # "Example-Game" -> "Example Game"

    def test_the_package_title_is_the_last_resort(self):
        self.make(asian_pkg(), member=f"{TID}/release.pkg", folder=KOREAN)           # a folder that is not Latin script either
        folder, _, e = self.named()
        self.assertEqual((folder, e["title_source"]), (f"{ASIAN} [{TID}]", "package"))

    def test_the_folder_is_the_one_the_release_was_downloaded_into_however_deep_the_archive_is(self):
        self.make(asian_pkg(), member=f"{TID}/release.pkg", folder="Example-Game/CD1")
        folder, _, e = self.named()
        self.assertEqual((folder, e["title_source"]), (f"Example Game [{TID}]", "folder name"))

    def test_the_title_id_may_come_first_and_the_configured_junk_is_removed(self):
        self.make(asian_pkg(), member=f"{TID}/{TID} Example Game v1.0 [SiteTag].pkg")
        self.assertEqual(self.named()[0], f"Example Game [{TID}]")
        self.make(asian_pkg(), member=f"{TID}/Example_Game SiteTag-{TID}.v1.0.pkg")
        self.assertEqual(self.named()[0], f"Example Game [{TID}]")                          # junk removed, _ and - read as spaces
        cfg = {**self.cfg, "junk_file_patterns": [], "junk_title_patterns": []}
        self.assertEqual(self.named(cfg)[0], f"Example Game SiteTag [{TID}]")                # nothing is hard-coded

    def test_the_file_name_wins_only_when_it_is_next_to_the_title_id_and_latin(self):
        self.make(asian_pkg(), member=f"{TID}/{ASIAN}-{TID}.v1.0.pkg", folder="Fallback-Folder")
        self.assertEqual(self.named()[0], f"Fallback Folder [{TID}]")                        # a Chinese file name is no English title either

    def test_the_helpers(self):
        for text, want in [("Example Game", True), ("\u00c9cho des \u00c2mes", True), ("1942", True), ("", True), (ASIAN, False), (KOREAN, False),
                           ("Example \u7bc4\u4f8b\u904a\u6232 Game", True), ("\u7bc4\u4f8b\u904a\u6232 Ex", False)]:
            with self.subTest(text):
                self.assertEqual(E.mostly_latin(text), want)
        self.assertEqual(E.ps4_title_from_name(f"{TID}/Example Game-{TID}.v1.0[SiteTag].pkg", TID), "Example Game")
        self.assertEqual(E.ps4_title_from_name("whatever.pkg", TID), "")
        self.assertEqual(E.ps4_title_from_name(f"{TID}.pkg", TID), "")


class Modes(Ps4Case):
    def test_skip_mode_lists_the_title_as_skipped_and_plans_nothing(self):
        p = self.entry({**self.cfg, "ps4_mode": "skip"})
        self.assertEqual((p["action"], p["why"]), ("skip", "PS4: skipped (ps4_mode is skip)"))

    def test_an_existing_skip_override_always_wins(self):
        for mode in ("copy", "skip"):
            with self.subTest(mode):
                cfg = {**self.cfg, "ps4_mode": mode, "overrides": {TID: {"skip": True, "note": "PS4 game: waiting for the PS4 copy-as-is mode"}}}
                p = self.entry(cfg)
                self.assertEqual((p["action"], p["why"]), ("skip", "skipped by override"))
        self.write_cfg({**self.cfg, "overrides": {TID: {"skip": True}}})
        r = self.run_engine("run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.output / "PS4").exists())                                                 # nothing was copied

    def test_the_default_is_copy_and_old_config_files_have_it(self):
        self.assertEqual(E.DEFAULT_CFG["ps4_mode"], "copy")
        old = {k: v for k, v in self.cfg.items() if k != "ps4_mode"}
        self.write_cfg(old)
        self.assertEqual(E.load_cfg()["ps4_mode"], "copy")
        self.write_cfg({"ps4_mode": "convert"})
        info = E.load_cfg_info()
        self.assertEqual(info.cfg["ps4_mode"], "copy")
        self.assertIn("ps4_mode must be copy or skip", info.problems[0])
        self.assertEqual(E.validate_setting("ps4_mode", "skip"), "skip")

    def test_a_ps4_title_that_is_not_a_set_of_packages_never_reaches_the_conversion(self):
        cand = dict(title_id=TID, title="Example", version="", kind="archive-pkg", source=str(self.archive), size=5 << 30, packed="", fw="")
        p = E.choose(self.cfg, [cand], {})[0]
        self.assertEqual(p["action"], "skip")
        self.assertIn("PS4", p["why"])

    def test_a_ps4_game_folder_is_listed_as_not_handled(self):
        game = Path(self.env["library"]) / "Example Game"
        (game / "sce_sys").mkdir(parents=True)
        (game / "eboot.bin").write_bytes(b"\0" * 16)
        from support import make_sfo
        (game / "sce_sys" / "param.sfo").write_bytes(make_sfo(TITLE="Folder Game", TITLE_ID="CUSA54321", APP_VER="01.02", CATEGORY="gd"))
        ident = E.ident_image_or_folder(str(game))
        self.assertEqual((ident["kind"], ident["title_id"], ident["title"], ident["version"]), ("ps4-folder", "CUSA54321", "Folder Game", "01.02"))
        p = {x["title_id"]: x for x in self.scan()[1]}["CUSA54321"]
        self.assertEqual((p["action"], p["why"]), ("skip", "PS4 folder: not handled (only PS4 package sets are copied)"))


class Copying(Ps4Case):
    def run_copy(self, **kw):
        e = self.entry()
        ledger = {}
        notes = []
        with mock.patch.object(E, "notify", lambda cfg, t, m: notes.append((t, m))):
            res = E.run_ps4(self.cfg, [e], lambda m: None, self.state / "ledger.json", ledger)
        return e, res, ledger, notes

    def test_the_packages_are_copied_byte_for_byte_into_the_title_folder(self):
        e, res, ledger, notes = self.run_copy()
        dest = self.output / "PS4" / f"Example Game [{TID}]"
        self.assertEqual((res[0]["status"], res[0]["title_id"]), ("copied", TID), res)
        self.assertEqual(sorted(p.name for p in dest.iterdir()), sorted([f"Example Game [{TID}] [v01.00].pkg", f"HP0177-{TID}_00-ZZZZZZZZZZZZZZZZ-A0101-V0100.pkg"]))
        self.assertEqual((dest / f"Example Game [{TID}] [v01.00].pkg").read_bytes(), BASE)             # no byte changed
        self.assertEqual((dest / f"HP0177-{TID}_00-ZZZZZZZZZZZZZZZZ-A0101-V0100.pkg").read_bytes(), PATCH)
        self.assertEqual([p.name for p in self.output.iterdir()], ["PS4"])                              # staging is gone
        self.assertEqual(len(notes), 1)

    def test_a_dlc_pkg_is_copied_and_a_dlc_zip_without_one_leaves_nothing(self):
        self.make_archive(dlc_pkg=True)
        _, res, _, _ = self.run_copy()
        dest = self.output / "PS4" / f"Example Game [{TID}]"
        self.assertEqual(res[0]["status"], "copied", res)
        self.assertEqual((dest / "Extra DLC.pkg").read_bytes(), DLC_PKG)
        self.assertFalse(list(dest.glob("*.zip")) or list(dest.glob("*.url")) or list(dest.glob("*.txt")))

    def test_a_big_nested_archive_is_opened_while_copying(self):
        self.make_archive(dlc_pkg=True)
        with mock.patch.object(E, "PS4_NESTED_MAX", 10):
            e = self.entry()
        with mock.patch.object(E, "notify", lambda *a: None):
            res = E.run_ps4(self.cfg, [e], lambda m: None, self.state / "ledger.json", {})
        dest = self.output / "PS4" / f"Example Game [{TID}]"
        self.assertEqual(res[0]["status"], "copied", res)
        self.assertEqual((dest / "Extra DLC.pkg").read_bytes(), DLC_PKG)

    def test_the_title_is_done_afterwards_including_in_a_new_process(self):
        e, res, ledger, _ = self.run_copy()
        entry = ledger[TID]
        self.assertEqual((entry["ps4"], entry["kind"], entry["version"], entry["output"]), (True, "ps4", "01.01", str(self.output / "PS4" / f"Example Game [{TID}]")))
        self.assertEqual(entry["files"], [f["dest"] for f in e["files"]])
        self.assertEqual(entry["bytes"], len(BASE) + len(PATCH))
        self.assertIn("CRC verified by 7-Zip: 2 package(s)", entry["validate"])
        self.assertEqual(json.loads((self.state / "ledger.json").read_text())[TID]["ps4"], True)
        p = {x["title_id"]: x for x in E.choose(self.cfg, self.scan()[0], ledger)}[TID]
        self.assertEqual((p["action"], p["why"].startswith("built ")), ("done", True))
        # a newer package set is copied again
        newer = make_ps4_pkg(f"UP9000-{TID}_00-EXAMPLEGAME00000", "Example Game", "01.04", "gp")
        self.make_archive(members={BASE_NAME: BASE, f"{TID}/Patch.pkg": newer})
        p = {x["title_id"]: x for x in E.choose(self.cfg, self.scan()[0], ledger)}[TID]
        self.assertEqual((p["action"], p["version"]), ("copy", "01.04"))

    def test_a_crc_error_in_the_archive_copies_nothing_and_is_a_failure(self):
        self.make_archive(compression=0)
        data = bytearray(self.archive.read_bytes())
        at = bytes(data).find(b"B" * 100)                                                              # inside the stored base game
        data[at + 10] ^= 0xFF
        self.archive.write_bytes(bytes(data))
        e, res, ledger, notes = self.run_copy()
        self.assertEqual((res[0]["status"], res[0]["code"], res[0]["phase"]), ("failed", "extract_failed", "copy"))
        self.assertIn("7-Zip failed on", res[0]["why"])
        self.assertFalse((self.output / "PS4").exists())                                               # no half-copied files
        self.assertEqual([p.name for p in self.output.iterdir()], [])                                  # staging cleaned up
        self.assertEqual(ledger, {})                                                                    # not done
        rec = F.Store(self.state / "failures.json").load()[TID]
        self.assertEqual((rec["code"], rec["phase"], rec["state"]), ("extract_failed", "copy", "failed"))
        self.assertEqual(len(notes), 1)
        self.assertEqual({x["title_id"]: x for x in E.choose(self.cfg, self.scan()[0], {}, F.Store(self.state / "failures.json").load())}[TID]["action"], "failed")

    def test_a_package_of_the_wrong_size_is_not_trusted(self):
        e = self.entry()
        e["files"][0]["size"] += 1
        with mock.patch.object(E, "notify", lambda *a: None):
            res = E.run_ps4(self.cfg, [e], lambda m: None, self.state / "ledger.json", {})
        self.assertEqual((res[0]["status"], res[0]["code"]), ("failed", "extract_failed"))
        self.assertFalse((self.output / "PS4").exists())

    def test_not_enough_room_in_the_output_is_waiting_not_failed(self):
        e = self.entry()
        with mock.patch.object(E.shutil, "disk_usage", return_value=mock.Mock(free=1000)), mock.patch.object(E, "notify") as notify:
            res = E.run_ps4(self.cfg, [e], lambda m: None, self.state / "ledger.json", {})
        self.assertEqual((res[0]["status"], res[0]["code"]), ("deferred", "output_no_space"))
        notify.assert_not_called()
        rec = F.Store(self.state / "failures.json").load()[TID]
        self.assertEqual((rec["state"], rec["code"]), ("waiting", "output_no_space"))
        self.assertIn("in the output folder", F.explain(rec)["summary"])

    def test_a_copy_over_an_existing_folder_keeps_what_it_replaces(self):
        _, res, _, _ = self.run_copy()
        dest = self.output / "PS4" / f"Example Game [{TID}]"
        (dest / "mine.txt").write_text("my own file")
        self.run_copy()
        names = sorted(p.name for p in dest.iterdir())
        self.assertIn("mine.txt", names)                                                                # nothing of the user's is touched
        self.assertEqual(len([n for n in names if "(replaced" in n]), 2)
        self.assertEqual((dest / f"Example Game [{TID}] [v01.00].pkg").read_bytes(), BASE)

    def test_the_notification_says_copied_and_where(self):
        os.environ["PS5PKG_HOST_OUTPUT"] = "/srv/games/ps5/pkg"
        try:
            e, res, ledger, notes = self.run_copy()
        finally:
            del os.environ["PS5PKG_HOST_OUTPUT"]
        self.assertEqual(notes, [("PS4: copied", f"Example Game [{TID}]\nSaved in /srv/games/ps5/pkg/PS4/Example Game [{TID}]")])

    def test_the_stop_request_is_honoured_between_titles(self):
        e = self.entry()
        (self.state / "stop.request").write_text("x")
        with mock.patch.object(E, "notify", lambda *a: None):
            res = E.run_ps4(self.cfg, [e, dict(e)], lambda m: None, self.state / "ledger.json", {})
        self.assertEqual(res, [])


class WholeRun(Ps4Case):
    def test_a_run_copies_the_title_and_the_next_one_has_nothing_to_do_even_after_a_restart(self):
        r = self.run_engine("run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("=== PS4: 1 title(s) to copy as they are", r.stdout)
        self.assertNotIn("PREPARE", r.stdout)
        self.assertNotIn("BUILD", r.stdout)                                                              # UltraPack is never started
        dest = self.output / "PS4" / f"Example Game [{TID}]"
        self.assertEqual((dest / f"Example Game [{TID}] [v01.00].pkg").read_bytes(), BASE)
        st = json.loads((self.state / "status.json").read_text())
        self.assertEqual([(x["title_id"], x["status"]) for x in st["results"]], [(TID, "copied")])
        self.assertIn("=== summary: built 0, copied 1, failed 0, deferred 0", r.stdout)         # a copy is not an UltraPack build
        plan = json.loads((self.state / "plan.json").read_text())[0]
        self.assertEqual(plan["action"], "copy")                                                         # the plan made at the start of the run
        r2 = self.run_engine("run")                                                                      # a new process: nothing in memory
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertNotIn("PS4 copy", r2.stdout)
        self.assertIn("=== run: 0 build(s)", r2.stdout)
        self.assertEqual(json.loads((self.state / "plan.json").read_text())[0]["action"], "done")
        self.assertEqual(sorted(p.name for p in dest.iterdir()).count("(replaced"), 0)

    def test_an_old_ident_cache_is_dropped_once_so_a_title_cached_as_a_conversion_candidate_is_seen_again(self):
        stale = {f"{self.archive}|{self.archive.stat().st_size}|{int(self.archive.stat().st_mtime)}": dict(
            title="Example", title_id=TID, version="", fw="", packed="", size=5 << 30, inner="x.pkg", kind="archive-pkg")}
        (self.state / "ident_cache.json").write_text(json.dumps(stale))
        self.assertEqual(self.run_engine("plan").returncode, 0)
        plan = json.loads((self.state / "plan.json").read_text())
        self.assertEqual((plan[0]["action"], plan[0]["kind"]), ("copy", "archive-ps4"))
        self.assertEqual(json.loads((self.state / "ident_cache.json").read_text())["__version__"], E.IDENT_VERSION)

    def test_the_only_option_retries_a_failed_ps4_title_and_a_plain_run_skips_it(self):
        self.assertEqual(self.run_engine("plan").returncode, 0)
        fp = json.loads((self.state / "plan.json").read_text())[0]["fingerprint"]
        F.Store(self.state / "failures.json").record(TID, code="extract_failed", phase="copy", message="m", fingerprint=fp, title="Example Game")
        r = self.run_engine("run")
        self.assertIn("skipped: failed on", r.stdout)
        self.assertNotIn("PS4 copy", r.stdout)
        r = self.run_engine("run", "--only", TID)
        self.assertIn("PS4 copy", r.stdout)
        self.assertTrue((self.output / "PS4" / f"Example Game [{TID}]").is_dir())
        self.assertEqual(F.Store(self.state / "failures.json").load(), {})                               # the copy worked: forgotten


if __name__ == "__main__":
    unittest.main()
