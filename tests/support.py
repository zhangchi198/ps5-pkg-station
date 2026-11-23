"""Shared test plumbing: import the engine (and, when fastapi is installed, the web server) from app/ and
point both at a fresh temporary state/logs directory for every test.

Run everything with:  python3 -m unittest discover -s tests
The server tests need fastapi and httpx (pip install fastapi==0.115.6 uvicorn==0.32.1 httpx) and are
skipped when they are missing; the engine tests need only the standard library."""
from __future__ import annotations

import atexit
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = ROOT / "app" / "engine"
WEB_DIR = ROOT / "app" / "web"

# the engine reads PS5PKG_STATE when it is imported: never let a test fall back to a real directory
_BASE = Path(tempfile.mkdtemp(prefix="ps5pkg-tests-"))
atexit.register(shutil.rmtree, _BASE, ignore_errors=True)
os.environ["PS5PKG_STATE"] = str(_BASE / "import-state")
os.environ["PS5PKG_LOGS"] = str(_BASE / "import-logs")

sys.path.insert(0, str(ENGINE_DIR))
import ps5_pkg_sync as E  # noqa: E402


def server_module():
    """The web server module, or SkipTest when fastapi / httpx aren't installed."""
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
    except ImportError as ex:
        raise unittest.SkipTest(f"server tests need fastapi and httpx ({ex})")
    if str(WEB_DIR) not in sys.path:
        sys.path.insert(0, str(WEB_DIR))
    import server
    return server


def legacy_config(**over):
    """A config.json as a previous version wrote it: every default, then the user's changes and overrides."""
    cfg = copy.deepcopy(E.DEFAULT_CFG)
    cfg.update(console_fw="9.60", compression_level=5, max_builds=2, skip_download_prefixes=["PC_", "_x"],
               junk_title_patterns=[r"\[Site\]"], schedule={"enabled": True, "times": ["03:00", "15:30"]},
               overrides={"PPSA01234": {"source": "/srv/games/ps5/Example Game [PPSA01234]", "modified_exec": "image",
                                        "note": "backport", "skip": True},
                          "PPSA05678": {"patch": "/config/patches/PPSA05678"}})
    cfg.update(over)
    return cfg


class StateCase(unittest.TestCase):
    """Each test gets an empty state/ and logs/ directory; the engine (and server) are pointed at them."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="case-", dir=_BASE))
        self.state, self.logs = self.dir / "state", self.dir / "logs"
        self.state.mkdir()
        self.logs.mkdir()
        self.cfg_path = self.state / "config.json"
        patches = [mock.patch.object(E, "STATE", self.state), mock.patch.object(E, "CFG_PATH", self.cfg_path),
                   mock.patch.object(E, "LOGS", self.logs),
                   mock.patch.dict(os.environ, {"PS5PKG_STATE": str(self.state), "PS5PKG_LOGS": str(self.logs)})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        E._last_good = None
        self.addCleanup(lambda: setattr(E, "_last_good", None))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    # --- helpers
    def write_cfg(self, obj_or_text, age=True):
        """Put a config.json in place the way a person or an old version would. With `age`, its mtime is set
        far in the past, so any later rewrite shows up as a changed mtime."""
        text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text, indent=2, ensure_ascii=False)
        self.cfg_path.write_text(text, encoding="utf-8")
        if age:
            os.utime(self.cfg_path, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))
        return text

    def snapshot(self):
        st = self.cfg_path.stat()
        return self.cfg_path.read_bytes(), st.st_mtime_ns

    def stray_files(self):
        """Anything in state/ other than the settings file and its lock (leftover temp files would show here)."""
        return sorted(p.name for p in self.state.iterdir() if p.name not in ("config.json", "config.json.lock"))


GAME_PARAM = {"titleId": "PPSA01234", "contentVersion": "01.002.003", "requiredSystemSoftwareVersion": "0x0900000000000000",
              "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Example Game"}}}


def make_env(base, downloads=True, game=True):
    """A set-up install under `base`: a library with one extracted game folder, an output and a work folder, an
    optional downloads folder and fake UltraPack files. Returns the config keys that point at them."""
    base = Path(base)
    lib, out, up = base / "library", base / "output", base / "ultrapack"
    (base / "work").mkdir(parents=True, exist_ok=True)
    lib.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    if game:
        g = lib / "Example Game [PPSA01234]"
        (g / "sce_sys").mkdir(parents=True, exist_ok=True)
        (g / "eboot.bin").write_bytes(b"\0" * 16)
        (g / "sce_sys" / "param.json").write_text(json.dumps(GAME_PARAM))
    if downloads:
        (base / "downloads").mkdir(exist_ok=True)
    for rel, mode in (("backend/cli.py", 0o644), ("backend/native/7zz", 0o755), ("backend/native/ffpfsc-pkg-tool", 0o755)):
        f = up / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("#!/bin/sh\n")
        f.chmod(mode)
    return dict(library=str(lib), downloads=str(base / "downloads"), output=str(out), temp=str(base / "work" / "tmp"),
                ultrapack_src=str(up), python=sys.executable, console_fw="9.60")


class ServerCase(StateCase):
    """StateCase plus the web server module pointed at the same folders, and a test client."""

    def setUp(self):
        super().setUp()
        self.srv = server_module()
        from fastapi.testclient import TestClient
        # no PS5PKG_HOST_* from the environment the tests run in, and no mount table: tests set what they need
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in [k for k in os.environ if k.startswith("PS5PKG_HOST_")]:
            del os.environ[k]
        for name, val in (("STATE", self.state), ("LOGS", self.logs), ("MOUNTINFO", str(self.dir / "mountinfo"))):
            if hasattr(self.srv, name):
                p = mock.patch.object(self.srv, name, val)
                p.start()
                self.addCleanup(p.stop)
        self.srv._scan.update(running=False, started=None, finished=None, rc=None)
        if hasattr(self.srv, "_run"):
            self.srv._scan.update(error=None, log=None, log_tail=[])
            self.srv._run.update(pid=None, started=None, finished=None, rc=None, error=None, log=None, log_tail=[])
        self.client = TestClient(self.srv.app)

    def get(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()
