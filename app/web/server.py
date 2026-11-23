"""PS5 PKG Station - web UI and scheduler around engine/ps5_pkg_sync.py.

The engine does all the work in its own process (detached, so a UI restart never stops a build);
this server only reads its state files, edits config.json, and starts / stops runs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / "engine" / "ps5_pkg_sync.py"
STATE = Path(os.environ.get("PS5PKG_STATE", "/config/state"))
LOGS = Path(os.environ.get("PS5PKG_LOGS", "/config/logs"))
PY = sys.executable

sys.path.insert(0, str(ENGINE.parent))
import ps5_pkg_sync as E  # noqa: E402  (same defaults/validation as the engine)

# optional: where the container's folders are on the host, for display only, e.g.
#   PS5PKG_HOST_PATHS='{"library": "/srv/games/ps5", "output": "/srv/games/ps5/pkg"}'
try:
    HOST_PATHS = {k: str(v) for k, v in json.loads(os.environ.get("PS5PKG_HOST_PATHS") or "{}").items()}
except Exception:
    HOST_PATHS = {}

app = FastAPI(title="PS5 PKG Station", docs_url=None, redoc_url=None)
_scan = {"running": False, "started": None, "finished": None, "rc": None}
_lock = threading.Lock()

# ---------------------------------------------------------------- helpers

def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def load_cfg():
    """Effective settings; never raises and never writes. If config.json can't be parsed this is the last
    good copy (see E.load_cfg_info)."""
    return E.load_cfg()


def update_cfg(mutate):
    """Locked, atomic read-modify-write of config.json (see E.update_cfg). A file that can't be parsed
    is not overwritten: the save is refused with the reason."""
    try:
        return E.update_cfg(mutate)
    except E.ConfigError as ex:
        raise HTTPException(409, f"The settings file could not be read ({ex.describe()}). "
                                 f"Nothing was saved: fix the file first, then save again.")


def engine_pid():
    lock = STATE / "run.lock"
    try:
        pid = int(lock.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


class EngineStartError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def start_engine(*args):
    """Start a detached run. False when one is already active; raises EngineStartError when the engine
    process exits straight away (a settings file it can't read, a folder that isn't mounted, ...)."""
    with _lock:
        if engine_pid() or _scan["running"]:
            return False
        lock = STATE / "run.lock"
        if lock.exists():
            lock.unlink()                     # stale (pid gone)
        LOGS.mkdir(parents=True, exist_ok=True)
        out = open(LOGS / f"run_{time.strftime('%Y%m%d-%H%M%S')}.stdout.log", "ab")
        proc = subprocess.Popen([PY, "-I", "-u", str(ENGINE), "run", *args], stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())
        for _ in range(50):                   # wait for the lock so the UI shows "running" at once
            if engine_pid() or proc.poll() is not None:
                break
            time.sleep(0.1)
        rc = proc.poll()
        if rc not in (None, 0) and not engine_pid():          # exited before it could take the lock
            if rc == E.EXIT_BAD_CONFIG:
                raise EngineStartError(409, "The settings file (config.json) could not be read, so the run did "
                                            "not start. Fix the file and try again.")
            raise EngineStartError(500, f"The run did not start: the engine exited with code {rc}. See Logs.")
        return True


def run_scan():
    with _lock:
        if _scan["running"] or engine_pid():
            return False
        _scan.update(running=True, started=time.time(), finished=None, rc=None)

    def work():
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "scan.stdout.log", "wb") as out:
            rc = subprocess.run([PY, "-I", "-u", str(ENGINE), "plan"], stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=os.environ.copy()).returncode
        _scan.update(running=False, finished=time.time(), rc=rc)
    threading.Thread(target=work, daemon=True).start()
    return True


# ---------------------------------------------------------------- scheduler

def scheduler_tick(fired, now=None):
    """One look at the schedule (every 20 s). load_cfg() falls back to the last good settings when
    config.json is unreadable, so a typo in the file doesn't stop scheduled runs from being attempted."""
    sch = load_cfg().get("schedule") or {}
    if not sch.get("enabled"):
        return
    now = now or time.localtime()
    hm = time.strftime("%H:%M", now)
    key = time.strftime("%Y-%m-%d ", now) + hm
    if hm in (sch.get("times") or []) and key not in fired:
        fired.add(key)
        try:
            msg = "started" if start_engine() else "skipped (a run or scan is active)"
        except EngineStartError as ex:
            msg = f"did not start: {ex}"
        with open(LOGS / "pkg_sync.log", "a", encoding="utf-8") as fh:
            E.log_to(fh)(f"=== scheduled run {hm}: {msg}")


def scheduler():
    fired = set()
    while True:
        time.sleep(20)
        try:
            scheduler_tick(fired)
        except Exception:
            pass


threading.Thread(target=scheduler, daemon=True).start()

# ---------------------------------------------------------------- API

@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/api/status")
def status():
    st = read_json(STATE / "status.json", {})
    pid = engine_pid()
    info = E.load_cfg_info()
    cfg = info.cfg
    disks = {}
    for k in ("temp", "output", "library", "downloads"):
        try:
            u = shutil.disk_usage(cfg[k])
            disks[k] = dict(path=cfg[k], free=u.free, total=u.total)
        except Exception:
            disks[k] = dict(path=cfg.get(k), error="not available")
    plan = read_json(STATE / "plan.json", [])
    return dict(engine_running=bool(pid), engine_pid=pid, scan=_scan, status=st if pid else dict(st, live=False),
                disks=disks, load=os.getloadavg(), cpus=os.cpu_count(), now=time.time(),
                plan_time=(STATE / "plan.json").stat().st_mtime if (STATE / "plan.json").exists() else None,
                plan_counts={a: sum(1 for p in plan if p.get("action") == a) for a in ("build", "done", "skip", "error")},
                stop_requested=(STATE / "stop.request").exists(), schedule=cfg.get("schedule"),
                host_paths=HOST_PATHS, config_rev=info.rev, config_problems=info.problems,
                config_error=dict(info.error.as_dict(), using=info.fallback) if info.error else None)


@app.get("/api/plan")
def plan():
    return read_json(STATE / "plan.json", [])


@app.post("/api/scan")
def scan():
    if not run_scan():
        raise HTTPException(409, "a run or scan is already active")
    return {"ok": True}


@app.post("/api/run")
async def run(req: Request):
    body = await req.json() if (await req.body()) else {}
    only = [t for t in (body.get("only") or []) if re.fullmatch(r"(PPSA|CUSA)\d{5}", t)]
    try:
        started = start_engine(*(["--only", *only] if only else []))
    except EngineStartError as ex:
        raise HTTPException(ex.status, str(ex))
    if not started:
        raise HTTPException(409, "a run or scan is already active")
    return {"ok": True}


@app.post("/api/stop")
def stop():
    if not engine_pid():
        raise HTTPException(409, "no run is active")
    (STATE / "stop.request").write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
    return {"ok": True}


SETTINGS = E.SETTINGS   # key: (type, min, max) - everything the Settings page may change


@app.get("/api/config")
def get_config():
    # config_rev says which version of the file the values are from; the Settings page compares it with the
    # one in /api/status to notice a change on disk
    info = E.load_cfg_info()
    return {**{k: info.cfg.get(k) for k in SETTINGS}, "config_rev": info.rev}


@app.put("/api/config")
async def put_config(req: Request):
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    changes = {}
    for k, v in body.items():
        if k not in SETTINGS:
            raise HTTPException(400, f"unknown setting {k}")
        try:
            changes[k] = E.validate_setting(k, v)     # the same checks as when the file is read
        except ValueError as ex:
            raise HTTPException(400, str(ex))
    cfg = update_cfg(lambda raw: raw.update(changes))
    return {k: cfg.get(k) for k in SETTINGS}


@app.get("/api/overrides")
def get_overrides():
    return load_cfg().get("overrides", {})


@app.put("/api/overrides/{tid}")
async def put_override(tid: str, req: Request):
    if not re.fullmatch(r"(PPSA|CUSA)\d{5}", tid):
        raise HTTPException(400, "bad title id")
    body = await req.json()
    ov = {}
    for k in ("source", "patch", "note"):
        if body.get(k):
            ov[k] = str(body[k])
    if body.get("modified_exec") in ("image", "original"):
        ov["modified_exec"] = body["modified_exec"]
    if body.get("skip"):
        ov["skip"] = True

    def apply(raw):
        if not isinstance(raw.get("overrides"), dict):
            raw["overrides"] = {}
        if ov:
            raw["overrides"][tid] = ov
        else:
            raw["overrides"].pop(tid, None)
    return update_cfg(apply).get("overrides", {}).get(tid, {})


@app.get("/api/ledger")
def ledger():
    return read_json(STATE / "ledger.json", {})


SAFE_LOG = re.compile(r"^(pkg/)?[\w.\-]+\.log$")


@app.get("/api/logs")
def logs():
    out = []
    for p in sorted(LOGS.glob("*.log"), key=lambda p: -p.stat().st_mtime)[:30] + \
            sorted((LOGS / "pkg").glob("*.log"), key=lambda p: -p.stat().st_mtime)[:60]:
        out.append(dict(name=str(p.relative_to(LOGS)), size=p.stat().st_size, mtime=p.stat().st_mtime))
    return out


@app.get("/api/logs/{name:path}")
def log_tail(name: str, lines: int = 400):
    if not SAFE_LOG.match(name):
        raise HTTPException(400, "bad log name")
    p = LOGS / name
    if not p.exists():
        raise HTTPException(404)
    with open(p, "rb") as fh:
        fh.seek(max(0, p.stat().st_size - (2 << 20)))
        txt = fh.read().decode("utf-8", "replace").replace("\r", "\n")
    keep = [ln for ln in txt.split("\n") if ln.strip()]
    return PlainTextResponse("\n".join(keep[-max(10, min(lines, 5000)):]))


@app.get("/api/health")
def health():
    return {"ok": True}
