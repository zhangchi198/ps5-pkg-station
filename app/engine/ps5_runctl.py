"""Run control: stop after the current games, stop now, cancel one game, pause the schedule (standard library only).

The web server and the engine are separate processes, so they talk through small files in the state folder:

  stop.request             {"mode": "after_current" | "now", "at": epoch}. The older plain-text content (a time stamp) means
                           after_current. The engine starts nothing new once it exists; "now" also kills what is running.
  cancel/<TITLEID>.request {"at": epoch}: this game must stop. Waiting: it is left out of the run. Being prepared or built:
                           what runs for it is killed and its work folders are removed. Either way it is recorded as
                           "cancelled", never as a failure.
  schedule_pause.json      {"since": epoch, "until": epoch | null}: the schedule starts no runs until `until` (or for ever).

Inside the engine a cancel is a flag per game (`CANCEL`) that every long step looks at (`check_cancel`), plus the processes
started for that game (`run_tracked` registers them) which `kill_title` ends. A cancelled step raises `Cancelled`; whatever
else a step raises while its game's flag is set is also a cancel (the process we killed made it fail), never a failure.
This module deletes nothing itself: the callers remove their own work folders, through the guarded delete."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

STOP_AFTER, STOP_NOW = "after_current", "now"
_TID = re.compile(r"(PPSA|CUSA)\d{5}")


class Cancelled(Exception):
    """The game's work was stopped on request (not an error)."""


# ------------------------------------------------------------------ the request files

def _atomic(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def stop_mode(state):
    """None when nobody asked the run to stop, else "after_current" or "now"."""
    p = Path(state) / "stop.request"
    try:
        raw = p.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return STOP_AFTER
    try:
        d = json.loads(raw)
    except ValueError:
        return STOP_AFTER                                   # an older version wrote a time stamp
    return STOP_NOW if isinstance(d, dict) and d.get("mode") == STOP_NOW else STOP_AFTER


def write_stop(state, mode, now=None):
    """Ask the run to stop. "now" is never taken back by a later "after_current"."""
    if mode not in (STOP_AFTER, STOP_NOW):
        raise ValueError(mode)
    if mode == STOP_AFTER and stop_mode(state) == STOP_NOW:
        return STOP_NOW
    _atomic(Path(state) / "stop.request", dict(mode=mode, at=now or time.time()))
    return mode


def withdraw_stop(state):
    """Take back an after_current request: True when there was one to take back, False when there was none or it is "now"."""
    if stop_mode(state) != STOP_AFTER:
        return False
    try:
        (Path(state) / "stop.request").unlink()
    except FileNotFoundError:
        return False
    return True


def request_cancel(state, tid, now=None):
    if not (isinstance(tid, str) and _TID.fullmatch(tid)):
        raise ValueError(f"not a title ID: {tid!r}")
    _atomic(Path(state) / "cancel" / f"{tid}.request", dict(at=now or time.time()))


def pending_cancels(state):
    """The games a cancel was asked for that the engine has not taken yet."""
    try:
        return sorted(p.stem for p in (Path(state) / "cancel").glob("*.request") if _TID.fullmatch(p.stem))
    except OSError:
        return []


def take_cancels(state):
    """The same, and they are taken: each request is removed."""
    out = []
    for tid in pending_cancels(state):
        try:
            (Path(state) / "cancel" / f"{tid}.request").unlink()
            out.append(tid)
        except FileNotFoundError:
            pass
    return out


def clear_requests(state):
    """A run's start or end: no stop or cancel left over from before."""
    (Path(state) / "stop.request").unlink(missing_ok=True)
    for tid in pending_cancels(state):
        (Path(state) / "cancel" / f"{tid}.request").unlink(missing_ok=True)


# ------------------------------------------------------------------ the schedule pause

def pause_read(state, now=None):
    """{since, until} while the schedule is paused, else None. A pause whose `until` has passed is over."""
    try:
        d = json.loads((Path(state) / "schedule_pause.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    until = d.get("until")
    until = float(until) if isinstance(until, (int, float)) and not isinstance(until, bool) else None
    if until is not None and (now or time.time()) >= until:
        return None
    since = d.get("since")
    return dict(since=float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else None, until=until)


def pause_write(state, until=None, now=None):
    now = now or time.time()
    if until is not None and until <= now:
        raise ValueError("the pause would already be over")
    _atomic(Path(state) / "schedule_pause.json", dict(since=now, until=until))
    return pause_read(state, now)


def pause_clear(state):
    try:
        (Path(state) / "schedule_pause.json").unlink()
        return True
    except FileNotFoundError:
        return False


# ------------------------------------------------------------------ inside the engine: flags and processes

CANCEL = {}                         # title id -> Event: that game's work must stop
CANCEL_ALL = threading.Event()      # stop now
_CTX = threading.local()            # which game the current thread works on
_PROCS = {}                         # title id -> set of Popen started for it by run_tracked
_LOCK = threading.Lock()


def begin_run():
    """A new run in this process (the engine runs one; tests run many): nothing is cancelled."""
    with _LOCK:
        CANCEL.clear()
        CANCEL_ALL.clear()
        _PROCS.clear()


def cancel_title(tid):
    with _LOCK:
        CANCEL.setdefault(tid, threading.Event()).set()


def cancel_requested(tid=None):
    tid = tid or getattr(_CTX, "tid", None)
    if CANCEL_ALL.is_set():
        return True
    ev = CANCEL.get(tid) if tid else None
    return bool(ev and ev.is_set())


def title_cancelled(tid):
    """This game was cancelled by name (not: everything is being stopped)."""
    ev = CANCEL.get(tid)
    return bool(ev and ev.is_set())


def check_cancel(tid=None):
    if cancel_requested(tid):
        raise Cancelled(tid or getattr(_CTX, "tid", None))


class working_on:
    """`with working_on(tid):` the steps run by this thread belong to that game (check_cancel / run_tracked use it)."""
    def __init__(self, tid):
        self.tid = tid

    def __enter__(self):
        self.prev = getattr(_CTX, "tid", None)
        _CTX.tid = self.tid

    def __exit__(self, *exc):
        _CTX.tid = self.prev
        return False


def run_tracked(cmd, **kw):
    """subprocess.run, but when the calling thread works on a game (`working_on`) the process is remembered, so a cancel of that
    game can end it, and a cancel that came before it started stops it from starting. Same arguments, same CompletedProcess."""
    tid = getattr(_CTX, "tid", None)
    if tid is None:
        return subprocess.run(cmd, **kw)
    check_cancel(tid)
    capture, timeout, data = kw.pop("capture_output", False), kw.pop("timeout", None), kw.pop("input", None)
    check = kw.pop("check", False)
    if capture:
        kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.PIPE)
    if data is not None:
        kw.setdefault("stdin", subprocess.PIPE)
    kw.setdefault("start_new_session", True)               # its own group: its helpers die with it
    proc = subprocess.Popen(cmd, **kw)
    with _LOCK:
        _PROCS.setdefault(tid, set()).add(proc)
    try:
        if cancel_requested(tid):                          # asked between the check and the registration
            kill_group(proc.pid, proc, grace=0)
        try:
            out, err = proc.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc.pid, proc, grace=0)
            proc.communicate()
            raise
        except BaseException:
            kill_group(proc.pid, proc, grace=0)
            raise
    finally:
        with _LOCK:
            _PROCS.get(tid, set()).discard(proc)
    rc = proc.returncode
    if check and rc:
        raise subprocess.CalledProcessError(rc, cmd, out, err)
    return subprocess.CompletedProcess(cmd, rc, out, err)


def kill_group(pid, proc=None, grace=8.0):
    """End a process and everything it started: SIGTERM to its group, SIGKILL after `grace` seconds (and, once it is gone, to
    anything it left in the group). Only a group the process leads is signalled as a group, and never this process's own:
    anything else gets the signal itself. True when the process is gone."""
    def alive():
        if proc is not None:
            return proc.poll() is None
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return not alive()
    own = pgid == os.getpgrp()
    target = (lambda sig: os.killpg(pgid, sig)) if (pgid == pid and not own) else (lambda sig: os.kill(pid, sig))
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 10.0)):
        try:
            target(sig)
        except (ProcessLookupError, PermissionError):
            pass
        end = time.time() + wait
        while alive() and time.time() < end:
            time.sleep(0.05)
        if not alive():
            break
    if pgid == pid and not own:                            # what the leader left behind (helpers, a 7-Zip it started)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return not alive()


def kill_title(tid, builds=()):
    """End everything running for a game: the processes run_tracked started, and its UltraPack build (`builds`: the engine's
    build records, each with `job` and `proc`). Returns how many processes were ended."""
    with _LOCK:
        procs = list(_PROCS.get(tid, ()))
    n = 0
    for b in builds:
        if (b.get("job") or {}).get("title_id") == tid and b["proc"].poll() is None:
            procs.append(b["proc"])
    for p in procs:
        if kill_group(p.pid, p, grace=3.0):
            n += 1
    return n


def kill_everything(builds=()):
    with _LOCK:
        tids = list(_PROCS)
    for tid in set(tids) | {(b.get("job") or {}).get("title_id") for b in builds}:
        if tid:
            kill_title(tid, builds)
