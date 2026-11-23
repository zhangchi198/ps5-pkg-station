"""Progress and estimates (app/engine/ps5_progress.py): what is read from UltraPack's log, the weighted bar, the
estimates and when they are shown, the history, the run simulation, and a replay of a whole timed build log.
Standard library only."""
from __future__ import annotations

import os
import unittest
from pathlib import Path

from support import ROOT, StateCase

import ps5_progress as P  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SH = P.normalise(P.DEFAULT_SHARES)
SH0 = P.normalise(P.DEFAULT_SHARES, with_copy=False)       # a build that is written where it ends up: no copy step
GIB = 2 ** 30

KRAKEN = "[stage 1/5] Building and compressing the inner pfs_image.dat (Kraken level 4)..."


def bar(pct, label):
    return f"[{'#' * (pct // 5)}{'-' * (20 - pct // 5)}] {pct}% {label}"


def tracker_at(stage, part, pct, *, t0=0.0, rate=None, eta=None, now=1000.0, exact=True):
    """A tracker told that a build is in `stage`, `part` at `pct` (and UltraPack's own rate in MB/s / ETA when given)."""
    tr = P.LogTracker(t0, exact=exact)
    lines = [KRAKEN] if stage == 1 else [f"[stage {n}/5] step {n}" for n in range(1, stage + 1)]
    if part:
        extra = f" @ {rate} MB/s ETA {eta}s" if rate is not None else ""
        lines.append(bar(pct, f"{ {'inner image': 'inner image (Kraken)', 'outer PFS': 'outer PFS', 'finalize': 'finalize (FIH digests)'}[part]}{extra}"))
    tr.feed("\n".join(lines), now)
    return tr


class Reading(StateCase):
    def test_the_lines_of_a_build_are_understood(self):
        tr = P.LogTracker(100.0)
        tr.feed("planning nwonly inner image: 5,350 files, 150,000,000,000 uncompressed bytes\n" + KRAKEN, 110.0)
        self.assertEqual((tr.stage, tr.stage_text, tr.total_bytes), (1, "Building and compressing the inner pfs_image.dat", 150_000_000_000))
        tr.feed(bar(61, "inner image (Kraken) @ 71.4 MB/s ETA 1234s"), 500.0)
        self.assertEqual((tr.part, tr.pct, tr.rate, tr.eta, tr.bar_at), ("inner image", 61, 71.4e6, 1234, 500.0))
        self.assertEqual(tr.stage_fraction(), 0.61)

    def test_stage_changes_are_timed_and_a_stage_is_complete_when_it_says_so(self):
        tr = P.LogTracker(0.0)
        tr.feed(KRAKEN, 1)
        tr.feed("[stage 1/5] Inner image complete\n[stage 2/5] Building the NAPS tables", 900)
        tr.feed("[stage 2/5] NAPS tables complete\n[stage 3/5] Preparing the fPKG identity (1/2)", 960)
        self.assertEqual(tr.stage, 3)
        self.assertEqual(tr.stage_text, "Preparing the fPKG identity")
        self.assertEqual({n: tr.ended[n] for n in (1, 2)}, {1: 900, 2: 960})
        self.assertEqual(tr.started[3], 960)
        self.assertEqual(tr.stage_secs(), {"1": 900.0, "2": 60.0})
        self.assertEqual([t["stage"] for t in tr.timings()], [1, 2, 3])
        self.assertIsNone(tr.timings()[-1]["ended"])

    def test_each_stage_has_its_own_meter_or_none(self):
        self.assertEqual(tracker_at(1, "inner image", 40).stage_fraction(), 0.4)
        self.assertEqual(tracker_at(3, "outer PFS", 25).stage_fraction(), 0.25)
        self.assertEqual(tracker_at(5, "finalize", 75).stage_fraction(), 0.5)         # UltraPack's meter starts at 50 %
        self.assertEqual(tracker_at(5, "finalize", 50).stage_fraction(), 0.0)
        self.assertEqual(tracker_at(2, "", 0).stage_fraction(), 0.0)
        self.assertEqual(tracker_at(4, "", 0).stage_fraction(), 0.0)
        self.assertEqual(tracker_at(1, "outer PFS", 90).stage_fraction(), 0.0)         # a meter of another part says nothing
        done = tracker_at(2, "", 0)
        done.feed("[stage 2/5] NAPS tables complete", 5)
        self.assertEqual(done.stage_fraction(), 1.0)

    def test_the_end_of_the_log(self):
        tr = tracker_at(5, "finalize", 100)
        tr.feed("build finished\n[SUCCESS] fPKG built.", 2000)
        self.assertTrue(tr.finished)
        self.assertEqual(tr.stage_fraction(), 1.0)
        self.assertEqual(sorted(tr.ended), [1, 2, 3, 4, 5])

    def test_lines_that_are_not_progress_are_ignored(self):
        tr = P.LogTracker(0.0)
        tr.feed("hello\n\n[WARN] something 50% of nothing\nkraken level 4: 10% of /x\n" + bar(100, "validate checklist"), 5)
        self.assertEqual((tr.stage, tr.part, tr.pct, tr.rate), (1, "", 0, None))

    def test_a_percentage_cannot_leave_the_range(self):
        tr = tracker_at(1, "inner image", 250)
        self.assertEqual(tr.pct, 100)

    def test_a_log_is_read_from_where_it_was_left(self):
        log = self.dir / "build.log"
        tr = P.LogTracker(0.0)
        self.assertFalse(tr.read(log, 1))                                              # not there yet
        log.write_text(KRAKEN + "\n" + bar(10, "inner image (Kraken) @ 50.0 MB/s ETA 600s")[:30])
        self.assertTrue(tr.read(log, 2))
        self.assertEqual((tr.stage, tr.pct), (1, 0))                                   # the half line waits for its end
        with open(log, "a") as fh:
            fh.write(bar(10, "inner image (Kraken) @ 50.0 MB/s ETA 600s")[30:] + "\r" + bar(11, "inner image (Kraken) @ 50.0 MB/s ETA 590s") + "\r")
        self.assertTrue(tr.read(log, 3))
        self.assertEqual((tr.pct, tr.eta), (11, 590))
        self.assertFalse(tr.read(log, 4))                                              # nothing new
        log.write_text("[stage 2/5] Building the NAPS tables\n")                       # replaced by a shorter file
        self.assertTrue(tr.read(log, 5))
        self.assertEqual(tr.stage, 2)

    def test_a_build_this_process_did_not_start_is_read_from_its_tail_and_not_trusted_for_timings(self):
        log = self.dir / "tail.log"
        log.write_bytes(b"x" * (3 << 20) + b"\n[stage 3/5] Writing the outer PFS\n" + bar(40, "outer PFS @ 300.0 MB/s ETA 90s").encode() + b"\n")
        tr = P.LogTracker(0.0, exact=False)
        tr.read(log, 50.0)
        self.assertEqual((tr.stage, tr.pct, tr.eta), (3, 40, 90))
        self.assertEqual(tr.stage_secs(), {})


class Weights(unittest.TestCase):
    def test_the_shares_add_up_and_the_first_stage_is_most_of_the_build(self):
        self.assertAlmostEqual(sum(SH.values()), 1.0)
        self.assertGreater(SH["1"], 0.5)
        self.assertEqual(P.normalise({}, with_copy=False)["copy"], 0.0)
        self.assertAlmostEqual(sum(P.normalise({}, with_copy=False).values()), 1.0)
        self.assertAlmostEqual(sum(P.normalise({"1": 5, "2": 1}).values()), 1.0)

    def test_a_build_in_stage_one_at_61_percent_is_not_a_12_percent_bar(self):
        tr = tracker_at(1, "inner image", 61)
        got = P.estimate_build(tr, SH, 1000.0)["progress"]
        self.assertAlmostEqual(got, SH["1"] * 0.61)
        self.assertGreater(got, 0.4)                                                  # the old bar said 12 %

    def test_the_bar_only_moves_forward_through_the_stages(self):
        seen = []
        for stage, part, pct in [(1, "inner image", 99), (2, "", 0), (3, "outer PFS", 1), (3, "outer PFS", 99), (4, "", 0), (5, "finalize", 50), (5, "finalize", 100)]:
            seen.append(P.estimate_build(tracker_at(stage, part, pct), SH, 1000.0)["progress"])
        self.assertEqual(seen, sorted(seen))
        self.assertAlmostEqual(P.build_progress_fraction(SH, 5, 1.0, finished=True), sum(SH[k] for k in ("1", "2", "3", "4", "5")))

    def test_checking_and_copying_come_after_the_five_stages(self):
        base = sum(SH[k] for k in ("1", "2", "3", "4", "5"))
        a, b, c = (P.finishing_progress(SH, "validating"), P.finishing_progress(SH, "copying", 0.0), P.finishing_progress(SH, "copying", 1.0))
        self.assertTrue(base < a < b + 1e-9 <= c + 1e-9)
        self.assertAlmostEqual(c, 1.0)


class Estimates(unittest.TestCase):
    def test_nothing_is_said_in_the_first_two_minutes(self):
        tr = tracker_at(1, "inner image", 5, rate=60, eta=2000, now=60.0)
        est = P.estimate_build(tr, SH, 60.0)
        self.assertIsNone(est["remaining"])
        self.assertEqual(est["elapsed"], 60.0)
        self.assertIsNotNone(P.estimate_build(tr, SH, 125.0)["remaining"])

    def test_nothing_is_said_without_a_signal_however_long_it_took(self):
        tr = tracker_at(1, "", 0)                                                     # no progress line at all yet
        self.assertIsNone(P.estimate_build(tr, SH, 3000.0)["remaining"])

    def test_the_estimate_is_the_log_s_own_rate_plus_what_the_other_stages_need_in_proportion(self):
        tr = tracker_at(1, "inner image", 50, rate=60, eta=1000, now=1000.0)           # stage 1 will have taken 2000 s
        est = P.estimate_build(tr, SH, 1000.0)
        scale = 2000 / SH["1"]
        self.assertAlmostEqual(est["remaining"], 1000 + scale * (1 - SH["1"]), places=3)
        self.assertEqual(est["rate"], 60e6)

    def test_it_ages_the_logs_eta_while_nothing_new_is_written(self):
        tr = tracker_at(1, "inner image", 50, rate=60, eta=1000, now=1000.0)
        a = P.estimate_build(tr, SH, 1000.0)["remaining"]
        b = P.estimate_build(tr, SH, 1060.0)["remaining"]
        self.assertGreater(a - b, 0)                                                   # counts down, from what the log said
        self.assertLess(a - b, 80)

    def test_without_an_eta_in_the_log_the_progress_over_time_is_used(self):
        tr = P.LogTracker(0.0)
        tr.feed(KRAKEN, 0)
        for i in range(1, 11):                                                         # 1 % every 30 s
            tr.feed(bar(i, "inner image (Kraken)"), i * 30.0)
        self.assertAlmostEqual(tr.local_remaining(300.0), 90 / (1 / 30), places=0)
        self.assertIsNotNone(P.estimate_build(tr, SH, 300.0)["remaining"])

    def test_a_finished_stage_one_fixes_the_scale_for_the_rest(self):
        tr = P.LogTracker(0.0)
        tr.feed(KRAKEN, 0)
        tr.feed("[stage 1/5] Inner image complete\n[stage 2/5] Building the NAPS tables", 2000.0)
        est = P.estimate_build(tr, SH, 2005.0)
        scale = 2000 / SH["1"]
        self.assertAlmostEqual(est["remaining"], scale * (1 - SH["1"]) * 1.0, delta=scale * SH["2"] * 0.1)

    def test_a_build_seen_from_the_middle_uses_its_average_pace(self):
        tr = tracker_at(3, "outer PFS", 50, t0=0.0, now=3000.0, exact=False)
        est = P.estimate_build(tr, SH, 3000.0)
        self.assertGreater(est["remaining"], 0)

    def test_the_whole_build_is_weighted_by_the_shares_of_earlier_builds(self):
        ledger = {"PPSA00001": dict(stage_secs={"1": 600, "2": 10, "3": 200, "4": 10, "5": 80, "validate": 50, "copy": 50}, source_bytes=100 * GIB, level=4, prep_min=10),
                  "PPSA00002": dict(stage_secs={"1": 1200, "2": 20, "3": 400, "4": 20, "5": 160, "validate": 100, "copy": 100}, source_bytes=200 * GIB, level=4, prep_min=20)}
        h = P.history(ledger, level=4)
        self.assertEqual(h["builds"], 2)
        self.assertAlmostEqual(h["shares"]["1"], 600 / 1000)
        sh = P.normalise(P.shares_from(h))
        self.assertLess(sh["1"], SH["1"])                                              # on this install the first stage is a smaller share than the default
        tr = tracker_at(1, "inner image", 61)
        self.assertLess(P.estimate_build(tr, sh, 1000.0)["progress"], P.estimate_build(tr, SH, 1000.0)["progress"])


class History(unittest.TestCase):
    LEDGER = {
        "PPSA00001": dict(stage_secs={"1": 600, "2": 10, "3": 200, "4": 10, "5": 80, "validate": 50, "copy": 50}, source_bytes=100 * GIB, level=4, prep_min=10),
        "PPSA00002": dict(stage_secs={"1": 1200, "2": 20, "3": 400, "4": 20, "5": 160, "validate": 100, "copy": 100}, source_bytes=200 * GIB, level=7, prep_min=30),
        "PPSA00003": dict(build_min=50, bytes=1, level=4),                              # an older entry: no stage times, no source size
        "PPSA00004": "garbage", "PPSA00005": dict(stage_secs="x", source_bytes="y"),
        "CUSA12345": dict(ps4=True, source_bytes=10 * GIB, copy_secs=100),
    }

    def test_what_is_learned_from_the_ledger(self):
        h = P.history(self.LEDGER, level=4)
        self.assertEqual(h["builds"], 2)
        self.assertAlmostEqual(h["build_per_gib"], 1000 / 100)                          # only the level-4 build counts for pace
        self.assertAlmostEqual(h["prep_per_gib"], P.median_of([600 / 100, 1800 / 200]))
        self.assertAlmostEqual(h["ps4_per_gib"], 10)
        self.assertAlmostEqual(P.history(self.LEDGER, level=7)["build_per_gib"], 2000 / 200)
        self.assertAlmostEqual(P.history(self.LEDGER)["build_per_gib"], P.median_of([10.0, 10.0]))

    def test_nothing_to_learn_from_gives_the_defaults(self):
        for ledger in ({}, None, {"x": dict(build_min=50)}, {"y": dict(stage_secs={"1": 5})}):
            h = P.history(ledger, level=4)
            self.assertIsNone(h["shares"])
            self.assertEqual(P.shares_from(h), P.DEFAULT_SHARES)
            self.assertIsNone(h["build_per_gib"])

    def test_a_ledger_of_a_thousand_entries_is_quick_to_read(self):
        big = {f"PPSA{i:05d}": dict(self.LEDGER["PPSA00001"]) for i in range(1000)}
        self.assertEqual(P.history(big, 4)["builds"], 1000)


class Run(unittest.TestCase):
    def test_the_last_build_to_finish_ends_the_run(self):
        self.assertEqual(P.simulate(0, [dict(end=500, kraken_end=300)], [], max_builds=2, max_compress=1), 500)
        self.assertEqual(P.simulate(0, [], [], max_builds=2, max_compress=1), 0)

    def test_a_queue_waits_for_a_compression_slot_and_a_process_slot(self):
        job = dict(prep=0, total=1000, kraken=800)
        # one compression slot: the second build can only start when the first has left stage 1
        end = P.simulate(0, [], [job, job], max_builds=3, max_compress=1)
        self.assertEqual(end, 800 + 1000)
        # two slots: both start at once
        self.assertEqual(P.simulate(0, [], [job, job], max_builds=3, max_compress=2), 1000)
        # one process slot: strictly one after the other
        self.assertEqual(P.simulate(0, [], [job, job], max_builds=1, max_compress=2), 2000)

    def test_a_running_build_holds_its_slots_until_it_is_done_with_them(self):
        running = [dict(end=900, kraken_end=600)]
        job = dict(prep=0, total=1000, kraken=800)
        self.assertEqual(P.simulate(0, running, [job], max_builds=3, max_compress=1), 600 + 1000)
        self.assertEqual(P.simulate(0, running, [job], max_builds=1, max_compress=1), 900 + 1000)

    def test_prepares_run_one_at_a_time_ahead_of_the_builds(self):
        job = dict(prep=300, total=1000, kraken=800)
        self.assertEqual(P.simulate(0, [], [job, job], max_builds=3, max_compress=3), 600 + 1000)
        self.assertEqual(P.simulate(0, [], [job], max_builds=3, max_compress=3, prep_free_at=100), 100 + 300 + 1000)

    def test_no_slot_at_all_cannot_be_estimated(self):
        self.assertIsNone(P.simulate(0, [], [dict(prep=0, total=1, kraken=1)], max_builds=0, max_compress=1))


class Smoothing(unittest.TestCase):
    def test_nothing_until_there_has_been_an_estimate_then_it_keeps_counting_down(self):
        s = P.Smoother()
        self.assertIsNone(s.update(None, 0))
        self.assertEqual(s.update(1000, 5), 1000)
        self.assertEqual(s.update(None, 15), 990)                                       # no new estimate: the old one counts down
        self.assertEqual(s.update(None, 25), 980)

    def test_a_steady_estimate_counts_down_in_real_time(self):
        s = P.Smoother()
        s.update(1000, 0)
        for t in range(5, 100, 5):
            self.assertAlmostEqual(s.update(1000 - t, t), 1000 - t, places=6)

    def test_a_sudden_change_is_followed_gradually_and_never_by_more_than_the_slew(self):
        s = P.Smoother(alpha=0.25, slew=60, tick=5)
        s.update(1000, 0)
        shown, t = [], 0
        for _ in range(40):                                                             # the raw estimate jumps up by 20 minutes at t=5
            t += 5
            shown.append(s.update(2200 - t, t))
        steps = [b - a + 5 for a, b in zip(shown, shown[1:])]                           # change beyond the countdown, per tick
        self.assertTrue(all(abs(x) <= 60 + 1e-6 for x in steps), max(steps))
        self.assertGreater(shown[0], 1000 - 5)                                          # it did move up...
        self.assertLess(shown[0], 1000 - 5 + 120)                                       # ...but not by the 20 minutes in one step
        self.assertAlmostEqual(shown[-1], 2200 - t, delta=60)                           # and caught up in the end

    def test_a_slow_poll_gets_a_proportional_step(self):
        s = P.Smoother(alpha=0.25, slew=60, tick=5)
        s.update(1000, 0)
        self.assertLessEqual(s.update(5000, 100) - 900, 60 * 20 + 1e-6)

    def test_the_rate_of_a_byte_counter(self):
        pts = [(0, 0), (10, 1000), (20, 3000)]
        self.assertEqual(P.rate_of(pts, 20), 150)
        self.assertEqual(P.rate_of(pts, 20, window=12), 200)
        self.assertIsNone(P.rate_of([(0, 5)], 1))
        self.assertIsNone(P.rate_of([(0, 5), (10, 5)], 10))
        self.assertIsNone(P.rate_of([(0, 5), (1, 9)], 1))                                # too short a span to say


class Words(unittest.TestCase):
    def test_durations(self):
        for secs, want in [(0, "0 s"), (44.6, "45 s"), (89, "89 s"), (91, "2 min"), (25 * 60, "25 min"), (59 * 60, "59 min"), (60 * 60, "1 h"), (80 * 60, "1 h 20 min"),
                           (3600 * 2, "2 h"), (100 * 60, "1 h 40 min"), (3900, "1 h 05 min"), (-5, "0 s")]:
            self.assertEqual(P.human_duration(secs), want, secs)


# ---------------------------------------------------------------- a whole build, replayed

def timed_logs():
    return sorted(FIXTURES.glob("*.timed.log"))


def load_timed(path):
    rows = []
    for ln in path.read_text().splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        t, _, text = ln.partition("\t")
        rows.append((float(t), text))
    return rows


def replay(rows, shares, *, tick=5, hist_total=None):
    """Feed a timed log to a tracker the way the engine does (a look every `tick` seconds) and record, at each look,
    (time, bar, raw remaining, shown remaining)."""
    tr, i, out = P.LogTracker(0.0), 0, []
    sm = P.Smoother(tick=tick)
    end = rows[-1][0]
    for now in range(tick, int(end) + tick + 1, tick):
        chunk = []
        while i < len(rows) and rows[i][0] <= now:
            chunk.append(rows[i][1])
            i += 1
        tr.feed("\n".join(chunk), now)
        est = P.estimate_build(tr, shares, now, hist_total=hist_total)
        out.append((now, est["progress"], est["remaining"], sm.update(est["remaining"], now)))
    return tr, out


VALIDATE = 100.0            # seconds the engine spends checking the package after UltraPack (about two minutes on the real build)


def errors(seen, total, lo, hi, which=3):
    """Relative error of the time left (shown by default, raw with which=2) against what was really left, for looks between lo and hi seconds."""
    return [(row[which] - (total - row[0])) / (total - row[0]) for row in seen if lo <= row[0] < hi and row[which] is not None]


def learned_shares(tr, validate=VALIDATE):
    h = P.history({"PPSA00001": dict(stage_secs=dict(tr.stage_secs(), validate=validate, copy=0.0), source_bytes=140 * GIB, level=4)}, level=4)
    return P.normalise(P.shares_from(h), with_copy=False)


class Replay(unittest.TestCase):
    """Every log in tests/fixtures: the synthetic one and the real one."""

    def test_there_is_a_log_to_replay(self):
        self.assertTrue(timed_logs(), "tests/fixtures needs at least one *.timed.log")
        self.assertTrue(REAL.exists())

    def test_the_bar_is_not_stuck_low_for_most_of_a_real_build(self):
        for path in timed_logs():
            with self.subTest(path.name):
                rows = load_timed(path)
                tr, seen = replay(rows, SH)
                low = sum(1 for _, bar_, _, _ in seen if bar_ < 0.20) * 5 / rows[-1][0]
                self.assertLessEqual(low, 0.5, f"the bar is below 20% for {low:.0%} of the build")
                self.assertLess(low, 0.35)
                bars = [b for _, b, _, _ in seen]
                self.assertEqual(bars, sorted(bars))                                    # never backwards
                self.assertAlmostEqual(bars[-1], sum(SH[k] for k in ("1", "2", "3", "4", "5")))
                self.assertEqual(sorted(tr.ended), [1, 2, 3, 4, 5])

    def test_the_old_formula_fails_the_same_test(self):
        # (stage - 1) * 20 + pct / 5: each stage worth a fifth. It sits under 20 % for the whole first stage.
        for path in timed_logs():
            rows = load_timed(path)
            tr, i, below = P.LogTracker(0.0), 0, 0
            ticks = list(range(5, int(rows[-1][0]) + 6, 5))
            for now in ticks:
                chunk = []
                while i < len(rows) and rows[i][0] <= now:
                    chunk.append(rows[i][1])
                    i += 1
                tr.feed("\n".join(chunk), now)
                old = ((tr.stage - 1) * 20 + tr.pct / 5) / 100
                below += old < 0.20
            self.assertGreater(below / len(ticks), 0.5, path.name)

    def test_it_says_estimating_until_there_is_signal_and_then_gives_one_soon(self):
        for path in timed_logs():
            with self.subTest(path.name):
                _, seen = replay(load_timed(path), SH0)
                self.assertTrue(all(s is None for t, _, _, s in seen if t < P.MIN_SIGNAL_SECS))
                first = next(t for t, _, raw, _ in seen if raw is not None)
                self.assertGreaterEqual(first, P.MIN_SIGNAL_SECS)
                self.assertLess(first, 300)

    def test_the_shown_estimate_does_not_jump_up(self):
        for path in timed_logs():
            with self.subTest(path.name):
                _, seen = replay(load_timed(path), SH0)
                shown = [s for *_, s in seen if s is not None]
                for a, b in zip(shown, shown[1:]):
                    self.assertLessEqual(b - a, 20.0 - 5.0 + 1e-6)                      # the slew limit, beyond the countdown, per look
                for i in range(len(shown) - 12):                                       # and over any minute: at most 3 minutes up
                    self.assertLessEqual(shown[i + 12] - shown[i], 180 + 1e-6, (path.name, i))

    def test_with_what_this_install_has_learned_it_is_close(self):
        for path in timed_logs():
            with self.subTest(path.name):
                rows = load_timed(path)
                total = rows[-1][0] + VALIDATE
                tr, _ = replay(rows, SH0)
                _, seen = replay(rows, learned_shares(tr))
                self.assertTrue(all(abs(e) < 0.30 for e in errors(seen, total, 600, rows[-1][0] - 30)), path.name)
                self.assertTrue(all(abs(e) < 0.20 for e in errors(seen, total, 1200, rows[-1][0] - 30)), path.name)


REAL = FIXTURES / "ultrapack_build.real-233gib.timed.log"


class RealBuild(unittest.TestCase):
    """The real 233 GiB build (level 4, 12 workers: 54 minutes of UltraPack, then about two minutes of checking the
    package). The bounds are what the code achieves on it today, with a margin; the numbers are in the pull request."""

    @classmethod
    def setUpClass(cls):
        cls.rows = load_timed(REAL)
        cls.log_secs = cls.rows[-1][0]
        cls.total = cls.log_secs + VALIDATE
        cls.tr, cls.defaults = replay(cls.rows, SH0)

    def test_it_is_the_build_it_says_it_is(self):
        secs = self.tr.stage_secs()
        self.assertAlmostEqual(secs["1"], 3094.5, delta=5)
        self.assertAlmostEqual(secs["3"], 151.3, delta=5)
        self.assertLess(secs["2"] + secs["4"] + secs["5"], 20)
        self.assertTrue(self.tr.finished)
        self.assertEqual(self.tr.total_bytes, 250_052_963_003)

    def test_the_default_shares_are_this_build_s(self):
        secs = dict(self.tr.stage_secs(), validate=VALIDATE)
        total = sum(secs.values())
        real = {k: secs.get(k, 0) / total for k in ("1", "2", "3", "4", "5", "validate")}
        for k, v in real.items():
            self.assertAlmostEqual(SH0[k], v, delta=0.03, msg=k)
        self.assertGreater(real["1"], 0.9)

    def test_the_bar(self):
        low = sum(1 for _, b, _, _ in self.defaults if b < 0.20) * 5 / self.log_secs
        self.assertLess(low, 0.25)                                                        # the old formula: 95 % of the build under 20 %
        self.assertGreater(self.defaults[int(len(self.defaults) * 0.5)][1], 0.4)           # half way through: not still near the bottom

    def test_accuracy_of_the_time_left_with_defaults_only(self):
        # no history at all: the first build on an install. UltraPack's own ETA undershoots early (it is still fast).
        e = lambda lo, hi: errors(self.defaults, self.total, lo, hi)
        self.assertTrue(all(-0.50 < x < 0.0 for x in e(120, 600)), (min(e(120, 600)), max(e(120, 600))))       # measured -46 % .. -11 %
        self.assertTrue(all(-0.30 < x < 0.20 for x in e(600, self.log_secs - 30)), (min(e(600, 3228)), max(e(600, 3228))))   # from minute 10: -26 % .. +18 %
        self.assertTrue(all(-0.20 < x < 0.20 for x in e(1200, self.log_secs - 30)), (min(e(1200, 3228)), max(e(1200, 3228))))  # from minute 20: -17 % .. +18 %

    def test_accuracy_with_the_shares_this_install_learned(self):
        _, seen = replay(self.rows, learned_shares(self.tr))
        e = lambda lo, hi: errors(seen, self.total, lo, hi)
        self.assertTrue(all(-0.30 < x < 0.15 for x in e(600, self.log_secs - 30)), (min(e(600, 3228)), max(e(600, 3228))))    # measured -27 % .. +12 %
        self.assertTrue(all(-0.20 < x < 0.15 for x in e(1200, self.log_secs - 30)), (min(e(1200, 3228)), max(e(1200, 3228))))  # measured -18 % .. +12 %

    def test_a_history_prior_fixes_the_early_undershoot_even_when_it_is_off_by_a_quarter(self):
        for ratio in (0.75, 0.9, 1.0, 1.1, 1.3):
            with self.subTest(ratio=ratio):
                _, seen = replay(self.rows, SH0, hist_total=ratio * self.total)
                early = errors(seen, self.total, 120, 600)
                self.assertTrue(all(-0.35 < x < 0.25 for x in early), (ratio, min(early), max(early)))                            # no worse than -46 % without it
                late = errors(seen, self.total, 1200, self.log_secs - 30)
                self.assertTrue(all(-0.25 < x < 0.20 for x in late), (ratio, min(late), max(late)))
        _, none = replay(self.rows, SH0)
        _, right = replay(self.rows, SH0, hist_total=self.total)
        self.assertLess(max(abs(x) for x in errors(right, self.total, 120, 600)), 0.5 * max(abs(x) for x in errors(none, self.total, 120, 600)))

    def test_the_shown_estimate_rises_by_at_most_three_minutes_a_minute_while_the_real_rate_falls(self):
        shown = [s for *_, s in self.defaults if s is not None]
        worst = max(shown[i + 12] - shown[i] for i in range(len(shown) - 12))
        self.assertLessEqual(worst, 180 + 1e-6)
        # UltraPack's own ETA rose by more than that here: it says 2083 s at 256 s and 2304 s at 412 s
        raw = [r for _, _, r, _ in self.defaults if r is not None]
        self.assertGreater(max(raw[i + 12] - raw[i] for i in range(len(raw) - 12)), 60)


if __name__ == "__main__":
    unittest.main()
