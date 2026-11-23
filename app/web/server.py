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
CFG = STATE / "config.json"
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
    return E.load_cfg()


def save_cfg(cfg):
    tmp = CFG.with_suffix(".part")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CFG)


def engine_pid():
    lock = STATE / "run.lock"
    try:
        pid = int(lock.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def start_engine(*args):
    with _lock:
        if engine_pid() or _scan["running"]:
            return False
        lock = STATE / "run.lock"
        if lock.exists():
            lock.unlink()                     # stale (pid gone)
        LOGS.mkdir(parents=True, exist_ok=True)
        out = open(LOGS / f"run_{time.strftime('%Y%m%d-%H%M%S')}.stdout.log", "ab")
        subprocess.Popen([PY, "-I", "-u", str(ENGINE), "run", *args], stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())
        for _ in range(50):                   # wait for the lock so the UI shows "running" at once
            if engine_pid():
                break
            time.sleep(0.1)
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

def scheduler():
    fired = set()
    while True:
        time.sleep(20)
        try:
            sch = load_cfg().get("schedule") or {}
            if not sch.get("enabled"):
                continue
            now = time.localtime()
            hm = time.strftime("%H:%M", now)
            key = time.strftime("%Y-%m-%d ", now) + hm
            if hm in (sch.get("times") or []) and key not in fired:
                fired.add(key)
                started = start_engine()
                E.log_to(open(LOGS / "pkg_sync.log", "a", encoding="utf-8"))(
                    f"=== scheduled run {hm}: " + ("started" if started else "skipped (a run or scan is active)"))
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
    cfg = load_cfg()
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
                host_paths=HOST_PATHS)


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
    if not start_engine(*(["--only", *only] if only else [])):
        raise HTTPException(409, "a run or scan is already active")
    return {"ok": True}


@app.post("/api/stop")
def stop():
    if not engine_pid():
        raise HTTPException(409, "no run is active")
    (STATE / "stop.request").write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
    return {"ok": True}


SETTINGS = {  # key: (type, min, max) - everything the Settings page may change
    "library": (str,), "downloads": (str,), "output": (str,), "temp": (str,),
    "compression_level": (int, 1, 9), "cpu_count": (int, 1, 64), "nice": (int, 0, 19),
    "max_compress": (int, 1, 4), "max_builds": (int, 1, 6), "ssd_reserve_gb": (int, 0, 10000),
    "build_on_ssd": (bool,), "console_fw": (str,), "notify_url": (str,),
    "skip_download_prefixes": (list,), "junk_file_patterns": (list,), "junk_title_patterns": (list,),
    "schedule": (dict,),
}


@app.get("/api/config")
def get_config():
    cfg = load_cfg()
    return {k: cfg.get(k) for k in SETTINGS}


@app.put("/api/config")
async def put_config(req: Request):
    body = await req.json()
    cfg = load_cfg()
    for k, v in body.items():
        spec = SETTINGS.get(k)
        if not spec:
            raise HTTPException(400, f"unknown setting {k}")
        t = spec[0]
        if t is int:
            v = int(v)
            if not spec[1] <= v <= spec[2]:
                raise HTTPException(400, f"{k} must be {spec[1]}..{spec[2]}")
        elif t is bool:
            v = bool(v)
        elif not isinstance(v, t):
            raise HTTPException(400, f"{k} must be {t.__name__}")
        if k == "console_fw" and v and not re.fullmatch(r"\d{1,2}\.\d{2}", v):
            raise HTTPException(400, "console_fw looks like 13.20")
        if k in ("junk_file_patterns", "junk_title_patterns"):
            v = [str(x) for x in v if str(x).strip()]
            for x in v:
                try:
                    re.compile(x)
                except re.error as ex:
                    raise HTTPException(400, f"{k}: bad pattern {x!r}: {ex}")
        if k == "schedule":
            times = [x for x in v.get("times", []) if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", x)]
            v = dict(enabled=bool(v.get("enabled")), times=times)
        cfg[k] = v
    save_cfg(cfg)
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
    cfg = load_cfg()
    if ov:
        cfg.setdefault("overrides", {})[tid] = ov
    else:
        cfg.get("overrides", {}).pop(tid, None)
    save_cfg(cfg)
    return cfg.get("overrides", {}).get(tid, {})


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
