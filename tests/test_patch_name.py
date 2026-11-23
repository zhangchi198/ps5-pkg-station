"""A patch folder that replaces sce_sys/param.json (the usual way to lower requiredSystemSoftwareVersion for an older
console): the package is named after the patched file, the ledger and the notification say so, an existing build under
the old name is still done (never rebuilt, never renamed), and a patch without a param.json changes nothing.
Standard library only."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from support import ENGINE_DIR, E, StateCase, make_env

import ps5_failures as F  # noqa: E402
from test_run_status import FAKE_ULTRAPACK  # noqa: E402

TID = "PPSA01234"
OLD = f"Example Game [{TID}] [v01.300.300] [fw13.60].pkg"
NEW = f"Example Game [{TID}] [v01.300.300] [fw12.00].pkg"


def param(fw="13.60", version="01.300.300", tid=TID, title="Example Game"):
    code = "0x" + "".join(f"{int(x):02d}" for x in fw.split(".")) + "0" * 12         # 13.60 -> 0x1360000000000000 (BCD in the top bytes)
    return {"titleId": tid, "contentVersion": version, "requiredSystemSoftwareVersion": code,
            "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": title}}}


class PatchCase(StateCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.dir / "env", game=False)
        self.cfg = {**copy.deepcopy(E.DEFAULT_CFG), **self.env, "ssd_reserve_gb": 0, "compression_level": 4, "cpu_count": 1}
        self.game = Path(self.env["library"]) / f"Example Game [{TID}]"
        (self.game / "sce_sys").mkdir(parents=True)
        (self.game / "eboot.bin").write_bytes(b"\0" * 16)
        (self.game / "sce_sys" / "param.json").write_text(json.dumps(param()))
        self.patch_dir = self.dir / "patches" / TID
        self.cfg["overrides"] = {TID: {"patch": str(self.patch_dir)}}

    def put_patch(self, obj=None, text=None, extra=True):
        (self.patch_dir / "sce_sys").mkdir(parents=True, exist_ok=True)
        if obj is not None or text is not None:
            (self.patch_dir / "sce_sys" / "param.json").write_text(text if text is not None else json.dumps(obj))
        if extra:
            (self.patch_dir / "eboot.bin").write_bytes(b"patched")

    def cands(self, version="01.300.300", fw="13.60"):
        return [dict(title_id=TID, title="Example Game", version=version, fw=fw, kind="folder", source=str(self.game), size=100, packed="")]

    def plan(self, ledger=None, **kw):
        return {p["title_id"]: p for p in E.choose(self.cfg, self.cands(**kw), ledger or {})}[TID]

    def old_build(self, **entry):
        """What an earlier version left: a package under the old name and its ledger entry."""
        out = Path(self.cfg["output"]) / OLD
        out.write_bytes(b"pkg")
        return {TID: dict(version="01.300.300", output=str(out), built="2026-11-20 03:00", **entry)}


class Planned(PatchCase):
    def test_the_plan_names_the_package_after_the_patched_param_json(self):
        self.put_patch(param(fw="12.00"))
        p = self.plan()
        self.assertEqual((p["name"], p["fw"], p["version"], p["action"]), (NEW, "12.00", "01.300.300", "build"))
        self.assertEqual((p["patched"], p["source_fw"], p["source_version"], p["source_name"]), (True, "13.60", "01.300.300", OLD))

    def test_a_patched_version_and_title_count_too(self):
        self.put_patch(param(fw="12.00", version="01.300.301", title="Example Game: Patched"))
        p = self.plan()
        self.assertEqual((p["version"], p["title"], p["source_version"]), ("01.300.301", "Example Game Patched", "01.300.300"))
        self.assertEqual(p["name"], f"Example Game Patched [{TID}] [v01.300.301] [fw12.00].pkg")

    def test_without_a_param_json_in_the_patch_nothing_changes(self):
        self.put_patch()                                                                 # only an eboot
        p = self.plan()
        self.assertEqual((p["name"], p["fw"]), (OLD, "13.60"))
        for key in ("patched", "source_name", "source_fw", "source_version"):
            self.assertNotIn(key, p)
        self.cfg["overrides"] = {}
        self.assertEqual(self.plan()["name"], OLD)                                       # and no patch at all

    def test_an_unusable_patch_param_json_is_left_to_the_build_to_report(self):
        for text in ("{not json", json.dumps(param(tid="PPSA09999")), json.dumps({"titleId": "PPSA01234"}), "[]", ""):
            with self.subTest(text[:30]):
                self.put_patch(text=text)
                p = self.plan()
                self.assertEqual((p["name"], p.get("patched")), (OLD, None))

    def test_a_patched_param_json_without_a_title_keeps_the_title(self):
        data = param(fw="12.00")
        data["localizedParameters"] = {"defaultLanguage": "en-US"}
        self.put_patch(data)
        p = self.plan()
        self.assertEqual((p["title"], p["name"]), ("Example Game", NEW))

    def test_the_patch_of_a_ps4_title_is_not_looked_at(self):
        self.put_patch(param(tid="CUSA12345", fw="12.00"))
        c = dict(self.cands()[0], title_id="CUSA12345", ps4=True, kind="archive-ps4", members=[], notes=[], title_en="", game_path="")
        self.cfg["overrides"] = {"CUSA12345": {"patch": str(self.patch_dir)}}
        p = {x["title_id"]: x for x in E.choose(self.cfg, [c], {})}["CUSA12345"]
        self.assertNotIn("patched", p)


class AlreadyBuilt(PatchCase):
    """The live case: built under the old name before this was fixed. It stays done; nothing is renamed or rebuilt."""

    def test_a_build_under_the_old_name_is_done_whatever_the_plan_would_call_it_now(self):
        self.put_patch(param(fw="12.00"))
        ledger = self.old_build()
        old_file = Path(ledger[TID]["output"])
        p = self.plan(ledger)
        self.assertEqual(p["action"], "done")
        self.assertEqual(p["name"], NEW)                                                 # the name it would get now...
        self.assertEqual(p["why"], f"built 2026-11-20 03:00: {OLD}")                       # ...and the file that is there
        self.assertTrue(old_file.exists())
        self.assertEqual(sorted(x.name for x in Path(self.cfg["output"]).iterdir() if x.is_file()), [OLD])      # nothing renamed
        self.assertEqual([x for x in E.choose(self.cfg, self.cands(), ledger) if x["action"] == "build"], [])

    def test_the_same_when_the_patch_changed_the_version_too(self):
        self.put_patch(param(fw="12.00", version="01.300.301"))
        self.assertEqual(self.plan(self.old_build())["action"], "done")                  # old entry: its version is the source's
        new_style = self.old_build(source_version="01.300.300")
        new_style[TID]["version"] = "01.300.301"                                         # what a build after the fix records
        self.assertEqual(self.plan(new_style)["action"], "done")

    def test_a_newer_source_is_still_built(self):
        self.put_patch(param(fw="12.00"))
        self.assertEqual(self.plan(self.old_build(), version="01.400.000")["action"], "build")
        ledger = self.old_build(source_version="01.300.300")
        ledger[TID]["version"] = "01.300.301"
        self.assertEqual(self.plan(ledger, version="01.400.000")["action"], "build")

    def test_a_missing_file_is_a_rebuild_as_before(self):
        self.put_patch(param(fw="12.00"))
        ledger = self.old_build()
        Path(ledger[TID]["output"]).unlink()
        self.assertEqual(self.plan(ledger)["action"], "build")

    def test_the_real_scan_says_so_too(self):
        # the whole path: a library folder, an override with a patch, the ledger and the file an older version made
        import subprocess
        self.put_patch(param(fw="12.00"))
        self.write_cfg({**self.cfg})
        (self.state / "ledger.json").write_text(json.dumps(self.old_build()))
        r = subprocess.run([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), "plan"], capture_output=True, text=True,
                           env=os.environ.copy(), timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        plan = json.loads((self.state / "plan.json").read_text())
        self.assertEqual([(p["title_id"], p["action"], p["name"]) for p in plan], [(TID, "done", NEW)])
        self.assertTrue((Path(self.cfg["output"]) / OLD).exists())


class Prepared(PatchCase):
    def job(self, **kw):
        return dict(self.plan(), **kw)

    def prepare(self, job, lines=None):
        lines = [] if lines is None else lines
        res = E.prepare(self.cfg, job, lines.append)
        return res, lines

    def test_the_name_follows_the_patch_when_the_game_is_prepared(self):
        self.put_patch(param(fw="12.00"))
        job = dict(self.plan(), name=OLD, fw="13.60")                                    # a plan made before the patch had a param.json
        res, lines = self.prepare(job)
        self.assertEqual((job["name"], job["fw"], job["version"]), (NEW, "12.00", "01.300.300"))
        self.assertEqual(job["source_version"], "01.300.300")
        self.assertTrue(any(ln == f"  name after patch: {NEW} (fw 13.60 -> 12.00)" for ln in lines), lines)
        marker = json.loads((Path(self.cfg["temp"]) / TID / ".prepared.json").read_text())
        self.assertEqual((marker["job"]["name"], marker["job"]["fw"]), (NEW, "12.00"))
        self.assertEqual(json.loads((res["app"] / "sce_sys" / "param.json").read_text())["requiredSystemSoftwareVersion"], param(fw="12.00")["requiredSystemSoftwareVersion"])

    def test_a_plan_that_already_has_the_name_is_not_logged_again(self):
        self.put_patch(param(fw="12.00"))
        job = self.plan()
        _, lines = self.prepare(job)
        self.assertEqual(job["name"], NEW)
        self.assertFalse([ln for ln in lines if "name after patch" in ln])

    def test_every_part_that_changed_is_named_in_the_log(self):
        self.put_patch(param(fw="12.00", version="01.300.301", title="Other Name"))
        job = dict(self.plan(), name=OLD, fw="13.60", version="01.300.300", title="Example Game")
        _, lines = self.prepare(job)
        self.assertTrue(any("(fw 13.60 -> 12.00, version 01.300.300 -> 01.300.301, title Example Game -> Other Name)" in ln for ln in lines), lines)
        self.assertEqual((job["version"], job["source_version"]), ("01.300.301", "01.300.300"))

    def test_without_a_param_json_in_the_patch_the_name_stays(self):
        self.put_patch()
        job = self.plan()
        _, lines = self.prepare(job)
        self.assertEqual((job["name"], job["fw"]), (OLD, "13.60"))
        self.assertNotIn("source_version", job)
        self.assertFalse([ln for ln in lines if "name after patch" in ln])
        self.assertIn("patch overlaid: eboot.bin", " ".join(lines))

    def test_a_patched_param_json_with_no_version_leaves_the_name_alone_and_says_so(self):
        data = param(fw="12.00")
        del data["contentVersion"]
        self.put_patch(data)
        job = dict(self.plan(), name=OLD, fw="13.60")
        _, lines = self.prepare(job)
        self.assertEqual((job["name"], job["fw"]), (OLD, "13.60"))
        self.assertTrue(any("has no contentVersion" in ln for ln in lines), lines)

    def test_a_patch_that_makes_it_another_game_is_an_error(self):
        self.put_patch(param(tid="PPSA09999", fw="12.00"))
        job = self.plan()
        with self.assertRaises(F.GameError) as cm:
            self.prepare(job)
        self.assertEqual((cm.exception.code, cm.exception.data["found"], cm.exception.data["expected"]), ("title_mismatch", "PPSA09999", TID))
        self.assertIn("after the patch", str(cm.exception))

    def test_a_copy_prepared_by_an_older_version_gets_the_right_name_when_it_is_reused(self):
        self.put_patch(param(fw="12.00"))
        job = dict(self.plan(), name=OLD, fw="13.60")
        self.prepare(job)                                                                # a prepared copy with its marker...
        marker_p = Path(self.cfg["temp"]) / TID / ".prepared.json"
        m = json.loads(marker_p.read_text())
        m["job"] = dict(m["job"], name=OLD, fw="13.60")                                  # ...as the old version wrote it
        marker_p.write_text(json.dumps(m))
        job2 = dict(self.plan(), name=OLD, fw="13.60", version="")
        _, lines = self.prepare(job2)
        self.assertTrue(any("reusing verified copy" in ln for ln in lines))
        self.assertEqual((job2["name"], job2["fw"]), (NEW, "12.00"))


@unittest.skipUnless(shutil.which("nice"), "needs nice(1)")
class Built(PatchCase):
    """A whole build with a stand-in for UltraPack."""

    def setUp(self):
        super().setUp()
        (Path(self.env["ultrapack_src"]) / "backend" / "cli.py").write_text(FAKE_ULTRAPACK)
        self.put_patch(param(fw="12.00"))
        p = mock.patch.dict(os.environ, {"FAKE_NAP": "0.01", "PS5PKG_STATUS_INTERVAL": "0.2"})
        p.start()
        self.addCleanup(p.stop)

    def build(self, job):
        notes, ledger = [], {}
        with mock.patch.object(E, "notify", lambda cfg, title, msg: notes.append((title, msg))):
            results = E.run_queue(self.cfg, [job], lambda m: None, self.state / "ledger.json", ledger)
        return results, notes, ledger

    def test_the_output_the_ledger_and_the_notification_say_the_patched_firmware(self):
        job = dict(self.plan(), name=OLD, fw="13.60")                                    # as a plan made before the fix would have it
        results, notes, ledger = self.build(job)
        self.assertEqual((results[0]["status"], results[0]["name"]), ("built", NEW))
        out = Path(self.cfg["output"]) / NEW
        self.assertTrue(out.is_file())
        self.assertFalse((Path(self.cfg["output"]) / OLD).exists())
        entry = json.loads((self.state / "ledger.json").read_text())[TID]
        self.assertEqual((Path(entry["output"]).name, entry["version"], entry["source_version"]), (NEW, "01.300.300", "01.300.300"))
        self.assertEqual(ledger[TID]["output"], str(out))
        self.assertEqual([n[0] for n in notes], ["PS5 pkg: built"])
        self.assertIn(NEW, notes[0][1])
        self.assertNotIn("fw13.60", notes[0][1])

    def test_a_rescan_after_it_says_done(self):
        results, _, ledger = self.build(self.plan())
        self.assertEqual(results[0]["status"], "built")
        p = self.plan(json.loads((self.state / "ledger.json").read_text()))
        self.assertEqual((p["action"], p["name"]), ("done", NEW))
        self.assertEqual(p["why"], f"built {ledger[TID]['built']}: {NEW}")
        self.assertEqual([x for x in E.choose(self.cfg, self.cands(), json.loads((self.state / "ledger.json").read_text())) if x["action"] == "build"], [])

    def test_a_patch_that_changes_the_version_is_recorded_and_still_done(self):
        self.put_patch(param(fw="12.00", version="01.300.301"))
        results, _, _ = self.build(self.plan())
        entry = json.loads((self.state / "ledger.json").read_text())[TID]
        self.assertEqual((entry["version"], entry["source_version"]), ("01.300.301", "01.300.300"))
        self.assertEqual(Path(entry["output"]).name, f"Example Game [{TID}] [v01.300.301] [fw12.00].pkg")
        self.assertEqual(self.plan(json.loads((self.state / "ledger.json").read_text()))["action"], "done")

    def test_without_a_param_json_in_the_patch_the_old_naming_is_unchanged(self):
        shutil.rmtree(self.patch_dir)
        self.put_patch()
        results, notes, _ = self.build(self.plan())
        self.assertEqual(results[0]["name"], OLD)
        entry = json.loads((self.state / "ledger.json").read_text())[TID]
        self.assertEqual((Path(entry["output"]).name, entry["version"], entry["source_version"]), (OLD, "01.300.300", "01.300.300"))


if __name__ == "__main__":
    unittest.main()
