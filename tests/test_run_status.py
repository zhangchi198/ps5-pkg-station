"""status.json while a run works: who started it and when, how many games, the builds with their stage, progress,
timings and estimate, what is being prepared or copied, and what the ledger learns for next time. One test runs the
real engine against a stand-in for UltraPack that prints its progress lines; the others drive RunStatus directly.
Standard library only."""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from support import ENGINE_DIR, E, StateCase, make_env

import ps5_progress as PG  # noqa: E402

GIB = 2 ** 30

FAKE_ULTRAPACK = r'''
import sys, time
from pathlib import Path
args = sys.argv[1:]
if "--fpkg-validate" in args:
    print("summary: 7 ok, 0 failed")
    sys.exit(0)
stage = Path(args[1])
stage.mkdir(parents=True, exist_ok=True)
nap = float(__import__("os").environ.get("FAKE_NAP", "0.25"))
def out(text):
    print(text, flush=True)
def bar(pct, label):
    return "[" + "#" * (pct // 5) + "-" * (20 - pct // 5) + f"] {pct}% {label}"
out("planning nwonly inner image: 10 files, 1,000,000,000 uncompressed bytes")
out("[stage 1/5] Building and compressing the inner pfs_image.dat (Kraken level 4)...")
t0 = time.time()
for pct in range(5, 101, 5):
    time.sleep(nap)
    el = max(0.01, time.time() - t0)
    got = 1e9 * pct / 100
    rate = got / el
    out(bar(pct, f"inner image (Kraken) @ {rate / 1e6:.1f} MB/s ETA {int((1e9 - got) / rate)}s"))
out("[stage 1/5] Inner image complete")
out("[stage 2/5] Building the NAPS tables"); time.sleep(nap); out("[stage 2/5] NAPS tables complete")
out("[stage 3/5] Writing the outer PFS")
for pct in (25, 50, 75, 100):
    time.sleep(nap); out(bar(pct, "outer PFS @ 300.0 MB/s ETA 1s"))
out("[stage 3/5] outer PFS complete")
out("[stage 4/5] Building the CNT image"); time.sleep(nap); out("[stage 4/5] CNT image complete")
out("[stage 5/5] Finalizing the package")
for pct in (50, 75, 100):
    time.sleep(nap); out(bar(pct, "finalize (FIH digests)"))
out("build finished")
(stage / "Example Game.pkg").write_bytes(b"x" * 2048)
out("[SUCCESS] fPKG built.")
'''


class FakeFuture:
    def __init__(self, done=False, value=None):
        self._done, self._value = done, value

    def done(self):
        return self._done

    def result(self):
        return self._value


class FakeProc:
    def __init__(self, alive=True):
        self.alive = alive

    def poll(self):
        return None if self.alive else 0


def job(tid="PPSA01234", size=100 * GIB, **kw):
    return dict(title_id=tid, title="Example Game", name=f"Example Game [{tid}] [v01.00].pkg", size=size, version="01.00", **kw)


class StatusCase(StateCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.dir / "env")
        self.cfg = {**copy.deepcopy(E.DEFAULT_CFG), **self.env, "ssd_reserve_gb": 0, "compression_level": 4}
        Path(self.cfg["temp"]).mkdir(parents=True, exist_ok=True)

    def status(self):
        return json.loads((self.state / "status.json").read_text())


class Snapshots(StatusCase):
    """RunStatus fed with stand-ins for what the queue holds."""

    def running_build(self, tid="PPSA01234", size=100 * GIB, at=1000.0, lines=None, now=None, alive=True):
        logf = self.logs / "pkg" / f"x_{tid}.log"
        logf.parent.mkdir(exist_ok=True)
        logf.write_text("")
        tr = PG.LogTracker(at)
        tr.feed("\n".join(lines or ["[stage 1/5] Building and compressing the inner pfs_image.dat (Kraken level 4)..."]), at + 10)
        return dict(job=job(tid, size), t0=at, proc=FakeProc(alive), logf=logf, tracker=tr, prep={}, stage=self.dir)

    def test_a_run_before_its_queue_exists_is_scanning(self):
        rs = E.RunStatus(self.cfg, "scheduled")
        rs.write()
        st = self.status()
        self.assertEqual((st["run"]["phase"], st["run"]["trigger"], st["run"]["total"], st["run"]["done"]), ("scanning", "scheduled", 0, 0))
        self.assertAlmostEqual(st["run"]["started"], time.time(), delta=5)
        self.assertEqual((st["queue"], st["preparing"], st["builds"], st["results"]), ([], [], [], []))
        self.assertTrue(st["pid"])
        self.assertNotIn("finished", st)

    def test_a_bad_trigger_is_manual(self):
        self.assertEqual(E.RunStatus(self.cfg, "whatever").trigger, "manual")

    def test_a_build_carries_its_stage_progress_timings_and_estimate(self):
        rs = E.RunStatus(self.cfg, "manual")
        b = self.running_build(at=1000.0)
        b["tracker"].feed("[#####---------------] 25% inner image (Kraken) @ 60.0 MB/s ETA 1500s", 1500.0)
        rs.plan([b["job"]], [])
        rs.bind(todo=[b["job"]], preparing={}, done_idx={0}, all_builds=[b], finishing=[FakeFuture()], results=[])
        with mock.patch.object(E.PG, "MIN_SIGNAL_SECS", 120):
            snap = rs.snapshot(now=1500.0)
        row = snap["builds"][0]
        self.assertEqual((row["title_id"], row["stage"], row["stage_name"], row["pct"], row["phase"]), ("PPSA01234", 1, "Compressing", 25, "building"))
        self.assertEqual(row["stage_text"], "Building and compressing the inner pfs_image.dat")            # UltraPack's own words, for a tooltip
        self.assertAlmostEqual(row["progress"], PG.normalise(PG.DEFAULT_SHARES, with_copy=False)["1"] * 0.25, places=3)     # no copy step: the package is written in place
        self.assertEqual((row["elapsed"], row["rate"]), (500.0, 60e6))
        self.assertGreater(row["remaining"], 1500)
        self.assertEqual(row["stages"], [dict(stage=1, started=1000.0, ended=None)])
        self.assertEqual((snap["run"]["total"], snap["run"]["done"]), (1, 0))
        self.assertGreater(snap["run"]["remaining"], 1500)
        self.assertGreater(snap["run"]["eta_at"], 1500.0 + 1500)
        self.assertEqual(snap["queue"], [])

    def test_a_build_with_less_than_two_minutes_of_data_is_still_estimating(self):
        rs = E.RunStatus(self.cfg)
        b = self.running_build(at=1000.0)
        b["tracker"].feed("[#---------------------] 3% inner image (Kraken) @ 60.0 MB/s ETA 1500s", 1050.0)
        rs.plan([b["job"]], [])
        rs.bind(todo=[b["job"]], preparing={}, done_idx={0}, all_builds=[b], finishing=[FakeFuture()], results=[])
        snap = rs.snapshot(now=1060.0)
        self.assertIsNone(snap["builds"][0]["remaining"])
        self.assertIsNone(snap["run"]["remaining"])
        self.assertGreater(snap["builds"][0]["progress"], 0)

    def test_what_is_waiting_and_what_is_done_are_counted_and_the_run_estimate_includes_the_queue(self):
        rs = E.RunStatus(self.cfg)
        b = self.running_build(at=1000.0, size=100 * GIB)
        b["tracker"].feed("[#####---------------] 25% inner image (Kraken) @ 60.0 MB/s ETA 1500s", 1500.0)
        waiting, other = job("PPSA05678", 200 * GIB), job("PPSA09999", 50 * GIB)
        finished = dict(job("PPSA00001", 10 * GIB), status="built", build_min=20, size=10 * GIB, at=900.0)
        failed = dict(job("PPSA00002", 10 * GIB), status="failed", why="x", at=950.0)
        todo = [finished, failed, b["job"], waiting, other]
        rs.plan(todo, [])
        rs.bind(todo=todo, preparing={}, done_idx={0, 1, 2}, all_builds=[b], finishing=[FakeFuture()], results=[failed, dict(finished)])
        with mock.patch.object(E.PG, "MIN_SIGNAL_SECS", 120):
            snap = rs.snapshot(now=1500.0)
        run = snap["run"]
        self.assertEqual((run["total"], run["done"], run["built"], run["failed"], run["copied"], run["deferred"]), (5, 2, 1, 1, 0, 0))
        self.assertEqual([q["title_id"] for q in snap["queue"]], ["PPSA05678", "PPSA09999"])
        self.assertEqual(run["queue_bytes"], 250 * GIB)
        with_queue = run["remaining"]
        rs2 = E.RunStatus(self.cfg)
        b2 = self.running_build(at=1000.0, size=100 * GIB)
        b2["tracker"].feed("[#####---------------] 25% inner image (Kraken) @ 60.0 MB/s ETA 1500s", 1500.0)
        rs2.plan([b2["job"]], [])
        rs2.bind(todo=[b2["job"]], preparing={}, done_idx={0}, all_builds=[b2], finishing=[FakeFuture()], results=[])
        with mock.patch.object(E.PG, "MIN_SIGNAL_SECS", 120):
            alone = rs2.snapshot(now=1500.0)["run"]["remaining"]
        # the 200 GiB game starts at once (a second compression slot) at this build's pace (2567 s for 100 GiB); the 50 GiB one waits for a slot
        self.assertAlmostEqual(with_queue, 2 * (500 + alone), delta=60)
        self.assertGreater(with_queue, alone)
        self.assertGreater(run["progress"], 0.0)
        self.assertLess(run["progress"], 1.0)

    def test_the_run_has_no_estimate_for_a_queue_nothing_has_shown_a_pace_for(self):
        rs = E.RunStatus(self.cfg)
        todo = [job("PPSA01234"), job("PPSA05678")]
        rs.plan(todo, [])
        rs.bind(todo=todo, preparing={}, done_idx=set(), all_builds=[], finishing=[], results=[])
        snap = rs.snapshot(now=1000.0)
        self.assertIsNone(snap["run"]["remaining"])
        self.assertEqual(len(snap["queue"]), 2)

    def test_earlier_builds_in_the_ledger_give_the_queue_a_pace_before_anything_runs(self):
        ledger = {"PPSA00001": dict(stage_secs={"1": 1200, "2": 20, "3": 400, "4": 20, "5": 160, "validate": 100, "copy": 100}, source_bytes=100 * GIB, level=4, prep_min=10)}
        rs = E.RunStatus(self.cfg, ledger=ledger)
        todo = [job("PPSA01234", 100 * GIB)]
        rs.plan(todo, [])
        rs.bind(todo=todo, preparing={}, done_idx=set(), all_builds=[], finishing=[], results=[])
        r = rs.snapshot(now=1000.0)["run"]["remaining"]
        self.assertAlmostEqual(r, 600 + 2000, delta=5)                               # its prepare, then a build at the pace of the one in the ledger

    def test_a_stop_request_takes_what_is_waiting_out_of_the_estimate(self):
        def eta(stopping):
            rs = E.RunStatus(self.cfg)
            b = self.running_build(at=1000.0, size=100 * GIB)
            b["tracker"].feed("[#####---------------] 25% inner image (Kraken) @ 60.0 MB/s ETA 1500s", 1500.0)
            todo = [b["job"], job("PPSA05678", 400 * GIB)]
            rs.plan(todo, [])
            rs.bind(todo=todo, preparing={}, done_idx={0}, all_builds=[b], finishing=[FakeFuture()], results=[])
            with mock.patch.object(E.PG, "MIN_SIGNAL_SECS", 120), mock.patch.object(E, "stop_requested", return_value=stopping):
                return rs.snapshot(now=1500.0)["run"]["remaining"]
        self.assertGreater(eta(False), eta(True) + 3600)
        sh = PG.normalise(PG.DEFAULT_SHARES, with_copy=False)
        self.assertAlmostEqual(eta(True), 1500 + 2000 / sh["1"] * (1 - sh["1"]), delta=30)          # only the build in flight

    def test_a_status_that_cannot_be_written_is_said_once_not_every_tick(self):
        import contextlib
        import io
        rs = E.RunStatus(self.cfg)
        out = io.StringIO()
        with mock.patch.object(rs, "snapshot", side_effect=ValueError("boom")), contextlib.redirect_stdout(out):
            for _ in range(5):
                rs.write()
        self.assertEqual(out.getvalue().count("status.json could not be written: ValueError: boom"), 1)

    def test_the_bar_of_the_run_only_grows(self):
        rs = E.RunStatus(self.cfg)
        b = self.running_build(at=1000.0, size=100 * GIB)
        todo = [b["job"], job("PPSA05678", 100 * GIB)]
        rs.plan(todo, [])
        rs.bind(todo=todo, preparing={}, done_idx={0}, all_builds=[b], finishing=[FakeFuture()], results=[])
        seen = []
        for pct, t in [(10, 1100), (50, 2000), (30, 2100)]:                           # a progress line that goes back (a new log)
            b["tracker"].feed(f"[###] {pct}% inner image (Kraken) @ 60.0 MB/s ETA 1500s", t)
            seen.append(rs.snapshot(now=t)["run"]["progress"])
        self.assertEqual(seen, sorted(seen))

    def test_a_game_being_prepared_shows_bytes_percentage_and_rate(self):
        rs = E.RunStatus(self.cfg)
        j = job("PPSA01234", 1000)
        jd = Path(self.cfg["temp"]) / "PPSA01234"
        (jd / "src").mkdir(parents=True)
        fut = FakeFuture(done=False)
        rs.plan([j], [])
        rs.bind(todo=[j], preparing={0: (900.0, fut)}, done_idx=set(), all_builds=[], finishing=[], results=[])
        (jd / "src" / "a.bin").write_bytes(b"x" * 100)
        first = rs.snapshot(now=1000.0)["preparing"][0]
        self.assertEqual((first["title_id"], first["bytes"], first["size"], first["pct"], first["ps4"]), ("PPSA01234", 100, 1000, 10, False))
        self.assertIsNone(first["rate"])                                              # one point is no rate
        (jd / "src" / "b.bin").write_bytes(b"x" * 400)
        with mock.patch.object(E, "STATUS_INTERVAL", 5.0):
            second = rs.snapshot(now=1010.0)["preparing"][0]
        self.assertEqual((second["bytes"], second["pct"]), (500, 50))
        self.assertAlmostEqual(second["rate"], 40.0)
        self.assertAlmostEqual(second["remaining"], 500 / 40.0)
        snap = rs.snapshot(now=1011.0)
        self.assertEqual(snap["queue"], [])                                           # a game being prepared is not "up next"

    def test_a_ps4_copy_in_progress_is_in_the_preparing_list_and_the_waiting_ones_in_the_queue(self):
        rs = E.RunStatus(self.cfg)
        a, b = dict(job("CUSA11111", 1000), ps4=True), dict(job("CUSA22222", 3000), ps4=True)
        rs.plan([], [a, b])
        rs.ps4_begin(a)
        stage = Path(self.cfg["output"]) / ".staging" / "CUSA11111-ps4"
        (stage / "x" / "0").mkdir(parents=True)
        (stage / "x" / "0" / "p.pkg").write_bytes(b"x" * 250)
        (stage / "nested" / "0").mkdir(parents=True)
        (stage / "nested" / "0" / "big.zip").write_bytes(b"x" * 900)            # an opened nested archive is not copied bytes
        snap = rs.snapshot(now=1000.0)
        row = snap["preparing"][0]
        self.assertEqual((row["title_id"], row["ps4"], row["bytes"], row["pct"]), ("CUSA11111", True, 250, 25))
        self.assertEqual([q["title_id"] for q in snap["queue"]], ["CUSA22222"])
        self.assertEqual((snap["run"]["total"], snap["run"]["done"]), (2, 0))
        rs.ps4_end(dict(a, status="copied", copy_secs=10, at=1010.0))
        snap = rs.snapshot(now=1011.0)
        self.assertEqual((snap["preparing"], snap["run"]["copied"], snap["run"]["done"]), ([], 1, 1))
        self.assertEqual(snap["results"][0]["status"], "copied")                      # a PS4 copy is part of "This run"

    def test_the_ps4_loop_tells_the_status_what_it_is_copying_and_what_it_learned(self):
        rs = E.RunStatus(self.cfg)
        j = dict(job("CUSA11111", 1000), ps4=True, source="/x.zip", dest_dir=str(Path(self.cfg["output"]) / "PS4" / "x"), files=[])
        rs.plan([], [j])
        seen, ledger = [], {}

        def copying(cfg, jb, log):
            seen.append(rs.snapshot())
            return dict(output=j["dest_dir"], files=["x.pkg"], bytes=1000, validate="CRC verified by 7-Zip: 1 package(s)")
        with mock.patch.object(E, "copy_ps4", copying), mock.patch.object(E, "notify", lambda *a: None):
            res = E.run_ps4(self.cfg, [j], lambda m: None, self.state / "ledger.json", ledger, rs=rs)
        self.assertEqual([(p["title_id"], p["ps4"]) for p in seen[0]["preparing"]], [("CUSA11111", True)])
        self.assertEqual((seen[0]["run"]["total"], seen[0]["run"]["done"]), (1, 0))
        self.assertIsNone(rs.ps4_current)
        self.assertEqual(rs.ps4_results, res)
        self.assertEqual((res[0]["status"], res[0]["size"]), ("copied", 1000))
        self.assertGreater(res[0]["at"], 0)
        entry = ledger["CUSA11111"]
        self.assertEqual(entry["source_bytes"], 1000)
        self.assertAlmostEqual(entry["built_ts"], time.time(), delta=60)
        self.assertGreaterEqual(entry["copy_secs"], 0)
        after = rs.snapshot()
        self.assertEqual((after["preparing"], after["run"]["copied"], after["run"]["done"]), ([], 1, 1))

    def test_a_finished_build_is_shown_validating_then_copying_with_its_bytes(self):
        rs = E.RunStatus({**self.cfg, "build_on_ssd": True})
        b = self.running_build(alive=False)
        b["tracker"].feed("[stage 1/5] Inner image complete\n[SUCCESS] fPKG built.", 1900.0)
        todo = [b["job"]]
        rs.plan(todo, [])
        rs.bind(todo=todo, preparing={}, done_idx={0}, all_builds=[b], finishing=[FakeFuture()], results=[])
        try:
            E.FINISH["PPSA01234"] = dict(phase="validating", frac=0.0)
            v = rs.snapshot(now=2000.0)["builds"][0]
            self.assertEqual((v["phase"], v["stage_name"]), ("validating", "Checking the package"))
            E.FINISH["PPSA01234"] = dict(phase="copying", done=20 * GIB, total=80 * GIB, frac=0.125, rate=200e6)
            c = rs.snapshot(now=2010.0)["builds"][0]
            self.assertEqual((c["phase"], c["bytes"], c["total_bytes"], c["rate"], c["stage_name"]), ("copying", 20 * GIB, 80 * GIB, 200e6, "Copying to the output folder"))
            self.assertGreater(c["progress"], v["progress"])
            self.assertLess(c["progress"], 1.0)
        finally:
            E.FINISH.pop("PPSA01234", None)

    def test_the_final_status_says_how_the_run_went(self):
        rs = E.RunStatus(self.cfg, "scheduled")
        rs.plan([job("PPSA01234"), job("PPSA05678")], [])
        rs.finish([dict(job("PPSA01234"), status="built", at=5.0), dict(job("PPSA05678"), status="failed", why="x", at=6.0)])
        st = self.status()
        self.assertTrue(st["finished"])
        self.assertIsNone(st["pid"])
        run = st["run"]
        self.assertEqual((run["trigger"], run["total"], run["done"], run["built"], run["failed"], run["phase"], run["progress"]), ("scheduled", 2, 2, 1, 1, "finished", 1.0))
        self.assertGreaterEqual(run["finished"], run["started"])
        self.assertNotIn("interrupted", run)
        rs.write()                                                                    # the writer thread's last tick must not undo it
        self.assertTrue(self.status()["finished"])
        rs2 = E.RunStatus(self.cfg)
        rs2.finish([], interrupted=True)
        self.assertTrue(self.status()["run"]["interrupted"])

    def test_the_status_of_a_run_with_nothing_wrong_with_it_is_valid_json_even_when_written_often(self):
        rs = E.RunStatus(self.cfg)
        for _ in range(50):
            rs.write()
            json.loads((self.state / "status.json").read_text())


class RealRun(StatusCase):
    """The real engine, with a stand-in for UltraPack that prints the lines UltraPack prints."""

    def setUp(self):
        super().setUp()
        if not shutil.which("nice"):
            self.skipTest("needs nice(1)")
        (Path(self.env["ultrapack_src"]) / "backend" / "cli.py").write_text(FAKE_ULTRAPACK)
        self.write_cfg({**copy.deepcopy(E.DEFAULT_CFG), **self.env, "ssd_reserve_gb": 0, "compression_level": 4, "cpu_count": 1, "nice": 5})

    def run_and_watch(self, *extra, nap="0.25"):
        env = dict(os.environ, PS5PKG_STATUS_INTERVAL="0.2", PS5PKG_ETA_MIN_SECS="1", FAKE_NAP=nap)
        proc = subprocess.Popen([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), "run", *extra], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        snaps, deadline = [], time.time() + 90
        while proc.poll() is None and time.time() < deadline:
            try:
                snaps.append(json.loads((self.state / "status.json").read_text()))
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        out = proc.communicate(timeout=60)[0]
        self.assertEqual(proc.returncode, 0, out)
        return snaps, out

    def test_a_scheduled_run_reports_itself_its_build_and_what_it_learned(self):
        snaps, out = self.run_and_watch("--trigger", "scheduled")
        self.assertTrue(snaps)
        first = snaps[0]
        self.assertEqual(first["run"]["trigger"], "scheduled")
        self.assertIn(first["run"]["phase"], ("scanning", "running"))
        building = [s for s in snaps if s.get("builds")]
        self.assertTrue(building, "no status with a build in it was seen")
        rows = [s["builds"][0] for s in building if s["builds"][0]["phase"] == "building"]
        self.assertGreater(len(rows), 4)
        self.assertEqual([r["title_id"] for r in rows][:1], ["PPSA01234"])
        stages = [r["stage"] for r in rows]
        self.assertEqual(stages, sorted(stages))                                      # never back to an earlier stage
        bars = [r["progress"] for r in rows]
        self.assertEqual(bars, sorted(bars))
        self.assertGreater(bars[-1], 0.5)
        self.assertEqual(rows[0]["stage_name"], "Compressing")
        self.assertTrue(any(r["rate"] for r in rows))
        self.assertTrue(any(r["remaining"] is not None for r in rows), "an estimate should appear once the log has a rate")
        self.assertTrue(any(s["run"]["remaining"] is not None for s in building))
        self.assertTrue(any(len(r["stages"]) > 1 for r in rows), "stage timings should grow with the build")
        for s in building:
            self.assertEqual((s["run"]["total"], s["run"]["trigger"]), (1, "scheduled"))
        last = json.loads((self.state / "status.json").read_text())
        self.assertTrue(last["finished"])
        self.assertEqual((last["run"]["built"], last["run"]["done"], last["run"]["total"], last["run"]["progress"]), (1, 1, 1, 1.0))
        res = last["results"][0]
        self.assertEqual((res["status"], res["title_id"]), ("built", "PPSA01234"))
        self.assertGreater(res["at"], last["run"]["started"])
        self.assertTrue(res["size"] > 0)
        # what the next estimate learns from it
        entry = json.loads((self.state / "ledger.json").read_text())["PPSA01234"]
        self.assertEqual(sorted(entry["stage_secs"]), ["1", "2", "3", "4", "5", "copy", "validate"])
        self.assertGreater(entry["stage_secs"]["1"], entry["stage_secs"]["2"])
        self.assertEqual(entry["source_bytes"], res["size"])
        self.assertAlmostEqual(entry["built_ts"], time.time(), delta=120)
        h = PG.history(json.loads((self.state / "ledger.json").read_text()), 4)
        self.assertEqual(h["builds"], 1)
        self.assertGreater(h["shares"]["1"], 0.3)

    def test_no_trigger_means_manual(self):
        snaps, _ = self.run_and_watch(nap="0.02")
        self.assertEqual(json.loads((self.state / "status.json").read_text())["run"]["trigger"], "manual")

    def test_a_run_with_nothing_to_do_still_ends_with_a_finished_status(self):
        (self.state / "ledger.json").write_text(json.dumps({"PPSA01234": dict(version="01.002.003", output=str(Path(self.env["output"])), built="2026-01-01 00:00")}))
        snaps, out = self.run_and_watch(nap="0.02")
        last = json.loads((self.state / "status.json").read_text())
        self.assertTrue(last["finished"])
        self.assertEqual((last["run"]["total"], last["run"]["done"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
