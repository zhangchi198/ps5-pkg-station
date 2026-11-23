"""Failed games in the web API: /api/failures with the explanation and the actions, /api/plan showing a title as
failed or waiting for space, Retry / Dismiss / the one-click fixes, and the staged package. Needs fastapi and
httpx (skipped without them)."""
from __future__ import annotations

import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from support import E, ServerCase, legacy_config

import ps5_failures as F  # noqa: E402

GIB = 2**30
SRC = "/library/Example Game [PPSA01234].exfat"
RAR = "/downloads/Example.rar"
ENTRY = dict(title_id="PPSA01234", title="Example Game", version="01.002.003", fw="9.00", source=SRC, kind="exfat", size=5, action="build",
             why="new", name="Example Game [PPSA01234] [v01.002.003].pkg", verify_archive=RAR,
             candidates=[dict(source=SRC, kind="exfat", version="01.002.003", packed="", size=5),
                         dict(source=RAR, kind="rar", version="01.002.003", packed="", size=5)])
HOST_ENV = {"PS5PKG_HOST_LIBRARY": "/srv/games/ps5", "PS5PKG_HOST_OUTPUT": "/srv/games/ps5/pkg", "PS5PKG_HOST_DOWNLOADS": "/srv/games/dl"}


class FailureCase(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output=str(self.dir / "out"), temp="/work/tmp",
                                     overrides={}))
        (self.dir / "out").mkdir()
        self.store = F.Store(self.state / "failures.json")
        self.put_plan([ENTRY])

    def put_plan(self, plan):
        (self.state / "plan.json").write_text(json.dumps(plan))

    def cfg(self):
        return E.load_cfg()

    def fp(self, entry=None):
        return F.fingerprint(self.cfg(), entry or ENTRY)

    def record(self, code="crc_mismatch", data=None, **kw):
        kw.setdefault("fingerprint", self.fp())
        kw.setdefault("phase", "prepare")
        return self.store.record("PPSA01234", code=code, data=data or {}, title="Example Game", name=ENTRY["name"], message=f"raw {code}",
                                 log=kw.pop("log", "pkg_sync.log"), excerpt=kw.pop("excerpt", ["l1", "l2"]), **kw)

    def failures(self):
        return self.get("/api/failures")

    def plan_entry(self):
        return self.get("/api/plan")[0]

    def action_ids(self, f=None):
        return [a["id"] for a in (f or self.failures()[0])["actions"]]


class Listing(FailureCase):
    def test_it_is_empty_when_nothing_failed(self):
        self.assertEqual(self.failures(), [])
        st = self.get("/api/status")
        self.assertEqual((st["attention"], st["plan_counts"]["failed"], st["plan_counts"]["build"]), ([], 0, 1))
        self.assertIsNone(st["failures_rev"])

    def test_a_failure_comes_with_its_explanation_excerpt_and_log(self):
        self.record(data=dict(files=["sce_sys/param.json"], count=1), log="pkg_sync.log", excerpt=["CRC MISMATCH sce_sys/param.json: ..."])
        f = self.failures()[0]
        self.assertEqual((f["title_id"], f["state"], f["code"], f["phase"], f["attempts"]), ("PPSA01234", "failed", "crc_mismatch", "prepare", 1))
        self.assertEqual(f["summary"], "The library copy differs from the original archive in 1 file: sce_sys/param.json. It may be modified or damaged.")
        self.assertEqual((f["message"], f["log"], f["excerpt"]), ("raw crc_mismatch", "pkg_sync.log", ["CRC MISMATCH sce_sys/param.json: ..."]))
        self.assertFalse(f["will_retry"])
        self.assertIn("Not retried by scheduled runs", f["next"])
        self.assertEqual(f["source"], SRC)

    def test_every_code_has_text_and_at_least_one_working_action(self):
        cases = {"crc_mismatch": ["use_archive", "keep_anyway", "view_log", "retry"], "modified_exec_unknown": ["set_console_fw", "keep_backport", "use_original", "retry"],
                 "ultrapack_failed": ["view_log", "retry"], "title_mismatch": ["skip_source", "view_log", "retry"], "copy_failed": ["view_log", "retry"],
                 "extract_failed": ["view_log", "retry"], "no_param_json": ["view_log", "retry"], "something_unknown": ["view_log", "retry"],
                 "validate_failed": ["view_log", "retry"]}
        for code, want in cases.items():
            with self.subTest(code):
                self.record(code=code, data={})
                f = self.failures()[0]
                self.assertTrue(f["summary"])
                self.assertEqual(self.action_ids(f), want)
                self.assertTrue(all(a["kind"] in ("api", "goto", "log", "retry") for a in f["actions"]))
        self.record(code="something_unknown")
        self.assertEqual(self.failures()[0]["summary"], "raw something_unknown")           # unknown: the raw message, with "View log"

    def test_view_log_opens_the_build_log_of_that_attempt(self):
        self.record(code="ultrapack_failed", log="pkg/20261122-031000_PPSA01234.log")
        act = next(a for a in self.failures()[0]["actions"] if a["id"] == "view_log")
        self.assertEqual((act["kind"], act["log"], act["label"]), ("log", "pkg/20261122-031000_PPSA01234.log", "View build log"))
        (self.logs / "pkg").mkdir()
        (self.logs / "pkg" / "20261122-031000_PPSA01234.log").write_text("[ERROR] boom\n")
        self.assertEqual(self.client.get("/api/logs/pkg/20261122-031000_PPSA01234.log").text, "[ERROR] boom")

    def test_modified_executables_point_at_the_console_firmware_setting(self):
        self.record(code="modified_exec_unknown")
        act = next(a for a in self.failures()[0]["actions"] if a["id"] == "set_console_fw")
        self.assertEqual((act["kind"], act["field"]), ("goto", "console_fw"))

    def test_waiting_for_space_shows_the_numbers_and_is_not_a_failure(self):
        self.record(code="no_space", state="waiting", data=dict(need=310 * GIB, usable=120 * GIB))
        f = self.failures()[0]
        self.assertEqual((f["state"], f["will_retry"]), ("waiting", True))
        self.assertEqual(f["summary"], "Needs about 310 GiB on the work drive; 120 GiB usable. Will be retried automatically.")
        self.assertEqual([a["id"] for a in f["actions"]], ["open_settings", "view_log"])
        p = self.plan_entry()
        self.assertEqual(p["action"], "build")
        self.assertIn("waiting for space: Needs about 310 GiB", p["why"])
        self.assertEqual(p["failure"]["state"], "waiting")

    def test_host_paths_are_shown_for_the_source_and_the_excerpt(self):
        with mock.patch.dict(os.environ, HOST_ENV):
            self.record(excerpt=[f"PREPARE <- {SRC}"])
            f = self.failures()[0]
        self.assertEqual(f["source_host"], "/srv/games/ps5/Example Game [PPSA01234].exfat")
        self.assertEqual(f["excerpt_host"], ["PREPARE <- /srv/games/ps5/Example Game [PPSA01234].exfat"])
        self.assertEqual(f["excerpt"], [f"PREPARE <- {SRC}"])


class Persistence(FailureCase):
    def test_a_failure_stays_after_the_next_run_starts_and_after_a_restart(self):
        self.record()
        self.assertEqual([f["title_id"] for f in self.failures()], ["PPSA01234"])
        self.srv._run.update(pid=None, started=1.0, finished=None, rc=None, error=None, log=None, log_tail=[])      # run N+1 starts
        self.srv._scan.update(running=True, started=1.0, finished=None, rc=None)
        self.assertEqual(self.get("/api/status")["plan_counts"]["failed"], 1)
        self.assertEqual(self.plan_entry()["action"], "failed")
        E._last_good = None                                                   # the container restarts: nothing in memory
        self.assertEqual([f["title_id"] for f in self.failures()], ["PPSA01234"])
        self.assertEqual([a["title_id"] for a in self.get("/api/status")["attention"]], ["PPSA01234"])

    def test_the_status_revision_follows_the_file(self):
        self.record()
        rev = self.get("/api/status")["failures_rev"]
        self.assertTrue(rev)
        self.record(code="ultrapack_failed")
        self.assertNotEqual(self.get("/api/status")["failures_rev"], rev)


class LibraryState(FailureCase):
    def test_failed_while_nothing_has_changed(self):
        self.record(data=dict(files=["sce_sys/param.json"], count=1))
        p = self.plan_entry()
        self.assertEqual(p["action"], "failed")
        self.assertIn("failed on", p["why"])
        self.assertIn("sce_sys/param.json", p["why"])
        self.assertEqual((p["failure"]["code"], p["failure"]["will_retry"]), ("crc_mismatch", False))
        self.assertEqual(self.get("/api/status")["plan_counts"], dict(build=0, copy=0, failed=1, done=0, skip=0, error=0))

    def test_eligible_again_when_the_override_or_the_source_changes(self):
        self.record()
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output=str(self.dir / "out"), temp="/work/tmp",
                                     overrides={"PPSA01234": {"modified_exec": "image"}}))
        p = self.plan_entry()
        self.assertEqual((p["action"], p["failure"]["will_retry"], p["failure"]["changed"]), ("build", True, True))
        self.assertIn("eligible for the next run", p["failure"]["next"])
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output=str(self.dir / "out"), temp="/work/tmp", overrides={}))
        self.assertEqual(self.plan_entry()["action"], "failed")
        self.put_plan([dict(ENTRY, size=6)])                                  # a different file was found
        self.assertEqual(self.plan_entry()["action"], "build")

    def test_a_note_does_not_make_it_eligible(self):
        self.record()
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output=str(self.dir / "out"), temp="/work/tmp",
                                     overrides={"PPSA01234": {"note": "thinking about it"}}))
        self.assertEqual(self.plan_entry()["action"], "failed")

    def test_a_plan_made_by_the_engine_as_failed_comes_back_as_build_when_the_record_is_gone(self):
        self.put_plan([dict(ENTRY, action="failed", why="failed on 2026-01-01 00:00, unchanged")])
        self.assertEqual((self.plan_entry()["action"], self.plan_entry()["why"]), ("build", "new"))

    def test_done_and_skipped_titles_are_not_touched(self):
        self.put_plan([dict(ENTRY, action="done", why="built")])
        self.record()
        p = self.plan_entry()
        self.assertEqual((p["action"], p["why"]), ("done", "built"))
        self.assertNotIn("failure", p)

    def test_a_new_app_version_makes_it_eligible(self):
        self.record()
        with mock.patch.object(F, "app_version", return_value="9.9.9"):
            self.assertEqual(self.plan_entry()["action"], "build")


class ThisRun(FailureCase):
    def test_results_are_explained_and_deferred_ones_say_they_wait(self):
        (self.state / "status.json").write_text(json.dumps(dict(updated=1, finished=True, results=[
            dict(title_id="PPSA01234", name="a.pkg", status="failed", why="prepare: GameError: x", code="crc_mismatch",
                 data=dict(files=["eboot.bin"], count=1)),
            dict(title_id="PPSA05678", name="b.pkg", status="deferred", why="needs ~330 GiB", code="no_space", data=dict(need=330 * GIB, usable=120 * GIB)),
            dict(title_id="PPSA09999", name="c.pkg", status="built"), dict(title_id="PPSA00001", name="d.pkg", status="failed", why="old style")])))
        r = self.get("/api/status")["status"]["results"]
        self.assertIn("differs from the original archive in 1 file: eboot.bin", r[0]["summary"])
        self.assertEqual(r[0]["why"], "prepare: GameError: x")
        self.assertIn("Needs about 330 GiB on the work drive; 120 GiB usable", r[1]["summary"])
        self.assertNotIn("summary", r[2])
        self.assertNotIn("summary", r[3])                                       # results without a code keep just their reason


class Actions(FailureCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(self.srv, "start_engine", return_value=True)
        self.start = p.start()
        self.addCleanup(p.stop)

    def post(self, path, **body):
        return self.client.post(path, json=body)

    def override(self):
        return json.loads(self.cfg_path.read_text()).get("overrides", {}).get("PPSA01234")

    def test_retry_makes_it_eligible_and_starts_a_run_for_that_title(self):
        self.record()
        r = self.post("/api/failures/PPSA01234/retry")
        self.assertEqual((r.status_code, r.json()["started"]), (200, True))
        self.start.assert_called_once_with("--only", "PPSA01234")
        self.assertTrue(self.store.load()["PPSA01234"]["retry"])
        self.assertEqual(self.plan_entry()["action"], "build")
        self.assertEqual(self.failures()[0]["next"], "Will be tried again with the next run.")

    def test_retry_while_a_run_is_active_leaves_it_for_the_next_run(self):
        self.record()
        self.start.return_value = False
        r = self.post("/api/failures/PPSA01234/retry").json()
        self.assertEqual((r["started"], "next run" in r["message"]), (False, True))
        self.assertTrue(self.store.load()["PPSA01234"]["retry"])

    def test_retry_that_cannot_start_says_why(self):
        self.record()
        self.start.side_effect = self.srv.EngineStartError(409, "The run did not start. Setup problem: Output")
        r = self.post("/api/failures/PPSA01234/retry")
        self.assertEqual((r.status_code, "Setup problem" in r.json()["detail"]), (409, True))

    def test_unknown_and_bad_titles(self):
        self.assertEqual(self.post("/api/failures/PPSA01234/retry").status_code, 404)
        self.assertEqual(self.post("/api/failures/nope/retry").status_code, 400)
        self.assertEqual(self.post("/api/failures/PPSA01234/dismiss").status_code, 404)
        self.record()
        self.assertEqual(self.post("/api/failures/PPSA01234/action", action="format_everything").status_code, 400)

    def test_dismiss_hides_it_from_needs_attention_only(self):
        self.record()
        self.assertEqual(self.post("/api/failures/PPSA01234/dismiss").json(), {"ok": True})
        self.assertEqual(self.get("/api/status")["attention"], [])
        self.assertEqual(len(self.failures()), 1)
        self.assertEqual(self.plan_entry()["action"], "failed")                 # the Library still says so
        self.record()                                                           # the same failure again: stays dismissed
        self.assertEqual(self.get("/api/status")["attention"], [])
        self.record(code="ultrapack_failed")                                    # a new kind: back in the card
        self.assertEqual(len(self.get("/api/status")["attention"]), 1)

    def test_use_the_archive_copy(self):
        self.record()
        r = self.post("/api/failures/PPSA01234/action", action="use_archive")
        self.assertEqual((r.status_code, r.json()["started"]), (200, True))
        self.assertEqual(self.override(), {"source": RAR})
        self.start.assert_called_once_with("--only", "PPSA01234")
        self.assertEqual(self.plan_entry()["action"], "build")                  # the override changed the fingerprint too

    def test_use_the_archive_needs_an_archive(self):
        self.put_plan([dict(ENTRY, verify_archive=None)])
        self.record(fingerprint=self.fp(dict(ENTRY, verify_archive=None)))
        self.assertNotIn("use_archive", self.action_ids())
        self.assertEqual(self.post("/api/failures/PPSA01234/action", action="use_archive").status_code, 400)

    def test_keep_this_copy_anyway(self):
        self.record()
        self.assertEqual(self.post("/api/failures/PPSA01234/action", action="keep_anyway").status_code, 200)
        self.assertEqual(self.override(), {"skip_verify": True})

    def test_keep_the_backport_or_use_the_original(self):
        self.record(code="modified_exec_unknown")
        self.post("/api/failures/PPSA01234/action", action="keep_backport")
        self.assertEqual(self.override(), {"modified_exec": "image"})
        self.post("/api/failures/PPSA01234/action", action="use_original")
        self.assertEqual(self.override(), {"modified_exec": "original"})

    def test_actions_add_to_the_options_already_set(self):
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output=str(self.dir / "out"), temp="/work/tmp",
                                     overrides={"PPSA01234": {"note": "mine", "patch": "/config/patches/PPSA01234"}}))
        self.record(fingerprint="whatever")
        self.post("/api/failures/PPSA01234/action", action="keep_anyway")
        self.assertEqual(self.override(), {"note": "mine", "patch": "/config/patches/PPSA01234", "skip_verify": True})

    def test_skip_this_source_picks_another_copy_or_skips_the_title(self):
        self.record(code="title_mismatch", data=dict(found="PPSA01235", expected="PPSA01234"))
        self.post("/api/failures/PPSA01234/action", action="skip_source")
        self.assertEqual(self.override(), {"source": RAR})
        self.put_plan([dict(ENTRY, candidates=[ENTRY["candidates"][0]])])
        self.record(code="title_mismatch", fingerprint="other")
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output=str(self.dir / "out"), temp="/work/tmp", overrides={}))
        self.post("/api/failures/PPSA01234/action", action="skip_source")
        self.assertEqual(self.override(), {"skip": True})

    def test_the_options_page_can_save_skip_verify(self):
        self.assertEqual(self.client.put("/api/overrides/PPSA01234", json={"skip_verify": True, "note": "n"}).status_code, 200)
        self.assertEqual(self.override(), {"note": "n", "skip_verify": True})
        self.assertEqual(self.get("/api/overrides")["PPSA01234"]["skip_verify"], True)

    def test_a_refused_action_changes_nothing(self):
        self.record()
        before = self.snapshot()
        self.assertEqual(self.post("/api/failures/PPSA01234/action", action="nonsense").status_code, 400)
        self.assertEqual(self.snapshot(), before)


class StagedPackage(FailureCase):
    def stage(self, name="PPSA01234"):
        d = self.dir / "out" / ".staging" / name
        d.mkdir(parents=True)
        (d / "game.pkg").write_bytes(b"x" * 2048)
        (d / "x.bin").write_bytes(b"y" * 100)
        return d

    def failed_validation(self, stage, stamp=True, **extra):
        """The failure as the engine records it: where the package was left, how big, and its stamp."""
        data = dict(summary="summary: 3 ok, 2 failed", failed_checks=2, staged=str(stage), staged_bytes=2148,
                    staged_stamp=F.dir_stamp(stage) if stamp else None, **extra)
        self.record(code="validate_failed", phase="validate", data=data)

    def delete(self):
        return self.client.post("/api/failures/PPSA01234/delete-staged")

    def assert_refused(self, stage, text):
        r = self.delete()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn(text, r.json()["detail"])
        self.assertTrue((stage / "game.pkg").exists() and (stage / "x.bin").exists())      # nothing was deleted
        self.assertEqual(self.store.load()["PPSA01234"]["data"]["staged"], str(stage))      # and the record still points at it

    def test_the_size_of_the_package_left_behind_is_shown_and_it_can_be_deleted(self):
        stage = self.stage()
        self.failed_validation(stage)
        f = self.failures()[0]
        self.assertEqual((f["staged"]["path"], f["staged"]["bytes"], f["staged"]["blocked"]), (str(stage), 2148, None))
        self.assertEqual(self.action_ids(f), ["view_log", "delete_staged", "retry"])
        self.assertIsNone([a for a in f["actions"] if a["id"] == "delete_staged"][0]["disabled"])
        self.assertIn("failed 2 validation checks", f["summary"])
        r = self.delete()
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True, "freed": 2148}))
        self.assertFalse(stage.exists())
        f = self.failures()[0]
        self.assertIsNone(f["staged"])
        self.assertNotIn("delete_staged", self.action_ids(f))
        self.assertEqual(self.delete().status_code, 404)
        self.assertEqual(f["state"], "failed")                                 # the failure itself stays
        self.assertIsNone(self.store.load()["PPSA01234"]["data"]["staged_stamp"])

    def test_delete_is_one_of_the_one_click_actions_too(self):
        stage = self.stage()
        self.failed_validation(stage)
        self.assertEqual(self.client.post("/api/failures/PPSA01234/action", json={"action": "delete_staged"}).status_code, 200)
        self.assertFalse(stage.exists())

    def test_only_folders_inside_the_staging_folder_can_be_deleted(self):
        outside = self.dir / "precious"
        outside.mkdir()
        (outside / "keep.txt").write_text("x")
        for sp in (str(outside), str(self.dir / "out" / ".staging"), str(self.dir / "out"), "/", str(self.dir / "out" / ".staging" / ".." / "..")):
            self.record(code="validate_failed", data=dict(staged=sp, staged_stamp=F.dir_stamp(sp)))
            with self.subTest(sp):
                self.assertEqual(self.delete().status_code, 404)
        self.assertTrue((outside / "keep.txt").exists())
        self.assertTrue((self.dir / "out").exists())

    def test_nothing_is_deleted_while_a_run_is_active(self):
        stage = self.stage()
        self.failed_validation(stage)
        with mock.patch.object(self.srv, "engine_pid", return_value=4242):        # a run holds the lock: Retry was just pressed
            self.assert_refused(stage, "a run is active")
            f = self.failures()[0]
            act = [a for a in f["actions"] if a["id"] == "delete_staged"][0]
            self.assertEqual((act["disabled"], f["staged"]["blocked"]), ("a run is active and may be using it",) * 2)
            self.assertEqual(self.client.post("/api/failures/PPSA01234/action", json={"action": "delete_staged"}).status_code, 409)
            self.assertTrue(stage.exists())
        self.assertEqual(self.delete().status_code, 200)                          # the run is over: allowed again

    def test_a_run_this_server_just_started_counts_too(self):
        stage = self.stage()
        self.failed_validation(stage)
        self.srv._run.update(pid=999, started=1.0, finished=None, rc=None)        # started, the engine has not taken its lock yet
        self.assert_refused(stage, "a run is active")
        self.srv._run.update(finished=2.0, rc=0)
        self.assertEqual(self.delete().status_code, 200)

    def test_a_folder_re_created_by_a_newer_attempt_is_never_deleted(self):
        stage = self.stage()
        self.failed_validation(stage)
        shutil.rmtree(stage)
        stage.mkdir()                                                              # the newer attempt's work folder
        (stage / "game.pkg").write_bytes(b"x" * 2048)
        (stage / "x.bin").write_bytes(b"y" * 100)                                  # even with the very same sizes
        os.utime(stage / "game.pkg", ns=(2 * 10**18, 2 * 10**18))
        self.assert_refused(stage, "changed after the failure was recorded")
        f = self.failures()[0]
        self.assertIn("changed after the failure", f["staged"]["blocked"])
        self.assertEqual([a["disabled"] for a in f["actions"] if a["id"] == "delete_staged"], [f["staged"]["blocked"]])

    def test_a_file_written_to_it_since_blocks_the_delete(self):
        for what in ("added", "rewritten", "removed", "touched"):
            with self.subTest(what):
                stage = self.stage(name=f"PPSA01234")
                self.failed_validation(stage)
                if what == "added":
                    (stage / "new.tmp").write_bytes(b"1")
                elif what == "rewritten":
                    (stage / "game.pkg").write_bytes(b"z" * 2048)
                    os.utime(stage / "game.pkg", ns=(3 * 10**18, 3 * 10**18))
                elif what == "removed":
                    (stage / "x.bin").unlink()
                else:
                    os.utime(stage / "x.bin", ns=(4 * 10**18, 4 * 10**18))
                self.assertEqual(self.delete().status_code, 409)
                self.assertTrue((stage / "game.pkg").exists())
                shutil.rmtree(stage)

    def test_a_record_without_a_stamp_cannot_be_confirmed_so_nothing_is_deleted(self):
        stage = self.stage()
        self.failed_validation(stage, stamp=False)
        self.assert_refused(stage, "can't be confirmed")
        self.assertIn("can't be confirmed", self.failures()[0]["staged"]["blocked"])


if __name__ == "__main__":
    unittest.main()
