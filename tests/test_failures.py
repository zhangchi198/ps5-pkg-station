"""Failed games: the retry fingerprint, the failure store and the explanations (ps5_failures). Standard library only."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from support import ENGINE_DIR, E, StateCase, make_env

import ps5_failures as F  # noqa: E402


class FingerprintCase(StateCase):
    """A title whose source is a real file, so the file's modification time and size can be changed."""

    def setUp(self):
        super().setUp()
        self.env = make_env(self.dir / "env")
        self.src = self.dir / "env" / "library" / "Example Game [PPSA01234].exfat"
        self.src.write_bytes(b"x" * 100)
        os.utime(self.src, (1_700_000_000, 1_700_000_000))
        self.twin = self.dir / "env" / "downloads" / "Example.rar"
        self.twin.write_bytes(b"r" * 50)
        os.utime(self.twin, (1_700_000_000, 1_700_000_000))
        self.cfg = {**copy.deepcopy(E.DEFAULT_CFG), **self.env}
        self.entry = dict(title_id="PPSA01234", source=str(self.src), size=100, verify_archive=str(self.twin))

    def fp(self, cfg=None, entry=None, version="0.2.1"):
        return F.fingerprint(cfg or self.cfg, entry or self.entry, version)


class Fingerprint(FingerprintCase):
    def test_the_same_inputs_give_the_same_fingerprint(self):
        self.assertEqual(self.fp(), self.fp())
        self.assertRegex(self.fp(), r"^[0-9a-f]{40}$")

    def test_what_changes_it(self):
        base = self.fp()
        changes = {}
        # the source file is replaced or edited
        os.utime(self.src, (1_700_000_500, 1_700_000_500))
        changes["source mtime"] = self.fp()
        os.utime(self.src, (1_700_000_000, 1_700_000_000))
        self.src.write_bytes(b"x" * 101)
        os.utime(self.src, (1_700_000_000, 1_700_000_000))
        changes["source size"] = self.fp()
        self.src.write_bytes(b"x" * 100)
        os.utime(self.src, (1_700_000_000, 1_700_000_000))
        self.assertEqual(self.fp(), base)
        changes["another source"] = self.fp(entry=dict(self.entry, source=str(self.src) + ".2"))
        os.utime(self.twin, (1_700_000_500, 1_700_000_500))
        changes["twin archive"] = self.fp()
        os.utime(self.twin, (1_700_000_000, 1_700_000_000))
        # the options for the title
        for k, v in dict(source="/x", patch="/p", modified_exec="image", skip=True, skip_verify=True).items():
            changes[f"override {k}"] = self.fp(cfg=dict(self.cfg, overrides={"PPSA01234": {k: v}}))
        # settings that decide what happens to it
        changes["console_fw"] = self.fp(cfg=dict(self.cfg, console_fw="10.00"))
        changes["junk file patterns"] = self.fp(cfg=dict(self.cfg, junk_file_patterns=["x"]))
        changes["junk title patterns"] = self.fp(cfg=dict(self.cfg, junk_title_patterns=["x"]))
        # the app itself: an image update can fix an environment problem
        changes["app version"] = self.fp(version="0.2.2")
        for what, value in changes.items():
            with self.subTest(what):
                self.assertNotEqual(value, base)
        self.assertEqual(len(set(changes.values())), len(changes), "every change gives its own fingerprint")

    def test_what_does_not_change_it(self):
        base = self.fp()
        unrelated = dict(self.cfg, compression_level=3, cpu_count=2, nice=1, notify_url="http://x", schedule={"enabled": True, "times": ["01:00"]},
                         overrides={"PPSA01234": {"note": "words"}, "PPSA09999": {"skip": True}})
        self.assertEqual(self.fp(cfg=unrelated), base)
        self.assertEqual(self.fp(entry=dict(self.entry, name="renamed", why="x", candidates=[1])), base)

    def test_an_edited_patch_folder_counts(self):
        patch = self.dir / "patch"
        patch.mkdir()
        os.utime(patch, (1_700_000_000, 1_700_000_000))
        cfg = dict(self.cfg, overrides={"PPSA01234": {"patch": str(patch)}})
        a = self.fp(cfg=cfg)
        os.utime(patch, (1_700_000_900, 1_700_000_900))
        self.assertNotEqual(self.fp(cfg=cfg), a)

    def test_a_missing_source_still_has_a_fingerprint(self):
        self.src.unlink()
        self.assertRegex(self.fp(), r"^[0-9a-f]{40}$")

    def test_blocks_only_a_failure_that_has_not_changed(self):
        rec = dict(state="failed", fingerprint=self.fp(version=F.app_version()))
        self.assertTrue(F.blocks(self.cfg, self.entry, rec))
        self.assertFalse(F.blocks(self.cfg, self.entry, None))
        self.assertFalse(F.blocks(self.cfg, self.entry, dict(rec, retry=True)))            # Retry was pressed
        self.assertFalse(F.blocks(self.cfg, self.entry, dict(rec, state="waiting")))       # waiting for space: always retried
        os.utime(self.src, (1_700_000_777, 1_700_000_777))
        self.assertFalse(F.blocks(self.cfg, self.entry, rec))                              # the source was replaced
        self.assertFalse(F.blocks(dict(self.cfg, overrides={"PPSA01234": {"modified_exec": "image"}}), dict(self.entry), dict(rec, fingerprint=self.fp(version=F.app_version()))))

    def test_a_new_app_version_makes_a_failure_eligible_again(self):
        rec = dict(state="failed", fingerprint=self.fp(version="0.2.1"))
        self.assertTrue(F.blocks(self.cfg, self.entry, rec, version="0.2.1"))
        self.assertFalse(F.blocks(self.cfg, self.entry, rec, version="0.2.2"))


class AppVersion(unittest.TestCase):
    def test_it_is_the_version_file_of_the_app(self):
        self.assertEqual(F.app_version(), (ENGINE_DIR.parent.parent / "VERSION").read_text().strip())
        self.assertNotEqual(F.app_version(), "unknown")


class Store(StateCase):
    def store(self):
        return F.Store(self.state / "failures.json")

    def rec(self, tid="PPSA01234", **kw):
        kw = dict(title="Example Game", name="Example Game.pkg", phase="prepare", code="crc_mismatch", message="1 extracted file(s) differ",
                  data={"files": ["sce_sys/param.json"], "count": 1}, log="pkg_sync.log", excerpt=["a", "b"], fingerprint="fp1", version="0.2.1", **kw)
        return self.store().record(tid, **kw)

    def test_a_failure_survives_in_the_file_and_reads_back(self):
        self.assertTrue(self.rec(now=1_700_000_000))
        got = F.Store(self.state / "failures.json").load()["PPSA01234"]            # a fresh object: nothing in memory
        self.assertEqual((got["state"], got["code"], got["attempts"], got["fingerprint"], got["retry"], got["dismissed"]),
                         ("failed", "crc_mismatch", 1, "fp1", False, False))
        self.assertEqual((got["data"], got["excerpt"], got["log"]), ({"files": ["sce_sys/param.json"], "count": 1}, ["a", "b"], "pkg_sync.log"))
        self.assertRegex(got["at_text"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")

    def test_the_same_failure_again_is_counted_not_announced(self):
        self.assertTrue(self.rec(now=1))
        self.assertFalse(self.rec(now=2))
        self.assertFalse(self.rec(now=3))
        got = self.store().load()["PPSA01234"]
        self.assertEqual((got["attempts"], got["first_at"], got["at"], len(got["history"])), (3, 1, 3, 3))

    def test_a_different_failure_is_announced(self):
        self.rec(now=1)
        self.assertTrue(self.store().record("PPSA01234", code="ultrapack_failed", fingerprint="fp1", message="x", phase="build"))
        self.assertTrue(self.store().record("PPSA01234", code="ultrapack_failed", fingerprint="fp2", message="x", phase="build"))   # app updated
        self.assertEqual(self.store().load()["PPSA01234"]["attempts"], 3)

    def test_retry_dismiss_and_a_repeat(self):
        self.rec()
        self.assertTrue(self.store().set_flag("PPSA01234", retry=True, dismissed=True))
        self.assertFalse(self.store().set_flag("PPSA09999", retry=True))
        got = self.store().load()["PPSA01234"]
        self.assertEqual((got["retry"], got["dismissed"]), (True, True))
        self.assertFalse(self.rec())                                        # fails the same way after the retry: still silent
        got = self.store().load()["PPSA01234"]
        self.assertEqual((got["retry"], got["dismissed"], got["attempts"]), (False, True, 2))
        self.store().record("PPSA01234", code="other", fingerprint="fp1")  # a new kind of failure comes back
        self.assertFalse(self.store().load()["PPSA01234"]["dismissed"])

    def test_clear(self):
        self.rec()
        self.rec("PPSA05678", state="waiting")
        self.assertFalse(self.store().clear("PPSA01234", only_state="waiting"))
        self.assertTrue(self.store().clear("PPSA05678", only_state="waiting"))
        self.assertTrue(self.store().clear("PPSA01234"))
        self.assertFalse(self.store().clear("PPSA01234"))
        self.assertEqual(self.store().load(), {})

    def test_excerpt_and_history_are_capped(self):
        for i in range(30):
            self.store().record("PPSA01234", code="c", fingerprint="f", message=f"m{i}", excerpt=[str(n) for n in range(100)], now=i + 1)
        got = self.store().load()["PPSA01234"]
        self.assertEqual((len(got["excerpt"]), len(got["history"]), got["attempts"]), (F.EXCERPT_LINES, F.HISTORY_LINES, 30))
        self.assertEqual(got["history"][-1]["message"], "m29")

    def test_reading_never_writes_and_odd_files_are_empty(self):
        s = self.store()
        self.assertEqual(s.load(), {})
        self.assertFalse((self.state / "failures.json").exists())
        s.update(lambda d: None)                                            # a no-op doesn't create it either
        self.assertFalse((self.state / "failures.json").exists())
        for text in ("", "[1]", "not json", '{"PPSA01234": 5, "PPSA05678": {"state": "failed"}}'):
            (self.state / "failures.json").write_text(text)
            self.assertEqual(set(s.load()), {"PPSA05678"} if "PPSA05678" in text else set())

    def test_a_no_op_leaves_the_file_alone(self):
        self.rec()
        os.utime(self.state / "failures.json", ns=(10**18, 10**18))
        self.store().clear("PPSA09999")
        self.store().set_flag("PPSA09999", retry=True)
        self.assertEqual((self.state / "failures.json").stat().st_mtime_ns, 10**18)

    def test_concurrent_writers_lose_nothing_across_threads_and_processes(self):
        def go(n):
            for i in range(10):
                self.store().record(f"PPSA{n:05d}", code=f"c{i}", fingerprint="f")
        threads = [threading.Thread(target=go, args=(n,)) for n in range(4)]
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import ps5_failures as F\n"
                "s = F.Store(sys.argv[2])\n"
                "for i in range(10): s.record('PPSA0000%d' % (5 + int(sys.argv[3])), code='c%d' % i, fingerprint='f')\n")
        procs = [subprocess.Popen([sys.executable, "-I", "-c", code, str(ENGINE_DIR), str(self.state / "failures.json"), str(n)]) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        got = self.store().load()
        self.assertEqual(sorted(got), [f"PPSA{n:05d}" for n in range(4)] + ["PPSA00005", "PPSA00006"])
        self.assertTrue(all(v["attempts"] == 10 for v in got.values()), {k: v["attempts"] for k, v in got.items()})
        self.assertEqual([p.name for p in self.state.iterdir() if p.suffix == ".part"], [])


class Explanations(unittest.TestCase):
    def say(self, code, **data):
        return F.explain(dict(code=code, data=data, message="raw message", title_id="PPSA01234"))

    def test_crc_mismatch_names_the_files_up_to_five_then_a_count(self):
        self.assertEqual(self.say("crc_mismatch", files=["sce_sys/param.json"], count=1)["summary"],
                         "The library copy differs from the original archive in 1 file: sce_sys/param.json. It may be modified or damaged.")
        five = [f"a/{i}.bin" for i in range(5)]
        self.assertEqual(self.say("crc_mismatch", files=five, count=5)["summary"],
                         "The library copy differs from the original archive in 5 files: " + ", ".join(five) + ". It may be modified or damaged.")
        many = [f"a/{i}.bin" for i in range(50)]
        s = self.say("crc_mismatch", files=many, count=73)["summary"]
        self.assertIn("in 73 files: " + ", ".join(many[:5]) + " and 68 more.", s)
        self.assertNotIn("a/5.bin", s)

    def test_the_other_codes(self):
        self.assertIn("console firmware is not set", self.say("modified_exec_unknown")["summary"])
        self.assertEqual(self.say("no_space", need=310 * 2**30, usable=120 * 2**30)["summary"],
                         "Needs about 310 GiB on the work drive; 120 GiB usable. Will be retried automatically.")
        self.assertEqual(self.say("validate_failed", failed_checks=2, summary="summary: x")["summary"],
                         "The package was built but failed 2 validation checks. It was not moved to the output folder.")
        self.assertIn("failed validation", self.say("validate_failed")["summary"])
        self.assertEqual(self.say("title_mismatch", found="PPSA01235", expected="PPSA01234")["summary"],
                         "The source contains PPSA01235, not PPSA01234.")
        self.assertIn("copying it to the output folder failed", self.say("copy_failed")["summary"])
        self.assertIn("sce_sys/param.json", self.say("no_param_json")["summary"])
        self.assertIn("Extracting", self.say("extract_failed", detail="boom")["summary"])
        self.assertEqual(self.say("extract_failed", detail="boom")["detail"], ["boom"])

    def test_ultrapack_names_the_stage_and_shows_the_error_lines_and_hints_at_the_image(self):
        e = self.say("ultrapack_failed", stage_text="Preparing the fPKG identity",
                     errors=["[ERROR] Could not resolve the fPKG identity: No module named 'ultra_core'"])
        self.assertEqual(e["summary"], "UltraPack stopped with an error during Preparing the fPKG identity.")
        self.assertIn("No module named 'ultra_core'", e["detail"][0])
        self.assertIn("Updating the app tries the game again", e["detail"][-1])
        plain = self.say("ultrapack_failed", tail=["line 1", "line 2"])
        self.assertEqual((plain["summary"], plain["detail"]), ("UltraPack stopped with an error.", ["line 1", "line 2"]))

    def test_an_unknown_code_falls_back_to_the_raw_message(self):
        self.assertEqual(self.say("something_new")["summary"], "raw message")
        self.assertEqual(F.explain({})["summary"], "The game failed for a reason the app doesn't know.")

    def test_typed_errors(self):
        e = F.GameError("no_space", "needs more", need=5, usable=1)
        self.assertEqual((F.classify(e), str(e), isinstance(e, RuntimeError)), (("no_space", "prepare", {"need": 5, "usable": 1}), "needs more", True))
        self.assertEqual(F.classify(F.GameError("copy_failed", "x", phase="copy"))[1], "copy")
        self.assertEqual(F.classify(ValueError("x")), ("unknown", "prepare", {}))


if __name__ == "__main__":
    unittest.main()
