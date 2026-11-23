"""History and logs, the engine's side (app/engine/ps5_history.py and what the engine does with it): history.jsonl and its reading, ledger
entries from every version of the app, the log files (which game, how much room, rotating, pruning), replaced packages, and the
rebuild request. Standard library only."""
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

import ps5_failures as F  # noqa: E402
import ps5_history as PH  # noqa: E402
import test_engine_failures as TEF  # noqa: E402

GIB = 2 ** 30
NOW = 1_795_000_000.0


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = Path(__import__("tempfile").mkdtemp(prefix="hist-"))
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)


class Writing(Tmp):
    def test_rows_are_appended_and_read_back_in_order_and_nothing_is_rewritten(self):
        PH.append(self.d, [dict(k="run", run="20261122-030000", trigger="scheduled", started=NOW)])
        first = (self.d / "history.jsonl").read_text()
        PH.append(self.d, [dict(k="job", run="20261122-030000", tid="PPSA01234", status="built", at=NOW + 5), dict(k="end", run="20261122-030000", finished=NOW + 9)])
        text = (self.d / "history.jsonl").read_text()
        self.assertTrue(text.startswith(first))                                            # earlier lines are never touched
        self.assertEqual([r["k"] for r in PH.read_rows(self.d)], ["run", "job", "end"])

    def test_empty_values_are_left_out_of_a_line(self):
        PH.append(self.d, [dict(k="job", run="r", tid="PPSA01234", why="", code=None, size=0)])
        row = json.loads((self.d / "history.jsonl").read_text())
        self.assertEqual(row, {"k": "job", "run": "r", "tid": "PPSA01234", "size": 0})

    def test_a_damaged_history_is_read_as_far_as_it_goes(self):
        (self.d / "history.jsonl").write_text('{"k":"run","run":"a"}\nnot json\n[1,2]\n"x"\n{"k":"job","run":"a","tid":"PPSA01234"}\n{"k":"job","run":"a","ti')
        self.assertEqual([r["k"] for r in PH.read_rows(self.d)], ["run", "job"])
        self.assertEqual(PH.read_rows(self.d / "nowhere"), [])
        (self.d / "history.jsonl").write_bytes(b"\xff\xfe\n" + b'{"k":"run","run":"b"}\n')
        self.assertEqual([r["run"] for r in PH.read_rows(self.d)], ["b"])

    def test_a_big_history_is_read_from_its_end(self):
        lines = "".join(json.dumps(dict(k="job", run="r", tid="PPSA01234", n=i)) + "\n" for i in range(2000))
        (self.d / "history.jsonl").write_text(lines)
        rows = PH.read_rows(self.d, max_bytes=5000)
        self.assertLess(len(rows), 2000)
        self.assertEqual(rows[-1]["n"], 1999)                                              # the newest is there, and the cut line is not
        self.assertTrue(all(isinstance(r.get("n"), int) for r in rows))

    def test_a_job_row_carries_what_the_history_shows(self):
        built = PH.job_row(dict(title="Example Game", title_id="PPSA01234", name="Example Game [PPSA01234].pkg", status="built", output="/output/x.pkg", bytes=5 * GIB,
                                prep_min=12.0, build_min=60.5, build_secs=3630, validate="summary: 40 passed, 0 warned, 0 failed", sha256="ab" * 32,
                                log="/logs/pkg/20261122-031500_PPSA01234.log", at=NOW, source="/library/x", size=9 * GIB), "20261122-030000", level=4)
        self.assertEqual((built["status"], built["bytes"], built["level"], built["log"]), ("built", 5 * GIB, 4, "pkg/20261122-031500_PPSA01234.log"))
        self.assertNotIn("summary", built)
        failed = PH.job_row(dict(title_id="PPSA01234", status="failed", code="crc_mismatch", phase="prepare", why="1 extracted file(s) differ from the archive CRCs",
                                 data={"files": ["sce_sys/param.json"], "count": 1}, at=NOW), "r", level=7)
        self.assertEqual((failed["code"], failed["phase"]), ("crc_mismatch", "prepare"))
        self.assertIn("sce_sys/param.json", failed["summary"])                              # the words #6 gives a failure
        self.assertEqual(PH.job_row(dict(title_id="PPSA01234", status="cancelled", code="cancelled", why="cancelled while building", at=NOW), "r")["status"], "cancelled")
        self.assertEqual(PH.job_row(dict(title_id="PPSA01234", status="deferred", code="no_space", why="needs room", data={"need": 5, "usable": 1}, at=NOW), "r")["status"], "deferred")

    def test_the_log_of_a_build_is_named_as_the_logs_page_names_it(self):
        self.assertEqual(PH.log_rel("/logs/pkg/20261122-031500_PPSA01234.log"), "pkg/20261122-031500_PPSA01234.log")
        self.assertEqual(PH.log_rel("pkg_sync.log"), "pkg_sync.log")
        self.assertIsNone(PH.log_rel(None))
        self.assertIsNone(PH.log_rel(""))


class Runs(unittest.TestCase):
    def rows(self):
        return [dict(k="run", run="20261122-030000", trigger="scheduled", started=NOW), dict(k="job", run="20261122-030000", tid="PPSA00002", status="built", at=NOW + 600),
                dict(k="job", run="20261122-030000", tid="PPSA00001", status="failed", at=NOW + 100), dict(k="end", run="20261122-030000", finished=NOW + 700),
                dict(k="run", run="20261123-030000", trigger="manual", started=NOW + 86400), dict(k="job", run="20261123-030000", tid="PPSA00003", status="cancelled", at=NOW + 86500)]

    def test_runs_come_newest_first_with_their_jobs_oldest_first_and_their_counts(self):
        r = PH.runs(self.rows())
        self.assertEqual([x["run"] for x in r], ["20261123-030000", "20261122-030000"])
        old = r[1]
        self.assertEqual((old["trigger"], old["seconds"], old["counts"], old["interrupted"]), ("scheduled", 700.0, {"built": 1, "failed": 1}, False))
        self.assertEqual([j["tid"] for j in old["jobs"]], ["PPSA00001", "PPSA00002"])

    def test_a_run_that_never_ended_is_marked_and_ends_at_its_last_job(self):
        new = PH.runs(self.rows())[0]
        self.assertTrue(new["interrupted"])
        self.assertEqual(new["finished"], NOW + 86500)
        self.assertEqual(new["counts"], {"cancelled": 1})

    def test_a_run_with_no_start_line_is_dated_by_its_name_and_odd_rows_are_ignored(self):
        r = PH.runs([dict(k="job", run="20261122-030000", tid="PPSA00001", status="built", at=NOW), dict(k="job", tid="PPSA00001"), dict(k="job", run=5), dict(k="?", run="x")])
        self.assertEqual(len(r), 2)                                                          # the run named x has no jobs but is still a run
        named = next(x for x in r if x["run"] == "20261122-030000")
        self.assertEqual(named["started"], time.mktime(time.strptime("20261122-030000", "%Y%m%d-%H%M%S")))
        self.assertEqual(PH.runs([]), [])


class Packages(unittest.TestCase):
    def test_a_current_entry(self):
        p = PH.package("PPSA01234", dict(version="01.004.000", output="/output/Example Game [PPSA01234].pkg", source="/library/x", built="2026-11-20 03:10", built_ts=NOW,
                                         prep_min=12.0, build_min=60.5, bytes=5 * GIB, source_bytes=9 * GIB, level=4, validate="summary: 40 passed, 2 warned, 0 failed", sha256="ab" * 32,
                                         backport={"differs": True, "used": "original", "files": 3}))
        self.assertEqual((p["name"], p["bytes"], p["level"], p["built_ts"], p["checks"]), ("Example Game [PPSA01234].pkg", 5 * GIB, 4, NOW, dict(passed=40, warned=2, failed=0)))
        self.assertEqual(PH.minutes_of(p), 72.5)
        self.assertEqual(p["sha256"], "ab" * 32)

    def test_an_entry_from_before_levels_and_hashes_were_kept_says_only_what_it_knows(self):
        p = PH.package("PPSA01234", dict(version="01.002.003", output="/output/x.pkg", validated="summary: 5 ok, 0 failed", seconds=1830, built="2026-01-02 03:04"))
        self.assertEqual((p["validate"], p["checks"], p["total_min"], p["level"], p["sha256"], p["bytes"], p["prep_min"], p["build_min"]),
                         ("summary: 5 ok, 0 failed", dict(passed=5, failed=0), 30.5, None, None, None, None, None))
        self.assertEqual(p["built_ts"], time.mktime(time.strptime("2026-01-02 03:04", "%Y-%m-%d %H:%M")))
        self.assertEqual(PH.minutes_of(p), 30.5)

    def test_a_ps4_entry(self):
        p = PH.package("CUSA00001", dict(version="01.00", output="/output/PS4/Old Game [CUSA00001]", ps4=True, kind="ps4", files=["a.pkg", "b.pkg"], bytes=3 * GIB,
                                         copy_secs=300, validate="CRC verified by 7-Zip: 2 package(s)", built_ts=NOW))
        self.assertEqual((p["ps4"], p["files"], p["checks"]), (True, ["a.pkg", "b.pkg"], None))
        self.assertEqual(PH.minutes_of(p), 5.0)

    def test_anything_else_is_not_recorded_rather_than_a_crash_or_a_guess(self):
        for v in ({}, {"output": 5, "version": 1, "bytes": "big", "built_ts": "soon", "level": "4", "sha256": 7, "validate": ["x"], "prep_min": True, "files": "a"}):
            p = PH.package("PPSA01234", v)
            self.assertEqual((p["name"], p["output"], p["bytes"], p["built_ts"], p["level"], p["sha256"], p["validate"], p["checks"], p["prep_min"], p["version"]),
                             (None,) * 10)
            self.assertIsNone(PH.minutes_of(p))
        self.assertIsNone(PH.package("PPSA01234", "garbage"))
        self.assertIsNone(PH.package("PPSA01234", None))

    def test_the_validator_summary_in_every_wording(self):
        for text, want in (("summary: 40 passed, 0 warned, 0 failed", dict(passed=40, warned=0, failed=0)), ("summary: 7 ok, 0 failed", dict(passed=7, failed=0)),
                           ("summary: 3 passed, 2 failed", dict(passed=3, failed=2)), ("nothing useful", None), (None, None), ("", None)):
            self.assertEqual(PH.parse_checks(text), want, text)


def touch(path, text="x" * 100, age_days=0, now=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (now - age_days * 86400, now - age_days * 86400))
    return path


class Logs(Tmp):
    def logs(self):
        logs = self.d / "logs"
        touch(logs / "pkg_sync.log", "a" * 1000, 0)
        touch(logs / "scan.stdout.log", "s" * 10, 400)
        touch(logs / "run_20260101-030000.stdout.log", "r" * 200, 300)
        touch(logs / "pkg_sync.20251201-000000.log", "o" * 500, 250)
        touch(logs / "pkg" / "20260105-031500_PPSA01234.log", "b" * 300, 200)
        touch(logs / "pkg" / "20261120-031500_PPSA01234.log", "b" * 300, 2)
        touch(logs / "pkg" / "20261121-031500_PPSA05678.log", "b" * 300, 1)
        touch(logs / "pkg" / "odd name.log", "?", 1)
        return logs

    def test_each_log_is_a_kind_and_a_build_log_knows_its_game_and_start(self):
        items = {d["name"]: d for d in PH.list_logs(self.logs())}
        kinds = {n: d["kind"] for n, d in items.items()}
        self.assertEqual(kinds, {"pkg_sync.log": "activity", "scan.stdout.log": "scan", "run_20260101-030000.stdout.log": "run", "pkg_sync.20251201-000000.log": "activity_old",
                                 "pkg/20260105-031500_PPSA01234.log": "build", "pkg/20261120-031500_PPSA01234.log": "build", "pkg/20261121-031500_PPSA05678.log": "build",
                                 "pkg/odd name.log": "other"})
        b = items["pkg/20261120-031500_PPSA01234.log"]
        self.assertEqual((b["tid"], b["started"]), ("PPSA01234", time.mktime(time.strptime("20261120-031500", "%Y%m%d-%H%M%S"))))
        self.assertEqual(PH.list_logs(self.d / "nothing"), [])

    def test_usage_counts_files_and_bytes_by_kind(self):
        u = PH.usage(self.logs())
        self.assertEqual((u["files"], u["bytes"], u["by_kind"]["build"]), (8, 1000 + 10 + 200 + 500 + 900 + 1, dict(files=3, bytes=900)))

    def test_one_games_logs_newest_first_and_the_one_that_made_a_package(self):
        items = PH.list_logs(self.logs())
        self.assertEqual([d["name"] for d in PH.game_logs(items, "PPSA01234")], ["pkg/20261120-031500_PPSA01234.log", "pkg/20260105-031500_PPSA01234.log"])
        t = time.mktime(time.strptime("20261120-040000", "%Y%m%d-%H%M%S"))
        self.assertEqual(PH.log_for_package(items, "PPSA01234", t), "pkg/20261120-031500_PPSA01234.log")
        self.assertEqual(PH.log_for_package(items, "PPSA01234", t - 5 * 86400), "pkg/20260105-031500_PPSA01234.log")      # an older package: the older build
        self.assertIsNone(PH.log_for_package(items, "PPSA01234", time.mktime(time.strptime("20250101-000000", "%Y%m%d-%H%M%S"))))
        self.assertIsNone(PH.log_for_package(items, "PPSA09999", t))
        self.assertEqual(PH.log_for_package(items, "PPSA05678", None), "pkg/20261121-031500_PPSA05678.log")

    def test_the_engines_log_is_renamed_when_it_has_grown_and_not_before(self):
        log = touch(self.d / "pkg_sync.log", "x" * 5000)
        self.assertIsNone(PH.rotate(log, 10_000, now=NOW))
        name = PH.rotate(log, 1000, now=NOW)
        self.assertRegex(name, r"^pkg_sync\.\d{8}-\d{6}\.log$")
        self.assertFalse(log.exists())
        self.assertEqual((self.d / name).read_text(), "x" * 5000)
        self.assertIsNone(PH.rotate(log, 1000))                                            # nothing there
        self.assertEqual(PH.list_logs(self.d)[0]["kind"], "activity_old")

    def test_pruning_by_age_removes_old_build_runs_and_rotated_logs_and_nothing_else(self):
        logs = self.logs()
        got = PH.prune_logs(logs, days=100, now=NOW)
        self.assertEqual(sorted(got["removed"]), ["pkg/20260105-031500_PPSA01234.log", "pkg_sync.20251201-000000.log", "run_20260101-030000.stdout.log"])
        self.assertEqual(got["freed"], 300 + 500 + 200)
        self.assertTrue((logs / "pkg_sync.log").exists() and (logs / "scan.stdout.log").exists())          # the engine's log and the scan's are never pruned
        self.assertTrue((logs / "pkg" / "20261120-031500_PPSA01234.log").exists())
        self.assertTrue((logs / "pkg" / "odd name.log").exists())

    def test_a_log_a_failure_points_at_is_kept_and_so_is_anything_written_to_just_now(self):
        logs = self.logs()
        touch(logs / "pkg" / "20261122-040000_PPSA00001.log", "live", 0, now=NOW - 60)
        got = PH.prune_logs(logs, days=1, protect={"pkg/20260105-031500_PPSA01234.log"}, now=NOW)
        self.assertNotIn("pkg/20260105-031500_PPSA01234.log", got["removed"])
        self.assertNotIn("pkg/20261122-040000_PPSA00001.log", got["removed"])
        self.assertIn("pkg/20261120-031500_PPSA01234.log", got["removed"])
        self.assertEqual(PH.failure_logs({"PPSA01234": {"log": "pkg/x.log"}, "PPSA05678": {"log": ""}, "PPSA00001": "odd", "PPSA00002": {}}), {"pkg/x.log"})

    def test_pruning_by_size_takes_the_oldest_until_it_fits(self):
        logs = self.logs()
        self.assertEqual(PH.prune_logs(logs, gb=0, now=NOW)["removed"], [])                  # 0: no size limit
        self.assertEqual(PH.prune_logs(logs, gb=1, now=NOW)["removed"], [])                  # everything fits in 1 GiB
        big = logs / "pkg" / "20250101-000000_PPSA00009.log"
        with open(big, "wb") as fh:
            fh.truncate(3 << 30)
        os.utime(big, (NOW - 3 * 86400, NOW - 3 * 86400))
        got = PH.prune_logs(logs, gb=1, now=NOW)
        self.assertIn("pkg/20250101-000000_PPSA00009.log", got["removed"])
        self.assertFalse(big.exists())
        self.assertTrue((logs / "pkg" / "20261121-031500_PPSA05678.log").exists())          # newer ones are not needed to get under the limit

    def test_a_big_log_written_to_just_now_is_not_pruned_for_its_size(self):
        logs = self.logs()
        big = logs / "pkg" / "20261122-040000_PPSA00009.log"
        with open(big, "wb") as fh:
            fh.truncate(3 << 30)
        os.utime(big, (NOW - 60, NOW - 60))                                                # a build that is writing it now
        got = PH.prune_logs(logs, gb=1, now=NOW)
        self.assertNotIn("pkg/20261122-040000_PPSA00009.log", got["removed"])
        self.assertTrue(big.exists())

    def test_a_dry_run_removes_nothing_but_says_what_it_would(self):
        logs = self.logs()
        got = PH.prune_logs(logs, days=100, now=NOW, dry_run=True)
        self.assertEqual(len(got["removed"]), 3)
        self.assertTrue((logs / "pkg" / "20260105-031500_PPSA01234.log").exists())


class Replaced(Tmp):
    def setUp(self):
        super().setUp()
        self.out = self.d / "output"
        self.out.mkdir()
        for name, n in (("Game [PPSA01234] [v01] [fw9.00].pkg", 10), ("Game [PPSA01234] [v01] [fw9.00] (replaced 202611201430).pkg", 20),
                        ("Game [PPSA01234] [v01] [fw9.00] (replaced 202611211430).pkg", 30), ("Other [PPSA05678] (replaced 202611211500).pkg", 40), ("not replaced (replaced).pkg", 5)):
            (self.out / name).write_bytes(b"x" * n)

    def test_replaced_packages_are_listed_newest_first_with_their_size(self):
        got = PH.replaced_files(self.out)
        self.assertEqual([(d["name"], d["bytes"]) for d in got], [("Other [PPSA05678] (replaced 202611211500).pkg", 40), ("Game [PPSA01234] [v01] [fw9.00] (replaced 202611211430).pkg", 30),
                                                                   ("Game [PPSA01234] [v01] [fw9.00] (replaced 202611201430).pkg", 20)])
        self.assertEqual(got[0]["stem"], "Other [PPSA05678]")
        self.assertEqual(PH.replaced_files(self.d / "missing"), [])

    def test_delete_only_a_file_of_that_shape_in_the_output_folder(self):
        name = "Game [PPSA01234] [v01] [fw9.00] (replaced 202611201430).pkg"
        self.assertEqual(PH.delete_replaced(self.out, name), 20)
        self.assertFalse((self.out / name).exists())
        for bad in ("Game [PPSA01234] [v01] [fw9.00].pkg", "../x (replaced 202611201430).pkg", "sub/x (replaced 202611201430).pkg", "x (replaced 2026).pkg", "", None,
                    "x (replaced 202611201430).pkg/../../etc"):
            with self.assertRaises(ValueError, msg=bad):
                PH.delete_replaced(self.out, bad)
        with self.assertRaises(FileNotFoundError):
            PH.delete_replaced(self.out, "Nothing (replaced 202611201430).pkg")
        self.assertTrue((self.out / "Game [PPSA01234] [v01] [fw9.00].pkg").exists())       # the current package was never a candidate

    def test_a_link_is_not_followed(self):
        outside = self.d / "precious.pkg"
        outside.write_bytes(b"keep")
        (self.out / "Link (replaced 202611201430).pkg").symlink_to(outside)
        with self.assertRaises(FileNotFoundError):
            PH.delete_replaced(self.out, "Link (replaced 202611201430).pkg")
        self.assertTrue(outside.exists())
        self.assertNotIn("Link (replaced 202611201430).pkg", [d["name"] for d in PH.replaced_files(self.out)])

    def test_keeping_fewer_after_a_rebuild(self):
        stem = "Game [PPSA01234] [v01] [fw9.00]"
        self.assertEqual(PH.prune_replaced(self.out, stem, None), [])
        self.assertEqual(len(PH.replaced_files(self.out)), 3)
        self.assertEqual(PH.prune_replaced(self.out, stem, 1), [f"{stem} (replaced 202611201430).pkg"])                   # the newest one stays
        self.assertEqual(PH.prune_replaced(self.out, stem, 0), [f"{stem} (replaced 202611211430).pkg"])
        self.assertEqual([d["stem"] for d in PH.replaced_files(self.out)], ["Other [PPSA05678]"])                       # another game's is not touched
        self.assertTrue((self.out / f"{stem}.pkg").exists())
        self.assertEqual([PH.keep_count(s) for s in ("all", "1", "never", "x", None)], [None, 1, 0, None, None])


class Settings(unittest.TestCase):
    def test_the_new_settings_are_validated_and_default_to_changing_nothing(self):
        self.assertEqual((E.DEFAULT_CFG["keep_replaced"], E.DEFAULT_CFG["keep_logs_days"], E.DEFAULT_CFG["keep_logs_gb"]), ("all", 0, 0))
        self.assertEqual([E.validate_setting("keep_replaced", v) for v in ("all", "1", "never")], ["all", "1", "never"])
        for key, bad in (("keep_replaced", "some"), ("keep_replaced", 1), ("keep_logs_days", -1), ("keep_logs_days", 4000), ("keep_logs_gb", "many"), ("keep_logs_gb", 1001)):
            with self.assertRaises(ValueError, msg=(key, bad)):
                E.validate_setting(key, bad)
        self.assertEqual(E.validate_setting("keep_logs_days", "30"), 30)


class Planning(TEF.EngineCase):
    def entry(self, **ledger):
        return {"PPSA01234": dict(version="01.002.003", output=str(self.src), built="2026-11-20 03:10", **ledger)}

    def plan_of(self, ledger, rebuild=None):
        return {p["title_id"]: p for p in E.choose(self.cfg, self.cands, ledger, {}, rebuild=rebuild)}["PPSA01234"]

    def test_a_built_game_stays_done_until_someone_asks_for_a_rebuild(self):
        p = self.plan_of(self.entry())
        self.assertEqual(p["action"], "done")
        p = self.plan_of(self.entry(), rebuild={"PPSA01234"})
        self.assertEqual((p["action"], p["why"]), ("build", "rebuild requested"))
        self.assertEqual(self.plan_of(self.entry(), rebuild={"PPSA09999"})["action"], "done")

    def test_a_request_does_not_beat_an_option_that_says_never_build(self):
        self.cfg["overrides"]["PPSA01234"] = {"skip": True}
        self.assertEqual(self.plan_of(self.entry(), rebuild={"PPSA01234"})["action"], "skip")

    def test_the_request_is_read_from_the_state_folder_by_a_scan_and_a_package_is_planned_again(self):
        env = make_env(self.dir / "env2")
        out = Path(env["output"])
        self.write_cfg({**copy.deepcopy(E.DEFAULT_CFG), **env})
        pkg = out / "Example Game.pkg"
        pkg.write_bytes(b"p")
        (self.state / "ledger.json").write_text(json.dumps({"PPSA01234": dict(version="01.002.003", output=str(pkg), built="2026-11-20 03:10")}))

        def plan():
            r = subprocess.run([sys.executable, "-I", str(ENGINE_DIR / "ps5_pkg_sync.py"), "plan"], capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return json.loads((self.state / "plan.json").read_text())[0]
        self.assertEqual(plan()["action"], "done")
        F.Store(self.state / "rebuild.json").update(lambda d: d.__setitem__("PPSA01234", dict(at=1)))
        p = plan()
        self.assertEqual((p["action"], p["why"]), ("build", "rebuild requested"))
        self.assertTrue(pkg.exists())                                                      # asking changes the plan, not the files


class Recording(TEF.EngineCase):
    """run_queue's results, as history.jsonl keeps them."""

    def run_jobs(self, todo):
        with mock.patch.object(E, "notify", lambda *a: None):
            return E.run_queue(self.cfg, todo, lambda m: None, self.state / "ledger.json", {})

    def rows(self):
        return PH.read_rows(self.state)

    def test_a_deferred_game_is_recorded_with_its_reason_and_the_run_is_closed(self):
        with mock.patch.object(E, "usable_ssd", return_value=1 * GIB):
            self.run_jobs([dict(self.plan(), size=300 * GIB)])
        kinds = [r["k"] for r in self.rows()]
        self.assertEqual(kinds, ["run", "job", "end"])
        job = self.rows()[1]
        self.assertEqual((job["status"], job["code"], job["tid"]), ("deferred", "no_space", "PPSA01234"))
        self.assertIn("GiB", job["summary"])
        self.assertEqual({r["run"] for r in self.rows()}, {self.rows()[0]["run"]})
        self.assertFalse(self.rows()[2]["interrupted"])

    def test_a_failed_game_and_a_second_run_both_stay(self):
        job = dict(self.plan(), verify_archive=str(self.twin), verify_root="")
        patches = Recording_crc(self)
        with patches[0], patches[1], patches[2]:
            self.run_jobs([dict(job)])
            time.sleep(1.1)                                                                # the next run has another name
            self.run_jobs([dict(job)])
        runs = PH.runs(self.rows())
        self.assertEqual(len(runs), 2)
        for r in runs:
            self.assertEqual([j["status"] for j in r["jobs"]], ["failed"])
            self.assertEqual(r["jobs"][0]["code"], "crc_mismatch")
        self.assertEqual(sum(1 for r in self.rows() if r["k"] == "job"), 2)                 # nothing was added twice, nothing was lost

    def test_a_result_is_written_once_however_often_the_status_is_written(self):
        rs = E.RunStatus(self.cfg)
        res = dict(title_id="PPSA01234", title="Example Game", status="built", at=NOW, bytes=5)
        for _ in range(3):
            rs.record_new([res])
        rs.record_new([dict(res), dict(res, at=NOW + 1)])                                   # the same result again, and a later one
        self.assertEqual([(r["tid"], r["at"]) for r in PH.read_rows(self.state) if r["k"] == "job"], [("PPSA01234", NOW), ("PPSA01234", NOW + 1)])
        rs.record_new([dict(title_id="PPSA01234", at=NOW + 2)])                             # no status: not a result yet
        self.assertEqual(sum(1 for r in PH.read_rows(self.state) if r["k"] == "job"), 2)

    def test_a_run_that_dies_is_closed_as_interrupted(self):
        with mock.patch.object(E, "prepare", return_value=dict(app=Path("x"), clean=None, prep_min=1)), \
                mock.patch.object(E, "start_build", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_jobs([self.plan()])
        kinds = [r["k"] for r in self.rows()]
        self.assertEqual(kinds[0], "run")
        self.assertEqual(kinds[-1], "end")                                                 # run_queue's fallback closes it
        self.assertTrue(self.rows()[-1]["interrupted"])

    def test_an_unwritable_history_does_not_stop_a_run(self):
        (self.state / "history.jsonl").mkdir()                                              # a directory where the file should be
        with mock.patch.object(E, "usable_ssd", return_value=1 * GIB):
            results = self.run_jobs([dict(self.plan(), size=300 * GIB)])
        self.assertEqual(results[0]["status"], "deferred")


def Recording_crc(case):
    """The CRC-mismatch scenario of test_engine_failures, for a test that is not in that class."""
    return TEF.Running.crc_scenario(case)


if __name__ == "__main__":
    unittest.main()
