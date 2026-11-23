"""PS4 titles in the web API: planned as copy, the folder they go to with its host path, the counts, a failed copy
and its Retry, and the ledger entry that marks one as PS4. Needs fastapi and httpx (skipped without them)."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from support import E, ServerCase, legacy_config

import ps5_failures as F  # noqa: E402

TID = "CUSA12345"
SRC = "/downloads/Example Game/Example.Game.CUSA12345.part1.rar"
PS4 = dict(title_id=TID, title="Example Game", version="01.01", fw="", source=SRC, kind="archive-ps4", size=8000, action="copy", why="PS4: copy packages as they are",
           name=f"Example Game [{TID}]", ps4=True, dest_dir=f"/output/PS4/Example Game [{TID}]", candidates=[dict(source=SRC, kind="archive-ps4", version="01.01", packed="", size=8000)],
           files=[dict(name=f"Example Game [{TID}] [v01.00].pkg", size=5000, kind="game", path="x", via=None, dest="x"),
                  dict(name="HP0177-patch.pkg", size=3000, kind="patch", path="y", via=None, dest="y")],
           notes=["DLC.zip: no .pkg inside, nothing is copied from it"])
PS5 = dict(title_id="PPSA01234", title="Other", version="01.000.000", fw="9.00", source="/library/x", kind="exfat", size=5, action="build", why="new",
           name="Other.pkg", candidates=[])


class Ps4Api(ServerCase):
    def setUp(self):
        super().setUp()
        self.write_cfg(legacy_config(library="/library", downloads="/downloads", output="/output", temp="/work/tmp", overrides={}))
        (self.state / "plan.json").write_text(json.dumps([PS4, PS5]))

    def fp(self):
        return F.fingerprint(E.load_cfg(), PS4)

    def entry(self, tid=TID):
        return {p["title_id"]: p for p in self.get("/api/plan")}[tid]


class Planning(Ps4Api):
    def test_a_ps4_title_is_copy_and_counted_separately_from_builds(self):
        p = self.entry()
        self.assertEqual((p["action"], p["ps4"], p["dest_dir"]), ("copy", True, f"/output/PS4/Example Game [{TID}]"))
        self.assertEqual([f["kind"] for f in p["files"]], ["game", "patch"])
        self.assertEqual(self.get("/api/status")["plan_counts"], dict(build=1, copy=1, failed=0, done=0, skip=0, error=0))

    def test_the_folder_it_is_copied_to_has_a_host_path_when_known(self):
        with mock.patch.dict(os.environ, {"PS5PKG_HOST_OUTPUT": "/srv/games/ps5/pkg"}):
            p = self.entry()
        self.assertEqual(p["dest_dir_host"], f"/srv/games/ps5/pkg/PS4/Example Game [{TID}]")
        self.assertNotIn("dest_dir_host", self.entry())                                              # not without a host location

    def test_a_failed_copy_is_failed_while_unchanged_and_retry_brings_it_back_as_copy_not_build(self):
        F.Store(self.state / "failures.json").record(TID, code="extract_failed", phase="copy", message="7-Zip failed", fingerprint=self.fp(), title="Example Game",
                                                     data=dict(detail="boom"))
        p = self.entry()
        self.assertEqual((p["action"], p["failure"]["code"], p["failure"]["will_retry"]), ("failed", "extract_failed", False))
        with mock.patch.object(self.srv, "start_engine", return_value=True) as start:
            self.assertEqual(self.client.post(f"/api/failures/{TID}/retry").status_code, 200)
        start.assert_called_once_with("--only", TID)
        self.assertEqual(self.entry()["action"], "copy")
        self.assertEqual(self.get("/api/status")["plan_counts"]["copy"], 1)

    def test_a_ps4_title_waiting_for_room_stays_a_copy(self):
        F.Store(self.state / "failures.json").record(TID, state="waiting", code="output_no_space", phase="copy", message="m", fingerprint=self.fp(),
                                                     data=dict(need=8 << 30, free=1 << 30))
        p = self.entry()
        self.assertEqual((p["action"], p["failure"]["state"]), ("copy", "waiting"))
        self.assertIn("Needs about 8 GiB in the output folder; 1 GiB free", p["why"])

    def test_an_override_still_skips_it_and_the_skip_shows_in_the_plan_the_engine_made(self):
        (self.state / "plan.json").write_text(json.dumps([dict(PS4, action="skip", why="skipped by override"), PS5]))
        self.assertEqual((self.entry()["action"], self.entry()["why"]), ("skip", "skipped by override"))


class History(Ps4Api):
    def test_a_copied_ps4_title_is_in_the_ledger_marked_ps4_with_its_folder(self):
        (self.state / "ledger.json").write_text(json.dumps({TID: dict(version="01.01", output=f"/output/PS4/Example Game [{TID}]", source=SRC, built="2026-11-22 10:00",
                                                                     bytes=8000, ps4=True, kind="ps4", files=["a.pkg", "b.pkg"], validate="CRC verified by 7-Zip: 2 package(s)")}))
        with mock.patch.dict(os.environ, {"PS5PKG_HOST_OUTPUT": "/srv/games/ps5/pkg"}):
            v = self.get("/api/ledger")[TID]
        self.assertEqual((v["ps4"], v["files"]), (True, ["a.pkg", "b.pkg"]))
        self.assertEqual(v["output_host"], f"/srv/games/ps5/pkg/PS4/Example Game [{TID}]")


class Settings(Ps4Api):
    def test_ps4_mode_is_a_setting(self):
        self.assertEqual(self.get("/api/config")["ps4_mode"], "copy")
        self.assertEqual(self.client.put("/api/config", json={"ps4_mode": "skip"}).status_code, 200)
        self.assertEqual(self.get("/api/config")["ps4_mode"], "skip")
        r = self.client.put("/api/config", json={"ps4_mode": "convert"})
        self.assertEqual((r.status_code, r.json()["detail"]), (400, "ps4_mode must be copy or skip"))
        self.assertEqual(self.get("/api/config")["ps4_mode"], "skip")

    def test_it_is_exported_and_imported_with_the_other_settings(self):
        self.client.put("/api/config", json={"ps4_mode": "skip"})
        doc = self.client.get("/api/export").json()
        self.assertEqual(doc["settings"]["ps4_mode"], "skip")
        self.client.put("/api/config", json={"ps4_mode": "copy"})
        self.assertEqual(self.client.post("/api/import", content=json.dumps(doc), headers={"Content-Type": "application/json"}).status_code, 200)
        self.assertEqual(self.get("/api/config")["ps4_mode"], "skip")


if __name__ == "__main__":
    unittest.main()
