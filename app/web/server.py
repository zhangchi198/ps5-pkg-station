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
import ps5_hostpaths as H  # noqa: E402  (where the container's folders are on the host)
import ps5_setup as S  # noqa: E402  (the first-run checklist)

MOUNTINFO = "/proc/self/mountinfo"      # the hint for "where is /library on the host" (see ps5_hostpaths)

app = FastAPI(title="PS5 PKG Station", docs_url=None, redoc_url=None)
# What the last scan / run did. rc is the engine's exit code; when it isn't 0, `error` says why in plain words
# and `log_tail` is the end of that log, so the Dashboard never has to show a bare "Idle" after a failure.
_scan = {"running": False, "started": None, "finished": None, "rc": None, "error": None, "log": None, "log_tail": []}
_run = {"pid": None, "started": None, "finished": None, "rc": None, "error": None, "log": None, "log_tail": []}
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


def host_view(cfg):
    """Where each folder is, in the container and on the host (see ps5_hostpaths for the precedence): {folders,
    configured, problems, mapping}. Read per call: it is cheap and the settings can change."""
    return H.folders_for(cfg, state=STATE, logs=LOGS, environ=os.environ, mountinfo=MOUNTINFO)


def folders_info(cfg):
    v = host_view(cfg)
    return v["folders"], v["configured"], v["problems"]


def host_twins(obj, mapping, keys):
    """A copy of the dict `obj` with `<key>_host` next to each container path that has a host location. The real
    fields stay as they are (the engine works on container paths); a path without a host location gets no twin,
    so a caller can tell it is still a container path."""
    if not mapping or not isinstance(obj, dict):
        return obj
    out = dict(obj)
    for k in keys:
        host = H.display_path(obj.get(k), mapping)
        if host:
            out[k + "_host"] = host
    return out


def text_twin(obj, key, mapping):
    """`<key>_host`: free text (a reason, a list of lines) with container prefixes swapped, when that changes it."""
    if not mapping or not isinstance(obj, dict):
        return obj
    val = obj.get(key)
    if isinstance(val, str):
        host = H.display_text(val, mapping)
    elif isinstance(val, list):
        host = [H.display_text(x, mapping) if isinstance(x, str) else x for x in val]
    else:
        return obj
    return dict(obj, **{key + "_host": host}) if host != val else obj


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


def tail_lines(path, n=12):
    """The last few non-empty lines of a log file."""
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, os.path.getsize(path) - 65536))
            txt = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [ln.strip()[:300] for ln in txt.replace("\r", "\n").split("\n") if ln.strip()][-n:]


def explain_exit(rc):
    """An engine exit code in plain words (see the EXIT_ codes in the engine)."""
    cfg = load_cfg()
    if rc == E.EXIT_NOT_MOUNTED:
        return (f"The game library folder ({cfg['library']}) was not found or can't be read. Mount it there in "
                f"docker-compose.yml, or change the folder under Settings.")
    if rc == E.EXIT_BAD_CONFIG:
        return "The settings file (config.json) could not be read. Fix the file and try again."
    if rc == E.EXIT_SETUP:
        fails = S.blocking(S.run_checks(cfg, state_dir=STATE))
        return ("Setup problem: " + "; ".join(f"{r['title']}: {r['message']}" for r in fails)) if fails else \
            "A setup check failed; see the Setup checklist."
    if rc == E.EXIT_LOCKED:
        return "Another run is already active."
    if rc < 0:
        return f"The engine was stopped (signal {-rc})."
    return f"The engine stopped unexpectedly (exit code {rc})."


def record_exit(rec, rc, logname):
    rec.update(finished=time.time(), rc=rc, log=logname, error=None if rc == 0 else explain_exit(rc),
               log_tail=[] if rc == 0 else tail_lines(LOGS / logname))


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
        logname = f"run_{time.strftime('%Y%m%d-%H%M%S')}.stdout.log"
        with open(LOGS / logname, "ab") as out:               # the child has its own copy of the descriptor
            proc = subprocess.Popen([PY, "-I", "-u", str(ENGINE), "run", *args], stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())
        _run.update(pid=proc.pid, started=time.time(), finished=None, rc=None, error=None, log=logname, log_tail=[])

        def watch():                          # notes how the run ended, whenever that is (and reaps the child)
            rc = proc.wait()
            if _run["pid"] == proc.pid:
                record_exit(_run, rc, logname)
        threading.Thread(target=watch, daemon=True).start()
        for _ in range(50):                   # wait for the lock so the UI shows "running" at once
            if engine_pid() or proc.poll() is not None:
                break
            time.sleep(0.1)
        rc = proc.poll()
        if rc not in (None, 0) and not engine_pid():          # exited before it could take the lock
            record_exit(_run, rc, logname)
            raise EngineStartError(409 if rc in (E.EXIT_NOT_MOUNTED, E.EXIT_BAD_CONFIG, E.EXIT_SETUP) else 500,
                                   f"The run did not start. {explain_exit(rc)}")
        return True


def run_scan():
    with _lock:
        if _scan["running"] or engine_pid():
            return False
        _scan.update(running=True, started=time.time(), finished=None, rc=None, error=None, log=None, log_tail=[])

    def work():
        rc = 1
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            with open(LOGS / "scan.stdout.log", "wb") as out:
                rc = subprocess.run([PY, "-I", "-u", str(ENGINE), "plan"], stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=os.environ.copy()).returncode
        except Exception as ex:                   # e.g. the engine can't be started at all
            try:
                with open(LOGS / "scan.stdout.log", "a", encoding="utf-8") as fh:
                    fh.write(f"could not start the scan: {type(ex).__name__}: {ex}\n")
            except OSError:
                pass
        finally:
            record_exit(_scan, rc, "scan.stdout.log")
            _scan["running"] = False
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
    host = host_view(cfg)
    folders, configured, host_problems, mapping = host["folders"], host["configured"], host["problems"], host["mapping"]
    if mapping and isinstance(st, dict) and isinstance(st.get("results"), list):   # reasons mention container paths
        st = dict(st, results=[text_twin(r, "why", mapping) for r in st["results"]])
    disks = {}
    for k, f in folders.items():
        try:
            u = shutil.disk_usage(f["container"])
            disks[k] = dict(path=f["container"], free=u.free, total=u.total)
        except Exception:
            disks[k] = dict(path=f["container"], error="not available")
    plan = read_json(STATE / "plan.json", [])
    return dict(engine_running=bool(pid), engine_pid=pid, clock=S.clock_info(),
                scan=text_twin(dict(_scan), "log_tail", mapping), run=text_twin(dict(_run), "log_tail", mapping),
                status=st if pid else dict(st, live=False),
                disks=disks, load=os.getloadavg(), cpus=os.cpu_count(), now=time.time(),
                plan_time=(STATE / "plan.json").stat().st_mtime if (STATE / "plan.json").exists() else None,
                plan_counts={a: sum(1 for p in plan if p.get("action") == a) for a in ("build", "done", "skip", "error")},
                stop_requested=(STATE / "stop.request").exists(), schedule=cfg.get("schedule"),
                folders=folders, host_paths=configured, host_problems=host_problems, in_docker=E.IN_DOCKER,
                config_rev=info.rev, config_problems=info.problems,
                config_error=dict(info.error.as_dict(), using=info.fallback) if info.error else None)


@app.get("/api/plan")
def plan():
    """plan.json with `source_host` / `patch_host` / `verify_archive_host`, `candidates[].source_host` and
    `skipped_duplicates_host` where the folder has a host location."""
    data, mapping = read_json(STATE / "plan.json", []), host_view(load_cfg())["mapping"]
    if not mapping or not isinstance(data, list):
        return data
    out = []
    for p in data:
        p = host_twins(p, mapping, ("source", "patch", "verify_archive"))
        p = text_twin(text_twin(p, "skipped_duplicates", mapping), "why", mapping)
        if isinstance(p, dict) and isinstance(p.get("candidates"), list):
            p = dict(p, candidates=[host_twins(c, mapping, ("source",)) for c in p["candidates"]])
        out.append(p)
    return out


@app.get("/api/setup")
def setup():
    """The first-run checklist: [{id, level ok|warn|fail, title, message, fix, advisory, link}]. Touches the
    disks (a write test in each folder that must be writable), so the page asks on load and on demand."""
    cfg = load_cfg()
    _, configured, host_problems = folders_info(cfg)
    return S.run_checks(cfg, state_dir=STATE, plan=read_json(STATE / "plan.json", []), host_paths=configured,
                        host_problems=host_problems)


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
    return {**{k: info.cfg.get(k) for k in SETTINGS}, "config_rev": info.rev, "in_docker": E.IN_DOCKER,
            "folders": folders_info(info.cfg)[0]}


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


def overrides_with_hosts(ovs, mapping):
    if not isinstance(ovs, dict):
        return ovs
    return {tid: host_twins(ov, mapping, ("source", "patch")) for tid, ov in ovs.items()}


@app.get("/api/overrides")
def get_overrides():
    cfg = load_cfg()
    return overrides_with_hosts(cfg.get("overrides", {}), host_view(cfg)["mapping"])


@app.put("/api/overrides/{tid}")
async def put_override(tid: str, req: Request):
    if not re.fullmatch(r"(PPSA|CUSA)\d{5}", tid):
        raise HTTPException(400, "bad title id")
    body = await req.json()
    ov = {}
    mapping = host_view(load_cfg())["mapping"]
    for k in ("source", "patch", "note"):
        if body.get(k):
            ov[k] = str(body[k])
            if k != "note":                   # a path typed as it is on the host is stored as the container path
                ov[k] = H.to_container(ov[k], mapping) or ov[k]
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
    cfg = update_cfg(apply)
    return host_twins(cfg.get("overrides", {}).get(tid, {}), host_view(cfg)["mapping"], ("source", "patch"))


@app.get("/api/ledger")
def ledger():
    """ledger.json with `output_host` (the package) and `source_host`. Entries written by older versions (other
    field names, missing fields) pass through untouched."""
    data, mapping = read_json(STATE / "ledger.json", {}), host_view(load_cfg())["mapping"]
    if not mapping or not isinstance(data, dict):
        return data
    return {tid: host_twins(v, mapping, ("output", "source")) for tid, v in data.items()}


SAFE_LOG = re.compile(r"^(pkg/)?[\w.\-]+\.log$")


@app.get("/api/logs")
def logs():
    out = []
    for p in sorted(LOGS.glob("*.log"), key=lambda p: -p.stat().st_mtime)[:30] + \
            sorted((LOGS / "pkg").glob("*.log"), key=lambda p: -p.stat().st_mtime)[:60]:
        out.append(dict(name=str(p.relative_to(LOGS)), size=p.stat().st_size, mtime=p.stat().st_mtime))
    return out


@app.get("/api/logs/{name:path}")
def log_tail(name: str, lines: int = 400, host: int = 0):
    """The end of a log. With host=1 container folder prefixes are shown as their host locations (display only:
    the file is never changed, and without host=1 the text is exactly what is on disk)."""
    if not SAFE_LOG.match(name):
        raise HTTPException(400, "bad log name")
    p = LOGS / name
    if not p.exists():
        raise HTTPException(404)
    with open(p, "rb") as fh:
        fh.seek(max(0, p.stat().st_size - (2 << 20)))
        txt = fh.read().decode("utf-8", "replace").replace("\r", "\n")
    keep = [ln for ln in txt.split("\n") if ln.strip()]
    out = "\n".join(keep[-max(10, min(lines, 5000)):])
    return PlainTextResponse(H.display_text(out, host_view(load_cfg())["mapping"]) if host else out)


@app.get("/api/health")
def health():
    return {"ok": True}
