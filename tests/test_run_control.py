"""Run control in the engine: stop after the current games, stop now, cancel one game, the request files, and what a cancel
leaves behind (nothing in the output folder but finished packages, nothing remembered as a failure, no notification).
Standard library only; the real-engine tests use a stand-in UltraPack (a script that runs for a few seconds and starts a helper
process, so ending a build's whole process group is checked, not only its first process)."""
from __future__ import annotations

import copy
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ENGINE_DIR, E, StateCase, make_env

import ps5_failures as F  # noqa: E402
import ps5_history as PH  # noqa: E402
import ps5_runctl as RC  # noqa: E402
import test_engine_failures as TEF  # noqa: E402


def wait_for(cond, deadline=30.0, what="the condition"):
    end = time.time() + deadline
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.1)
    raise AssertionError(f"timed out after {deadline:.0f} s waiting for {what}")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                              # a zombie that nobody has collected yet is dead
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(out) and not out.startswith("Z")
    except OSError:
        return True


class Isolated(unittest.TestCase):
    def setUp(self):
        RC.begin_run()
        self.addCleanup(RC.begin_run)


# ------------------------------------------------------------------ the request files

class Requests(Isolated):
    def setUp(self):
        super().setUp()
        self.d = Path(__import__("tempfile").mkdtemp(prefix="rc-"))
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_a_stop_is_after_current_or_now_and_an_old_time_stamp_means_after_current(self):
        self.assertIsNone(RC.stop_mode(self.d))
        (self.d / "stop.request").write_text("2026-11-22 03:12:44")
        self.assertEqual(RC.stop_mode(self.d), "after_current")
        RC.write_stop(self.d, "now")
        self.assertEqual(RC.stop_mode(self.d), "now")
        self.assertEqual(json.loads((self.d / "stop.request").read_text())["mode"], "now")
        (self.d / "stop.request").write_text("{broken")
        self.assertEqual(RC.stop_mode(self.d), "after_current")

    def test_now_is_never_weakened_by_a_later_after_current(self):
        RC.write_stop(self.d, "now")
        self.assertEqual(RC.write_stop(self.d, "after_current"), "now")
        self.assertEqual(RC.stop_mode(self.d), "now")
        with self.assertRaises(ValueError):
            RC.write_stop(self.d, "later")

    def test_only_an_after_current_request_can_be_withdrawn(self):
        self.assertFalse(RC.withdraw_stop(self.d))
        RC.write_stop(self.d, "after_current")
        self.assertTrue(RC.withdraw_stop(self.d))
        self.assertIsNone(RC.stop_mode(self.d))
        RC.write_stop(self.d, "now")
        self.assertFalse(RC.withdraw_stop(self.d))
        self.assertEqual(RC.stop_mode(self.d), "now")

    def test_cancel_requests_are_per_game_validated_and_taken_once(self):
        RC.request_cancel(self.d, "PPSA01234")
        RC.request_cancel(self.d, "CUSA00001")
        self.assertEqual(RC.pending_cancels(self.d), ["CUSA00001", "PPSA01234"])
        for bad in ("../x", "PPSA1234", "ppsa01234", "PPSA01234/../../etc", ""):
            with self.assertRaises(ValueError):
                RC.request_cancel(self.d, bad)
        self.assertEqual(RC.take_cancels(self.d), ["CUSA00001", "PPSA01234"])
        self.assertEqual((RC.pending_cancels(self.d), RC.take_cancels(self.d)), ([], []))

    def test_a_run_starts_and_ends_with_no_request_left(self):
        RC.write_stop(self.d, "now")
        RC.request_cancel(self.d, "PPSA01234")
        RC.clear_requests(self.d)
        self.assertEqual((RC.stop_mode(self.d), RC.pending_cancels(self.d)), (None, []))

    def test_a_request_file_is_never_half_written(self):
        RC.write_stop(self.d, "now")
        self.assertEqual([p.name for p in self.d.iterdir()], ["stop.request"])


class Pause(Isolated):
    def setUp(self):
        super().setUp()
        self.d = Path(__import__("tempfile").mkdtemp(prefix="rc-"))
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_paused_for_ever_until_a_time_and_over(self):
        self.assertIsNone(RC.pause_read(self.d))
        r = RC.pause_write(self.d, None, now=1000.0)
        self.assertEqual((r["since"], r["until"]), (1000.0, None))
        self.assertIsNotNone(RC.pause_read(self.d, now=10 ** 12))                       # no end: still paused
        RC.pause_write(self.d, 5000.0, now=1000.0)
        self.assertEqual(RC.pause_read(self.d, now=4999.0)["until"], 5000.0)
        self.assertIsNone(RC.pause_read(self.d, now=5000.0))                            # over at the time
        self.assertTrue(RC.pause_clear(self.d))
        self.assertFalse(RC.pause_clear(self.d))

    def test_a_pause_that_would_already_be_over_is_refused(self):
        with self.assertRaises(ValueError):
            RC.pause_write(self.d, 900.0, now=1000.0)
        self.assertIsNone(RC.pause_read(self.d))

    def test_a_damaged_file_means_not_paused(self):
        for text in ("", "{", "[]", '"x"', '{"until": "soon"}'):
            (self.d / "schedule_pause.json").write_text(text)
            r = RC.pause_read(self.d)
            self.assertTrue(r is None or r["until"] is None, text)
        (self.d / "schedule_pause.json").write_text("{")
        self.assertIsNone(RC.pause_read(self.d))


# ------------------------------------------------------------------ processes

SLEEPER = r'''
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
open(sys.argv[1], "w").write(f"{os.getpid()} {child.pid}")
time.sleep(600)
'''


class Processes(Isolated):
    def setUp(self):
        super().setUp()
        self.d = Path(__import__("tempfile").mkdtemp(prefix="rc-"))
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        (self.d / "sleeper.py").write_text(SLEEPER)

    def start(self):
        proc = subprocess.Popen([sys.executable, str(self.d / "sleeper.py"), str(self.d / "pids")], start_new_session=True)
        self.addCleanup(lambda: proc.poll() is None and os.killpg(proc.pid, signal.SIGKILL))
        pid, child = map(int, wait_for(lambda: (self.d / "pids").exists() and (self.d / "pids").read_text().strip() or None, 15, "the helper pids").split())
        return proc, pid, child

    def test_ending_a_process_ends_the_helpers_it_started_too(self):
        proc, pid, child = self.start()
        self.assertTrue(alive(pid) and alive(child))
        self.assertTrue(RC.kill_group(pid, proc, grace=3.0))
        wait_for(lambda: not alive(child), 10, "the helper to end")
        self.assertFalse(alive(pid))

    def test_a_group_that_is_ours_is_never_signalled_as_a_group(self):
        def fake_kill(pid, sig):
            if sig == 0:
                raise ProcessLookupError                                                # gone after the first signal
        with mock.patch.object(RC.os, "getpgid", return_value=os.getpgrp()), mock.patch.object(RC.os, "killpg") as killpg, \
                mock.patch.object(RC.os, "kill", side_effect=fake_kill) as kill:
            self.assertTrue(RC.kill_group(424242, None, grace=0.0))
        killpg.assert_not_called()
        kill.assert_any_call(424242, signal.SIGTERM)                                    # the one process gets the signal, not our group

    def test_a_process_run_for_a_game_is_ended_by_its_cancel_and_reports_a_failure_code(self):
        results = {}

        def work():
            with RC.working_on("PPSA01234"):
                results["r"] = RC.run_tracked([sys.executable, str(self.d / "sleeper.py"), str(self.d / "pids")], capture_output=True)
        t = threading.Thread(target=work, daemon=True)
        t.start()
        pid, child = map(int, wait_for(lambda: (self.d / "pids").exists() and (self.d / "pids").read_text().strip() or None, 15, "the process").split())
        self.addCleanup(lambda: [os.kill(x, signal.SIGKILL) for x in (pid, child) if alive(x)])
        RC.cancel_title("PPSA01234")
        self.assertEqual(RC.kill_title("PPSA01234"), 1)
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertNotEqual(results["r"].returncode, 0)
        wait_for(lambda: not alive(child), 10, "the helper to end")
        self.assertFalse(alive(pid))

    def test_other_games_processes_are_left_alone(self):
        proc, pid, child = self.start()
        mine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(lambda: mine.poll() is None and mine.kill())
        with RC._LOCK:
            RC._PROCS["PPSA05678"] = {mine}
        RC.kill_title("PPSA01234")
        time.sleep(0.3)
        self.assertIsNone(mine.poll())
        self.assertTrue(alive(pid))
        RC.kill_everything()
        wait_for(lambda: mine.poll() is not None, 10, "the other game's process to end")

    def test_a_cancel_that_came_first_stops_a_process_from_starting(self):
        RC.cancel_title("PPSA01234")
        with RC.working_on("PPSA01234"), self.assertRaises(RC.Cancelled):
            RC.run_tracked([sys.executable, "-c", f"open({str(self.d / 'ran')!r}, 'w')"], capture_output=True)
        self.assertFalse((self.d / "ran").exists())

    def test_without_a_game_it_is_plain_subprocess_run(self):
        r = RC.run_tracked([sys.executable, "-c", "print('hi')"], capture_output=True, text=True, timeout=30)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "hi"))
        RC.CANCEL_ALL.set()                                                             # a scan is not a game: not affected
        self.assertEqual(RC.run_tracked([sys.executable, "-c", "pass"], capture_output=True).returncode, 0)

    def test_run_tracked_keeps_subprocess_runs_arguments(self):
        with RC.working_on("PPSA01234"):
            r = RC.run_tracked([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], input="abc", capture_output=True, text=True, timeout=30)
            self.assertEqual(r.stdout.strip(), "ABC")
            with self.assertRaises(subprocess.TimeoutExpired):
                RC.run_tracked([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
            with self.assertRaises(subprocess.CalledProcessError):
                RC.run_tracked([sys.executable, "-c", "raise SystemExit(3)"], check=True)

    def test_stop_now_flag_cancels_every_game(self):
        RC.CANCEL_ALL.set()
        self.assertTrue(RC.cancel_requested("PPSA00001") and RC.cancel_requested("PPSA00002"))
        self.assertFalse(RC.title_cancelled("PPSA00001"))
        RC.begin_run()
        self.assertFalse(RC.cancel_requested("PPSA00001"))


# ------------------------------------------------------------------ a build that is cancelled at each step

class Finishing(TEF.EngineCase):
    """finish_build with a stand-in UltraPack: what each kind of cancel leaves in the output folder and the work folder."""

    def setUp(self):
        super().setUp()
        RC.begin_run()
        self.addCleanup(RC.begin_run)
        self.out = Path(self.cfg["output"])

    def finish(self, rc=0, cli="print('summary: 5 ok, 0 failed')\n", stage_files=None, in_temp=False, wait=None, name="Example Game [PPSA01234] [v01.002.003].pkg"):
        jd = Path(self.cfg["temp"]) / "PPSA01234"
        stage = (jd / "out") if in_temp else self.out / ".staging" / "PPSA01234"
        stage.mkdir(parents=True, exist_ok=True)
        jd.mkdir(parents=True, exist_ok=True)
        for fname, size in (stage_files if stage_files is not None else {"game.pkg": 64}).items():
            (stage / fname).write_bytes(b"x" * size)
        (jd / ".building").write_text("{}")
        (Path(self.cfg["ultrapack_src"]) / "backend" / "cli.py").write_text(cli)
        logf = self.logs / "pkg" / "20261122-031000_PPSA01234.log"
        logf.parent.mkdir(parents=True, exist_ok=True)
        logf.write_text("ok\n")
        job = dict(self.plan(), version="01.002.003", name=name)
        b = dict(job=job, prep=dict(clean=None, prep_min=1), stage=stage, logf=logf, t0=time.time(), proc=mock.Mock(wait=wait or (lambda: rc)))
        notes = []
        with mock.patch.object(E, "notify", lambda cfg, title, msg: notes.append((title, msg))):
            res = E.finish_build(self.cfg, b, lambda m: None, self.state / "ledger.json", {}, threading.Lock())
        return res, notes, stage

    def finished_package(self, text=b"the finished package"):
        p = self.out / "Example Game [PPSA01234] [v01.002.003].pkg"
        p.write_bytes(text)
        os.utime(p, (1_000_000_000, 1_000_000_000))
        return p

    def assert_clean(self, stage):
        self.assertFalse(stage.exists())
        self.assertFalse((Path(self.cfg["temp"]) / "PPSA01234").exists())               # the work folder, with its .building marker
        self.assertFalse((self.out / ".staging").exists())                              # nothing half-made in the output folder

    def assert_not_a_failure(self, res, notes):
        self.assertEqual(self.store.load(), {})
        self.assertEqual(notes, [])
        self.assertEqual((res["status"], res["code"]), ("cancelled", "cancelled"))

    def test_cancelled_while_building_leaves_nothing_and_remembers_nothing(self):
        kept = self.finished_package()
        before = (kept.read_bytes(), kept.stat().st_mtime)
        res, notes, stage = self.finish(wait=lambda: (RC.cancel_title("PPSA01234"), -15)[1], stage_files={"partial.bin": 1000})
        self.assert_not_a_failure(res, notes)
        self.assertEqual((res["phase"], res["why"]), ("building", "cancelled while building"))
        self.assert_clean(stage)
        self.assertEqual((kept.read_bytes(), kept.stat().st_mtime), before)               # a finished package is never touched
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [kept.name])

    def test_cancelled_after_the_build_ended_well_is_still_cancelled_not_built(self):
        res, notes, stage = self.finish(wait=lambda: (RC.cancel_title("PPSA01234"), 0)[1])
        self.assert_not_a_failure(res, notes)
        self.assert_clean(stage)
        self.assertEqual([p.name for p in self.out.iterdir()], [])
        self.assertEqual(json.loads((self.state / "ledger.json").read_text()) if (self.state / "ledger.json").exists() else {}, {})

    def test_cancelled_while_the_package_is_checked(self):
        kept = self.finished_package()
        cli = "import time\ntime.sleep(60)\n"
        threading.Timer(1.0, lambda: (RC.cancel_title("PPSA01234"), RC.kill_title("PPSA01234"))).start()
        t0 = time.time()
        res, notes, stage = self.finish(cli=cli)
        self.assertLess(time.time() - t0, 20)
        self.assert_not_a_failure(res, notes)
        self.assertEqual((res["phase"], res["why"]), ("validating", "cancelled while checking the package"))
        self.assert_clean(stage)
        self.assertEqual(kept.read_bytes(), b"the finished package")

    def test_cancelled_while_copying_leaves_no_partial_file_and_the_old_package_untouched(self):
        kept = self.finished_package()
        before = (kept.read_bytes(), kept.stat().st_mtime)
        real = E.copy_verified

        def copy_then_cancel(src, dst, log, progress=None, before_rename=None):
            return real(src, dst, log, progress=lambda *a: (RC.cancel_title("PPSA01234"), progress(*a)), before_rename=before_rename)
        with mock.patch.object(E, "copy_verified", copy_then_cancel):
            res, notes, stage = self.finish(in_temp=True)
        self.assert_not_a_failure(res, notes)
        self.assertEqual((res["phase"], res["why"]), ("copying", "cancelled while copying it to the output folder"))
        self.assert_clean(stage)
        self.assertEqual((kept.read_bytes(), kept.stat().st_mtime), before)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [kept.name])         # no "(replaced ...)", no .partial

    def test_a_failed_copy_no_longer_moves_the_older_package_aside(self):
        kept = self.finished_package()
        with mock.patch.object(E, "copy_verified", side_effect=F.GameError("copy_failed", "NAS copy failed verification", phase="copy")):
            res, notes, _ = self.finish(in_temp=True)
        self.assertEqual(res["code"], "copy_failed")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [kept.name])
        self.assertEqual(kept.read_bytes(), b"the finished package")

    def test_a_good_rebuild_still_moves_the_older_package_aside_when_the_new_one_is_complete(self):
        for in_temp in (False, True):
            with self.subTest(in_temp=in_temp):
                for p in self.out.iterdir():
                    if p.is_file():
                        p.unlink()
                kept = self.finished_package(b"old")
                res, _, _ = self.finish(in_temp=in_temp, stage_files={"game.pkg": 64})
                self.assertEqual(res["status"], "built", res)
                names = sorted(p.name for p in self.out.iterdir() if p.is_file())
                self.assertEqual(len(names), 2, names)
                self.assertTrue(any("(replaced " in n for n in names))
                self.assertEqual(kept.stat().st_size, 64)                                # the new package is where the old one was

    def test_a_built_package_carries_what_the_history_needs_and_ends_a_rebuild_request(self):
        F.Store(self.state / "rebuild.json").update(lambda d: d.__setitem__("PPSA01234", dict(at=1)))
        F.Store(self.state / "rebuild.json").update(lambda d: d.__setitem__("PPSA05678", dict(at=1)))
        res, notes, _ = self.finish(in_temp=True)
        self.assertEqual(res["status"], "built", res)
        self.assertEqual((res["bytes"], res["level"]), (64, self.cfg["compression_level"]))
        row = PH.job_row(res, "20261122-030000")
        self.assertEqual((row["bytes"], row["log"], row["status"]), (64, "pkg/20261122-031000_PPSA01234.log", "built"))
        self.assertEqual(list(F.Store(self.state / "rebuild.json").load()), ["PPSA05678"])                # asked for, and done: only this game's request is gone

    def test_what_happens_to_the_older_package_follows_the_setting(self):
        stem = "Example Game [PPSA01234] [v01.002.003]"
        for setting, want in (("all", 3), ("1", 1), ("never", 0)):
            with self.subTest(setting):
                for p in self.out.iterdir():
                    if p.is_file():
                        p.unlink()
                self.finished_package(b"old")
                for ts in ("202601010000", "202601020000"):
                    (self.out / f"{stem} (replaced {ts}).pkg").write_bytes(b"older")
                other = self.out / "Other [PPSA05678] (replaced 202601010000).pkg"
                other.write_bytes(b"another game's")
                self.cfg["keep_replaced"] = setting
                res, _, _ = self.finish(in_temp=True)
                self.assertEqual(res["status"], "built", res)
                mine = [d for d in PH.replaced_files(self.out) if d["stem"] == stem]
                self.assertEqual(len(mine), want, [d["name"] for d in mine])
                if setting == "1":
                    self.assertGreater(mine[0]["replaced_at"], time.time() - 3600)                       # the one kept is the one just made
                self.assertTrue(other.exists())                                                           # another game's is never touched
                self.assertEqual((self.out / f"{stem}.pkg").stat().st_size, 64)                           # the new package is in place

    def test_cancelled_with_the_stage_on_the_work_drive_removes_it_with_the_work_folder(self):
        res, notes, stage = self.finish(in_temp=True, wait=lambda: (RC.cancel_title("PPSA01234"), -15)[1])
        self.assert_not_a_failure(res, notes)
        self.assert_clean(stage)

    def test_discard_only_touches_staging(self):
        outside = self.out / "Example Game.pkg"
        outside.write_bytes(b"finished")
        stage = self.out / ".staging" / "PPSA01234"
        stage.mkdir(parents=True)
        (stage / "a.bin").write_bytes(b"1")
        (self.out / ".staging" / "other [PPSA09999].pkg.partial").write_bytes(b"2")
        E.discard_staged(self.cfg, stage, "Example Game [PPSA01234] [v01.002.003].pkg")
        self.assertFalse(stage.exists())
        self.assertTrue(outside.exists())
        self.assertTrue((self.out / ".staging" / "other [PPSA09999].pkg.partial").exists())    # another game's partial copy
        elsewhere = Path(self.cfg["temp"]) / "PPSA01234" / "out"
        elsewhere.mkdir(parents=True)
        E.discard_staged(self.cfg, elsewhere, "x.pkg")                                          # not under .staging: left to the work folder's removal
        self.assertTrue(elsewhere.exists())


class Preparing(TEF.EngineCase):
    def setUp(self):
        super().setUp()
        RC.begin_run()
        self.addCleanup(RC.begin_run)
        self.cfg["ssd_reserve_gb"] = 0
        self.sz = Path(self.cfg["ultrapack_src"]) / "backend" / "native" / "7zz"

    def job(self):
        return dict(self.plan(), kind="rar", source=str(self.twin), archive_root=None, size=1000)

    def test_cancelling_a_prepare_ends_the_7zip_it_runs_and_removes_the_half_made_copy(self):
        self.sz.write_text(f"#!/bin/sh\necho $$ > {self.dir / 'pid'}\nsleep 40\n")
        self.sz.chmod(0o755)
        err = {}

        def work():
            try:
                E.prepare(self.cfg, self.job(), lambda m: None)
            except BaseException as ex:
                err["e"] = ex
        t = threading.Thread(target=work)
        t.start()
        pid = int(wait_for(lambda: (self.dir / "pid").exists() and (self.dir / "pid").read_text().strip() or None, 15, "7-Zip to start"))
        jd = Path(self.cfg["temp"]) / "PPSA01234"
        self.assertTrue(jd.exists())
        RC.cancel_title("PPSA01234")
        RC.kill_title("PPSA01234")
        t.join(20)
        self.assertFalse(t.is_alive())
        self.assertIsInstance(err.get("e"), RC.Cancelled)                                 # not an "extract_failed" error
        self.assertFalse(jd.exists())
        wait_for(lambda: not alive(pid), 10, "7-Zip to end")
        self.assertTrue(self.twin.exists())                                               # the source is only ever read

    def test_a_prepared_copy_that_is_complete_is_kept_when_the_game_is_cancelled(self):
        jd = Path(self.cfg["temp"]) / "PPSA01234"
        (jd / "src" / "app" / "sce_sys").mkdir(parents=True)
        (jd / "src" / "app" / "sce_sys" / "param.json").write_text("{}")
        job = self.job()
        (jd / ".prepared.json").write_text(json.dumps(dict(app=str(jd / "src" / "app"), clean=None, prep_min=1, source=job["source"], at="x", job={})))
        with mock.patch.object(E, "name_after_patch", return_value={}):
            got = E.prepare(self.cfg, job, lambda m: None)                               # reused
        self.assertEqual(got["app"], jd / "src" / "app")
        RC.cancel_title("PPSA01234")
        with self.assertRaises(RC.Cancelled):
            E.prepare(self.cfg, job, lambda m: None)
        self.assertTrue((jd / ".prepared.json").exists())                                 # a cancel before it starts does not delete a finished copy

    def test_copying_a_folder_looks_at_the_cancel_between_files_and_inside_a_big_one(self):
        src = self.dir / "srcgame"
        src.mkdir()
        for n in range(3):
            (src / f"f{n}.bin").write_bytes(b"x" * 10)
        big = src / "big.bin"
        with open(big, "wb") as fh:
            fh.truncate(80 << 20)
        calls = []

        def counting():
            calls.append(1)
            if len(calls) == 6:
                raise RC.Cancelled("PPSA01234")
        with RC.working_on("PPSA01234"), mock.patch.object(RC, "check_cancel", counting):
            with self.assertRaises(RC.Cancelled):
                shutil.copytree(src, self.dir / "dst", copy_function=E.copy_cancelable)
        self.assertGreaterEqual(len(calls), 6)
        shutil.rmtree(self.dir / "dst", ignore_errors=True)
        shutil.copytree(src, self.dir / "dst2", copy_function=E.copy_cancelable)         # not cancelled: a faithful copy
        self.assertEqual((self.dir / "dst2" / "big.bin").stat().st_size, 80 << 20)
        self.assertEqual(sorted(p.name for p in (self.dir / "dst2").iterdir()), ["big.bin", "f0.bin", "f1.bin", "f2.bin"])


class Ps4(TEF.EngineCase):
    def test_a_ps4_title_taken_out_of_the_run_is_cancelled_not_failed_and_nothing_is_copied(self):
        RC.begin_run()
        self.addCleanup(RC.begin_run)
        RC.cancel_title("CUSA00001")
        job = dict(title_id="CUSA00001", title="Old Game", name="Old Game [CUSA00001]", source=str(self.src), size=10, version="01.00",
                   dest_dir=str(Path(self.cfg["output"]) / "PS4" / "Old Game [CUSA00001]"), files=[])
        with mock.patch.object(E, "copy_ps4", side_effect=AssertionError("must not start")):
            res = E.run_ps4(self.cfg, [job], lambda m: None, self.state / "ledger.json", {})
        self.assertEqual((res[0]["status"], res[0]["code"], res[0]["phase"]), ("cancelled", "cancelled", "queue"))
        self.assertEqual(self.store.load(), {})
        self.assertFalse((Path(self.cfg["output"]) / "PS4").exists())

    def test_a_ps4_copy_cancelled_in_flight_is_cancelled_not_failed(self):
        RC.begin_run()
        self.addCleanup(RC.begin_run)
        job = dict(title_id="CUSA00001", title="Old Game", name="Old Game [CUSA00001]", source=str(self.src), size=10, version="01.00",
                   dest_dir=str(Path(self.cfg["output"]) / "PS4" / "x"), files=[])

        def killed(cfg, j, log):
            RC.cancel_title("CUSA00001")
            raise F.GameError("extract_failed", "7-Zip failed (-15)", phase="copy")           # what the killed process looks like
        notes = []
        with mock.patch.object(E, "copy_ps4", killed), mock.patch.object(E, "notify", lambda c, t, m: notes.append(t)):
            res = E.run_ps4(self.cfg, [job], lambda m: None, self.state / "ledger.json", {})
        self.assertEqual((res[0]["status"], res[0]["code"]), ("cancelled", "cancelled"))
        self.assertEqual((self.store.load(), notes), ({}, []))


# ------------------------------------------------------------------ the whole engine, a stand-in UltraPack

FAKE = r'''
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
if "--fpkg-validate" in args:
    print("summary: 7 ok, 0 failed")
    sys.exit(0)
stage = Path(args[1])
stage.mkdir(parents=True, exist_ok=True)
(stage / "partial.bin").write_bytes(b"x" * 1000)
pids = os.environ.get("FAKE_PIDS")
if pids:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    Path(pids, f"{os.getpid()}.json").write_text(json.dumps(dict(pid=os.getpid(), child=child.pid, stage=str(stage))))
print("[stage 1/5] Building and compressing the inner pfs_image.dat (Kraken level 4)...", flush=True)
end = time.time() + float(os.environ.get("FAKE_SECS", "6"))
while time.time() < end:
    time.sleep(0.1)
for n in range(2, 6):
    print(f"[stage {n}/5] x", flush=True)
(stage / "partial.bin").unlink()
(stage / "Example Game.pkg").write_bytes(b"x" * 2048)
print("[SUCCESS] fPKG built.", flush=True)
'''

GAMES = (("PPSA01234", "Alpha Game", 100), ("PPSA05678", "Bravo Game", 200), ("PPSA09999", "Charlie Game", 300))


class Hook(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Hook.seen.append(self.headers.get("Title"))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


class WholeEngine(StateCase):
    """A real engine process (scan, prepare, build, finish) over three small games."""

    def setUp(self):
        super().setUp()
        if not shutil.which("nice"):
            self.skipTest("needs nice(1)")
        self.env = make_env(self.dir / "env", game=False)
        lib = Path(self.env["library"])
        for tid, title, pad in GAMES:
            g = lib / f"{title} [{tid}]"
            (g / "sce_sys").mkdir(parents=True)
            (g / "eboot.bin").write_bytes(b"\0" * pad)
            (g / "sce_sys" / "param.json").write_text(json.dumps({"titleId": tid, "contentVersion": "01.00", "requiredSystemSoftwareVersion": "0x0900000000000000",
                                                                    "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": title}}}))
        (Path(self.env["ultrapack_src"]) / "backend" / "cli.py").write_text(FAKE)
        self.out, self.temp = Path(self.env["output"]), Path(self.env["temp"])
        self.pids = self.dir / "pids"
        self.pids.mkdir()
        Hook.seen = []
        self.hook = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=self.hook.serve_forever, daemon=True).start()
        self.addCleanup(self.hook.shutdown)
        self.addCleanup(self.hook.server_close)
        self.procs = []
        self.addCleanup(self.kill_leftovers)

    def kill_leftovers(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
        for f in self.pids.glob("*.json"):
            d = json.loads(f.read_text())
            for pid in (d["pid"], d["child"]):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def config(self, **kw):
        self.write_cfg({**copy.deepcopy(E.DEFAULT_CFG), **self.env, "ssd_reserve_gb": 0, "max_builds": 3, "max_compress": 3, "cpu_count": 1, "nice": 5,
                        "notify_url": f"http://127.0.0.1:{self.hook.server_address[1]}/t", **kw})

    def start(self, secs=6, *extra):
        env = dict(os.environ, PS5PKG_STATUS_INTERVAL="0.2", PS5PKG_CONTROL_INTERVAL="0.2", FAKE_PIDS=str(self.pids), FAKE_SECS=str(secs))
        proc = subprocess.Popen([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), "run", *extra], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(proc)
        self.log = None
        threading.Thread(target=lambda: setattr(self, "log", proc.stdout.read()), daemon=True).start()
        return proc

    def status(self):
        try:
            return json.loads((self.state / "status.json").read_text())
        except (OSError, ValueError):
            return {}

    def builders(self):
        return {json.loads(f.read_text())["stage"].rsplit("/", 1)[-1]: json.loads(f.read_text()) for f in self.pids.glob("*.json")}

    def result_of(self, tid):
        return next((r for r in self.status().get("results", []) if r.get("title_id") == tid), None)

    def finish(self, proc, deadline=60):
        wait_for(lambda: proc.poll() is not None, deadline, "the engine to end")
        wait_for(lambda: self.status().get("finished"), 10, "the final status")
        wait_for(lambda: self.log is not None, 10, "the engine's log")
        self.assertEqual(proc.returncode, 0, self.log)
        return self.status()

    def assert_nothing_left(self, tids):
        self.assertEqual([p.name for p in self.temp.iterdir()] if self.temp.exists() else [], [])
        self.assertFalse((self.out / ".staging").exists(), list((self.out / ".staging").glob("*")) if (self.out / ".staging").exists() else "")
        for tid in tids:
            self.assertFalse((self.temp / tid / ".building").exists())

    def test_cancelling_one_build_ends_only_that_build_and_the_others_finish(self):
        self.config()
        existing = self.out / "Earlier Game [PPSA00001] [v01.00] [fw9.00].pkg"
        existing.write_bytes(b"finished long ago")
        os.utime(existing, (1_000_000_000, 1_000_000_000))
        proc = self.start(secs=8)
        wait_for(lambda: len(self.builders()) == 3 and len(self.status().get("builds", [])) == 3, 40, "three builds")
        b = self.builders()
        self.assertTrue((self.out / ".staging" / "PPSA05678" / "partial.bin").exists())
        RC.request_cancel(self.state, "PPSA05678")
        res = wait_for(lambda: self.result_of("PPSA05678"), 20, "the cancelled result")
        self.assertEqual((res["status"], res["code"]), ("cancelled", "cancelled"))
        wait_for(lambda: not alive(b["PPSA05678"]["child"]) and not alive(b["PPSA05678"]["pid"]), 10, "its processes to end")
        self.assertFalse((self.out / ".staging" / "PPSA05678").exists())
        self.assertFalse((self.temp / "PPSA05678").exists())
        for other in ("PPSA01234", "PPSA09999"):                                   # the others go on
            self.assertTrue(alive(b[other]["pid"]), other)
            self.assertTrue((self.temp / other).exists(), other)
        st = self.finish(proc)
        self.assertEqual({r["title_id"]: r["status"] for r in st["results"]}, {"PPSA01234": "built", "PPSA05678": "cancelled", "PPSA09999": "built"})
        self.assertEqual((st["run"]["built"], st["run"]["cancelled"], st["run"]["failed"]), (2, 1, 0))
        ledger = json.loads((self.state / "ledger.json").read_text())
        self.assertEqual(sorted(ledger), ["PPSA01234", "PPSA09999"])
        self.assertFalse((self.state / "failures.json").exists() and json.loads((self.state / "failures.json").read_text()))
        self.assertEqual(sorted(Hook.seen), ["PS5 pkg: built", "PS5 pkg: built"])  # nobody was told about a failure, or about the cancel
        pkgs = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(len(pkgs), 3, pkgs)
        self.assertEqual((existing.read_bytes(), existing.stat().st_mtime), (b"finished long ago", 1_000_000_000))
        self.assert_nothing_left(("PPSA05678",))
        last = json.loads((self.state / "last_run.json").read_text())
        self.assertEqual(sorted(r["status"] for r in last), ["built", "built", "cancelled"])
        rows = PH.read_rows(self.state)                                            # the history has every job of the run, the cancelled one too
        self.assertEqual([r["k"] for r in rows if r["k"] != "job"], ["run", "end"])
        self.assertEqual({r["tid"]: r["status"] for r in rows if r["k"] == "job"}, {"PPSA01234": "built", "PPSA05678": "cancelled", "PPSA09999": "built"})
        built = next(r for r in rows if r.get("tid") == "PPSA01234")
        self.assertTrue(built["bytes"] > 0 and built["level"] == E.DEFAULT_CFG["compression_level"] and built["log"].startswith("pkg/"))
        self.assertTrue((self.logs / built["log"]).exists())
        self.assertEqual(rows[0]["trigger"], "manual")
        self.assertFalse(rows[-1]["interrupted"])
        # a cancelled game is not remembered as a failure: the next scan plans it as a build, not as "failed"
        plan = subprocess.run([sys.executable, "-I", str(ENGINE_DIR / "ps5_pkg_sync.py"), "plan"], capture_output=True, text=True, timeout=120)
        self.assertEqual(plan.returncode, 0, plan.stdout + plan.stderr)
        actions = {p["title_id"]: p["action"] for p in json.loads((self.state / "plan.json").read_text())}
        self.assertEqual(actions, {"PPSA01234": "done", "PPSA05678": "build", "PPSA09999": "done"})

    def test_stop_now_ends_every_process_and_leaves_no_marker_and_no_partial_file(self):
        self.config()
        existing = self.out / "Earlier Game [PPSA00001] [v01.00] [fw9.00].pkg"
        existing.write_bytes(b"finished long ago")
        os.utime(existing, (1_000_000_000, 1_000_000_000))
        proc = self.start(secs=30)
        wait_for(lambda: len(self.builders()) == 3 and len(self.status().get("builds", [])) == 3, 40, "three builds")
        b = self.builders()
        for d in b.values():
            self.assertTrue(alive(d["pid"]) and alive(d["child"]))
        t0 = time.time()
        RC.write_stop(self.state, "now")
        st = self.finish(proc, 30)
        self.assertLess(time.time() - t0, 25)
        for d in b.values():
            wait_for(lambda d=d: not alive(d["pid"]) and not alive(d["child"]), 10, "every process to end")
        self.assertEqual({r["title_id"]: r["status"] for r in st["results"]}, {t: "cancelled" for t, _, _ in GAMES})
        self.assertEqual((st["run"]["cancelled"], st["run"]["built"], st["run"]["failed"]), (3, 0, 0))
        self.assert_nothing_left([t for t, _, _ in GAMES])
        self.assertEqual([p.name for p in self.out.iterdir()], [existing.name])
        self.assertEqual((existing.read_bytes(), existing.stat().st_mtime), (b"finished long ago", 1_000_000_000))
        self.assertFalse((self.state / "failures.json").exists() and json.loads((self.state / "failures.json").read_text()))
        self.assertEqual(Hook.seen, [])
        self.assertFalse((self.state / "stop.request").exists())
        self.assertIn("stop now requested", self.log)
        # nothing could be adopted as a live build by the next run
        for tid, _, _ in GAMES:
            self.assertFalse((self.temp / tid).exists())

    def test_stop_after_current_finishes_the_running_game_starts_nothing_and_can_be_withdrawn(self):
        self.config(max_builds=1, max_compress=1)
        proc = self.start(secs=3)
        wait_for(lambda: len(self.status().get("builds", [])) == 1, 40, "the first build")
        RC.write_stop(self.state, "after_current")
        wait_for(lambda: self.status().get("stop_mode") == "after_current", 10, "the status to say so")
        wait_for(lambda: "waiting for the running games to end" in (self.logs / "pkg_sync.log").read_text(), 10, "the run to hold")
        self.assertEqual(len(self.status()["queue"]), 2)                           # still waiting: nothing was discarded
        self.assertEqual(len(self.status()["builds"]), 1)
        self.assertTrue(RC.withdraw_stop(self.state))
        wait_for(lambda: self.status().get("stop_mode") is None, 10, "the request to be gone")
        st = self.finish(proc, 90)                                                 # taken back: the run goes on to build everything
        self.assertEqual(sorted(r["status"] for r in st["results"]), ["built", "built", "built"])
        # the same request left standing: the running game finishes, the others are not started
        (self.state / "ledger.json").unlink(missing_ok=True)
        for p in self.out.iterdir():
            p.unlink()
        (self.logs / "pkg_sync.log").write_text("")
        proc = self.start(secs=3)
        wait_for(lambda: len(self.status().get("builds", [])) == 1 and not self.status().get("finished"), 40, "the first build of the second run")
        RC.write_stop(self.state, "after_current")
        st = self.finish(proc, 60)
        self.assertEqual([r["status"] for r in st["results"]], ["built"])
        self.assertIn("stop requested: not starting the remaining 2 job(s)", self.log)
        self.assertEqual(len(list(self.out.glob("*.pkg"))), 1)
        self.assertFalse((self.state / "stop.request").exists())
        self.assertFalse((self.out / ".staging").exists())
        left = sorted(p.name for p in self.temp.iterdir())                         # a copy prepared ahead of time is kept for the next run...
        self.assertEqual(left, ["PPSA05678"])
        self.assertTrue((self.temp / "PPSA05678" / ".prepared.json").exists())      # ...ready to be used, and never taken for a running build
        self.assertFalse((self.temp / "PPSA05678" / ".building").exists())

    def test_a_game_waiting_in_the_queue_can_be_taken_out_of_the_run(self):
        self.config(max_builds=1, max_compress=1)
        proc = self.start(secs=6)
        wait_for(lambda: len(self.status().get("builds", [])) == 1, 40, "the first build")
        t0 = time.time()
        RC.request_cancel(self.state, "PPSA09999")
        res = wait_for(lambda: self.result_of("PPSA09999"), 4, "its result, at once")         # not only when the loop gets to it, after the build in progress
        self.assertLess(time.time() - t0, 4)
        self.assertEqual(len(self.status()["builds"]), 1)
        self.assertEqual((res["status"], res["code"], res["phase"]), ("cancelled", "cancelled", "queue"))
        self.assertIn("removed from this run", res["why"])
        self.assertNotIn("PPSA09999", [q["title_id"] for q in self.status().get("queue", [])])
        st = self.finish(proc, 90)
        self.assertEqual({r["title_id"]: r["status"] for r in st["results"]}, {"PPSA01234": "built", "PPSA05678": "built", "PPSA09999": "cancelled"})
        self.assertEqual(st["run"]["total"], 3)
        self.assertEqual(st["run"]["done"], 3)
        self.assertEqual(sorted(json.loads((self.state / "ledger.json").read_text())), ["PPSA01234", "PPSA05678"])
        self.assertEqual(self.store_failures(), {})
        self.assert_nothing_left(["PPSA09999"])

    def store_failures(self):
        p = self.state / "failures.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def test_the_engines_log_is_rotated_and_old_logs_are_pruned_when_a_run_starts(self):
        self.config(keep_logs_days=30, keep_logs_gb=0)
        old = self.logs / "pkg" / "20250101-031500_PPSA00001.log"
        kept_for_a_failure = self.logs / "pkg" / "20250102-031500_PPSA00002.log"
        recent = self.logs / "pkg" / "20261120-031500_PPSA00003.log"
        for p, days in ((old, 200), (kept_for_a_failure, 200), (recent, 2)):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("log")
            os.utime(p, (time.time() - days * 86400,) * 2)
        F.Store(self.state / "failures.json").update(lambda d: d.__setitem__("PPSA00002", dict(title_id="PPSA00002", log="pkg/20250102-031500_PPSA00002.log", state="failed")))
        (self.logs / "pkg_sync.log").write_text("earlier line\n" * 500)
        env = dict(os.environ, PS5PKG_LOG_ROTATE_BYTES="1000", FAKE_SECS="1")
        r = subprocess.run([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), "run"], env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(kept_for_a_failure.exists() and recent.exists())                       # a failing game's log and a recent one stay
        rotated = [p for p in self.logs.glob("pkg_sync.*.log")]
        self.assertEqual(len(rotated), 1)
        self.assertEqual(rotated[0].read_text(), "earlier line\n" * 500)
        now = (self.logs / "pkg_sync.log").read_text()
        self.assertIn("the log had grown past", now)
        self.assertIn("old logs removed (1 files", now)
        self.assertNotIn("earlier line", now)

    def test_requests_left_over_from_before_do_not_affect_the_next_run(self):
        self.config()
        RC.write_stop(self.state, "now")
        RC.request_cancel(self.state, "PPSA01234")
        proc = self.start(secs=1)
        st = self.finish(proc, 90)
        self.assertEqual(sorted(r["status"] for r in st["results"]), ["built", "built", "built"])
        self.assertEqual(RC.pending_cancels(self.state), [])


if __name__ == "__main__":
    unittest.main()
