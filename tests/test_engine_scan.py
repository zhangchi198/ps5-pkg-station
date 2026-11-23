"""What the engine does at the start of a scan or run when a folder is missing or unwritable: the downloads
folder is optional, a missing library is exit code 2 with a log line, and a run stops before doing any work
when it couldn't write its results. Standard library only."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

from support import ENGINE_DIR, E, StateCase, make_env

ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0     # root ignores file permissions: those tests can't run


class EngineScanAndRun(StateCase):
    def setUp(self):
        super().setUp()
        self.env = make_env(self.dir / "env")

    def run_engine(self, mode):
        return subprocess.run([sys.executable, "-I", "-u", str(ENGINE_DIR / "ps5_pkg_sync.py"), mode],
                              capture_output=True, text=True, env=os.environ.copy(), timeout=120)

    def log(self):
        return (self.logs / "pkg_sync.log").read_text()

    def test_a_scan_without_the_downloads_volume_finds_the_library_games(self):
        shutil.rmtree(self.env["downloads"])
        self.write_cfg({**E.DEFAULT_CFG, **self.env})
        r = self.run_engine("plan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("not mounted (optional); scanning the library only", self.log())
        plan = json.loads((self.state / "plan.json").read_text())
        self.assertEqual([(p["title_id"], p["action"]) for p in plan], [("PPSA01234", "build")])

    @unittest.skipIf(ROOT_USER, "root can read everything")
    def test_a_downloads_folder_that_cannot_be_read_is_skipped_too(self):
        os.chmod(self.env["downloads"], 0o000)
        self.addCleanup(os.chmod, self.env["downloads"], 0o755)
        self.write_cfg({**E.DEFAULT_CFG, **self.env})
        r = self.run_engine("plan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("scanning the library only", self.log())

    def test_a_missing_library_is_exit_2_and_a_log_line(self):
        shutil.rmtree(self.env["library"])
        self.write_cfg({**E.DEFAULT_CFG, **self.env})
        for mode in ("plan", "run"):
            with self.subTest(mode):
                r = self.run_engine(mode)
                self.assertEqual(r.returncode, 2)
                self.assertIn(f"not mounted: {self.env['library']}", r.stdout)
                self.assertNotIn("Traceback", r.stdout + r.stderr)

    @unittest.skipIf(ROOT_USER, "root can read everything")
    def test_an_unreadable_library_is_exit_2_not_a_traceback(self):
        os.chmod(self.env["library"], 0o000)
        self.addCleanup(os.chmod, self.env["library"], 0o755)
        self.write_cfg({**E.DEFAULT_CFG, **self.env})
        r = self.run_engine("plan")
        self.assertEqual(r.returncode, 2)
        self.assertIn("can't read", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    @unittest.skipIf(ROOT_USER, "root can write everywhere")
    def test_a_run_stops_at_once_when_output_is_not_writable_but_a_scan_does_not_care(self):
        os.chmod(self.env["output"], 0o555)
        self.addCleanup(os.chmod, self.env["output"], 0o755)
        self.write_cfg({**E.DEFAULT_CFG, **self.env})
        r = self.run_engine("run")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("setup problem: Output:", r.stdout)
        self.assertIn(f"not writable by uid {os.getuid()}", r.stdout)
        self.assertIn("write access", r.stdout)                        # the fix is in the log line too
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertFalse((self.state / "run.lock").exists())
        self.assertFalse((self.state / "plan.json").exists())        # stopped before scanning
        self.assertEqual(self.run_engine("plan").returncode, 0)

    def test_a_run_stops_at_once_when_the_ultrapack_files_are_missing(self):
        shutil.rmtree(self.env["ultrapack_src"])
        self.write_cfg({**E.DEFAULT_CFG, **self.env})
        r = self.run_engine("run")
        self.assertEqual(r.returncode, 5)
        self.assertIn("setup problem: UltraPack tools: missing: backend/cli.py", r.stdout)
        self.assertFalse((self.state / "run.lock").exists())


if __name__ == "__main__":
    unittest.main()
