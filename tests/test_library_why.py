"""The plan explains itself: a verdict and a reason for every copy found, the backport question and who decides it,
titles for games that sit in an archive, the sources that could not be identified, and what a build did about a backport.
Standard library only."""
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

from support import ENGINE_DIR, GAME_PARAM, E, StateCase, make_env

import ps5_failures as F  # noqa: E402
from test_run_status import FAKE_ULTRAPACK  # noqa: E402

TID = "PPSA01234"
GIB = 2 ** 30


def cand(source, kind, version, fw="", packed="", size=100, title="Example Game", **kw):
    return dict(title_id=TID, title=title, version=version, fw=fw, kind=kind, source=source, size=size, packed=packed, origin="library", **kw)


class PlanCase(StateCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.dir / "env", game=False)
        self.cfg = {**copy.deepcopy(E.DEFAULT_CFG), **self.env, "console_fw": "10.01"}

    def plan(self, cands, ledger=None, titles=None, **cfg):
        self.cfg.update(cfg)
        return {p["title_id"]: p for p in E.choose(self.cfg, cands, ledger or {}, titles=titles)}[TID]

    @staticmethod
    def verdicts(p):
        return {c["source"]: c["verdict"] for c in p["candidates"]}


class Verdicts(PlanCase):
    IMG, RAR, OLD = "/library/Example.exfat", "/downloads/Example/PPSA01234.part01.rar", "/library/Example old.ffpfsc"

    def three(self, **cfg):
        return self.plan([cand(self.IMG, "exfat", "01.004.000", "7.00"), cand(self.RAR, "rar", "01.004.000", "9.60"), cand(self.OLD, "ffpfsc", "01.002.000", "7.00")], **cfg)

    def test_every_copy_gets_a_verdict_and_a_reason_with_its_full_path(self):
        p = self.three()
        self.assertEqual([c["source"] for c in p["candidates"]], [self.IMG, self.RAR, self.OLD])                # as found, full paths
        self.assertEqual(self.verdicts(p), {self.IMG: "chosen", self.RAR: "verifies", self.OLD: "older"})
        by = {c["source"]: c for c in p["candidates"]}
        self.assertIn("newest version (v01.004.000)", by[self.IMG]["reason"])
        self.assertIn("read faster than the other copies", by[self.IMG]["reason"])
        self.assertIn("checked against PPSA01234.part01.rar", by[self.IMG]["reason"])
        self.assertIn("checked file by file against this archive", by[self.RAR]["reason"])
        self.assertEqual(by[self.OLD]["reason"], "older version (v01.002.000; the newest is v01.004.000)")
        self.assertEqual((by[self.IMG]["kind_label"], by[self.RAR]["kind_label"], by[self.OLD]["kind_label"]), ("image (.exfat)", "archive (.rar)", "image (.ffpfsc)"))
        self.assertEqual((by[self.IMG]["fw"], by[self.RAR]["fw"]), ("7.00", "9.60"))
        self.assertEqual((p["source"], p["verify_archive"]), (self.IMG, self.RAR))

    def test_a_game_with_one_copy_says_so(self):
        p = self.plan([cand(self.IMG, "exfat", "01.00")])
        self.assertEqual(p["candidates"][0]["reason"], "the only copy found")

    def test_equal_copies_say_which_is_used_and_why(self):
        p = self.plan([cand("/a/x.exfat", "exfat", "01.00"), cand("/b/x.exfat", "exfat", "01.00"), cand("/c/folder", "folder", "01.00")])
        by = {c["source"]: c for c in p["candidates"]}
        self.assertEqual(self.verdicts(p), {"/a/x.exfat": "chosen", "/b/x.exfat": "same_version", "/c/folder": "same_version"})
        self.assertEqual(by["/b/x.exfat"]["reason"], "same version; the first one found is used")
        self.assertEqual(by["/c/folder"]["reason"], "same version; image (.exfat) is read faster than unpacked folder")
        self.assertEqual(by["/a/x.exfat"]["reason"], "image (.exfat) is read faster than the other copies of that version")
        p = self.plan([cand("/a/x.exfat", "exfat", "01.00"), cand("/b/x.exfat", "exfat", "01.00")])
        self.assertEqual(p["candidates"][0]["reason"], "the first of equal copies")

    def test_your_choice_is_the_reason_and_the_others_were_not_used(self):
        self.cfg["overrides"] = {TID: {"source": self.OLD}}
        p = self.three()
        self.assertEqual(self.verdicts(p), {self.IMG: "not_used", self.RAR: "not_used", self.OLD: "chosen"})
        by = {c["source"]: c for c in p["candidates"]}
        self.assertEqual(by[self.OLD]["reason"], "chosen by you (Copy to use)")
        self.assertEqual(by[self.IMG]["reason"], "not used: you chose another copy")

    def test_a_packed_copy_is_passed_over_and_says_why(self):
        p = self.plan([cand("/p/packed.exfat", "exfat", "01.005.000", packed="LZ4"), cand("/p/plain.exfat", "exfat", "01.004.000")])
        by = {c["source"]: c for c in p["candidates"]}
        self.assertEqual(self.verdicts(p), {"/p/packed.exfat": "packed", "/p/plain.exfat": "chosen"})
        self.assertIn("LZ4-packed: UltraPack builds from unpacked files", by["/p/packed.exfat"]["reason"])
        self.assertIn("the packed copies are passed over", by["/p/plain.exfat"]["reason"])
        self.assertEqual((p["action"], by["/p/packed.exfat"]["packed"]), ("build", "LZ4"))

    def test_only_packed_copies_is_a_skip_with_an_explanation_and_a_help_marker(self):
        p = self.plan([cand("/p/a.exfat", "exfat", "01.00", packed="LZ4")])
        self.assertEqual((p["action"], p["help"]), ("skip", "packed"))
        self.assertIn("only LZ4-packed copies", p["why"])
        self.assertIn("unpacked", p["why"])
        self.assertEqual(p["candidates"][0]["reason"], "the only copy found")
        p2 = self.plan([cand("/p/a.exfat", "exfat", "01.00", packed="LZ4"), cand("/p/b.exfat", "exfat", "01.00", packed="LZ4")])
        self.assertEqual((p2["action"], p2["candidates"][0]["reason"]), ("skip", "every copy is LZ4-packed"))
        self.assertNotIn("help", self.plan([cand("/p/a.exfat", "exfat", "01.00")]))

    def test_the_old_fields_are_still_there(self):
        p = self.three()
        self.assertEqual([k in p["candidates"][0] for k in ("source", "kind", "version", "packed", "size")], [True] * 5)
        self.assertEqual(p["skipped_duplicates"], [f"{self.RAR} (v01.004.000)", f"{self.OLD} (v01.002.000)"])


class Backport(PlanCase):
    def plan_with(self, console="10.01", image_fw="7.00", original_fw="9.60", **kw):
        return self.plan([cand("/l/x.exfat", "exfat", "01.00", image_fw), cand("/d/x.rar", "rar", "01.00", original_fw)], console_fw=console, **kw)

    def test_the_two_copies_ask_for_different_firmware(self):
        b = self.plan_with()["backport"]
        self.assertEqual((b["present"], b["confirmed"], b["image_fw"], b["original_fw"], b["console_fw"]), (True, False, "7.00", "9.60", "10.01"))
        self.assertEqual((b["decision"], b["decided_by"]), ("original", "your console's firmware"))                  # the original runs on 10.01

    def test_the_decision_follows_the_console_and_the_options(self):
        self.assertEqual(self.plan_with(console="8.00")["backport"]["decision"], "image")                           # the original needs newer firmware
        b = self.plan_with(console="6.00")["backport"]
        self.assertEqual((b["decision"], b["decided_by"]), (None, "neither copy is known to run on the console"))
        b = self.plan_with(console="")["backport"]
        self.assertEqual((b["decision"], b["decided_by"]), (None, "no console firmware is set (Settings)"))
        self.cfg["overrides"] = {TID: {"modified_exec": "image"}}
        self.assertEqual(self.plan_with()["backport"]["decision"], "image")
        self.cfg["overrides"] = {TID: {"modified_exec": "original"}}
        b = self.plan_with(console="8.00")["backport"]
        self.assertEqual((b["decision"], b["decided_by"]), ("original", "your option"))

    def test_the_same_firmware_is_not_a_backport_but_the_image_is_still_checked(self):
        b = self.plan_with(image_fw="9.60", original_fw="9.60")["backport"]
        self.assertFalse(b["present"])

    def test_when_both_copies_ask_for_the_same_firmware_there_is_nothing_to_choose_and_it_says_so_calmly(self):
        for console in ("13.20", "10.01", "6.00", ""):
            b = self.plan_with(console=console, image_fw="13.60", original_fw="13.60")["backport"]
            self.assertEqual((b["present"], b["decision"]), (False, None), console)
            self.assertEqual(b["decided_by"], "both copies ask for the same firmware: nothing to choose")
            self.assertNotIn("neither", b["decided_by"])
        b = self.plan_with(image_fw="9.60", original_fw="7.00")["backport"]                                        # an image that asks for MORE: no backport either
        self.assertEqual((b["present"], b["decision"], b["decided_by"]), (False, None, "the image does not ask for lower firmware than the original: nothing to choose"))
        b = self.plan_with(image_fw="", original_fw="9.60")["backport"]
        self.assertEqual((b["present"], b["decision"]), (False, None))
        self.assertIn("not known", b["decided_by"])

    def test_your_own_choice_still_shows_when_there_is_no_backport(self):
        self.cfg["overrides"] = {TID: {"modified_exec": "original"}}
        b = self.plan_with(image_fw="13.60", original_fw="13.60")["backport"]
        self.assertEqual((b["present"], b["decision"], b["decided_by"]), (False, "original", "your option"))

    def test_the_live_shape_equal_firmware_twin_a_console_in_between_and_a_patch_that_lowers_it(self):
        patch_dir = self.dir / "patches" / TID
        (patch_dir / "sce_sys").mkdir(parents=True)
        (patch_dir / "sce_sys" / "param.json").write_text(json.dumps({"titleId": TID, "contentVersion": "01.00", "requiredSystemSoftwareVersion": "0x1200000000000000",
                                                                      "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Example Game"}}}))
        self.cfg["overrides"] = {TID: {"patch": str(patch_dir)}}
        p = self.plan_with(console="13.20", image_fw="13.60", original_fw="13.60")
        self.assertEqual((p["fw"], p["patched"], p["source_fw"]), ("12.00", True, "13.60"))                         # the package will say 12.00
        self.assertEqual((p["backport"]["present"], p["backport"]["decision"]), (False, None))
        self.assertNotIn("neither", json.dumps(p))

    def test_no_pair_no_backport(self):
        self.assertIsNone(self.plan([cand("/l/x.exfat", "exfat", "01.00", "7.00")])["backport"])
        self.assertIsNone(self.plan([cand("/d/x.rar", "rar", "01.00", "9.60"), cand("/d/x2.rar", "rar", "01.00", "9.60")])["backport"])
        self.assertIsNone(self.plan([cand("/l/x.exfat", "exfat", "01.00", "7.00"), cand("/d/x.rar", "rar", "01.01", "9.60")])["backport"])      # not the same version

    def test_the_decision_rules(self):
        for image, original, console, override, want in [
                ("7.00", "9.60", "10.01", None, ("original", "your console's firmware")), ("7.00", "9.60", "9.60", None, ("original", "your console's firmware")),
                ("7.00", "9.60", "8.00", None, ("image", "your console's firmware")), ("7.00", "9.60", "7.00", None, ("image", "your console's firmware")),
                ("7.00", "9.60", "6.00", None, (None, "neither copy is known to run on the console")), ("7.00", "9.60", "", None, (None, "no console firmware is set (Settings)")),
                ("7.00", "9.60", "10.01", "image", ("image", "your option")), ("7.00", "9.60", "", "original", ("original", "your option")),
                ("", "9.60", "8.00", None, (None, "neither copy is known to run on the console")),                          # an image of unknown firmware is not assumed to run
                ("7.00", "9.60", "8.00", "original", ("original", "your option"))]:                                         # your option beats what the console would pick
            self.assertEqual(E.backport_decision(image, original, console, override), want, (image, original, console, override))


class Titles(PlanCase):
    WRAPPED = [cand("/d/PPSA01234-e/x.rar", "archive-image", "", title="PPSA01234-e")]

    def test_a_wrapped_image_has_only_its_folder_name_until_something_better_is_known(self):
        p = self.plan(self.WRAPPED)
        self.assertEqual((p["title"], p["name"]), ("PPSA01234-e", f"PPSA01234-e [{TID}] (version read after extraction)"))

    def test_a_title_learned_from_an_earlier_extraction_is_used(self):
        p = self.plan(self.WRAPPED, titles={TID: "Real Title"})
        self.assertEqual((p["title"], p["name"]), ("Real Title", f"Real Title [{TID}] (version read after extraction)"))

    def test_so_is_the_name_of_what_was_built_from_it(self):
        out = Path(self.cfg["output"]) / f"Ledger Title [{TID}] [v01.00].pkg"
        out.write_bytes(b"x")
        p = self.plan(self.WRAPPED, ledger={TID: dict(version="01.00", output=str(out), built="2026-01-01 00:00")})
        self.assertEqual(p["title"], "Ledger Title")
        p = self.plan(self.WRAPPED, ledger={TID: dict(version="01.00", output="/gone/odd name.pkg")}, titles={TID: "Remembered"})
        self.assertEqual(p["title"], "Remembered")                                                                   # learned title first, then the ledger
        self.assertEqual(self.plan(self.WRAPPED, ledger={TID: dict(output="/gone/odd name.pkg")})["title"], "PPSA01234-e")

    def test_a_loose_copy_with_a_real_title_names_the_wrapped_one(self):
        # a wrapped copy that wins (newer) takes the title of a loose copy of the same game
        wrapped = cand("/d/x.rar", "archive-image", "01.09.000", title="PPSA01234-e")
        loose = cand("/l/x.exfat", "exfat", "01.00", title="Example Game")
        self.assertEqual(self.plan([loose, wrapped])["title"], "Example Game")

    def test_titles_are_kept_in_a_file_and_read_back(self):
        self.assertEqual(E.load_titles(), {})
        E.remember_title(TID, "Example Game")
        E.remember_title("PPSA05678", "Other")
        E.remember_title(TID, "")                                                                                    # nothing to remember
        self.assertEqual(E.load_titles(), {TID: "Example Game", "PPSA05678": "Other"})
        (self.state / "titles.json").write_text("[1, 2]")
        self.assertEqual(E.load_titles(), {})
        (self.state / "titles.json").write_text(json.dumps({TID: 5, "x": "ok"}))
        self.assertEqual(E.load_titles(), {"x": "ok"})

    def test_a_game_identified_after_extraction_is_remembered(self):
        game = Path(self.env["library"]) / "folder"
        (game / "sce_sys").mkdir(parents=True)
        (game / "eboot.bin").write_bytes(b"\0" * 16)
        (game / "sce_sys" / "param.json").write_text(json.dumps(GAME_PARAM))
        self.cfg.update(ssd_reserve_gb=0)
        job = dict(title_id=TID, title="PPSA01234-e", version="", fw="", name="PPSA01234-e [PPSA01234] (version read after extraction)", kind="folder",
                   source=str(game), size=100, verify_archive=None)
        E.prepare(self.cfg, job, lambda m: None)
        self.assertEqual(job["title"], "Example Game")
        self.assertEqual(E.load_titles(), {TID: "Example Game"})


class NotRecognised(PlanCase):
    def test_the_list_has_the_sources_that_failed_and_why(self):
        got = E.unrecognised([dict(source="/l/b", origin="library", error="ValueError: param.json has no valid title ID"), dict(source="/l/ok", title_id=TID, origin="library"),
                              dict(source="/d/a.rar", origin="download", error="BadRarFile: broken"), dict(source="/d/noid", origin="download", title_id="")])
        self.assertEqual([(x["source"], x["origin"]) for x in got], [("/d/a.rar", "download"), ("/d/noid", "download"), ("/l/b", "library")])
        self.assertEqual(got[0]["error"], "BadRarFile: broken")
        self.assertEqual(got[1]["error"], "no title ID found")
        self.assertEqual(E.unrecognised([]), [])
        self.assertEqual(len(E.unrecognised([dict(source="/x", error="e" * 1000)])[0]["error"]), 300)

    def test_a_real_scan_writes_it_next_to_the_plan(self):
        good = Path(self.env["library"]) / f"Good [{TID}]"
        (good / "sce_sys").mkdir(parents=True)
        (good / "eboot.bin").write_bytes(b"\0" * 16)
        (good / "sce_sys" / "param.json").write_text(json.dumps(GAME_PARAM))
        bad = Path(self.env["library"]) / "Broken"
        (bad / "sce_sys").mkdir(parents=True)
        (bad / "eboot.bin").write_bytes(b"\0" * 16)
        (bad / "sce_sys" / "param.json").write_text("{this is not json")
        self.write_cfg({**self.cfg})
        r = subprocess.run([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), "plan"], capture_output=True, text=True, env=os.environ.copy(), timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        plan = json.loads((self.state / "plan.json").read_text())
        self.assertEqual([p["title_id"] for p in plan], [TID])
        un = json.loads((self.state / "unrecognised.json").read_text())
        self.assertEqual([(x["source"], x["origin"]) for x in un], [(str(bad), "library")])
        self.assertIn("JSONDecodeError", un[0]["error"])


class Outcome(PlanCase):
    """What the build decided about a backport is kept with the build."""

    def fake_image(self, crcs_differ):
        files = {"eboot.bin": b"ELF-modified", "data.bin": b"d"}

        class Ent:
            def __init__(self, data):
                self.size, self.data = len(data), data

        class FS:
            def __init__(self):
                self.files = {k: Ent(v) for k, v in files.items()}

            def read(self, e, off, n):
                return e.data[off:off + n]
        import zlib
        want = {k: zlib.crc32(v) for k, v in files.items()}
        if crcs_differ:
            want["eboot.bin"] ^= 1
        return mock.patch.object(E.A, "open_game_fs", lambda path: (mock.Mock(root="", fs=FS()), None)), want

    def resolve(self, differ, console="10.01", override=None):
        patcher, want = self.fake_image(differ)
        self.cfg.update(console_fw=console, overrides={TID: {"modified_exec": override}} if override else {})
        job = dict(title_id=TID, title="Example Game", version="01.00", source="/l/x.exfat", verify_archive="/d/x.rar", verify_root="", fw="7.00", name="x")
        with patcher, mock.patch.object(E, "archive_param", return_value=dict(fw="9.60")):
            try:
                E.resolve_modified_exec(self.cfg, job, want, lambda m: None)
            except F.GameError:
                job["raised"] = True
        return job

    def test_identical_files_leave_a_note_that_nothing_differed(self):
        self.assertEqual(self.resolve(False)["backport_outcome"], dict(differs=False, used="image", image_fw="7.00"))

    def test_a_console_that_runs_the_original_uses_it(self):
        o = self.resolve(True)["backport_outcome"]
        self.assertEqual((o["differs"], o["used"], o["decided_by"], o["files"], o["image_fw"], o["original_fw"], o["console_fw"]),
                         (True, "original", "your console's firmware", 1, "7.00", "9.60", "10.01"))
        self.assertEqual(o["examples"], ["eboot.bin"])

    def test_a_console_that_needs_the_backport_keeps_the_image(self):
        o = self.resolve(True, console="8.00")["backport_outcome"]
        self.assertEqual((o["used"], o["decided_by"]), ("image", "your console's firmware"))

    def test_your_option_wins_and_says_so(self):
        self.assertEqual(self.resolve(True, override="image")["backport_outcome"]["decided_by"], "your option")
        o = self.resolve(True, console="8.00", override="original")["backport_outcome"]
        self.assertEqual((o["used"], o["decided_by"]), ("original", "your option"))

    def test_when_neither_is_known_to_run_the_build_fails_as_before_and_no_outcome_is_kept(self):
        job = self.resolve(True, console="")
        self.assertTrue(job.get("raised"))
        self.assertNotIn("backport_outcome", job)


@unittest.skipUnless(shutil.which("nice"), "needs nice(1)")
class KeptWithTheBuild(PlanCase):
    def test_the_ledger_and_the_result_carry_the_outcome(self):
        (Path(self.env["ultrapack_src"]) / "backend" / "cli.py").write_text(FAKE_ULTRAPACK)
        game = Path(self.env["library"]) / f"Example Game [{TID}]"
        (game / "sce_sys").mkdir(parents=True)
        (game / "eboot.bin").write_bytes(b"\0" * 16)
        (game / "sce_sys" / "param.json").write_text(json.dumps(GAME_PARAM))
        self.cfg.update(ssd_reserve_gb=0, cpu_count=1)
        job = self.plan([cand(str(game), "folder", "01.002.003", "9.00")])
        outcome = dict(differs=True, used="original", files=3, examples=["eboot.bin"], image_fw="7.00", original_fw="9.60", console_fw="10.01", decided_by="your console's firmware")
        job["backport_outcome"] = outcome
        with mock.patch.dict(os.environ, {"FAKE_NAP": "0.01", "PS5PKG_STATUS_INTERVAL": "0.2"}), mock.patch.object(E, "notify", lambda *a: None):
            results = E.run_queue(self.cfg, [job], lambda m: None, self.state / "ledger.json", {})
        self.assertEqual(results[0]["status"], "built")
        self.assertEqual(results[0]["backport"], outcome)
        self.assertEqual(json.loads((self.state / "ledger.json").read_text())[TID]["backport"], outcome)
        st = json.loads((self.state / "status.json").read_text())
        self.assertEqual(st["results"][0]["backport"], outcome)


if __name__ == "__main__":
    unittest.main()
