"""PS5 PKG Station - web UI and scheduler around engine/ps5_pkg_sync.py.

The engine does all the work in its own process (detached, so a UI restart never stops a build);
this server only reads its state files, edits config.json, and starts / stops runs."""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent / "engine" / "ps5_pkg_sync.py"
sys.path.insert(0, str(HERE))             # ps5_websec lives next to this file, however the app is started
STATE = Path(os.environ.get("PS5PKG_STATE", "/config/state"))
LOGS = Path(os.environ.get("PS5PKG_LOGS", "/config/logs"))
PY = sys.executable

sys.path.insert(0, str(ENGINE.parent))
import ps5_pkg_sync as E  # noqa: E402  (same defaults/validation as the engine)
import ps5_failures as FL  # noqa: E402  (games that failed: why, and when another try can help)
import ps5_progress as PG  # noqa: E402  (what earlier builds say about how long a game takes)
import ps5_settingsmeta as M  # noqa: E402  (what the Settings page shows and checks)
import ps5_runctl as RC  # noqa: E402  (stop, cancel and pause requests: the files the engine and the scheduler read)
import ps5_history as PH  # noqa: E402  (what runs did: history.jsonl, ledger entries of every version, logs, replaced packages)
import ps5_notify as PN  # noqa: E402  (what is sent where, and what became of it)
import ps5_hostpaths as H  # noqa: E402  (where the container's folders are on the host)
import ps5_settingsio as IO  # noqa: E402  (export / import of the settings)
import ps5_setup as S  # noqa: E402  (the first-run checklist)
import ps5_websec as WS  # noqa: E402  (host / origin checks, the optional password, response headers)

MOUNTINFO = "/proc/self/mountinfo"      # the hint for "where is /library on the host" (see ps5_hostpaths)

app = FastAPI(title="PS5 PKG Station", docs_url=None, redoc_url=None)
app.add_middleware(WS.Guard, state_dir=lambda: STATE)      # no login by default: see ps5_websec for what is checked instead
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


def versions():
    """What is running: the app's own version and the UltraPack release it was built with (the image sets it)."""
    return dict(app=FL.app_version(), ultrapack=os.environ.get("PS5PKG_ULTRAPACK_VERSION") or None)


def failure_store():
    return FL.Store(STATE / "failures.json")


def failures_rev():
    try:
        st = (STATE / "failures.json").stat()
    except OSError:
        return None
    return f"{st.st_mtime_ns}-{st.st_size}"


LOG_LABEL = {"ultrapack_failed": "View build log", "validate_failed": "View report"}


def run_active():
    """An engine run (or one just started by this server) that may be working in the staging folder."""
    return bool(engine_pid()) or (_run.get("pid") is not None and _run.get("finished") is None)


def staged_block_reason(rec, sp):
    """Why the staged package of a failure must not be deleted right now, or None. It is the live work folder of
    a build of that title, so nothing is deleted during a run; and it must still be the folder that was there when
    the failure was recorded (a newer attempt re-creates or writes to it)."""
    if run_active():
        return "a run is active and may be using it"
    stamp = (rec.get("data") or {}).get("staged_stamp")
    if not stamp:
        return "it can't be confirmed to be the package of that failure"
    if FL.dir_stamp(sp) != stamp:
        return "it changed after the failure was recorded (a newer attempt?)"
    return None


def failure_actions(rec, entry, view):
    """What can be done about a failure: [{id, label, kind}], kind api (POST .../action), goto (a Settings field),
    log (open that log) or retry."""
    code, acts = rec.get("code"), []
    log = {"id": "view_log", "label": LOG_LABEL.get(code, "View log"), "kind": "log", "log": rec.get("log") or "pkg_sync.log"}
    retry = {"id": "retry", "label": "Retry", "kind": "retry"}
    if rec.get("state") == FL.WAITING:
        return [{"id": "open_settings", "label": "Open Settings", "kind": "goto", "field": "ssd_reserve_gb"}, log]
    if code == "crc_mismatch":
        if (entry or {}).get("verify_archive"):
            acts.append({"id": "use_archive", "label": "Use the archive copy", "kind": "api"})
        acts += [{"id": "keep_anyway", "label": "Keep this copy anyway", "kind": "api"}, log]
    elif code == "modified_exec_unknown":
        acts += [{"id": "set_console_fw", "label": "Set console firmware", "kind": "goto", "field": "console_fw"},
                 {"id": "keep_backport", "label": "Keep backport", "kind": "api"},
                 {"id": "use_original", "label": "Use original", "kind": "api"}]
    elif code == "validate_failed":
        delete = dict(id="delete_staged", label="Delete staged package", kind="api", disabled=(view.get("staged") or {}).get("blocked"))
        acts += [log] + ([delete] if view.get("staged") else []) + [retry]
    elif code == "title_mismatch":
        acts += [{"id": "skip_source", "label": "Skip this source", "kind": "api"}, log]
    else:
        acts += [log, retry]
    if not any(a["id"] == "retry" for a in acts):             # every failed game has a one-click Retry
        acts.append(retry)
    return acts


def failure_view(rec, entry, cfg, mapping):
    """One stored failure for the API: the record, its explanation in words, whether it will be tried again by
    itself, the staged package (if any) and what can be done."""
    ex = FL.explain(rec)
    state = rec.get("state")
    changed = bool(entry) and rec.get("fingerprint") != FL.fingerprint(cfg, entry)
    will_retry = state == FL.WAITING or bool(rec.get("retry")) or changed
    sp = (rec.get("data") or {}).get("staged")
    staged = (dict(path=sp, bytes=E.dir_size(sp), blocked=staged_block_reason(rec, sp)) if sp and os.path.isdir(sp) else None)
    v = dict(rec, summary=ex["summary"], detail=ex["detail"], will_retry=will_retry, changed=changed, staged=staged,
             source=(entry or {}).get("source"))
    v["actions"] = failure_actions(rec, entry, v)
    if will_retry and state == FL.FAILED:
        v["next"] = ("Will be tried again with the next run." if rec.get("retry") else
                     "Something it depends on has changed since: it is eligible for the next run.")
    elif state == FL.FAILED:
        v["next"] = ("Not retried by scheduled runs until its source or options change, the app is updated, "
                     "or you press Retry.")
    else:
        v["next"] = "Retried automatically when there is room."
    v = host_twins(v, mapping, ("source",))
    if v["staged"]:
        v["staged"] = host_twins(v["staged"], mapping, ("path",))
    return text_twin(v, "excerpt", mapping)


def explain_result(r):
    """A result of "This run" with its failure in words (`summary`) when the engine gave it a code."""
    if isinstance(r, dict) and r.get("code") and r.get("status") in ("failed", "deferred"):
        return dict(r, summary=FL.explain(dict(code=r["code"], data=r.get("data") or {}, message=r.get("why") or ""))["summary"])
    return r


def plan_entries(cfg):
    data = read_json(STATE / "plan.json", [])
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []


_json_cache = {}


def read_json_cached(p: Path, default):
    """read_json, parsed again only when the file changed (the ledger is read on every poll of every open page)."""
    try:
        st = p.stat()
    except OSError:
        return default
    key = (st.st_mtime_ns, st.st_size)
    hit = _json_cache.get(str(p))
    if hit and hit[0] == key:
        return hit[1]
    data = read_json(p, default)
    _json_cache[str(p)] = (key, data)
    return data


def built_after(v, since):
    """Was this ledger entry written after `since` (the epoch time the plan was made)? Entries from older versions
    only know the minute: those count when that minute began after the plan was made."""
    ts = v.get("built_ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return ts > since
    try:
        return time.mktime(time.strptime(str(v.get("built", "")), "%Y-%m-%d %H:%M")) > since
    except (ValueError, OverflowError):
        return False


def live_plan(entries):
    """The plan as it stands now. plan.json is written when a scan or a run starts, so a game built or copied
    since is still `build` / `copy` in it: here it is `done` (the same line the next scan would write), and the
    counts and the Library follow a run as it goes."""
    try:
        plan_time = (STATE / "plan.json").stat().st_mtime
    except OSError:
        return entries
    ledger = read_json_cached(STATE / "ledger.json", {})
    if not isinstance(ledger, dict) or not ledger:
        return entries
    out = []
    for p in entries:
        v = ledger.get(p.get("title_id")) if isinstance(p, dict) else None
        if (isinstance(p, dict) and p.get("action") in ("build", "copy", "failed") and isinstance(v, dict) and built_after(v, plan_time)
                and (not p.get("version") or E.vkey(str(v.get("version", ""))) >= E.vkey(str(p["version"])))):
            p = dict(p, action="done", why=f"built {v.get('built', '')}: {Path(str(v.get('output', ''))).name}", live_done=True)
        out.append(p)
    return out


def build_pace(cfg):
    """What earlier builds of this install say about the time a game takes: seconds per source GiB to prepare and to
    build at the current compression level, and how many builds that rests on (None until there is one)."""
    ledger = read_json_cached(STATE / "ledger.json", {})
    h = PG.history(ledger if isinstance(ledger, dict) else {}, cfg.get("compression_level"))
    return dict(build_per_gib=h["build_per_gib"], prep_per_gib=h["prep_per_gib"], builds=h["builds"])


def run_preview(cfg, plan):
    """What a run started now would do, for the confirmation: {builds, copies, bytes (of the sources), seconds (about how long, from
    what earlier builds took here, None until there has been one), work_free is the page's: it has the disks}."""
    builds = sorted((p for p in plan if p.get("action") == "build" and not p.get("ps4")), key=lambda p: int(p.get("size") or 0))
    copies = [p for p in plan if p.get("action") == "copy"]
    ledger = read_json_cached(STATE / "ledger.json", {})
    h = PG.history(ledger if isinstance(ledger, dict) else {}, cfg.get("compression_level"))
    seconds = None
    if builds and h["build_per_gib"] and not (copies and not h.get("ps4_per_gib")):
        shares = PG.normalise(PG.shares_from(h), with_copy=bool(cfg.get("build_on_ssd")))
        queue = []
        for p in builds:
            gib = int(p.get("size") or 0) / PG.GIB
            total = h["build_per_gib"] * gib
            queue.append(dict(prep=(h["prep_per_gib"] or 0.0) * gib, total=total, kraken=total * shares["1"]))
        end = PG.simulate(0.0, [], queue, max_builds=int(cfg.get("max_builds", 3)), max_compress=int(cfg.get("max_compress", 2)))
        if end is not None:
            seconds = end + sum(int(p.get("size") or 0) / PG.GIB * (h.get("ps4_per_gib") or 0.0) for p in copies)
    return dict(builds=len(builds), copies=len(copies), bytes=sum(int(p.get("size") or 0) for p in builds + copies), seconds=seconds)


def pending_cancels_in(st, running):
    """The games a cancel was asked for and that are not out of the run yet: the engine's list plus the requests it has not taken."""
    if not running:
        return []
    mine = [t for t in (st.get("cancelling") or []) if isinstance(t, str)] if isinstance(st, dict) and not st.get("finished") else []
    return sorted(set(mine) | set(RC.pending_cancels(STATE)))


def library_extras(p, cfg, ledger):
    """A plan entry as the Library shows it, with what depends on the settings of the moment (so a change to the console
    firmware or a backport option shows at once, without a new scan): the backport decision, and whether the copy that
    will be used asks for more firmware than the console has (`fw_check`: need, console, ok = True / False / None)."""
    if not isinstance(p, dict) or p.get("ps4"):
        return p
    tid, console = p.get("title_id"), (cfg.get("console_fw") or "").strip()
    bp, need = p.get("backport"), (p.get("fw") if isinstance(p.get("fw"), str) else "")
    if isinstance(bp, dict):
        ov = (cfg.get("overrides") or {}).get(tid) or {}
        image_fw, original_fw = (str(bp.get(k) or "") for k in ("image_fw", "original_fw"))
        present, decision, by = E.backport_state(image_fw, original_fw, console, ov.get("modified_exec") if isinstance(ov, dict) else None)
        if present and not decision and p.get("patched"):
            by = f"the patch sets the package's firmware to {need or '?'}; the copy only matters if the executables differ"
        p = dict(p, backport=dict(bp, present=present, console_fw=console, decision=decision, decided_by=by))
        # the firmware the package will carry decides whether it runs: a patch's param.json (already in `fw`) beats the copies' own
        if present and decision and not p.get("patched"):
            need = (original_fw if decision == "original" else image_fw) or need
    if need:
        p = dict(p, fw_check=dict(need=need, console=console, ok=(E.vkey(need) <= E.vkey(console)) if console else None))
    last = (ledger or {}).get(tid) if isinstance(ledger, dict) else None
    if p.get("action") == "done" and isinstance(last, dict) and isinstance(last.get("backport"), dict):
        p = dict(p, last_backport=last["backport"])
    return p


def next_run(schedule, now=None):
    """When the schedule starts the next run, from the server's clock: {at, time, day: today|tomorrow, label}, or
    None when nothing is scheduled."""
    if not isinstance(schedule, dict) or not schedule.get("enabled"):
        return None
    times = sorted(t for t in (schedule.get("times") or []) if isinstance(t, str) and len(t) == 5 and t[2] == ":")
    if not times:
        return None
    now = now or time.time()
    lt = time.localtime(now)
    for day_offset in (0, 1):
        for t in times:
            try:
                h, m = int(t[:2]), int(t[3:])
                at = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset, h, m, 0, 0, 0, -1))
            except ValueError:
                continue
            if at > now:
                day = "today" if day_offset == 0 else "tomorrow"
                return dict(at=at, time=t, day=day, label=f"{day} {t}")
    return None


def last_run(st, running):
    """What the last finished run did, from status.json: {started, finished, trigger, total, done, built, copied,
    failed, deferred, interrupted}. None while a run is active or when there has been none. A status file from
    before runs were described has only the results: their count and the time of the file stand in."""
    if running or not isinstance(st, dict) or not st:
        return None
    run = st.get("run")
    results = st.get("results") if isinstance(st.get("results"), list) else []
    if isinstance(run, dict) and run.get("started"):
        interrupted = bool(run.get("interrupted")) or not st.get("finished")
        counts = {k: int(run.get(k) or 0) for k in ("built", "copied", "failed", "deferred", "cancelled")}
        return dict(started=run.get("started"), finished=run.get("finished") or st.get("updated"), trigger=run.get("trigger"),
                    total=run.get("total") or run.get("done") or 0, done=int(run.get("done") or 0), interrupted=interrupted, **counts)
    if not results:
        return None
    counts = {s: sum(1 for r in results if isinstance(r, dict) and r.get("status") == s) for s in ("built", "copied", "failed", "deferred", "cancelled")}
    return dict(started=None, finished=st.get("updated"), trigger=None, total=len(results), done=len(results), interrupted=not st.get("finished"), **counts)


def plan_with_failures(plan, cfg, mapping):
    """The plan as the page should show it: a title that failed is `failed` while nothing it depends on has
    changed and nobody asked for a retry (the engine skips it too), a title waiting for space stays `build` with
    the numbers, and each carries its `failure`."""
    store = failure_store().load()
    out = []
    for p in plan:
        rec = store.get(p.get("title_id")) if isinstance(p, dict) else None
        if isinstance(p, dict) and p.get("action") in ("build", "copy", "failed"):
            todo_action = "copy" if p.get("ps4") else "build"         # what the engine does with it when it is tried
            if rec:
                fv = failure_view(rec, p, cfg, mapping)
                p = dict(p, failure=fv, action=todo_action if fv["will_retry"] else "failed",
                         why=(f"failed on {rec.get('at_text', '?')}: {fv['summary']} {fv['next']}" if rec.get("state") == FL.FAILED
                              else f"waiting for space: {fv['summary']}"))
            elif p.get("action") == "failed":
                p = dict(p, action=todo_action, why="new")
        out.append(p)
    return out


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


def server_log(msg):
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / "pkg_sync.log", "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def server_notify(event, title, message):
    """A message from the web server's side: a scheduled run that was skipped or stopped, a settings file that can't be read. Never raises."""
    try:
        return PN.send(load_cfg(), event, title, message, state=STATE, log=server_log)
    except Exception:
        return None


class EngineStartError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def start_engine(*args, trigger="manual"):
    """Start a detached run (`trigger`: manual or scheduled, shown on the Dashboard). False when one is already
    active; raises EngineStartError when the engine process exits straight away (a settings file it can't read, a
    folder that isn't mounted, ...)."""
    with _lock:
        if engine_pid() or _scan["running"]:
            return False
        lock = STATE / "run.lock"
        if lock.exists():
            lock.unlink()                     # stale (pid gone)
        LOGS.mkdir(parents=True, exist_ok=True)
        logname = f"run_{time.strftime('%Y%m%d-%H%M%S')}.stdout.log"
        with open(LOGS / logname, "ab") as out:               # the child has its own copy of the descriptor
            proc = subprocess.Popen([PY, "-I", "-u", str(ENGINE), "run", "--trigger", trigger, *args], stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True, env=os.environ.copy())
        _run.update(pid=proc.pid, started=time.time(), finished=None, rc=None, error=None, log=logname, log_tail=[])

        def watch():                          # notes how the run ended, whenever that is (and reaps the child)
            rc = proc.wait()
            if _run["pid"] == proc.pid:
                record_exit(_run, rc, logname)
            # a scheduled run nobody is watching that ends badly is told to a person. The engine says it itself when it can (a folder not
            # mounted, a failing setup check, another run holding the lock); what it can't (a settings file it can't read, a crash) is said here
            if trigger == "scheduled" and rc != 0 and rc not in (E.EXIT_NOT_MOUNTED, E.EXIT_SETUP, E.EXIT_LOCKED):
                server_notify("attention", "PS5 pkg: scheduled run did not finish", explain_exit(rc))
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
            if RC.pause_read(STATE, time.mktime(now)):             # a run that comes due while paused is skipped, not made up later
                msg = "skipped (the schedule is paused)"
            elif start_engine(trigger="scheduled"):
                msg = "started"
            else:
                msg = "skipped (a run or scan is active)"
                server_notify("attention", "PS5 pkg: scheduled run skipped", f"The run planned for {hm} did not start: a run or scan was still active.")
        except EngineStartError as ex:
            msg = f"did not start: {ex}"
        with open(LOGS / "pkg_sync.log", "a", encoding="utf-8") as fh:
            E.log_to(fh)(f"=== scheduled run {hm}: {msg}")


_replan = {"pending": False}


def request_replan():
    """Plan again so the Library shows what an option change does, without anyone pressing Scan: at once when
    nothing runs (with the scan cache warm that is about a second), else as soon as the run or scan is over (the
    scheduler's tick sees the flag). Returns "now", "after_run" or "after_scan": what the page tells the user."""
    if run_scan():
        _replan["pending"] = False
        return "now"
    _replan["pending"] = True
    return "after_run" if engine_pid() else "after_scan"


def replan_tick():
    if _replan["pending"] and not engine_pid() and not _scan["running"]:
        _replan["pending"] = False
        run_scan()


def config_tick():
    """A settings file that can't be read is told once (and again if it breaks again after it was fixed): the scheduled runs go on with the
    last settings that worked, so nobody would otherwise notice."""
    info = E.load_cfg_info()
    if info.error:
        if PN.once(STATE, "config_error", True):
            server_notify("attention", "PS5 pkg: settings file can't be read", f"{info.error.describe()} The last settings that worked are used until it is fixed.")
    else:
        PN.once(STATE, "config_error", False)


def scheduler():
    fired = set()
    while True:
        time.sleep(20)
        try:
            scheduler_tick(fired)
            replan_tick()
            config_tick()
        except Exception:
            pass


threading.Thread(target=scheduler, daemon=True).start()

# ---------------------------------------------------------------- API

NO_CACHE = {"Cache-Control": "no-cache"}
LOGIN_DELAY = 1.0                 # seconds a wrong password waits for its answer
_throttle = WS.LoginThrottle()


@app.get("/")
def index(request: Request):
    if WS.password() and not request.scope.get("ps5_authed"):
        return HTMLResponse(WS.login_page(), headers=NO_CACHE)
    return FileResponse(HERE / "static" / "index.html", headers=NO_CACHE)


@app.get("/static/app.js")
def app_js():
    """The page's script: a file of its own, so the Content-Security-Policy can forbid inline script."""
    return FileResponse(HERE / "static" / "app.js", media_type="text/javascript", headers=NO_CACHE)


@app.get("/login")
def login_page():
    return RedirectResponse("/", status_code=303)           # the form is the front page while there is no session


@app.post("/login")
async def login(request: Request):
    """The login form (only when PS5PKG_PASSWORD is set): a signed session cookie, HttpOnly and SameSite=Strict."""
    pw = WS.password()
    if not pw:
        return RedirectResponse("/", status_code=303)
    who = request.client.host if request.client else "?"
    if _throttle.blocked(who):
        return HTMLResponse(WS.login_page("Too many wrong passwords. Wait a few minutes, then try again."), status_code=429, headers=NO_CACHE)
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    if not WS.password_ok((form.get("password") or [""])[0], pw):
        _throttle.fail(who)
        await asyncio.sleep(LOGIN_DELAY)
        return HTMLResponse(WS.login_page("Wrong password."), status_code=401, headers=NO_CACHE)
    _throttle.ok(who)
    r = RedirectResponse("/", status_code=303)
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"
    r.set_cookie(WS.COOKIE, WS.make_session(STATE, pw), max_age=WS.SESSION_DAYS * 86400, httponly=True, samesite="strict", secure=https, path="/")
    return r


@app.post("/api/logout")
def logout():
    r = JSONResponse({"ok": True})
    r.delete_cookie(WS.COOKIE, path="/", httponly=True, samesite="strict")
    return r


@app.get("/api/status")
def status(request: Request):
    st = read_json(STATE / "status.json", {})
    if not isinstance(st, dict):
        st = {}
    pid = engine_pid()
    info = E.load_cfg_info()
    cfg = info.cfg
    host = host_view(cfg)
    folders, configured, host_problems, mapping = host["folders"], host["configured"], host["problems"], host["mapping"]
    if isinstance(st, dict) and isinstance(st.get("results"), list):
        st = dict(st, results=[explain_result(text_twin(r, "why", mapping)) for r in st["results"]])
    disks = {}
    for k, f in folders.items():
        try:
            u = shutil.disk_usage(f["container"])
            disks[k] = dict(path=f["container"], free=u.free, total=u.total)
        except Exception:
            disks[k] = dict(path=f["container"], error="not available")
    entries = live_plan(plan_entries(cfg))
    plan = plan_with_failures(entries, cfg, mapping)
    by_id = {p.get("title_id"): p for p in entries}
    attention = [failure_view(r, by_id.get(t), cfg, mapping) for t, r in failure_store().load().items() if not r.get("dismissed")]
    pause = RC.pause_read(STATE)                     # the schedule starts nothing while it is paused (see scheduler_tick)
    big = S.biggest_build(plan)                      # the biggest game still to build: what the folders must have room for (as the Setup card judges it)
    space = dict(biggest=dict(title=big.get("title") or big["title_id"], size=big["size"]) if big else None,
                 reserve=int(cfg.get("ssd_reserve_gb") or 0) << 30)
    return dict(engine_running=bool(pid), engine_pid=pid, clock=S.clock_info(), failures_rev=failures_rev(), version=versions(),
                access=WS.exposure(request.client.host if request.client else None, request.headers, login=bool(WS.password()),
                                   enabled=WS.warning_enabled(), server_ip=(request.scope.get("server") or (None,))[0]),
                attention=[dict(title_id=a["title_id"], name=a.get("name"), state=a["state"], code=a["code"], summary=a["summary"],
                                at_text=a.get("at_text"), will_retry=a["will_retry"]) for a in attention],
                scan=text_twin(dict(_scan), "log_tail", mapping), run=text_twin(dict(_run), "log_tail", mapping),
                status=st if pid else dict(st, live=False), next_run=None if pause else next_run(cfg.get("schedule")), last_run=last_run(st, bool(pid)), space=space,
                pace=build_pace(cfg), rescan=rescan_info(cfg), todo=run_preview(cfg, plan), schedule_pause=pause,
                stop_mode=RC.stop_mode(STATE), cancelling=pending_cancels_in(st, bool(pid)),
                notify_last=PN.read_last(STATE) if PN.url_of(cfg) else None,
                disks=disks, load=os.getloadavg(), cpus=os.cpu_count(), now=time.time(),
                plan_time=(STATE / "plan.json").stat().st_mtime if (STATE / "plan.json").exists() else None,
                plan_counts={a: sum(1 for p in plan if p.get("action") == a) for a in ("build", "copy", "failed", "done", "skip", "error")},
                stop_requested=(STATE / "stop.request").exists(), schedule=cfg.get("schedule"),
                folders=folders, host_paths=configured, host_problems=host_problems, in_docker=E.IN_DOCKER,
                config_rev=info.rev, config_problems=info.problems,
                config_error=dict(info.error.as_dict(), using=info.fallback) if info.error else None)


@app.get("/api/plan")
def plan():
    """plan.json with `source_host` / `patch_host` / `verify_archive_host`, `candidates[].source_host` and
    `skipped_duplicates_host` (and `dest_dir_host`, the folder a PS4 title is copied into) where the folder has a host
    location, and `failure` (see /api/failures) on a title
    that failed or is waiting for space: action `failed` means it is skipped until something changes."""
    cfg = load_cfg()
    data, mapping = read_json(STATE / "plan.json", []), host_view(cfg)["mapping"]
    if isinstance(data, list):
        ledger = read_json_cached(STATE / "ledger.json", {})
        stale = rescan_info(cfg)["stale"]                                  # made by an older version: its fields are not trusted, a scan refreshes them
        data = [p if stale else library_extras(p, cfg, ledger) for p in plan_with_failures(live_plan(data), cfg, mapping)]
    if not mapping or not isinstance(data, list):
        return data
    out = []
    for p in data:
        p = host_twins(p, mapping, ("source", "patch", "verify_archive", "dest_dir"))
        p = text_twin(text_twin(p, "skipped_duplicates", mapping), "why", mapping)
        if isinstance(p, dict) and isinstance(p.get("candidates"), list):
            p = dict(p, candidates=[host_twins(c, mapping, ("source",)) for c in p["candidates"]])
        out.append(p)
    return out


@app.get("/api/unrecognised")
def unrecognised_sources():
    """The sources the last scan could not use: [{source, source_host, origin, error}], so "why is my game not
    listed" has an answer. A source that is not a game at all (an add-on, a patch) is not an error and not here."""
    data, mapping = read_json(STATE / "unrecognised.json", []), host_view(load_cfg())["mapping"]
    return [host_twins(x, mapping, ("source",)) for x in data if isinstance(x, dict)] if isinstance(data, list) else []


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
    only = [t for t in (body.get("only") or []) if E.valid_title_id(t)]
    try:
        started = start_engine(*(["--only", *only] if only else []))
    except EngineStartError as ex:
        raise HTTPException(ex.status, str(ex))
    if not started:
        raise HTTPException(409, "a run or scan is already active")
    return {"ok": True}


@app.post("/api/stop")
async def stop(req: Request):
    """Ask the run to stop: mode "after_current" (the default: start nothing new, let the running games finish) or "now" (also end
    what is running: those games are cancelled, and prepared copies are kept)."""
    body = await req.json() if (await req.body()) else {}
    mode = body.get("mode", RC.STOP_AFTER) if isinstance(body, dict) else None
    if mode not in (RC.STOP_AFTER, RC.STOP_NOW):
        raise HTTPException(400, 'mode must be "after_current" or "now"')
    if not engine_pid():
        raise HTTPException(409, "no run is active")
    return {"ok": True, "mode": RC.write_stop(STATE, mode)}


@app.delete("/api/stop")
def withdraw_stop():
    """Take back a "finish the current games, then stop" request while the run is going. Not possible once "stop now" was asked."""
    if not engine_pid():
        raise HTTPException(409, "no run is active")
    if RC.stop_mode(STATE) == RC.STOP_NOW:
        raise HTTPException(409, "Stop now is already under way and can't be taken back.")
    if not RC.withdraw_stop(STATE):
        raise HTTPException(409, "No stop was requested.")
    return {"ok": True}


@app.post("/api/builds/{tid}/cancel")
def cancel_build(tid: str):
    """Take one game out of the run in progress: queued, it is left out; being prepared or built, it is ended and its work folders
    are removed. It is recorded as cancelled, never as a failure, and is built again by the next run."""
    if not E.valid_title_id(tid):
        raise HTTPException(400, "not a title ID")
    if not engine_pid():
        raise HTTPException(409, "no run is active")
    st = read_json(STATE / "status.json", {})
    in_run = set()
    if isinstance(st, dict) and not st.get("finished"):
        in_run = {x.get("title_id") for key in ("builds", "preparing", "queue") for x in (st.get(key) or []) if isinstance(x, dict)}
    if tid not in in_run:
        raise HTTPException(404, "that game is not in the run in progress (it may just have finished)")
    RC.request_cancel(STATE, tid)
    return {"ok": True}


def parse_until(v, now=None):
    """The end of a pause as epoch seconds: a number, or a time on the server's clock like 2026-11-30T08:00. None for no end."""
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        raise ValueError("until must be a time")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return time.mktime(time.strptime(v.strip(), fmt))
            except ValueError:
                pass
    raise ValueError("until must be a time like 2026-11-30T08:00 (the server's clock), or empty")


@app.post("/api/schedule/pause")
async def schedule_pause(req: Request):
    """Pause the schedule (no run starts by itself) until `until` (server clock), or until it is resumed."""
    body = await req.json() if (await req.body()) else {}
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    try:
        until = parse_until(body.get("until"))
        got = RC.pause_write(STATE, until)
    except ValueError as ex:
        raise HTTPException(400, "that time has already passed" if "already be over" in str(ex) else str(ex))
    return {"ok": True, "pause": got}


@app.delete("/api/schedule/pause")
def schedule_resume():
    RC.pause_clear(STATE)
    return {"ok": True}


SETTINGS = E.SETTINGS   # key: (type, min, max) - everything the Settings page may change


def machine_info(cfg):
    """What the performance settings are judged against: the CPU threads, and the size of the work drive."""
    total, p = None, Path(cfg.get("temp") or "/")
    while True:
        try:
            total = shutil.disk_usage(p).total
            break
        except OSError:
            if p == p.parent:
                break
            p = p.parent
    return dict(cpus=os.cpu_count(), work_total=total)


def rescan_info(cfg):
    """{needed: [labels of the settings changed since the last scan that only show in the Library after another one],
    stale: the plan was made by an older version of the app}. Nothing to say without a plan."""
    if not (STATE / "plan.json").exists():
        return dict(needed=[], stale=False)
    info = read_json_cached(STATE / "plan_settings.json", None)
    if not isinstance(info, dict) or info.get("format") != E.PLAN_FORMAT:
        return dict(needed=[], stale=True)
    return dict(needed=M.rescan_needed(info.get("settings"), cfg), stale=False)


@app.get("/api/config")
def get_config():
    # config_rev says which version of the file the values are from; the Settings page compares it with the
    # one in /api/status to notice a change on disk
    info = E.load_cfg_info()
    machine = machine_info(info.cfg)
    return {**{k: info.cfg.get(k) for k in SETTINGS}, "config_rev": info.rev, "in_docker": E.IN_DOCKER,
            "folders": folders_info(info.cfg)[0], "schema": M.schema(), "groups": M.GROUPS, "machine": machine,
            "warnings": M.warnings(info.cfg, machine), "problems": M.cross_errors(info.cfg), "rescan_note": M.RESCAN_NOTE, "rescan_keys": list(E.RESCAN_KEYS),
            "run_active": run_active(), "labels": M.LABEL, "notify_last": PN.read_last(STATE) if PN.url_of(info.cfg) else None}


@app.post("/api/notify/test")
async def notify_test(req: Request):
    """Send a test message with the values on the Settings form (not what is saved) and say what came of it. Nothing is stored: a test to an
    address that is not saved must not make the saved one look broken. The answer never has the address in it."""
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    cfg = dict(load_cfg())
    sent = {k: body[k] for k in ("notify_url", "notify_type", "notify_link") if k in body}
    clean, errors, _ = M.check(cfg, sent)
    if not errors and not str(clean.get("notify_url", cfg.get("notify_url")) or "").strip():
        errors = [dict(field="notify_url", message="Enter the address to send to first.")]
    if errors:
        return invalid(errors)
    cfg.update(clean)
    res = PN.send(cfg, "test", "PS5 pkg: test", "This is a test from PS5 PKG Station. If you can read it, notifications work.", record=False)
    server_log(f"notification test ({cfg.get('notify_type') or 'ntfy'}): " + ("delivered" + (f" (HTTP {res['status']})" if res["status"] else "") if res["ok"] else f"NOT delivered: {res['error']}"))
    return {"ok": res["ok"], "status": res["status"], "error": res["error"], "type": res["type"]}


def check_config(body):
    """(changes, errors, warnings) for settings sent by the page, on top of the current ones."""
    info = E.load_cfg_info()
    return M.check(info.cfg, body, machine_info(info.cfg))


def invalid(errors):
    """400 with every problem, field by field, and the first one as the plain `detail`."""
    return JSONResponse(status_code=400, content={"detail": errors[0]["message"], "errors": errors})


@app.put("/api/config")
async def put_config(req: Request):
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    changes, errors, warns = check_config(body)
    if errors:
        return invalid(errors)
    cfg = update_cfg(lambda raw: raw.update(changes))
    return {**{k: cfg.get(k) for k in SETTINGS}, "warnings": warns, "run_active": run_active(), "rescan": rescan_info(cfg)}


@app.post("/api/config/validate")
async def validate_config(req: Request):
    """What is wrong with these settings, and what is only worth a look, without saving anything: the Settings page asks
    while a field is being edited."""
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "expected a JSON object")
    _, errors, warns = check_config(body)
    return {"errors": errors, "warnings": warns}


@app.post("/api/config/preview-patterns")
async def preview_patterns(req: Request):
    """Which titles ("title") or file names ("file") of the last scan these cleanup patterns would match."""
    body = await req.json()
    if not isinstance(body, dict) or body.get("kind") not in ("title", "file") or not isinstance(body.get("patterns"), list):
        raise HTTPException(400, "expected {kind: title | file, patterns: [...]}")
    return M.preview_patterns(STATE, body["kind"], body["patterns"])


def patch_root():
    """Where patch folders belong: <config folder>/patches (/config/patches in the container)."""
    return Path(H.config_root(STATE, LOGS)) / "patches"


def patch_problem(path, mapping, current=None):
    """Why a patch folder can't be set, or None. Its files are copied over the game that is built, so it must be a
    folder below <config>/patches (symbolic links and `..` are resolved first). The value already stored is
    accepted as it is: a file edited by hand keeps working, only a new value is judged."""
    if not path or path == current:
        return None
    root = patch_root()
    try:
        inside = os.path.isabs(path) and Path(os.path.realpath(root)) in Path(os.path.realpath(path)).parents
    except (OSError, ValueError):
        inside = False
    if inside:
        return None
    where = H.display_path(str(root), mapping) or str(root)
    return (f"A patch folder must be inside {where}, in a folder of its own for each game (for example "
            f"{where.rstrip('/')}/PPSA01234). Other places are not accepted: the files in it are copied into the game that is built.")


def overrides_with_hosts(ovs, mapping):
    if not isinstance(ovs, dict):
        return ovs
    return {tid: host_twins(ov, mapping, ("source", "patch")) for tid, ov in ovs.items()}


def describe_override(ov):
    """What a per-game override changes, in a few words."""
    bits = []
    if ov.get("source"):
        bits.append("source: " + re.split(r"[\\/]", str(ov["source"]).rstrip("/\\"))[-1])
    if ov.get("modified_exec") == "image":
        bits.append("keep the backport")
    elif ov.get("modified_exec") == "original":
        bits.append("use the original")
    if ov.get("patch"):
        bits.append("patch folder")
    if ov.get("skip"):
        bits.append("skipped")
    if ov.get("skip_verify"):
        bits.append("no archive check")
    other = [k for k in ov if k not in IO.KNOWN_OVERRIDE_FIELDS]
    if other:
        bits.append("other: " + ", ".join(sorted(other)))
    return bits


def overrides_table(cfg, mapping):
    """Every override in config.json, also those of titles that are not in the library (any more)."""
    in_plan = {p.get("title_id"): p for p in plan_entries(cfg)}
    rows = []
    for tid, ov in sorted((cfg.get("overrides") or {}).items()):
        if isinstance(ov, dict):
            rows.append(host_twins(dict(title_id=tid, title=(in_plan.get(tid) or {}).get("title") or "", in_library=tid in in_plan,
                                        what=describe_override(ov), note=ov.get("note") or "", source=ov.get("source"),
                                        patch=ov.get("patch"), override=ov), mapping, ("source", "patch")))
    return rows


def file_info(path, mapping):
    p = Path(path)
    try:
        st = p.stat()
        changed, size = st.st_mtime, st.st_size
    except OSError:
        changed = size = None
    return dict(path=str(p), host=H.display_path(str(p), mapping), exists=changed is not None, changed=changed, size=size)


@app.get("/api/storage")
def storage():
    """Where the settings live, what else is stored beside them, the automatic backups, the versions and the
    per-game overrides: the Settings page's "Storage & backup" card."""
    info = E.load_cfg_info()
    mapping = host_view(info.cfg)["mapping"]
    ledger_ = read_json(STATE / "ledger.json", {})
    failed = read_json(STATE / "failures.json", {})
    backups = E.list_backups()
    return dict(
        settings=dict(file_info(E.CFG_PATH, mapping), error=info.error.as_dict() if info.error else None),
        history=dict(file_info(STATE / "ledger.json", mapping), packages=sum(1 for v in ledger_.values() if isinstance(v, dict)) if isinstance(ledger_, dict) else 0),
        failures=dict(file_info(STATE / "failures.json", mapping), count=len(failed) if isinstance(failed, dict) else 0),
        state_note=("The build history (ledger.json) and the failed-game records (failures.json) are state, not settings, so an export "
                    "does not contain them. The full backup (.zip) includes the build history; the failed-game records only if you tick "
                    "the box for them."),
        backups=dict(keep=E.BACKUP_KEEP, count=len(backups), last=backups[0]["at"] if backups else None, items=backups,
                     folder=file_info(E.backups_dir(), mapping), error=E.backup_status["error"]),
        versions=versions(), overrides=overrides_table(info.cfg, mapping))


@app.get("/api/export")
def export_settings(notify_url: int = 1):
    """The settings and the per-game overrides as one JSON file (see ps5_settingsio). The notification URL can
    be left out (?notify_url=0): it may be a private topic."""
    info = E.load_cfg_info()
    if info.error:
        raise HTTPException(409, f"The settings file could not be read ({info.error.describe()}), so there is nothing reliable "
                                 f"to export. Fix the file first.")
    doc = IO.export_doc(info.cfg, include_notify_url=bool(notify_url), app_version=FL.app_version())
    return Response(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="ps5-pkg-station-settings-{time.strftime("%Y%m%d")}.json"'})


@app.get("/api/backup.zip")
def full_backup(failures: int = 0):
    """A zip with config.json exactly as it is, the build history (ledger.json), the list of patch folders (not their
    contents) and a note on restoring. The failed-game records (failures.json) only with ?failures=1."""
    cfg = load_cfg()
    mapping = host_view(cfg)["mapping"]
    patches = [dict(title_id=t, patch=ov.get("patch"), host=H.display_path(ov.get("patch"), mapping))
               for t, ov in sorted((cfg.get("overrides") or {}).items()) if isinstance(ov, dict) and ov.get("patch")]
    patch_root = Path(H.config_root(STATE, LOGS)) / "patches"
    try:
        existing = sorted(p.name for p in patch_root.iterdir() if p.is_dir())
    except OSError:
        existing = []
    files = [("config.json", E.CFG_PATH), ("ledger.json", STATE / "ledger.json")] + ([("failures.json", STATE / "failures.json")] if failures else [])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, p in files:
            if p.is_file():
                z.write(p, name)
        z.writestr("patch-folders.json", json.dumps(dict(referenced_by_overrides=patches, folders_in_patches_dir=existing,
                                                         note="Only the names are listed. Copy the folders themselves to keep their files."),
                                                    indent=2, ensure_ascii=False) + "\n")
        z.writestr("manifest.json", json.dumps(dict(app_version=FL.app_version(), created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                                    contains=[n for n, p in files if p.is_file()]), indent=2) + "\n")
        z.writestr("README.txt", "PS5 PKG Station backup.\n\nTo restore, stop the container, copy config.json (and ledger.json, failures.json) back into "
                                 "the config folder's state/ folder, and start it again. The Settings page can also import the "
                                 "settings from an export file.\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="ps5-pkg-station-backup-{time.strftime("%Y%m%d-%H%M%S")}.zip"'})


def _problems(status, ex):
    return JSONResponse(status_code=status, content={"detail": str(ex), "problems": getattr(ex, "problems", [str(ex)])})


@app.post("/api/import")
async def import_settings(req: Request, dry_run: int = 0, parts: str = ""):
    """Import an export. dry_run=1 only says what would change. Everything is checked first (the same rules as the
    Settings page); a problem names its key and nothing is written. parts=settings,overrides (default both)."""
    try:
        doc = await req.json()
    except Exception:
        return _problems(400, ValueError("The file is not valid JSON."))
    info = E.load_cfg_info()
    try:
        want = IO.parse_parts(parts)
        current = info.cfg.get("overrides") or {}
        mapping = host_view(info.cfg)["mapping"]
        settings, overrides = IO.parse_import(doc, (lambda tid, p: patch_problem(p, mapping, current=(current.get(tid) or {}).get("patch")))
                                              if "overrides" in want else None)
    except ValueError as ex:                        # ImportProblem is one
        return _problems(400, ex)
    if info.error:
        raise HTTPException(409, f"The settings file could not be read ({info.error.describe()}). Nothing was imported: fix the file first.")
    d = IO.diff(info.cfg, settings, overrides, want)
    if dry_run:
        return dict(d, ok=True, dry_run=True)
    before = {b["name"] for b in E.list_backups()}
    cfg = update_cfg(lambda raw: IO.apply_import(raw, settings, overrides, want))
    request_replan()
    made = [b["name"] for b in E.list_backups() if b["name"] not in before]
    return dict(d, ok=True, dry_run=False, backup=made[0] if made else None,
                config={k: cfg.get(k) for k in SETTINGS}, overrides=cfg.get("overrides", {}))


@app.get("/api/backups")
def backups():
    return dict(keep=E.BACKUP_KEEP, items=E.list_backups(), error=E.backup_status["error"])


@app.post("/api/backups/{name}/restore")
def restore_backup(name: str, dry_run: int = 0):
    """Make an automatic backup the current settings file (dry_run=1: only say what would change). The file it
    replaces is backed up first."""
    if not E.BACKUP_RE.match(name):
        raise HTTPException(400, "not a backup name")
    try:
        data = json.loads((E.backups_dir() / name).read_text(encoding="utf-8").lstrip("\ufeff"))
    except FileNotFoundError:
        raise HTTPException(404, "no such backup")
    except (OSError, ValueError) as ex:
        raise HTTPException(400, f"that backup can't be read: {ex}")
    if not isinstance(data, dict):
        raise HTTPException(400, "that backup is not a JSON object")
    cfg_b, problems = E.validate_cfg(data)
    info = E.load_cfg_info()
    d = IO.diff(info.cfg, {k: cfg_b[k] for k in SETTINGS}, cfg_b["overrides"], IO.PARTS, replace_all=True)
    d.update(ok=True, dry_run=bool(dry_run), problems=problems, replaces_unreadable_file=bool(info.error))
    if dry_run:
        return d
    before = {b["name"] for b in E.list_backups()}
    try:
        E.restore_backup(name)
    except ValueError as ex:
        raise HTTPException(400, str(ex))
    request_replan()
    made = [b["name"] for b in E.list_backups() if b["name"] not in before]
    return dict(d, backup=made[0] if made else None)


@app.delete("/api/overrides/{tid}")
def delete_override(tid: str):
    """Remove one per-game override (and nothing else)."""
    gone = []

    def apply(raw):
        ovs = raw.get("overrides")
        if isinstance(ovs, dict) and tid in ovs:
            gone.append(ovs.pop(tid))
    update_cfg(apply)
    if not gone:
        raise HTTPException(404, "there is no override for that title")
    return {"ok": True, "removed": tid, "replan": request_replan()}


@app.get("/api/overrides")
def get_overrides():
    cfg = load_cfg()
    return overrides_with_hosts(cfg.get("overrides", {}), host_view(cfg)["mapping"])


@app.put("/api/overrides/{tid}")
async def put_override(tid: str, req: Request):
    if not E.valid_title_id(tid):
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
    if body.get("skip_verify"):
        ov["skip_verify"] = True
    if ov.get("patch"):
        why = patch_problem(ov["patch"], mapping, current=((load_cfg().get("overrides") or {}).get(tid) or {}).get("patch"))
        if why:
            raise HTTPException(400, why)

    def apply(raw):
        if not isinstance(raw.get("overrides"), dict):
            raw["overrides"] = {}
        if ov:
            raw["overrides"][tid] = ov
        else:
            raw["overrides"].pop(tid, None)
    cfg = update_cfg(apply)
    return dict(host_twins(cfg.get("overrides", {}).get(tid, {}), host_view(cfg)["mapping"], ("source", "patch")), replan=request_replan())


@app.get("/api/failures")
def failures():
    """Every game that failed or is waiting for space, newest first, with the explanation, what can be done
    and the log excerpt of its last attempt."""
    cfg = load_cfg()
    mapping, entries = host_view(cfg)["mapping"], {p.get("title_id"): p for p in plan_entries(cfg)}
    out = [failure_view(r, entries.get(t), cfg, mapping) for t, r in failure_store().load().items()]
    return sorted(out, key=lambda v: -(v.get("at") or 0))


def _failure(tid):
    if not E.valid_title_id(tid):
        raise HTTPException(400, "bad title id")
    rec = failure_store().load().get(tid)
    if not rec:
        raise HTTPException(404, "no stored failure for this title")
    return rec


def retry_title(tid):
    """Make the title eligible again and start a run for it (or leave it for the next run when one is active)."""
    failure_store().set_flag(tid, retry=True, dismissed=False)
    try:
        started = start_engine("--only", tid)
    except EngineStartError as ex:
        raise HTTPException(ex.status, str(ex))
    return {"ok": True, "started": started,
            "message": "Retrying." if started else "A run or scan is active: this game is retried by the next run."}


@app.post("/api/failures/{tid}/retry")
def failure_retry(tid: str):
    _failure(tid)
    return retry_title(tid)


@app.post("/api/failures/{tid}/dismiss")
def failure_dismiss(tid: str):
    _failure(tid)
    failure_store().set_flag(tid, dismissed=True)
    return {"ok": True}


@app.post("/api/failures/{tid}/delete-staged")
def failure_delete_staged(tid: str):
    """Delete the package a failed validation left in the staging folder: only inside <output>/.staging, never
    during a run, and only while it is still the folder that was there when the failure was recorded."""
    rec, cfg = _failure(tid), load_cfg()
    sp = (rec.get("data") or {}).get("staged")
    root = (Path(cfg["output"]) / ".staging").resolve()
    if not sp or not os.path.isdir(sp) or Path(sp).resolve().parent != root:
        raise HTTPException(404, "there is no staged package to delete")
    reason = staged_block_reason(rec, sp)
    if reason:
        raise HTTPException(409, f"Not deleted: {reason}.")
    freed = E.dir_size(sp)
    shutil.rmtree(sp, ignore_errors=True)
    failure_store().update(lambda all_: all_[tid].update(
        data=dict(all_[tid].get("data") or {}, staged=None, staged_bytes=0, staged_stamp=None)))
    try:
        root.rmdir()
    except OSError:
        pass
    return {"ok": True, "freed": freed}


@app.post("/api/failures/{tid}/action")
async def failure_action(tid: str, req: Request):
    """The one-click fixes: change the title's override, then retry it."""
    rec = _failure(tid)
    action = (await req.json()).get("action")
    entry = next((p for p in plan_entries(load_cfg()) if p.get("title_id") == tid), {}) or {}
    changes = {}
    if action == "use_archive":
        if not entry.get("verify_archive"):
            raise HTTPException(400, "there is no archive copy of this game")
        changes = dict(source=entry["verify_archive"], skip_verify=None)
    elif action == "keep_anyway":
        changes = dict(skip_verify=True)
    elif action in ("keep_backport", "use_original"):
        changes = dict(modified_exec="image" if action == "keep_backport" else "original")
    elif action == "skip_source":
        other = next((c["source"] for c in entry.get("candidates") or []
                      if isinstance(c, dict) and c.get("source") and c["source"] != entry.get("source")), None)
        changes = dict(source=other) if other else dict(skip=True)
    elif action == "delete_staged":
        return failure_delete_staged(tid)
    else:
        raise HTTPException(400, "unknown action")

    def apply(raw):
        if not isinstance(raw.get("overrides"), dict):
            raw["overrides"] = {}
        ov = dict(raw["overrides"].get(tid) or {})
        for k, v in changes.items():
            if v is None:
                ov.pop(k, None)
            else:
                ov[k] = v
        raw["overrides"][tid] = ov
    update_cfg(apply)
    return dict(retry_title(tid), changed=changes)


@app.get("/api/ledger")
def ledger():
    """ledger.json with `output_host` (the package) and `source_host`. Entries written by older versions (other
    field names, missing fields) pass through untouched."""
    data, mapping = read_json(STATE / "ledger.json", {}), host_view(load_cfg())["mapping"]
    if not mapping or not isinstance(data, dict):
        return data
    return {tid: host_twins(v, mapping, ("output", "source")) for tid, v in data.items()}


SAFE_LOG = re.compile(r"^(pkg/)?[\w.\-]+\.log$")
RUN_ID = re.compile(r"^\d{8}-\d{6}$")
LOG_KEEP = dict(build=500, run=100, activity_old=100)       # of each kind, the newest are listed (the files stay until retention removes them)


def title_lookup(cfg):
    """tid -> the game's title: from the plan, else from what a past run or a failure recorded, else what the scans learned, else the name of
    its package. A game that is no longer in the library is still named by the history."""
    plan = {p.get("title_id"): p.get("title") for p in plan_entries(cfg) if p.get("title")}
    titles = E.load_titles()
    seen = {}
    for rec in FL.Store(STATE / "failures.json").load().values():
        if rec.get("title") and rec.get("title_id"):
            seen[rec["title_id"]] = rec["title"]
    for r in PH.read_rows(STATE):
        if r.get("k") == "job" and r.get("tid") and r.get("title"):
            seen[r["tid"]] = r["title"]                                 # later runs win

    def title(tid, name=None):
        return (plan.get(tid) or seen.get(tid) or (titles.get(tid) if isinstance(titles, dict) else None)
                or (str(name).split(" [")[0] if name else None) or tid)
    return title


def job_view(j, mapping):
    """A job row of history.jsonl for the page: host twins of its paths, and a log only when it is a log the Logs page can open."""
    j = dict(text_twin(host_twins(j, mapping, ("output", "source")), "why", mapping))
    if not (isinstance(j.get("log"), str) and SAFE_LOG.match(j["log"]) and (LOGS / j["log"]).exists()):
        j.pop("log", None)
    j["checks"] = PH.parse_checks(j.get("validate"))
    return j


def run_output_log(items, started):
    """The raw output file of a run (run_<time>.stdout.log), found by when it started."""
    best = None
    for d in items:
        m = PH.RUN_LOG_RE.match(Path(d["name"]).name) if d["kind"] == "run" else None
        if not m or started is None:
            continue
        try:
            gap = abs(time.mktime(time.strptime(m.group("ts"), "%Y%m%d-%H%M%S")) - started)
        except ValueError:
            continue
        if gap <= 120 and (best is None or gap < best[0]):
            best = (gap, d["name"])
    return best[1] if best else None


@app.get("/api/history/runs")
def history_runs(limit: int = 30, offset: int = 0):
    """The runs recorded in history.jsonl, newest first: when, how it started, how long, the counts of what happened. A run's jobs are
    in /api/history/runs/{run}. Runs before the app kept this history are not in it (their packages are in /api/history/packages)."""
    runs = PH.runs(PH.read_rows(STATE))
    items = PH.list_logs(LOGS)
    out = []
    for r in runs[max(0, offset):max(0, offset) + max(1, min(limit, 200))]:
        out.append(dict(run=r["run"], trigger=r["trigger"], started=r["started"], finished=r["finished"], seconds=r["seconds"], interrupted=r["interrupted"],
                        counts=r["counts"], jobs=len(r["jobs"]), bytes=sum(int(j.get("bytes") or 0) for j in r["jobs"] if j.get("status") in ("built", "copied")),
                        output_log=run_output_log(items, r["started"])))
    return dict(total=len(runs), runs=out, since=runs[-1]["started"] if runs else None)


@app.get("/api/history/runs/{run}")
def history_run(run: str):
    if not RUN_ID.match(run):
        raise HTTPException(400, "not a run")
    found = next((r for r in PH.runs(PH.read_rows(STATE)) if r["run"] == run), None)
    if found is None:
        raise HTTPException(404, "no such run")
    mapping = host_view(load_cfg())["mapping"]
    return dict(run=found["run"], trigger=found["trigger"], started=found["started"], finished=found["finished"], seconds=found["seconds"],
                interrupted=found["interrupted"], counts=found["counts"], jobs=[job_view(j, mapping) for j in found["jobs"]])


def replaced_view(cfg, mapping):
    out = Path(cfg["output"])
    items = []
    for d in PH.replaced_files(out):
        items.append(host_twins(dict(d, path=str(out / d["name"])), mapping, ("path",)))
    return dict(items=items, count=len(items), bytes=sum(d["bytes"] for d in items))


@app.get("/api/history/packages")
def history_packages():
    """The packages the ledger remembers (one per game, the latest build), each as one set of fields whatever version of the app wrote
    it (None: not recorded), with whether its file is still there, its build log, and a rebuild mark; totals; and the packages that were
    replaced by a newer build and are still in the output folder."""
    cfg = load_cfg()
    mapping = host_view(cfg)["mapping"]
    data = read_json(STATE / "ledger.json", {})
    items = PH.list_logs(LOGS)
    last = {}
    for r in PH.read_rows(STATE):
        if r.get("k") == "job" and r.get("status") in ("built", "copied") and r.get("tid"):
            if r["tid"] not in last or (r.get("at") or 0) >= (last[r["tid"]].get("at") or 0):
                last[r["tid"]] = r
    marked = set(FL.Store(STATE / "rebuild.json").load())
    title_of = title_lookup(cfg)
    out = []
    for tid, v in (data.items() if isinstance(data, dict) else []):
        p = PH.package(tid, v)
        if p is None:
            continue
        exists = bool(p["output"]) and E.nas_exists(p["output"])
        size = p["bytes"]
        if size is None and exists and not p["ps4"]:
            try:
                size = float(os.stat(p["output"]).st_size)
            except OSError:
                pass
        log, guessed = None, False
        job = last.get(tid)
        if job and job.get("output") == p["output"] and isinstance(job.get("log"), str) and SAFE_LOG.match(job["log"]) and (LOGS / job["log"]).exists():
            log = job["log"]
        elif not p["ps4"]:
            log = PH.log_for_package(items, tid, p["built_ts"])
            guessed = log is not None
        out.append(host_twins(dict(p, title=title_of(tid, p["name"]), bytes=size, exists=exists, log=log, log_guessed=guessed, marked=tid in marked,
                                   minutes=PH.minutes_of(p)), mapping, ("output", "source")))
    known = [p for p in out if p["minutes"] is not None]
    return dict(packages=out, replaced=replaced_view(cfg, mapping),
                totals=dict(count=len(out), present=sum(1 for p in out if p["exists"]), bytes=int(sum(p["bytes"] or 0 for p in out if p["exists"])),
                            minutes=round(sum(p["minutes"] for p in known), 1), minutes_known=len(known)))


@app.get("/api/replaced")
def replaced_list():
    cfg = load_cfg()
    return replaced_view(cfg, host_view(cfg)["mapping"])


@app.delete("/api/replaced/{name}")
def replaced_delete(name: str):
    """Delete one package that was moved aside when its game was built again. Only a file of that exact name shape, directly in the output folder."""
    try:
        freed = PH.delete_replaced(load_cfg()["output"], name)
    except ValueError:
        raise HTTPException(400, "that is not a replaced package")
    except FileNotFoundError:
        raise HTTPException(404, "that file is not there")
    return {"ok": True, "freed": freed}


def ledger_entry(tid):
    if not E.valid_title_id(tid):
        raise HTTPException(400, "not a title ID")
    data = read_json(STATE / "ledger.json", {})
    if not isinstance(data, dict) or not isinstance(data.get(tid), dict):
        raise HTTPException(404, "that game has no package in the history")
    return data


@app.post("/api/history/{tid}/rebuild")
def history_rebuild(tid: str):
    """Mark a game to be built again by the next run (its package is replaced only after the new one has validated)."""
    ledger_entry(tid)
    FL.Store(STATE / "rebuild.json").update(lambda d: d.__setitem__(tid, dict(at=time.time())))
    when = request_replan()
    return {"ok": True, "message": "Marked: the next run builds it again" + (" (the Library shows it as to build after the scan that follows)" if when != "now" else ""), "replan": when}


@app.delete("/api/history/{tid}/rebuild")
def history_rebuild_undo(tid: str):
    if not E.valid_title_id(tid):
        raise HTTPException(400, "not a title ID")
    FL.Store(STATE / "rebuild.json").clear(tid)
    request_replan()
    return {"ok": True}


@app.post("/api/history/{tid}/forget")
async def history_forget(tid: str, req: Request):
    """Remove a game's entry from the ledger (the package file stays where it is). The planner then treats the game as never built: it
    is built again unless `skip` is true, which also sets the game's "never build" option. Not while a run may be writing the ledger."""
    body = await req.json() if (await req.body()) else {}
    data = ledger_entry(tid)
    if engine_pid():
        raise HTTPException(409, "A run is active: try again when it has ended.")
    del data[tid]
    E.write_json_atomic(STATE / "ledger.json", data)
    FL.Store(STATE / "rebuild.json").clear(tid)
    if isinstance(body, dict) and body.get("skip") is True:
        def apply(raw):
            if not isinstance(raw.get("overrides"), dict):
                raw["overrides"] = {}
            raw["overrides"][tid] = dict(raw["overrides"].get(tid) or {}, skip=True)
        update_cfg(apply)
    request_replan()
    return {"ok": True}


def log_items(cfg):
    """The logs for the Logs page, newest first, builds with the game's title and what became of the build."""
    items = PH.list_logs(LOGS)
    title_of = title_lookup(cfg)
    outcome = {}
    for r in PH.read_rows(STATE):
        if r.get("k") == "job" and isinstance(r.get("log"), str):
            outcome[r["log"]] = r.get("status")
    for rec in FL.Store(STATE / "failures.json").load().values():
        if isinstance(rec.get("log"), str) and rec["log"] not in outcome:
            outcome[rec["log"]] = "failed"
    seen = {k: 0 for k in LOG_KEEP}
    out = []
    for d in items:
        if d["kind"] in LOG_KEEP:
            seen[d["kind"]] += 1
            if seen[d["kind"]] > LOG_KEEP[d["kind"]]:
                continue
        if d["kind"] == "build":
            d = dict(d, title=title_of(d["tid"]), outcome=outcome.get(d["name"]))
        out.append(d)
    return out


@app.get("/api/logs")
def logs(info: int = 0):
    """The log files. With info=1 also how much room they take and the retention settings."""
    cfg = load_cfg()
    items = log_items(cfg)
    if not info:
        return items
    return dict(items=items, usage=PH.usage(LOGS), retention=dict(days=int(cfg.get("keep_logs_days") or 0), gb=int(cfg.get("keep_logs_gb") or 0)))


@app.post("/api/logs/prune")
async def logs_prune(req: Request):
    """Remove the logs the retention settings say are too old or too many now (dry_run: only say which)."""
    body = await req.json() if (await req.body()) else {}
    cfg = load_cfg()
    days, gb = int(cfg.get("keep_logs_days") or 0), int(cfg.get("keep_logs_gb") or 0)
    if not days and not gb:
        return {"ok": True, "removed": [], "freed": 0, "note": "No retention is set (Settings > Cleanup): nothing is removed."}
    res = PH.prune_logs(LOGS, days, gb, protect=PH.failure_logs(FL.Store(STATE / "failures.json").load()),
                        dry_run=bool(isinstance(body, dict) and body.get("dry_run")))
    return dict(res, ok=True)


SEG = re.compile(rb"[^\r\n]+")                  # a line of a log: UltraPack ends its progress lines with a carriage return, the rest with a newline


def log_lines_before(fh, end, n, max_bytes=16 << 20):
    """(byte offset where they start, text) of the last n non-empty lines that end at byte `end`, reading from the end in blocks."""
    pos, data = end, b""
    while pos > 0 and len(data) < max_bytes:
        step = min(1 << 20, pos)
        pos -= step
        fh.seek(pos)
        data = fh.read(step) + data
        if len(SEG.findall(data)) > n + 1:
            break
    segs = list(SEG.finditer(data))
    if pos > 0 and segs:
        segs = segs[1:]                           # its first line may be cut
    segs = segs[-n:]
    if not segs:
        return end, ""
    return pos + segs[0].start(), b"\n".join(m.group() for m in segs).decode("utf-8", "replace")


def log_chunk(fh, size, offset, limit):
    """(next offset, text) of the bytes after `offset`: at most `limit`, ending at a line end when the file goes on, and never inside
    a multi-byte character."""
    end = min(size, offset + limit)
    fh.seek(offset)
    data = fh.read(end - offset)
    if end < size:
        cut = max(data.rfind(b"\n"), data.rfind(b"\r"))
        if cut >= 0:
            data = data[:cut + 1]
    for _ in range(4):
        try:
            data.decode("utf-8")
            break
        except UnicodeDecodeError as ex:
            if ex.start < len(data) - 3:
                break                                # a bad byte in the middle: shown as a replacement character
            data = data[:ex.start]
    segs = [m.group() for m in SEG.finditer(data)]
    return offset + len(data), ("\n".join(s.decode("utf-8", "replace") for s in segs) + "\n") if segs else ""


@app.get("/api/logs/{name:path}")
def log_tail(name: str, lines: int = 400, host: int = 0, offset: int = -1, before: int = -1, limit: int = 1 << 20, download: int = 0):
    """A log, as text. Without more: its last `lines` lines. `offset=N`: what was written after byte N (at most `limit` bytes; the
    headers X-Log-Next / X-Log-Size say where to go on, X-Log-Reset when the file is shorter than N, then the last lines come instead).
    `before=N`: the `lines` lines before byte N (what "load earlier" asks for; X-Log-Start says where the text begins). `download=1`: the file
    exactly as it is on disk. With host=1 container folder prefixes are shown as their host locations (display only: the file is never
    changed, and without host=1 the text is exactly what is on disk, carriage returns apart)."""
    if not SAFE_LOG.match(name):
        raise HTTPException(400, "bad log name")
    p = LOGS / name
    if not p.is_file():
        raise HTTPException(404)
    if offset >= 0 and before >= 0:
        raise HTTPException(400, "offset and before can't be combined")
    if download:
        return FileResponse(p, media_type="text/plain; charset=utf-8", filename=p.name)
    n = max(10, min(lines, 5000))
    size = p.stat().st_size
    heads, start, reset = {}, 0, False
    with open(p, "rb") as fh:
        if offset >= 0 and offset <= size:
            nxt, text = log_chunk(fh, size, offset, max(4096, min(limit, 8 << 20)))        # never smaller than a line and a character: it always moves on
            start = offset
        else:
            reset = offset > size
            start, text = log_lines_before(fh, min(before, size) if before >= 0 else size, n)
            nxt = size
    heads = {"X-Log-Size": str(size), "X-Log-Start": str(start), "X-Log-Next": str(nxt)}
    if reset:
        heads["X-Log-Reset"] = "1"
    if host:
        text = H.display_text(text, host_view(load_cfg())["mapping"])
    return PlainTextResponse(text, headers=heads)


@app.get("/api/health")
def health():
    return {"ok": True}
