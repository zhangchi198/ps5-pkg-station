"""The example compose file: each host folder is written once and feeds both the volume and the PS5PKG_HOST_*
variable, so the two can't drift. Standard library only (the `docker compose config` check runs when the docker
CLI is installed; it only parses the file)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest

from support import ROOT

TEXT = (ROOT / "docker-compose.yml").read_text()


class ExampleCompose(unittest.TestCase):
    def test_every_folder_uses_one_anchor_for_its_volume_and_its_variable(self):
        anchors = dict(re.findall(r"^\s{2}(\w+):\s+&(\w+)\s", TEXT, re.M))
        self.assertEqual(anchors, {k: k for k in ("library", "downloads", "output", "work", "config")})
        env = {k.lower(): v for k, v in re.findall(r"^\s+PS5PKG_HOST_(\w+): \*(\w+)", TEXT, re.M)}
        vol = {target: anchor for anchor, target in re.findall(r"source: \*(\w+),\s+target: /(\w+)", TEXT)}
        self.assertEqual(env, anchors)                         # PS5PKG_HOST_WORK: *work, ...
        self.assertEqual(vol, anchors)                         # source: *work, target: /work, ...
        self.assertEqual(len(re.findall(r"/path/to/", TEXT)), len(anchors))    # each path is written exactly once

    @unittest.skipUnless(shutil.which("docker"), "docker CLI not installed")
    def test_docker_compose_config_accepts_it(self):
        env = dict(os.environ, DOCKER_HOST="unix:///nonexistent.sock")         # parsing only: never talk to a daemon
        r = subprocess.run(["docker", "compose", "-f", str(ROOT / "docker-compose.yml"), "config", "-q"],
                           capture_output=True, text=True, env=env, timeout=60)
        if r.returncode and "is not a docker command" in r.stderr:
            self.skipTest("the docker compose plugin is not installed")
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
