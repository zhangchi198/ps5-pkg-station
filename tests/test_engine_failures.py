"""The engine and failed games: typed errors, what is remembered, what is queued again, and what is announced.
Standard library only."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

from support import ENGINE_DIR, E, StateCase, make_env

import ps5_failures as F  # noqa: E402

GIB = 2**30


class EngineCase(StateCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.dir / "env")
        self.cfg = {**copy.deepcopy(E.DEFAULT_CFG), **self.env}
        self.src = Path(self.env["library"]) / "Example Game [PPSA01234].exfat"
        self.src.write_bytes(b"x" * 100)
        self.twin = Path(self.env["downloads"]) / "Example.rar"
        self.twin.write_bytes(b"r" * 50)
        self.cands = [dict(title_id="PPSA01234", title="Example Game", version="01.002.003", kind="exfat", source=str(self.src),
                           size=100, fw="9.00", packed=""),
                      dict(title_id="PPSA01234", title="Example Game", version="01.002.003", kind="rar", source=str(self.twin),
                           size=100, fw="9.00", packed="", archive_root=None)]
        self.store = F.Store(self.state / "failures.json")

    def plan(self, **kw):
        return {p["title_id"]: p for p in E.choose(self.cfg, self.cands, {}, self.store.load())}["PPSA01234"]

    def fail(self, plan_entry, **kw):
        kw = dict(code="crc_mismatch", phase="prepare", message="m", fingerprint=plan_entry["fingerprint"], state="failed", **kw)
        return self.store.record("PPSA01234", **kw)


class Choosing(EngineCase):
    def test_a_title_with_no_history_is_queued(self):
        p = self.plan()
        self.assertEqual((p["action"], p["why"]), ("build", "new"))
        self.assertRegex(p["fingerprint"], r"^[0-9a-f]{40}$")

    def test_a_failure_with_nothing_changed_is_not_queued_again(self):
        self.fail(self.plan(), data={"files": ["sce_sys/param.json"], "count": 1})
        p = self.plan()
        self.assertEqual(p["action"], "failed")
        self.assertIn("failed on", p["why"])
        self.assertIn("unchanged", p["why"])
        self.assertIn("sce_sys/param.json", p["why"])
        self.assertIn("Retry", p["why"])

    def test_replacing_the_source_changes_the_plan(self):
        self.fail(self.plan())
        self.src.write_bytes(b"y" * 200)
        self.assertEqual(self.plan()["action"], "build")

    def test_changing_an_override_or_the_console_firmware_changes_the_plan(self):
        self.fail(self.plan())
        self.assertEqual(self.plan()["action"], "failed")
        self.cfg["overrides"] = {"PPSA01234": {"modified_exec": "image"}}
        self.assertEqual(self.plan()["action"], "build")
        self.cfg["overrides"] = {"PPSA01234": {"note": "only a note"}}
        self.assertEqual(self.plan()["action"], "failed")
        self.cfg["console_fw"] = "10.00"
        self.assertEqual(self.plan()["action"], "build")

    def test_retry_and_waiting_for_space_are_queued(self):
        self.fail(self.plan())
        self.store.set_flag("PPSA01234", retry=True)
        self.assertEqual(self.plan()["action"], "build")
        self.store.clear("PPSA01234")
        self.fail(self.plan(), data={"need": 5, "usable": 1})
        self.store.update(lambda d: d["PPSA01234"].update(state="waiting"))
        self.assertEqual(self.plan()["action"], "build")

    def test_an_app_update_makes_it_eligible_again(self):
        p = self.plan()
        self.store.record("PPSA01234", code="ultrapack_failed", phase="build", message="m", fingerprint=p["fingerprint"])
        self.assertEqual(self.plan()["action"], "failed")
        with mock.patch.object(F, "app_version", return_value="9.9.9"):
            self.assertEqual(self.plan()["action"], "build")


class TypedErrors(EngineCase):
    """What the engine raises: a code and numbers, not a bare RuntimeError string."""

    def image(self, files):
        class Ent:
            def __init__(self, data):
                self.size, self.data = len(data), data

        class FS:
            def __init__(self):
                self.files = {k: Ent(v) for k, v in files.items()}

            def read(self, e, off, n):
                return e.data[off:off + n]
        fs = FS()
        return mock.patch.object(E.A, "open_game_fs", lambda path: (mock.Mock(root="", fs=fs), None))

    def test_a_crc_mismatch_names_the_files(self):
        files = {"sce_sys/param.json": b"{}", "eboot.bin": b"ELF", "a.bin": b"a", "b.bin": b"b"}
        want = {k.lower(): zlib.crc32(v) for k, v in files.items()}
        want["sce_sys/param.json"] ^= 1
        want["a.bin"] ^= 1
        lines = []
        with self.image(files), self.assertRaises(F.GameError) as cm:
            E.extract_image_files("x.exfat", self.dir / "app", want, lines.append, "Example.rar")
        e = cm.exception
        self.assertEqual((e.code, e.phase, e.data["count"], sorted(e.data["files"]), e.data["against"]),
                         ("crc_mismatch", "prepare", 2, ["a.bin", "sce_sys/param.json"], "Example.rar"))
        self.assertIn("2 extracted file(s) differ from the archive CRCs", str(e))
        self.assertTrue(any("CRC MISMATCH sce_sys/param.json" in ln for ln in lines))

    def test_no_common_files(self):
        with self.image({"x.bin": b"x"}), self.assertRaises(F.GameError) as cm:
            E.extract_image_files("x.exfat", self.dir / "app", {"other.bin": 1}, lambda m: None)
        self.assertEqual(cm.exception.code, "crc_no_common_files")

    def test_not_enough_room_carries_the_numbers(self):
        job = dict(self.plan(), size=300 * GIB)
        with mock.patch.object(E, "usable_ssd", return_value=120 * GIB), self.assertRaises(F.GameError) as cm:
            E.prepare(self.cfg, job, lambda m: None)
        e = cm.exception
        self.assertEqual((e.code, e.data["need"], e.data["usable"]), ("no_space", int(300 * GIB * 1.10), 120 * GIB))
        self.assertIn("needs ~330 GiB on the SSD, 120 GiB usable", str(e))

    def test_a_game_that_is_another_title(self):
        job = dict(self.plan(), kind="folder")
        game = Path(self.env["library"]) / "Other Game"
        (game / "sce_sys").mkdir(parents=True)
        (game / "sce_sys" / "param.json").write_text(json.dumps(dict(titleId="PPSA01235", contentVersion="01.000.000",
                                                                     localizedParameters={"defaultLanguage": "en-US", "en-US": {"titleName": "Other"}})))
        job["source"] = str(game)
        with self.assertRaises(F.GameError) as cm:
            E.prepare(self.cfg, job, lambda m: None)
        self.assertEqual((cm.exception.code, cm.exception.data), ("title_mismatch", {"found": "PPSA01235", "expected": "PPSA01234"}))

    def test_a_backport_with_no_console_firmware(self):
        job = dict(self.plan(), verify_archive=str(self.twin), verify_root="")
        fs = mock.Mock()
        fs.files = {"eboot.bin": mock.Mock(size=3)}
        uw = mock.Mock(root="", fs=fs)
        self.cfg["console_fw"] = ""
        with mock.patch.object(E.A, "open_game_fs", lambda p: (uw, None)), mock.patch.object(E, "image_crc", return_value=1), \
                mock.patch.object(E, "backport_param", return_value=False), \
                mock.patch.object(E, "archive_param", return_value=dict(fw="10.00")), \
                self.assertRaises(F.GameError) as cm:
            E.resolve_modified_exec(self.cfg, job, {"eboot.bin": 2}, lambda m: None)
        self.assertEqual((cm.exception.code, cm.exception.data["files"], cm.exception.data["original_fw"]),
                         ("modified_exec_unknown", ["eboot.bin"], "10.00"))

    def test_the_archive_check_can_be_waived_for_one_title(self):
        job = dict(self.plan(), verify_archive=str(self.twin), verify_root="")
        self.cfg["overrides"] = {"PPSA01234": {"skip_verify": True}}
        lines = []
        with mock.patch.object(E, "archive_listing", side_effect=AssertionError("the archive must not be read")), \
                mock.patch.object(E, "extract_image_files") as ext:
            (self.dir / "jd").mkdir()
            try:
                E.materialise(self.cfg, job, self.dir / "jd", lines.append)
            except Exception:
                pass                                         # nothing was extracted by the stub: the param.json check fails
        ext.assert_called_once()
        self.assertEqual(ext.call_args.args[2], {})            # no CRCs to enforce
        self.assertTrue(any("not checking against the original archive" in ln for ln in lines))


class Running(EngineCase):
    """run_queue with the real prepare / finish_build code paths, and a recorded failure each time."""

    def run_jobs(self, todo):
        notes = []
        with mock.patch.object(E, "notify", lambda cfg, title, msg: notes.append((title, msg))):
            results = E.run_queue(self.cfg, todo, lambda m: None, self.state / "ledger.json", {})
        return results, notes

    def crc_scenario(self):
        files = {"sce_sys/param.json": b"{}", "eboot.bin": b"ELF"}
        want = {k.lower(): zlib.crc32(v) for k, v in files.items()}
        want["sce_sys/param.json"] ^= 1
        fs = mock.Mock(files={k: mock.Mock(size=len(v)) for k, v in files.items()}, read=lambda e, off, n: files[
            next(k for k, x in fs.files.items() if x is e)][off:off + n])
        return (mock.patch.object(E.A, "open_game_fs", lambda p: (mock.Mock(root="", fs=fs), None)),
                mock.patch.object(E, "archive_listing", lambda cfg, a, crcs, timeout=0: crcs.update(want)),
                mock.patch.object(E, "resolve_modified_exec", lambda cfg, job, w, log: w))

    def test_a_prepare_failure_is_stored_with_its_explanation_data_and_log_lines_and_announced_once(self):
        job = dict(self.plan(), verify_archive=str(self.twin), verify_root="")
        patches = self.crc_scenario()
        with patches[0], patches[1], patches[2]:
            results, notes = self.run_jobs([dict(job)])
            self.assertEqual((results[0]["status"], results[0]["code"], results[0]["data"]["files"]), ("failed", "crc_mismatch", ["sce_sys/param.json"]))
            rec = self.store.load()["PPSA01234"]
            self.assertEqual((rec["state"], rec["code"], rec["phase"], rec["attempts"], rec["log"]), ("failed", "crc_mismatch", "prepare", 1, "pkg_sync.log"))
            self.assertEqual(rec["data"]["count"], 1)
            self.assertTrue(any("CRC MISMATCH sce_sys/param.json" in ln for ln in rec["excerpt"]), rec["excerpt"])
            self.assertEqual(rec["fingerprint"], job["fingerprint"])
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0][0], "PS5 pkg: failed")
            self.assertIn("differs from the original archive in 1 file: sce_sys/param.json", notes[0][1])
            # forced again (Retry), same failure: counted, no second notification
            self.store.set_flag("PPSA01234", retry=True)
            _, notes2 = self.run_jobs([dict(job)])
            self.assertEqual(notes2, [])
            self.assertEqual(self.store.load()["PPSA01234"]["attempts"], 2)
            self.assertFalse(self.store.load()["PPSA01234"]["retry"])
        self.assertFalse((Path(self.cfg["temp"]) / "PPSA01234").exists())          # the half-copy is removed

    def test_the_failure_is_in_the_status_file_with_its_code(self):
        job = dict(self.plan(), verify_archive=str(self.twin), verify_root="")
        patches = self.crc_scenario()
        with patches[0], patches[1], patches[2]:
            self.run_jobs([job])
        st = json.loads((self.state / "status.json").read_text())
        self.assertEqual((st["results"][0]["code"], st["results"][0]["status"]), ("crc_mismatch", "failed"))

    def test_not_enough_room_is_waiting_not_failed_and_is_not_announced(self):
        job = self.plan()
        with mock.patch.object(E, "usable_ssd", return_value=1 * GIB):
            results, notes = self.run_jobs([dict(job, size=300 * GIB)])
        self.assertEqual((results[0]["status"], results[0]["code"]), ("deferred", "no_space"))
        self.assertEqual(notes, [])
        rec = self.store.load()["PPSA01234"]
        self.assertEqual((rec["state"], rec["code"], rec["data"]["need"]), ("waiting", "no_space", int(300 * GIB * 1.10)))
        self.assertEqual(self.plan()["action"], "build")                           # a waiting title is always tried again

    def test_a_game_that_fits_again_stops_waiting_but_a_failure_stays_until_the_build_succeeds(self):
        for state, left in (("waiting", set()), ("failed", {"PPSA01234"})):
            with self.subTest(state):
                self.store.clear("PPSA01234")
                self.store.record("PPSA01234", state=state, code="no_space" if state == "waiting" else "ultrapack_failed", fingerprint="x")
                # prepare succeeds, then the run is interrupted before anything is built
                with mock.patch.object(E, "prepare", return_value=dict(app=Path("x"), clean=None, prep_min=1)), \
                        mock.patch.object(E, "start_build", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
                    self.run_jobs([self.plan()])
                self.assertEqual(set(self.store.load()), left)

    def finish(self, rc, log_text="", cli="", stage_files=None, in_temp=False):
        stage = (Path(self.cfg["temp"]) / "PPSA01234" / "out") if in_temp else Path(self.cfg["output"]) / ".staging" / "PPSA01234"
        stage.mkdir(parents=True, exist_ok=True)
        for name, size in (stage_files or {}).items():
            (stage / name).write_bytes(b"x" * size)
        Path(self.cfg["temp"]).mkdir(parents=True, exist_ok=True)
        (Path(self.cfg["ultrapack_src"]) / "backend" / "cli.py").write_text(cli)
        logf = self.logs / "pkg" / "20261122-031000_PPSA01234.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        logf.write_text(log_text)
        job = dict(self.plan(), version="01.002.003", name="Example Game [PPSA01234] [v01.002.003].pkg")
        b = dict(job=job, prep=dict(clean=None, prep_min=1), stage=stage, logf=logf, t0=time.time(), proc=mock.Mock(wait=lambda: rc))
        notes = []
        with mock.patch.object(E, "notify", lambda cfg, title, msg: notes.append((title, msg))):
            res = E.finish_build(self.cfg, b, lambda m: None, self.state / "ledger.json", {}, threading.Lock())
        return res, notes, stage

    ENV_ERROR = ("[stage 3/5] Preparing the fPKG identity (1/2)\n"
                 "[ERROR] Could not resolve the fPKG identity: No module named 'ultra_core'\n")

    def test_an_ultrapack_failure_keeps_the_stage_and_the_error_lines_and_links_the_build_log(self):
        res, notes, _ = self.finish(1, self.ENV_ERROR)
        self.assertEqual((res["status"], res["code"], res["phase"]), ("failed", "ultrapack_failed", "build"))
        d = res["data"]
        self.assertEqual((d["rc"], d["stage"], d["stage_text"].strip()), (1, 3, "Preparing the fPKG identity"))
        self.assertEqual(d["errors"], ["[ERROR] Could not resolve the fPKG identity: No module named 'ultra_core'"])
        rec = self.store.load()["PPSA01234"]
        self.assertEqual((rec["code"], rec["phase"], rec["log"]), ("ultrapack_failed", "build", "pkg/20261122-031000_PPSA01234.log"))
        self.assertEqual(len(notes), 1)
        self.assertIn("UltraPack stopped with an error during Preparing the fPKG identity", notes[0][1])
        # the same failure on the next try is silent; a new app version is a different failure, so it is announced
        self.store.set_flag("PPSA01234", retry=True)
        self.assertEqual(self.finish(1, self.ENV_ERROR)[1], [])
        with mock.patch.object(F, "app_version", return_value="9.9.9"):
            self.assertEqual(len(self.finish(1, self.ENV_ERROR)[1]), 1)

    def test_a_failed_validation_leaves_a_staged_package_whose_size_is_recorded(self):
        res, notes, stage = self.finish(0, "ok\n", cli="import sys\nprint('summary: 3 ok, 2 failed')\nsys.exit(1)\n", stage_files={"game.pkg": 64, "x.bin": 10})
        self.assertEqual((res["status"], res["code"], res["phase"]), ("failed", "validate_failed", "validate"))
        self.assertEqual((res["data"]["failed_checks"], res["data"]["staged"], res["data"]["staged_bytes"]), (2, str(stage), 74))
        self.assertTrue(stage.exists())
        self.assertEqual(res["data"]["staged_stamp"], F.dir_stamp(stage))          # lets the server confirm it is still this folder
        self.assertEqual(self.store.load()["PPSA01234"]["data"]["staged_stamp"], F.dir_stamp(stage))
        rec = self.store.load()["PPSA01234"]
        self.assertEqual((rec["code"], rec["data"]["staged"]), ("validate_failed", str(stage)))
        self.assertIn("failed 2 validation checks", notes[0][1])
        self.assertFalse((Path(self.cfg["output"]) / "Example Game [PPSA01234] [v01.002.003].pkg").exists())

    def test_a_built_package_forgets_the_failure_and_announces_the_build(self):
        self.store.record("PPSA01234", code="ultrapack_failed", fingerprint="x")
        res, notes, stage = self.finish(0, "ok\n", cli="print('summary: 5 ok, 0 failed')\n", stage_files={"game.pkg": 64})
        self.assertEqual(res["status"], "built", res)
        self.assertEqual(self.store.load(), {})
        self.assertEqual([n[0] for n in notes], ["PS5 pkg: built"])
        self.assertFalse(stage.exists())

    def test_a_copy_that_fails_verification_is_a_copy_failure(self):
        with mock.patch.object(E, "copy_verified", side_effect=F.GameError("copy_failed", "NAS copy of x.pkg failed verification", phase="copy")):
            res, notes, _ = self.finish(0, "ok\n", cli="print('summary: 5 ok, 0 failed')\n", stage_files={"game.pkg": 64}, in_temp=True)
        self.assertEqual((res["status"], res["code"], res["phase"]), ("failed", "copy_failed", "copy"))
        self.assertEqual(self.store.load()["PPSA01234"]["phase"], "copy")
        self.assertIn("copying it to the output folder failed", notes[0][1])

    def test_an_unexpected_exception_is_stored_as_unknown_with_its_message(self):
        with mock.patch.object(E.subprocess, "run", side_effect=ValueError("odd")):
            res, notes, _ = self.finish(0, "ok\n", cli="", stage_files={"game.pkg": 64})
        self.assertEqual((res["status"], res["code"], res["phase"]), ("failed", "unknown", "build"))
        self.assertEqual(self.store.load()["PPSA01234"]["message"], "ValueError: odd")
        self.assertIn("ValueError: odd", notes[0][1])                                # the raw message is the fallback text


class RunningTheRealEngine(StateCase):
    """The command line: a failed title is skipped by a run and retried by --only."""

    def setUp(self):
        super().setUp()
        if not shutil.which("nice"):
            self.skipTest("needs nice(1)")
        self.env = make_env(self.dir / "env")
        self.write_cfg({**E.DEFAULT_CFG, **self.env})
        self.store = F.Store(self.state / "failures.json")

    def engine(self, *args):
        return subprocess.run([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), *args], capture_output=True, text=True,
                              env=os.environ.copy(), timeout=180)

    def test_a_scheduled_run_skips_a_failed_title_until_something_changes_and_only_retries_it(self):
        self.assertEqual(self.engine("plan").returncode, 0)
        entry = json.loads((self.state / "plan.json").read_text())[0]
        self.assertEqual((entry["title_id"], entry["action"]), ("PPSA01234", "build"))
        self.store.record("PPSA01234", code="crc_mismatch", phase="prepare", message="1 extracted file(s) differ", fingerprint=entry["fingerprint"],
                          data={"files": ["sce_sys/param.json"], "count": 1}, title="Example Game")
        r = self.engine("run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"=== skipped: failed on \d{4}-\d\d-\d\d \d\d:\d\d, unchanged: PPSA01234")
        self.assertIn("differs from the original archive in 1 file: sce_sys/param.json", r.stdout)
        self.assertIn("=== run: 0 build(s)", r.stdout)
        self.assertFalse((Path(self.env["temp"]) / "PPSA01234").exists())          # nothing was copied
        plan = json.loads((self.state / "plan.json").read_text())[0]
        self.assertEqual(plan["action"], "failed")
        # "Build only this" always retries; the fake UltraPack builds nothing, so it fails again, differently
        r = self.engine("run", "--only", "PPSA01234")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("skipped", r.stdout)
        self.assertIn("PREPARE", r.stdout)
        rec = self.store.load()["PPSA01234"]
        self.assertEqual((rec["code"], rec["phase"], rec["attempts"]), ("ultrapack_failed", "build", 2))
        self.assertTrue(rec["log"].startswith("pkg/") and rec["log"].endswith("_PPSA01234.log"))
        self.assertTrue((self.logs / rec["log"]).exists())

    def test_a_changed_override_makes_the_next_plain_run_try_it(self):
        self.assertEqual(self.engine("plan").returncode, 0)
        fp = json.loads((self.state / "plan.json").read_text())[0]["fingerprint"]
        self.store.record("PPSA01234", code="modified_exec_unknown", phase="prepare", message="m", fingerprint=fp)
        self.assertIn("skipped: failed on", self.engine("run").stdout)
        self.write_cfg({**E.DEFAULT_CFG, **self.env, "overrides": {"PPSA01234": {"modified_exec": "image"}}})
        r = self.engine("run")
        self.assertNotIn("skipped", r.stdout)
        self.assertIn("PREPARE", r.stdout)

    def test_a_title_that_has_been_built_since_is_forgotten(self):
        self.store.record("PPSA01234", code="crc_mismatch", fingerprint="old")
        out = Path(self.env["output"]) / "Example Game [PPSA01234] [v01.002.003] [fw9.00].pkg"
        out.write_bytes(b"pkg")
        (self.state / "ledger.json").write_text(json.dumps({"PPSA01234": dict(version="01.002.003", output=str(out), built="2026-11-20 03:10")}))
        self.assertEqual(self.engine("plan").returncode, 0)
        self.assertEqual(self.store.load(), {})


if __name__ == "__main__":
    unittest.main()
