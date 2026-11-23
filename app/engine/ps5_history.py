"""What runs did, and the files they left (standard library plus ps5_failures).

* history.jsonl: one line per fact, appended by the engine as it happens and never rewritten: `run` (a run starts), `job` (a game's
  result: built, copied, failed, cancelled or deferred), `end` (the run is over). The ledger stays what the planner reads; this is what
  people read. Reading tolerates anything: a torn last line, a line from a later version, a file that is not there.
* package(): a ledger entry from any version of the app as one set of fields, `None` where the entry does not say.
* the build logs: which log belongs to which game, how much room the logs take, rotating the engine's own log, and pruning old ones
  by age and by size (never the ones a recorded failure points at, never anything written to in the last hour).
* replaced packages: "<name> (replaced YYYYmmddHHMM).pkg", what finish_build leaves when it builds a package again."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

import ps5_failures

HISTORY = "history.jsonl"
MAX_READ = 32 << 20                      # of history.jsonl, the last 32 MiB are read
KEEP_REPLACED = ("all", "1", "never")
REPLACED_RE = re.compile(r"^(?P<stem>.+) \(replaced (?P<ts>\d{12})\)\.pkg$")
BUILD_LOG_RE = re.compile(r"^(?P<ts>\d{8}-\d{6})_(?P<tid>(?:PPSA|CUSA)\d{5})\.log$")
RUN_LOG_RE = re.compile(r"^run_(?P<ts>\d{8}-\d{6})\.stdout\.log$")
ROTATED_RE = re.compile(r"^pkg_sync\.(?P<ts>\d{8}-\d{6})\.log$")
RECENT_SECS = 3600                       # a log written to within the last hour is never pruned


def run_id(ts):
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(ts))


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _clean(d):
    return {k: v for k, v in d.items() if v is not None and v != ""}


# ------------------------------------------------------------------ writing and reading

def append(state, rows):
    """Add lines to history.jsonl. A history that can't be written must never stop a run: the caller ignores errors."""
    if not rows:
        return
    p = Path(state) / HISTORY
    p.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(_clean(r), ensure_ascii=False, default=str) + "\n" for r in rows)
    fd = os.open(p.with_name(p.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o666) if fcntl else None
    try:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(text)
    finally:
        if fd is not None:
            os.close(fd)


def read_rows(state, max_bytes=MAX_READ):
    """Every line of history.jsonl that is a JSON object, oldest first (only the last `max_bytes` of a big file)."""
    p = Path(state) / HISTORY
    try:
        size = p.stat().st_size
        with open(p, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()                                         # the line that was cut
            raw = fh.read()
    except OSError:
        return []
    rows = []
    for ln in raw.decode("utf-8", "replace").splitlines():
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


def log_rel(path):
    """A build log as the Logs page names it: pkg/<file>.log."""
    if not path:
        return None
    p = Path(str(path))
    return f"pkg/{p.name}" if p.suffix == ".log" and (p.parent.name == "pkg" or BUILD_LOG_RE.match(p.name)) else (p.name or None)


def job_row(res, run, level=None):
    """A job's result (what finish_build, the prepare step or a PS4 copy returned) as one line of history."""
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    status = res.get("status")
    summary = None
    if status in ("failed", "deferred") and res.get("code"):
        try:
            summary = ps5_failures.short_reason(dict(code=res.get("code"), data=data, message=str(res.get("why") or "")))
        except Exception:
            summary = None
    bsecs = _num(res.get("build_secs"))
    return _clean(dict(
        k="job", run=run, at=_num(res.get("at")) or time.time(), tid=res.get("title_id"), title=res.get("title"), name=res.get("name"),
        status=status, code=res.get("code"), phase=res.get("phase"), why=str(res.get("why") or "")[:400] or None, summary=summary,
        size=_num(res.get("size")), source=res.get("source"), output=res.get("output"),
        bytes=_num(res.get("bytes")), prep_min=_num(res.get("prep_min")), build_min=_num(res.get("build_min")), build_secs=bsecs,
        level=res.get("level", level), validate=res.get("validate") or None, sha256=res.get("sha256"),
        log=log_rel(res.get("log")), backport=res.get("backport") if isinstance(res.get("backport"), dict) else None))


def runs(rows):
    """The runs in `rows`, newest first: {run, trigger, started, finished, interrupted, counts, jobs (oldest first)}. A run that
    was cut short (no `end` line) is shown with the time of its last job as its end and `interrupted`."""
    by = {}
    for r in rows:
        run = r.get("run")
        if not isinstance(run, str):
            continue
        d = by.setdefault(run, dict(run=run, trigger=None, started=None, finished=None, interrupted=False, ended=False, jobs=[]))
        k = r.get("k")
        if k == "run":
            d.update(trigger=r.get("trigger"), started=_num(r.get("started")))
        elif k == "end":
            d.update(finished=_num(r.get("finished")), interrupted=bool(r.get("interrupted")), ended=True)
        elif k == "job":
            d["jobs"].append(r)
    out = []
    for d in by.values():
        d["jobs"].sort(key=lambda j: _num(j.get("at")) or 0)
        if d["started"] is None:
            try:
                d["started"] = time.mktime(time.strptime(d["run"], "%Y%m%d-%H%M%S"))
            except ValueError:
                pass
        if d["finished"] is None and d["jobs"]:
            d["finished"] = _num(d["jobs"][-1].get("at"))
        if not d["ended"]:
            d["interrupted"] = True
        counts = {}
        for j in d["jobs"]:
            counts[j.get("status") or "?"] = counts.get(j.get("status") or "?", 0) + 1
        d["counts"] = counts
        d["seconds"] = (d["finished"] - d["started"]) if d["finished"] is not None and d["started"] is not None else None
        out.append(d)
    out.sort(key=lambda d: d["started"] or 0, reverse=True)
    return out


# ------------------------------------------------------------------ ledger entries from every version

def parse_checks(text):
    """{passed, warned, failed} from a validator summary line ("summary: 40 passed, 0 warned, 0 failed", older "5 ok, 0 failed"), else None."""
    if not isinstance(text, str):
        return None
    m = re.search(r"summary:\s*(.*)", text)
    line = m.group(1) if m else text
    got = {}
    for key, pat in (("passed", r"(\d+)\s+(?:passed|ok)\b"), ("warned", r"(\d+)\s+warn\w*"), ("failed", r"(\d+)\s+fail\w*")):
        mm = re.search(pat, line)
        if mm:
            got[key] = int(mm.group(1))
    return got or None


def parse_minute(text):
    try:
        return time.mktime(time.strptime(str(text), "%Y-%m-%d %H:%M"))
    except (ValueError, OverflowError):
        return None


def package(tid, v):
    """A ledger entry as the History shows it, whatever version of the app wrote it: `validate` / the older `validated`, `build_min` or
    only the older total `seconds`, `built_ts` or only the `built` text. What the entry does not say is None (shown as "not recorded"),
    never a guess. Returns None for an entry that is not an object."""
    if not isinstance(v, dict):
        return None
    out = v.get("output") if isinstance(v.get("output"), str) else None
    built_ts = _num(v.get("built_ts")) or parse_minute(v.get("built"))
    build_min = _num(v.get("build_min"))
    if build_min is None and _num(v.get("build_secs")):
        build_min = round(v["build_secs"] / 60, 1)
    old_total = _num(v.get("seconds"))
    validate = v.get("validate") if isinstance(v.get("validate"), str) else v.get("validated") if isinstance(v.get("validated"), str) else None
    return dict(
        tid=tid, name=Path(out).name if out else None, version=v.get("version") if isinstance(v.get("version"), str) else None,
        output=out, source=v.get("source") if isinstance(v.get("source"), str) else None, ps4=bool(v.get("ps4")),
        files=v.get("files") if isinstance(v.get("files"), list) else None,
        bytes=_num(v.get("bytes")), source_bytes=_num(v.get("source_bytes")), built_ts=built_ts,
        built_text=v.get("built") if isinstance(v.get("built"), str) else (time.strftime("%Y-%m-%d %H:%M", time.localtime(built_ts)) if built_ts else None),
        prep_min=_num(v.get("prep_min")), build_min=build_min, total_min=round(old_total / 60, 1) if old_total else None,
        copy_secs=_num(v.get("copy_secs")), level=v.get("level") if isinstance(v.get("level"), int) else None,
        validate=validate, checks=parse_checks(validate), sha256=v.get("sha256") if isinstance(v.get("sha256"), str) else None,
        backport=v.get("backport") if isinstance(v.get("backport"), dict) else None)


def minutes_of(p):
    """How long a package took, in minutes: prepare + build when the entry has them, else the older single total; None when unknown."""
    if p.get("prep_min") is not None or p.get("build_min") is not None:
        return round((p.get("prep_min") or 0) + (p.get("build_min") or 0), 1)
    if p.get("total_min") is not None:
        return p["total_min"]
    if p.get("copy_secs") is not None:
        return round(p["copy_secs"] / 60, 1)
    return None


# ------------------------------------------------------------------ logs

def list_logs(logs):
    """Every log file under the logs folder: [{name (relative), kind, size, mtime, tid?, started?}], newest first. kind: activity (the engine's
    log), activity_old (a rotated one), scan, run (a run's raw output), build (one UltraPack build)."""
    logs = Path(logs)
    out = []
    for p in list(logs.glob("*.log")) + list((logs / "pkg").glob("*.log")):
        try:
            st = p.stat()
        except OSError:
            continue
        rel = p.relative_to(logs).as_posix()
        m = BUILD_LOG_RE.match(p.name) if p.parent.name == "pkg" else None
        if m:
            kind = "build"
        elif p.name == "pkg_sync.log":
            kind = "activity"
        elif ROTATED_RE.match(p.name):
            kind = "activity_old"
        elif p.name == "scan.stdout.log":
            kind = "scan"
        elif RUN_LOG_RE.match(p.name):
            kind = "run"
        else:
            kind = "other"
        d = dict(name=rel, kind=kind, size=st.st_size, mtime=st.st_mtime)
        if m:
            d.update(tid=m.group("tid"), started=time.mktime(time.strptime(m.group("ts"), "%Y%m%d-%H%M%S")))
        out.append(d)
    out.sort(key=lambda d: -d["mtime"])
    return out


def usage(logs):
    items = list_logs(logs)
    by = {}
    for d in items:
        b = by.setdefault(d["kind"], dict(files=0, bytes=0))
        b["files"] += 1
        b["bytes"] += d["size"]
    return dict(files=len(items), bytes=sum(d["size"] for d in items), by_kind=by)


def rotate(path, max_bytes, now=None):
    """The engine's own log grows for ever: past `max_bytes` it is renamed pkg_sync.<date>.log and a new one starts. Returns the new name or None."""
    p = Path(path)
    try:
        if p.stat().st_size <= max_bytes:
            return None
        new = p.with_name(f"pkg_sync.{run_id(now or time.time())}.log")
        if new.exists():
            return None
        p.rename(new)
        return new.name
    except OSError:
        return None


def prunable(d):
    return d["kind"] in ("build", "run", "activity_old")


def prune_logs(logs, days=0, gb=0, protect=(), now=None, dry_run=False):
    """Remove old logs: build logs, runs' raw output and rotated activity logs older than `days` (0: no age limit), then the oldest of
    them until what is left is under `gb` GiB (0: no size limit). Never the engine's current log or the scans', never a log named in
    `protect` (those a recorded failure points at), never one written to within the last hour. {removed: [names], freed: bytes}."""
    now = now or time.time()
    protect = set(protect)
    items = [d for d in list_logs(logs) if prunable(d) and d["name"] not in protect and now - d["mtime"] > RECENT_SECS]
    items.sort(key=lambda d: d["mtime"])                                      # oldest first
    doomed = []
    if days:
        doomed = [d for d in items if now - d["mtime"] > days * 86400]
    if gb:
        total = sum(d["size"] for d in list_logs(logs) if prunable(d))
        gone = {d["name"] for d in doomed}
        total -= sum(d["size"] for d in doomed)
        for d in items:
            if total <= (gb << 30):
                break
            if d["name"] not in gone:
                doomed.append(d)
                gone.add(d["name"])
                total -= d["size"]
    removed, freed = [], 0
    for d in doomed:
        if not dry_run:
            try:
                (Path(logs) / d["name"]).unlink()
            except OSError:
                continue
        removed.append(d["name"])
        freed += d["size"]
    return dict(removed=removed, freed=freed)


def failure_logs(failures):
    """The logs recorded failures point at: kept by the pruning."""
    out = set()
    for rec in (failures or {}).values():
        if isinstance(rec, dict) and isinstance(rec.get("log"), str) and rec["log"]:
            out.add(rec["log"])
    return out


def game_logs(items, tid):
    """The build logs of one game, newest first."""
    return sorted((d for d in items if d.get("tid") == tid), key=lambda d: -(d.get("started") or 0))


def log_for_package(items, tid, built_ts):
    """The build log of the build that made a package, for a ledger entry that does not say: of the game's logs the newest that started
    before the package was finished. None when there is none."""
    mine = game_logs(items, tid)
    if not mine:
        return None
    if built_ts is None:
        return mine[0]["name"]
    for d in mine:
        if (d.get("started") or 0) <= built_ts + 60:
            return d["name"]
    return None


# ------------------------------------------------------------------ replaced packages

def replaced_files(out):
    """Packages that were moved aside when a game was built again: [{name, stem, bytes, replaced_at (epoch), mtime}], newest first."""
    out = Path(out)
    res = []
    try:
        names = os.listdir(out)
    except OSError:
        return []
    for n in names:
        m = REPLACED_RE.match(n)
        p = out / n
        if not m or p.is_symlink() or not p.is_file():
            continue
        try:
            st = p.stat()
            at = time.mktime(time.strptime(m.group("ts"), "%Y%m%d%H%M"))
        except (OSError, ValueError, OverflowError):
            continue
        res.append(dict(name=n, stem=m.group("stem"), bytes=st.st_size, replaced_at=at, mtime=st.st_mtime))
    res.sort(key=lambda d: -d["replaced_at"])
    return res


def delete_replaced(out, name):
    """Delete one replaced package: only a file of that exact shape directly in the output folder. Returns its size, or raises ValueError."""
    if not REPLACED_RE.match(name or "") or "/" in name or "\\" in name or name != os.path.basename(name):
        raise ValueError("not a replaced package")
    p = Path(out) / name
    if p.is_symlink() or not p.is_file():
        raise FileNotFoundError(name)
    size = p.stat().st_size
    p.unlink()
    return size


def prune_replaced(out, stem, keep):
    """After a game was built again: keep only the `keep` newest replaced copies of it (None: keep all). Returns the names removed."""
    if keep is None:
        return []
    gone = []
    for d in [d for d in replaced_files(out) if d["stem"] == stem][keep:]:
        try:
            delete_replaced(out, d["name"])
            gone.append(d["name"])
        except (ValueError, OSError):
            pass
    return gone


def keep_count(setting):
    """The `keep_replaced` setting as a number of copies to keep: None for all, else 0 or 1."""
    return {"all": None, "1": 1, "never": 0}.get(str(setting), None)
