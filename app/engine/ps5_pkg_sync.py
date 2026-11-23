#!/usr/bin/env python3
"""ps5_pkg_sync - turn every PS5 game in the library / download folder into one
clean, well-named .pkg, and on later runs only the new ones.

  python3 -I ps5_pkg_sync.py plan              read-only: what would be built and why
  python3 -I ps5_pkg_sync.py run [--only PPSA12345 ...] [--limit N]

Per title the newest unpacked (non-LZ4/Zstd) source wins; among equal
versions a RAR set is preferred (one sequential read from the NAS), then an
image, then an extracted folder.  Each job:
  1. materialise the game on the local SSD   (7-Zip for archives, read-only
     readers for images, copy for folders) - sources are only ever read
  2. clean it: cut the configured junk_title_patterns out of every param.json
     title, delete files matching junk_file_patterns, overlay any per-title patch
  3. build with PS5 UltraPack (--stage-in-place consumes the SSD copy as the
     image grows; --fpkg-image-in-temp when it fits), N workers, low priority
  4. --fpkg-validate, then rename into the output as
     "<Title> [PPSA12345] [v01.002.003] [fw10.00].pkg"
  5. delete the job's SSD folder whether it worked or not
A ledger (state/ledger.json) remembers what was built.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import unicodedata
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

try:
    import fcntl
except ImportError:          # not POSIX: writes stay atomic, only the lock shared between processes is lost
    fcntl = None

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ps5_ampr_audit as A  # noqa: E402  (read-only exFAT/PFS/PFSC/UFS2 readers)
import ps5_failures  # noqa: E402  (what failed, in words, and whether another try can help)
import ps5_progress as PG  # noqa: E402  (what UltraPack's log says, how far a build is, how long it will take)
import ps5_ps4pkg  # noqa: E402  (PS4 package headers: name and classify packages by what they say)
import ps5_hostpaths  # noqa: E402  (where the container's folders are on the host: validated here, shown by the server)
import ps5_setup  # noqa: E402  (first-run setup checks, shared with the web server)

ROOT = HERE.parent
IN_DOCKER = os.environ.get("PS5PKG_PROFILE") == "docker"
STATE = Path(os.environ.get("PS5PKG_STATE") or ROOT / "state")
LOGS = Path(os.environ.get("PS5PKG_LOGS") or ROOT / "logs")
CFG_PATH = STATE / "config.json"
DEFAULT_CFG = {
    "library": "/library" if IN_DOCKER else str(Path.home() / "PS5" / "library"),
    "downloads": "/downloads" if IN_DOCKER else str(Path.home() / "PS5" / "downloads"),
    "output": "/output" if IN_DOCKER else str(Path.home() / "PS5" / "pkg"),
    "temp": "/work/tmp" if IN_DOCKER else str(ROOT / "work" / "pkg-temp"),
    "ultrapack_src": "/opt/ultrapack" if IN_DOCKER else str(ROOT / "work" / "ultrapack-src"),
    "python": "/opt/venv/bin/python" if IN_DOCKER else str(ROOT / "work" / "venv-ultrapack" / "bin" / "python"),
    "max_compress": 2,
    "max_builds": 3,
    "build_on_ssd": False,
    "console_fw": "",
    "notify_url": "",
    "schedule": {"enabled": False, "times": ["03:00"]},
    "cpu_count": 8,
    "nice": 10,
    "compression_level": 7,
    "ssd_reserve_gb": 80,
    "skip_download_prefixes": ["PC_"],
    "junk_file_patterns": [],      # regexes, e.g. a release group's site name in .txt/.url files
    "junk_title_patterns": [],     # regexes cut out of game titles
    # title_id -> {"source": exact path (optional), "patch": dir of loose files to overlay}
    "overrides": {},
    "host_paths": {},              # folder -> where it is on the host, for display only (see ps5_hostpaths)
    "ps4_mode": "copy",            # PS4 titles (CUSA...): "copy" their ready-made packages as they are, or "skip" them
}
SEVENZ = ps5_setup.SEVENZ
JUNK_FILE = JUNK_TITLE = None   # compiled from cfg junk_file_patterns / junk_title_patterns by load_cfg()


def set_junk(cfg):
    """Release-group watermarks to clean: files whose name matches a junk_file_pattern are deleted,
    and junk_title_patterns are cut out of titles (param.json and the package name)."""
    global JUNK_FILE, JUNK_TITLE
    jf = [x for x in cfg.get("junk_file_patterns") or [] if x]
    jt = [x for x in cfg.get("junk_title_patterns") or [] if x]
    JUNK_FILE = re.compile("|".join(f"(?:{x})" for x in jf), re.I) if jf else None
    JUNK_TITLE = re.compile(r"\s*[-_–—]?\s*(?:" + "|".join(f"(?:{x})" for x in jt) + r")\s*", re.I) if jt else None


# ------------------------------------------------------------------ settings file
#
# config.json is read by two processes (the web server on every status poll and scheduler tick, the engine
# when it starts), written by the web server when Settings or a per-game option is saved, and may be edited
# by hand. So: reading never writes; the file is only ever replaced atomically; a save is a locked
# read-modify-write of what is in the file (a concurrent save can't be reverted, keys this version doesn't
# know survive); and a file that can't be used is reported instead of raised.

SETTINGS = {  # key: (type, min, max) - everything the Settings page may change
    "library": (str,), "downloads": (str,), "output": (str,), "temp": (str,),
    "compression_level": (int, 1, 9), "cpu_count": (int, 1, 64), "nice": (int, 0, 19),
    "max_compress": (int, 1, 4), "max_builds": (int, 1, 6), "ssd_reserve_gb": (int, 0, 10000),
    "build_on_ssd": (bool,), "console_fw": (str,), "notify_url": (str,),
    "skip_download_prefixes": (list,), "junk_file_patterns": (list,), "junk_title_patterns": (list,),
    "schedule": (dict,), "host_paths": (dict,), "ps4_mode": (str,),
}
PS4_MODES = ("copy", "skip")
NOTIFY_URL_RE = re.compile(r"https?://[^\s/?#]+", re.I)     # a push notification goes to a web address, never to file:// and the like
FILE_ONLY = {"ultrapack_src": (str,), "python": (str,)}   # valid in the file, not on the Settings page
EXIT_NOT_MOUNTED = 2        # exit codes of main(); the web server explains them to the user
EXIT_LOCKED = 3
EXIT_BAD_CONFIG = 4
EXIT_SETUP = 5
_WORDS = {str: "text", list: "a list", dict: "an object"}


class ConfigError(Exception):
    """config.json exists but can't be used: not JSON, not an object, or unreadable."""

    def __init__(self, message, line=None, column=None):
        super().__init__(message)
        self.message, self.line, self.column = message, line, column

    def describe(self):
        return f"line {self.line}: {self.message}" if self.line else self.message

    def as_dict(self):
        return dict(message=self.message, line=self.line, column=self.column)


class CfgInfo(NamedTuple):
    cfg: dict               # effective settings: defaults, then the file, invalid values replaced
    error: object           # ConfigError when the file can't be used (cfg is then the last good one), else None
    problems: list          # invalid values found in the file and replaced by their defaults
    rev: object             # "mtime_ns-size" of the file, changes whenever it is rewritten; None when absent
    fallback: object = None  # with an error: "last_good" or "defaults", whichever cfg is


def _short(v):
    r = repr(v)
    return r if len(r) <= 60 else r[:57] + "..."


def validate_setting(key, v):
    """One value from the Settings page or from config.json: returns it cleaned up, or raises ValueError(why)."""
    spec = SETTINGS.get(key) or FILE_ONLY.get(key)
    if spec is None:
        raise ValueError(f"unknown setting {key}")
    t = spec[0]
    if t is int:
        n = v
        if isinstance(v, str) and re.fullmatch(r"\s*-?\d+\s*", v):
            n = int(v)
        elif isinstance(v, float) and v.is_integer():
            n = int(v)
        if isinstance(n, bool) or not isinstance(n, int):
            raise ValueError(f"{key} must be a whole number {spec[1]}..{spec[2]}")
        if not spec[1] <= n <= spec[2]:
            raise ValueError(f"{key} must be {spec[1]}..{spec[2]}")
        return n
    if t is bool:
        if not isinstance(v, bool):
            raise ValueError(f"{key} must be true or false")
        return v
    if not isinstance(v, t):
        raise ValueError(f"{key} must be {_WORDS[t]}")
    if key == "console_fw" and v and not re.fullmatch(r"\d{1,2}\.\d{2}", v):
        raise ValueError("console_fw looks like 13.20")
    if key == "notify_url":
        v = v.strip()
        if v and not NOTIFY_URL_RE.match(v):
            raise ValueError("notify_url must start with http:// or https:// (an ntfy topic URL, for example)")
    if t is list:
        if not all(isinstance(x, str) for x in v):
            raise ValueError(f"{key} must be a list of text")
        v = [x for x in v if x.strip()]
        if key in ("junk_file_patterns", "junk_title_patterns"):
            for x in v:
                try:
                    re.compile(f"(?:{x})")
                except re.error as ex:
                    raise ValueError(f"{key}: bad pattern {x!r}: {ex}")
    if key == "ps4_mode" and v not in PS4_MODES:
        raise ValueError(f"ps4_mode must be {' or '.join(PS4_MODES)}")
    if key == "host_paths":
        v = ps5_hostpaths.validate_setting(v)
    if key == "schedule":
        enabled, times = v.get("enabled", False), v.get("times", [])
        if not isinstance(enabled, bool):
            raise ValueError("schedule.enabled must be true or false")
        if not isinstance(times, list):
            raise ValueError("schedule.times must be a list")
        for x in times:
            if not (isinstance(x, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", x)):
                raise ValueError(f"schedule.times: {_short(x)} is not a 24-hour HH:MM time")
        v = dict(enabled=enabled, times=times)
    return v


def _clean_overrides(v):
    """Per-title options: returns (clean overrides, notes). Fields the engine doesn't read are kept as they are."""
    if not isinstance(v, dict):
        return {}, [f"overrides must be an object (found {_short(v)}); ignoring it"]
    is_str, is_bool = (lambda x: isinstance(x, str)), (lambda x: isinstance(x, bool))
    fields = (("source", is_str), ("patch", is_str), ("note", is_str), ("skip", is_bool), ("skip_verify", is_bool),
              ("modified_exec", lambda x: x in ("", "image", "original")))
    out, notes = {}, []
    for tid, ov in v.items():
        if not isinstance(ov, dict):
            notes.append(f"overrides.{tid} must be an object (found {_short(ov)}); ignoring it")
            continue
        ov = dict(ov)
        for k, ok in fields:
            if ov.get(k) is not None and not ok(ov[k]):
                notes.append(f"overrides.{tid}.{k} is not valid (found {_short(ov[k])}); ignoring it")
                del ov[k]
        out[tid] = ov
    return out, notes


def validate_cfg(raw):
    """The effective settings for the contents of config.json: (cfg, problems). Defaults fill what the file
    leaves out; a value that fails validation is replaced by its default and listed in problems; keys this
    version doesn't know are kept. Never touches the file."""
    cfg = copy.deepcopy(DEFAULT_CFG)
    problems = []
    for k, v in raw.items():
        if k == "overrides":
            cfg[k], notes = _clean_overrides(v)
            problems += notes
        elif k in SETTINGS or k in FILE_ONLY:
            try:
                cfg[k] = validate_setting(k, v)
            except ValueError as ex:
                problems.append(f"{ex} (found {_short(v)}); using the default {_short(DEFAULT_CFG[k])}")
        else:
            cfg[k] = v
    return cfg, problems


def cfg_rev():
    try:
        st = CFG_PATH.stat()
    except OSError:
        return None
    return f"{st.st_mtime_ns}-{st.st_size}"


def _read_raw():
    """The parsed file as a dict, None when there is no file; raises ConfigError. A second try after a moment
    covers an editor that saves by truncating and rewriting in place."""
    err = None
    for attempt in (0, 1):
        try:
            text = CFG_PATH.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError:
            err = ConfigError("the file is not UTF-8 text")
        except OSError as ex:
            raise ConfigError(f"can't read the file: {ex.strerror or ex}")
        else:
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as ex:
                err = ConfigError(ex.msg, ex.lineno, ex.colno)
            else:
                if isinstance(raw, dict):
                    return raw
                err = ConfigError("the top level must be a JSON object ({ ... })", 1, 1)
        if attempt == 0:
            time.sleep(0.05)
    raise err


def write_json_atomic(path, obj):
    """Pretty JSON into a temp file next to `path`, then one rename over it: a reader sees the old file or the
    new one, never half of it. Keeps the permission bits the file already had."""
    write_text_atomic(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def write_text_atomic(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        except FileNotFoundError:
            pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_cfg_thread_lock = threading.Lock()
_good_lock = threading.Lock()
_last_good = None            # (cfg, problems) from the last file that parsed, for when it stops parsing


class _cfg_write_lock:
    """Exclusive lock on config.json.lock: held by anything about to change config.json, in any process."""

    def __enter__(self):
        _cfg_thread_lock.acquire()
        self.fd = None
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            if fcntl:
                lock = STATE / (CFG_PATH.name + ".lock")
                try:
                    self.fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o666)
                except PermissionError:          # created by another user: a read-only handle can still lock
                    self.fd = os.open(lock, os.O_RDONLY)
                fcntl.flock(self.fd, fcntl.LOCK_EX)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)                    # closing the descriptor releases the lock
        _cfg_thread_lock.release()


# ------------------------------------------------------------------ automatic backups
# Before config.json is changed, its previous content is copied to state/backups/config-YYYYmmdd-HHMMSS.json and
# only the newest BACKUP_KEEP stay. Only a real change (a save, an import, a restore) makes one: reading, polling
# and a save that changes nothing never write anything.
BACKUP_KEEP = 10
BACKUP_RE = re.compile(r"^config-(\d{8}-\d{6})(?:-(\d+))?\.json$")
backup_status = {"error": None}          # why the last backup could not be made, for the storage card


def backups_dir():
    return STATE / "backups"


def _backup_order(name):
    m = BACKUP_RE.match(name)
    return (m.group(1), int(m.group(2) or 1)) if m else ("", 0)


def list_backups():
    """[{name, at, size}], newest first."""
    out = []
    try:
        for p in backups_dir().iterdir():
            if BACKUP_RE.match(p.name) and p.is_file():
                st = p.stat()
                out.append(dict(name=p.name, at=st.st_mtime, size=st.st_size))
    except OSError:
        return []
    return sorted(out, key=lambda b: _backup_order(b["name"]), reverse=True)


def _backup_current():
    """Copy config.json as it is now into the backups folder (call with the write lock held); prune. Returns the
    name, or None when there was nothing to copy or it could not be done (a settings save must not fail over
    its backup: the reason is in backup_status)."""
    try:
        data = CFG_PATH.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as ex:
        backup_status["error"] = f"can't read {CFG_PATH.name}: {ex.strerror or ex}"
        return None
    try:
        d = backups_dir()
        d.mkdir(parents=True, exist_ok=True)
        stamp, n = time.strftime("%Y%m%d-%H%M%S"), 1
        name = f"config-{stamp}.json"
        while (d / name).exists():
            n += 1
            name = f"config-{stamp}-{n}.json"
        (d / name).write_bytes(data)
        for old in list_backups()[BACKUP_KEEP:]:
            (d / old["name"]).unlink(missing_ok=True)
        backup_status["error"] = None
        return name
    except OSError as ex:
        backup_status["error"] = f"can't write the backup: {ex.strerror or ex}"
        return None


def _remember_good(cfg, problems):
    global _last_good
    with _good_lock:
        _last_good = (copy.deepcopy(cfg), list(problems))


def load_cfg_info():
    """Read-only view of the settings. Never writes the file (except creating it from the defaults when there
    is none yet) and never raises for a bad file: it returns the last good settings with `error` set, or the
    defaults when the file has been bad since this process started."""
    rev = cfg_rev()
    try:
        raw = _read_raw()
    except ConfigError as err:
        with _good_lock:
            good = copy.deepcopy(_last_good)
        cfg, problems = good if good else (copy.deepcopy(DEFAULT_CFG), [])
        return CfgInfo(cfg, err, problems, rev, "last_good" if good else "defaults")
    if raw is None:                             # first start: leave a file to edit, once
        try:
            with _cfg_write_lock():
                if not CFG_PATH.exists():
                    write_json_atomic(CFG_PATH, DEFAULT_CFG)
        except OSError:
            pass
        rev = cfg_rev()
        raw = {}
    cfg, problems = validate_cfg(raw)
    _remember_good(cfg, problems)
    set_junk(cfg)
    return CfgInfo(cfg, None, problems, rev)


def load_cfg():
    return load_cfg_info().cfg


def update_cfg(mutate):
    """Change config.json: under the lock, re-read the file, let mutate(raw) edit that dict in place, write it
    back atomically. Only what mutate touches changes; other values, keys this version doesn't know and
    anything invalid stay as they are. Returns the effective settings. Raises ConfigError, writing nothing,
    when the file can't be parsed: a hand edit is never overwritten."""
    with _cfg_write_lock():
        raw = _read_raw()
        existed = raw is not None
        if raw is None:
            raw = copy.deepcopy(DEFAULT_CFG)
        before = copy.deepcopy(raw)
        mutate(raw)
        if raw != before or not existed:          # a save that changes nothing leaves the file and the backups alone
            if existed:
                _backup_current()
            write_json_atomic(CFG_PATH, raw)
    cfg, problems = validate_cfg(raw)
    _remember_good(cfg, problems)
    return cfg


def restore_backup(name):
    """Make a backup the current config.json. The file it replaces is backed up first, like any change. Raises
    ValueError for a name that isn't one of ours or a backup that isn't a JSON object, FileNotFoundError when
    it is gone."""
    if not BACKUP_RE.match(name):
        raise ValueError("not a backup name")
    text = (backups_dir() / name).read_text(encoding="utf-8")       # FileNotFoundError when it is gone
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except json.JSONDecodeError as ex:
        raise ValueError(f"that backup is not valid JSON (line {ex.lineno}: {ex.msg})")
    if not isinstance(data, dict):
        raise ValueError("that backup is not a JSON object")
    with _cfg_write_lock():
        if CFG_PATH.exists():
            _backup_current()
        write_text_atomic(CFG_PATH, text)
    cfg, problems = validate_cfg(data)
    _remember_good(cfg, problems)
    return cfg


def vkey(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def fw_from_param(req):
    try:
        w = int(req, 16) >> 32
        bcd = lambda b: (b >> 4) * 10 + (b & 0xF)
        return f"{bcd((w >> 24) & 0xFF)}.{bcd((w >> 16) & 0xFF):02d}"
    except Exception:
        return ""


TITLE_ID_RE = re.compile(r"(PPSA|CUSA)\d{5}")


def valid_title_id(tid) -> bool:
    """A title ID as the consoles write it: PPSA or CUSA and five digits, nothing else. The ID of a source comes
    from a file somebody else made (param.json, PARAM.SFO), ends up in folder names, URLs and the web page, and is
    never trusted until it has passed this."""
    return isinstance(tid, str) and TITLE_ID_RE.fullmatch(tid) is not None


def parse_param(raw: bytes) -> dict:
    j = json.loads(raw.decode("utf-8-sig"))
    lp = j.get("localizedParameters", {}) or {}
    title = (lp.get("en-US") or {}).get("titleName") or (lp.get(lp.get("defaultLanguage", "")) or {}).get("titleName")
    tid = j.get("titleId", "")
    if not valid_title_id(tid):
        raise ValueError(f"param.json has no valid title ID (PPSA or CUSA and 5 digits); found {_short(tid)}")
    return dict(title=title or "", title_id=tid, version=j.get("contentVersion", ""),
                fw=fw_from_param(j.get("requiredSystemSoftwareVersion")))


PATCHED_PARAM = "sce_sys/param.json"


def patched_identity(patch):
    """What a patch folder's sce_sys/param.json makes of the game (title id, title, version, firmware), or None when
    the patch has none, it can't be read, or it says no version (then the name is not taken from it). The patch is
    copied over the game's files, so this is the file the package is built from, and the one its name must come from."""
    if not patch:
        return None
    try:
        ident = parse_param((Path(patch) / PATCHED_PARAM).read_bytes())
    except (OSError, ValueError, AttributeError):
        return None
    return ident if ident["version"] else None


def clean_title(t: str) -> str:
    t = (JUNK_TITLE.sub(" ", t) if JUNK_TITLE else t).replace("™", "").replace("®", "")
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", t)
    return re.sub(r"\s+", " ", t).strip(" .-_")[:120] or "Unknown"


def pkg_name(ident):
    fw = f" [fw{ident['fw']}]" if ident.get("fw") and ident["fw"] != "0.00" else ""
    return f"{clean_title(ident['title'])} [{ident['title_id']}] [v{ident['version']}]{fw}.pkg"


def log_to(fh):
    def log(m):
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
        print(line, flush=True)
        fh.write(line + "\n")
        fh.flush()
        return line
    return log


_JOB_LINES = {}          # title_id -> the log lines of that game's current attempt (for the failure excerpt)


def job_log(log, tid):
    """`log` that also keeps the lines of one game: when it fails, its stored failure carries them."""
    def jl(m):
        line = log(m)
        lines = _JOB_LINES.setdefault(tid, [])
        lines.append(str(line if line is not None else m))
        del lines[:-300]
    return jl


def job_excerpt(tid, n=60):
    return [ln[:300] for ln in _JOB_LINES.get(tid, [])[-n:]]


def failure_store():
    return ps5_failures.Store(STATE / "failures.json")


def record_failure(cfg, job, log, *, phase, code, message, data=None, state=ps5_failures.FAILED, logf=None):
    """Remember why a game failed (or is waiting for space). True when this is a new kind of failure worth a
    notification; never raises: losing the memory must not stop a run."""
    try:
        tid = job["title_id"]
        fp = job.get("fingerprint") or ps5_failures.fingerprint(cfg, job)
        return failure_store().record(
            tid, state=state, title=job.get("title", ""), name=job.get("name", ""), phase=phase, code=code, message=message,
            data=data, log=(f"pkg/{Path(logf).name}" if logf else "pkg_sync.log"), excerpt=job_excerpt(tid),
            fingerprint=fp, version=ps5_failures.app_version())
    except Exception as ex:
        log(f"  could not store the failure of {job.get('title_id')}: {type(ex).__name__}: {ex}")
        return True


# ------------------------------------------------------------------ discovery

def ident_image_or_folder(path):
    uw, base = A.open_game_fs(path)
    try:
        root = uw.root or ""
        files = {k[len(root):].lower(): e for k, e in uw.fs.files.items() if k.startswith(root)}
        pj = files.get("sce_sys/param.json")
        if not pj:
            sfo = files.get("sce_sys/param.sfo")           # a PS4 game folder: named, so Library can say it is not handled
            if sfo:
                p = ps5_ps4pkg.parse_sfo(uw.fs.read(sfo, 0, min(sfo.size, 1 << 20)))
                if valid_title_id(p.get("TITLE_ID")) and p["TITLE_ID"].startswith("CUSA"):
                    return dict(title=str(p.get("TITLE") or ""), title_id=p["TITLE_ID"], version=ps5_ps4pkg.version_of(p), fw="",
                                packed="", size=sum(e.size for e in files.values()), kind="ps4-folder", ps4=True)
            raise ValueError("no sce_sys/param.json")
        d = parse_param(uw.fs.read(pj, 0, pj.size))
        d["packed"] = ("LZ4" if "ampr_assets.index" in files else "") + ("Zstd" if "ampr_zstd.index" in files else "")
        d["size"] = sum(e.size for e in files.values())
        d["kind"] = "folder" if os.path.isdir(path) else Path(path).suffix.lstrip(".").lower()
        return d
    finally:
        if base:
            base.close()


def seven(cfg, args, **kw):
    return subprocess.run([str(Path(cfg["ultrapack_src"]) / SEVENZ), *args], capture_output=True, **kw)


LISTING_CACHE = STATE / "listing_cache"


def archive_listing(cfg, path, crcs=None, timeout=1800):
    """7zz listing. Multi-volume RARs with many files cost one NAS seek per file header
    (Returnal: >30 min), so listings are cached by path + size + mtime of the first volume."""
    st = os.stat(path)
    key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()
    cache = LISTING_CACHE / f"{key}.txt"
    if cache.exists():
        text = cache.read_text(encoding="utf-8")
    else:
        r = seven(cfg, ["l", "-slt", "-ba", "-p-", path], timeout=timeout, stdin=subprocess.DEVNULL)
        text = r.stdout.decode("utf-8", "replace")
        if r.returncode != 0:
            return _parse_listing(text, crcs)    # partial/odd archive: use what was listed, don't cache
        LISTING_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".part")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(cache)
    return _parse_listing(text, crcs)


def _parse_listing(text, crcs):
    entries, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                entries.append(cur)
            cur = {}
        elif " = " in line:
            k, v = line.split(" = ", 1)
            cur[k] = v
    if cur:
        entries.append(cur)
    out = {e["Path"].replace("\\", "/"): int(e.get("Size") or 0)
           for e in entries if e.get("Path") and e.get("Folder") != "+"}
    if crcs is not None:
        for e in entries:
            if e.get("Path") and e.get("CRC"):
                crcs[e["Path"].replace("\\", "/")] = int(e["CRC"], 16)
    return out


IDENT_VERSION = 3                    # bump to make every archive be identified again (the ident cache is versioned)
PS4_NESTED_MAX = 2 << 30            # a nested archive (DLC.zip) up to this size is opened while scanning to see its packages
PS4_MAX_PKGS = 40
ARCHIVE_EXT = (".zip", ".rar", ".7z")


def seven_env():
    return dict(os.environ, LC_ALL="C.UTF-8")           # names with accents or kana in the archive and on the command line


def seven_head(cfg, archive, member, nbytes=ps5_ps4pkg.HEAD_BYTES, timeout=180):
    """The first bytes of an archive member: 7-Zip streams it, we stop reading once we have them and stop 7-Zip."""
    exe = str(Path(cfg["ultrapack_src"]) / SEVENZ)
    proc = subprocess.Popen([exe, "e", "-so", "-p-", archive, member], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, env=seven_env())
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    try:
        return proc.stdout.read(nbytes)
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()
        proc.stdout.close()


def ps4_describe(cfg, archive, member, size, via=None):
    """One .pkg of a PS4 set: {path, size, via, kind, title, version, content_id, header} from its own header (the
    file name only when the header can't be read)."""
    head = None
    try:
        head = ps5_ps4pkg.parse_header(seven_head(cfg, archive, member))
    except OSError:
        pass
    d = dict(path=member, size=size, via=via, kind="other", title="", version="", content_id="", header=False, ps4=None)
    if head and head["ps4"]:
        sfo = head["sfo"]
        d.update(header=True, ps4=True, content_id=head["content_id"], title_id=head["title_id"], title=str(sfo.get("TITLE") or ""),
                 title_en=ps5_ps4pkg.english_title(sfo), version=ps5_ps4pkg.version_of(sfo), category=str(sfo.get("CATEGORY") or ""))
        d["kind"] = ps5_ps4pkg.category_kind(sfo.get("CATEGORY")) if sfo else ps5_ps4pkg.kind_from_name(Path(member).name)
    elif head is not None:
        d.update(header=True, ps4=False)                 # a package of another kind (PS5), or not a package at all
    else:
        d["kind"] = ps5_ps4pkg.kind_from_name(Path(member).name)
    return d


def ps4_open_nested(cfg, archive, member, workdir):
    """Extract a nested archive (DLC.zip) from `archive` into `workdir` and list it: (path of the nested archive on
    disk, {member: size}, "" or why it could not be opened)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([str(Path(cfg["ultrapack_src"]) / SEVENZ), "x", "-y", "-bso0", "-bsp0", f"-o{workdir}", "-p-", archive, member],
                       capture_output=True, stdin=subprocess.DEVNULL, env=seven_env())
    nested = workdir / member
    if r.returncode != 0 or not nested.is_file():
        return None, {}, f"extracting {Path(member).name} failed ({r.returncode})"
    ls = subprocess.run([str(Path(cfg["ultrapack_src"]) / SEVENZ), "l", "-slt", "-ba", "-p-", str(nested)], capture_output=True,
                        stdin=subprocess.DEVNULL, env=seven_env())
    if ls.returncode != 0:
        return None, {}, f"{Path(member).name} can't be read as an archive"
    return nested, _parse_listing(ls.stdout.decode("utf-8", "replace"), None), ""


def ident_ps4(cfg, path, paths):
    """A PS4 title in an archive that holds ready-made packages: a candidate (kind archive-ps4) listing its .pkg
    members, each named and classified by its own header (game / patch / dlc), or None when this isn't a PS4 set.
    Packages inside a nested archive (DLC.zip) are listed too; one with no .pkg in it is noted, copying nothing."""
    pkgs = sorted(p for p in paths if p.lower().endswith(".pkg"))[:PS4_MAX_PKGS]
    if not pkgs:
        return None
    hint = re.search(r"CUSA\d{5}", path) or any(re.search(r"CUSA\d{5}", p) for p in paths)
    members = [ps4_describe(cfg, path, p, paths[p]) for p in pkgs]
    if not (any(m["ps4"] for m in members) or (hint and any(m["ps4"] is None for m in members))):
        return None                                      # not a PS4 set (PS5 packages, say)
    members = [m for m in members if m["ps4"] is not False]
    notes = []
    for nested in [p for p in paths if p.lower().endswith(ARCHIVE_EXT)][:10]:
        if paths[nested] > PS4_NESTED_MAX:
            members.append(dict(path=nested, size=paths[nested], via=None, kind="nested-archive", inspected=False, ps4=None))
            notes.append(f"{Path(nested).name} ({paths[nested] / 2**30:.1f} GiB) is opened when the packages are copied")
            continue
        tmp = Path(cfg["temp"]) / f"ps4-inspect-{os.getpid()}-{hashlib.sha1(nested.encode()).hexdigest()[:8]}"
        try:
            inner_path, inner, why = ps4_open_nested(cfg, path, nested, tmp)
            if inner_path is None:
                notes.append(f"{Path(nested).name}: {why}; nothing is copied from it")
                continue
            ips = sorted(q for q in inner if q.lower().endswith(".pkg"))[:PS4_MAX_PKGS]
            if not ips:
                notes.append(f"{Path(nested).name}: no .pkg inside, nothing is copied from it")
            members += [m for m in (ps4_describe(cfg, str(inner_path), q, inner[q], via=nested) for q in ips) if m["ps4"] is not False]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    ids = [m["title_id"] for m in members if m.get("title_id")]
    tid = max(set(ids), key=ids.count) if ids else (re.search(r"CUSA\d{5}", " ".join([path] + [m["path"] for m in members])) or [None])[0]
    if not tid:
        return None
    foreign = [m for m in members if m.get("title_id") and m["title_id"] != tid]
    if foreign:
        notes.append(f"{len(foreign)} package(s) of another title ({', '.join(sorted({m['title_id'] for m in foreign}))}) are not copied")
        members = [m for m in members if m not in foreign]
    game = next((m for m in members if m["kind"] == "game"), None)
    if not game:                                         # no header said which is the game: the biggest package, then
        unread = [m for m in members if m["ps4"] is None and m["kind"] != "nested-archive"]
        if unread:
            game = max(unread, key=lambda m: m["size"])
            game["kind"] = "game"
            notes.append(f"the header of {Path(game['path']).name} could not be read: it is taken as the game because it is the biggest")
    if not game:
        return None                                      # patches / DLC only: not a game to copy
    versions = [m["version"] for m in members if m.get("version")]
    return dict(title=game.get("title") or "", title_en=game.get("title_en") or "", game_path=game["path"],
                title_id=tid, version=max(versions, key=vkey) if versions else "", fw="", packed="",
                size=sum(m["size"] for m in members if m["kind"] != "nested-archive"), kind="archive-ps4", ps4=True,
                members=members, notes=notes)


def ident_archive(cfg, path):
    paths = archive_listing(cfg, path)
    pj = [p for p in paths if p.lower().endswith("sce_sys/param.json")]
    if not pj:
        ps4 = ident_ps4(cfg, path, paths)
        if ps4:
            return ps4
        inner = [p for p in paths if p.lower().endswith((".exfat", ".ffpfsc", ".ffpfs", ".ffpkg", ".pkg"))
                 and paths[p] > (1 << 30)]
        if len(inner) != 1:
            return None                 # patch / backport / DLC / unlocker archive
        m = re.search(r"(PPSA|CUSA)\d{5}", inner[0]) or re.search(r"(PPSA|CUSA)\d{5}", path)
        if not m or m.group(0).startswith("CUSA"):
            return None                 # (a PS4 title never goes to the PS5 conversion)
        is_pkg = inner[0].lower().endswith(".pkg")
        return dict(title=Path(path).parent.parent.name if is_pkg else Path(path).parent.name, title_id=m.group(0),
                    version="", fw="", packed="", size=paths[inner[0]], inner=inner[0],
                    kind="archive-pkg" if is_pkg else "archive-image")
    pj = min(pj, key=lambda p: p.count("/"))
    root = pj[: -len("sce_sys/param.json")]
    names = {p[len(root):].lower() for p in paths if p.startswith(root)}
    if "eboot.bin" not in names:
        return None                     # add-on content, not a game
    d = parse_param(seven(cfg, ["e", "-so", path, pj], timeout=1800).stdout)
    d["packed"] = ("LZ4" if "ampr_assets.index" in names else "") + ("Zstd" if "ampr_zstd.index" in names else "")
    d["size"] = sum(s for p, s in paths.items() if p.startswith(root))
    d["kind"] = Path(path).suffix.lstrip(".").lower()
    d["archive_root"] = root
    return d


def walk_downloads(folder: Path):
    """Archive-set heads and images under folder; extracted game folders (eboot.bin + sce_sys) are not entered."""
    heads, images = [], []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(("@", "#recycle", "."))]  # DSM metadata
        low = {f.lower() for f in filenames}
        if "eboot.bin" in low and any(d.lower() == "sce_sys" for d in dirnames):
            dirnames[:] = []
            continue
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.startswith("._"):
                continue
            p = Path(dirpath) / fn
            n = fn.lower()
            m = re.search(r"\.part0*(\d+)\.rar$", n)
            if m:
                if int(m.group(1)) == 1:
                    heads.append(p)
            elif re.search(r"\.part\d+[^.]+\.rar$", n):
                continue                    # renamed duplicate of a volume, e.g. "x.part2_20261122141625.rar"
            elif n.endswith((".rar", ".zip", ".7z")):
                try:
                    if p.stat().st_size > (1 << 30):
                        heads.append(p)
                except OSError:
                    pass
            elif n.endswith((".exfat", ".ffpfsc", ".ffpfs", ".ffpkg")):
                images.append(p)
    return heads, images


def discover(cfg, log, cache):
    out = Path(cfg["output"])
    cands = []

    def add(path, origin, fn):
        p = str(path)
        try:
            st = os.stat(p)
            key = f"{p}|{st.st_size}|{int(st.st_mtime)}"
        except OSError:
            key = p
        if key not in cache:
            try:
                cache[key] = fn()
            except Exception as ex:
                cache[key] = {"error": f"{type(ex).__name__}: {ex}"}
        ident = cache[key]
        if ident is None:
            return
        if not ident.get("error") and not valid_title_id(ident.get("title_id")):      # also what an older version cached
            ident = {"error": f"not a valid title ID: {_short(ident.get('title_id'))}"}
        ident = dict(ident, source=p, origin=origin)
        cands.append(ident)
        log(f"  {origin:8} {ident.get('title_id', '?'):10} v{ident.get('version', '?'):11} {ident.get('kind', '?'):6} "
            f"{('[' + ident['packed'] + ']') if ident.get('packed') else '':6} {ident.get('error', '')} {p}")

    log("library:")
    for kind, p in A.inventory(cfg["library"]):
        if kind == "pkg" or Path(p).is_relative_to(out):
            continue
        add(p, "library", lambda p=p: ident_image_or_folder(p))
    log("downloads (archive sets and images only):")
    downloads, tops = Path(cfg["downloads"]), []
    if not downloads.is_dir():                 # optional: a library-only install doesn't mount it
        log(f"  {downloads} is not mounted (optional); scanning the library only")
    else:
        try:
            tops = sorted(downloads.iterdir())
        except OSError as ex:
            log(f"  can't read {downloads}: {ex}; scanning the library only")
    for top in tops:
        if not top.is_dir() or any(top.name.startswith(x) for x in cfg["skip_download_prefixes"]):
            continue
        heads, images = walk_downloads(top)
        for img in images:
            add(img, "download", lambda img=img: ident_image_or_folder(str(img)))
        for h in heads:
            add(h, "download", lambda h=h: ident_archive(cfg, str(h)))
    return cands


# Same content either way (checked: image vs RAR differ only in ampr_emu.index, which UltraPack rebuilds).
# Uncompressed images are a straight sequential read; RAR costs single-threaded decode; .ffpfsc costs zlib
# decode of every block (plus a nested UFS2 walk); folders over SMB pay per-file overhead.
# Measured 2026-11-22 extracting the same ~20 GiB to the SSD (logs/pkg/bench_read.log):
# .ffpfsc 409 MiB/s, .exfat 264 MiB/s, RAR 172 MiB/s; folders over SMB are slower still.
KIND_PREF = {"ffpfsc": 0, "ffpfs": 0, "exfat": 0, "ffpkg": 0, "rar": 1, "zip": 1, "7z": 1,
             "archive-image": 2, "archive-pkg": 2, "archive-ps4": 2, "ps4-folder": 3, "folder": 3}


def nas_exists(p) -> bool:
    """smbfs keeps names as stored (NFD here): check both Unicode normal forms."""
    import unicodedata
    return any(Path(unicodedata.normalize(f, str(p))).exists() for f in ("NFC", "NFD"))


def ps4_clean_name(name):
    """The name of a PS4 package that is copied under its own name (patches, DLC): the configured junk patterns
    removed (junk_title_patterns and the text junk_file_patterns match), illegal characters and empty brackets gone.
    If nothing sensible is left the name is only made safe, never emptied."""
    stem, ext = os.path.splitext(name)
    t = stem
    for rx in (JUNK_TITLE, JUNK_FILE):
        if rx:
            t = rx.sub(" ", t)
    t = re.sub(r"\[\s*\]|\(\s*\)", " ", t)
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .-_")
    if not t:
        t = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", stem)).strip(" .-_") or "package"
    return t[:150] + ext


def latin_only(text):
    """True when the title has no letter from another script (Han, kana, Hangul, Cyrillic, Greek, Arabic, Thai, ...):
    only then can it stand for the English title. Digits, punctuation and Latin letters with accents are fine, so a
    French title passes and "<3 Han characters>GAME EDITION" does not."""
    return all(c in "\u00aa\u00ba" or re.search(r"\bLATIN\b", unicodedata.name(c, "")) for c in text if c.isalpha())    # ordinal indicators count as Latin


def _plain_title(text):
    """A title taken from a file or folder name: junk patterns removed, - and _ read as spaces."""
    for rx in (JUNK_TITLE, JUNK_FILE):
        if rx:
            text = rx.sub(" ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)                      # a [site tag] left over
    return re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", text)).strip(" .")


def ps4_title_from_name(member_path, tid):
    """The English title a PS4 package's file name carries next to the title ID ("<Title>-CUSA12345.v1.0[tag].pkg",
    or "CUSA12345 <Title> v1.0.pkg"); "" when the name has no ID or nothing sits next to it."""
    stem = Path(member_path).stem
    m = re.search(re.escape(tid), stem) or re.search(r"CUSA\d{5}", stem)
    if not m:
        return ""
    before, after = stem[:m.start()], stem[m.end():]
    if not before.strip(" ._-[]()"):
        after = re.sub(r"^_\d{2}", "", after)                        # "CUSA12345_00" content ID counter
        before = re.sub(r"[-_. ]v?\d+(?:\.\d+)+\b.*$", "", after)       # the title after the ID, up to its version
    return _plain_title(before)


def ps4_source_folder(cfg, source):
    """The name of the folder a release was downloaded into: the first folder below the library or downloads folder
    that holds the archive ("" when it sits directly in one)."""
    src = Path(source)
    for key in ("downloads", "library"):
        try:
            parts = src.relative_to(cfg[key]).parts
        except ValueError:
            continue
        return parts[0] if len(parts) > 1 else ""
    return src.parent.name


def ps4_pick_title(cfg, pick, game):
    """(title, where it came from, the title the package itself carries): the PS5 side names games from the English
    title, so: the package's English title (TITLE_01, then TITLE_18); else its default TITLE when it has no letter
    of another script; else the English title in the base game's file name; else the source folder's name; else TITLE."""
    default = (game or {}).get("title") or pick.get("title") or ""
    tid = pick["title_id"]
    if (game or {}).get("title_en"):
        return game["title_en"], "package (English)", default
    if default and latin_only(default):
        return default, "package", default
    from_name = ps4_title_from_name(pick.get("game_path") or (game or {}).get("path") or "", tid)
    if from_name and latin_only(from_name):
        return from_name, "file name", default
    folder = _plain_title(ps4_source_folder(cfg, pick.get("source") or ""))
    if folder and latin_only(folder):
        return folder, "folder name", default
    return default or tid, "package", default


def ps4_plan(cfg, pick):
    """The plan fields of a PS4 title: its folder under <output>/PS4 and what is copied under which name."""
    tid, members = pick["title_id"], pick.get("members") or []
    game = next((m for m in members if m["kind"] == "game"), None)
    chosen, source, in_package = ps4_pick_title(cfg, pick, game)
    title = clean_title(chosen)
    folder = f"{title} [{tid}]"
    used, files = set(), []
    for m in members:
        if m["kind"] == "nested-archive":
            files.append(dict(name=Path(m["path"]).name, size=m["size"], kind="nested archive", path=m["path"], via=None, dest=None))
            continue
        if m is game:
            name = f"{title} [{tid}]" + (f" [v{m['version']}]" if m.get("version") else "") + ".pkg"
        else:
            name = ps4_clean_name(Path(m["path"]).name)
        stem, ext, n, name0 = Path(name).stem, Path(name).suffix, 1, name
        while name.lower() in used:
            n += 1
            name = f"{stem} ({n}){ext}"
        used.add(name.lower())
        files.append(dict(name=name, size=m["size"], kind=m["kind"], path=m["path"], via=m.get("via"), version=m.get("version", ""),
                          content_id=m.get("content_id", ""), dest=name))
    return dict(ps4=True, name=folder, title=title, title_source=source, title_in_package=in_package,
                dest_dir=str(Path(cfg["output"]) / "PS4" / folder), files=files, notes=list(pick.get("notes") or []))


KIND_LABEL = {"ffpfsc": "image (.ffpfsc)", "ffpfs": "image (.ffpfs)", "exfat": "image (.exfat)", "ffpkg": "image (.ffpkg)",
              "rar": "archive (.rar)", "zip": "archive (.zip)", "7z": "archive (.7z)", "folder": "unpacked folder",
              "archive-image": "image inside an archive", "archive-pkg": ".pkg inside an archive", "archive-ps4": "PS4 packages in an archive",
              "ps4-folder": "PS4 game folder"}
ARCHIVE_KINDS = ("rar", "zip", "7z")


def kind_label(kind):
    return KIND_LABEL.get(kind, kind or "?")


def backport_decision(image_fw, original_fw, console_fw, override=None):
    """Which copy of a game with modified executables runs on the console: ("image" | "original" | None, who decided).
    The rules of resolve_modified_exec(), so the Library can show now what the build will do."""
    console = (console_fw or "").strip()
    if override == "image":
        return "image", "your option"
    if override == "original":
        return "original", "your option"
    if console and original_fw and vkey(original_fw) > vkey(console) and vkey(image_fw or "99") <= vkey(console):
        return "image", "your console's firmware"
    if console and original_fw and vkey(original_fw) <= vkey(console):
        return "original", "your console's firmware"
    return None, ("no console firmware is set (Settings)" if not console else "neither copy is known to run on the console")


def backport_state(image_fw, original_fw, console_fw, override=None):
    """(present, decision, decided_by) for an image and its original archive. A backport is present when the image asks
    for lower firmware than the original; only then is there a choice between the two. Without one the decision is None
    and says so, neutrally, unless the user made a choice of their own (it applies if the executables differ anyway)."""
    present = bool(image_fw and original_fw and vkey(image_fw) < vkey(original_fw))
    if present or override in ("image", "original"):
        decision, by = backport_decision(image_fw, original_fw, console_fw, override)
        return present, decision, by
    if not image_fw or not original_fw:
        return False, None, "the firmware of one of the copies is not known: nothing to compare"
    if vkey(image_fw) == vkey(original_fw):
        return False, None, "both copies ask for the same firmware: nothing to choose"
    return False, None, "the image does not ask for lower firmware than the original: nothing to choose"


def explain_candidates(lst, pick, ov, twin):
    """Every copy found of one game, with what became of it: [{source, kind, kind_label, version, fw, packed, size, origin,
    verdict, reason}]. verdict: chosen | verifies | older | packed | same_version | not_used (the user chose another)."""
    out = []
    newer = any(vkey(c["version"]) < vkey(pick["version"]) for c in lst if c is not pick)
    same = [c for c in lst if c is not pick and vkey(c["version"]) == vkey(pick["version"]) and not c.get("packed")]
    pref = lambda c: KIND_PREF.get(c["kind"], 9)
    for c in lst:
        if c is pick:
            if ov.get("source"):
                verdict, reason = "chosen", "chosen by you (Copy to use)"
            elif len(lst) == 1:
                verdict, reason = "chosen", "the only copy found"
            else:
                bits = []
                if newer:
                    bits.append(f"newest version (v{pick['version'] or '?'})")
                if any(pref(pick) < pref(c) for c in same):
                    bits.append(f"{kind_label(pick['kind'])} is read faster than the other copies of that version")
                if any(c.get("packed") and not pick.get("packed") for c in lst if c is not pick):
                    bits.append("the packed copies are passed over")
                if pick.get("packed"):
                    bits.append(f"every copy is {pick['packed']}-packed")
                verdict, reason = "chosen", "; ".join(bits) or "the first of equal copies"
            if twin is not None and pick["kind"] not in ARCHIVE_KINDS + ("folder",):
                reason += f"; checked against {Path(twin['source']).name}"
        elif ov.get("source"):
            verdict, reason = "not_used", "not used: you chose another copy"
        elif twin is not None and c is twin and pick["kind"] not in ARCHIVE_KINDS + ("folder",):
            verdict, reason = "verifies", "same version; the image is checked file by file against this archive before it is built"
        elif c.get("packed") and not pick.get("packed"):
            verdict, reason = "packed", f"{c['packed']}-packed: UltraPack builds from unpacked files, so this copy is passed over"
        elif vkey(c["version"]) < vkey(pick["version"]):
            verdict, reason = "older", f"older version (v{c['version'] or '?'}; the newest is v{pick['version'] or '?'})"
        elif pref(pick) < pref(c):
            verdict, reason = "same_version", f"same version; {kind_label(pick['kind'])} is read faster than {kind_label(c['kind'])}"
        else:
            verdict, reason = "same_version", "same version; the first one found is used"
        out.append(dict(source=c["source"], kind=c["kind"], kind_label=kind_label(c["kind"]), version=c["version"], fw=c.get("fw", ""),
                        packed=c.get("packed", ""), size=c.get("size", 0), origin=c.get("origin", ""), verdict=verdict, reason=reason))
    return out


def backport_info(cfg, pick, twin, ov):
    """The backport question for a game with an image and an original archive of one version: None without such a
    pair, else {present, confirmed, image_fw, original_fw, console_fw, decision, decided_by}. `present` is judged
    from the firmware the two copies ask for (known from the scan); whether the executables really differ is only
    found when the build starts (`confirmed` stays false until then, and the outcome is kept with the build)."""
    if twin is None or pick["kind"] in ARCHIVE_KINDS + ("folder", "archive-ps4", "ps4-folder"):
        return None
    image_fw, original_fw, console = pick.get("fw", ""), twin.get("fw", ""), (cfg.get("console_fw") or "")
    present, decision, by = backport_state(image_fw, original_fw, console, ov.get("modified_exec"))
    return dict(present=present, confirmed=False, image_fw=image_fw, original_fw=original_fw, console_fw=console, decision=decision, decided_by=by)


# ------------------------------------------------------------------ titles learned from extracted games

_titles_lock = threading.Lock()


def load_titles():
    """{title_id: title} of games whose real title was only known after extraction (state/titles.json)."""
    try:
        data = json.loads((STATE / "titles.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and v} if isinstance(data, dict) else {}


def remember_title(tid, title):
    """Keep the title a game turned out to have, so the Library can show it before the next extraction."""
    if not title:
        return
    with _titles_lock:
        titles = load_titles()
        if titles.get(tid) != title:
            titles[tid] = title
            try:
                write_json_atomic(STATE / "titles.json", titles)
            except OSError:
                pass


WRAPPED_KINDS = ("archive-image", "archive-pkg")     # no param.json is read while scanning these: their "title" is a folder name


def best_title(tid, lst, pick, ledger, titles):
    """The title to show for a game. A copy wrapped in an archive has only its folder's name; a loose copy of the
    same game, a title learned when it was last extracted, or the name of what was built from it say better."""
    if pick["kind"] not in WRAPPED_KINDS:
        return pick["title"]
    other = next((c["title"] for c in lst if c["kind"] not in WRAPPED_KINDS and c.get("title")), None)
    if other:
        return other
    if (titles or {}).get(tid):
        return titles[tid]
    out = ((ledger or {}).get(tid) or {}).get("output")
    m = re.match(r"(.+?) \[(?:PPSA|CUSA)\d{5}\]", Path(str(out)).name) if out else None
    return m.group(1) if m else pick["title"]


def unrecognised(cands):
    """The sources a scan could not use, with why: [{source, origin, error}]. They are not in the plan (there is
    no game to plan), so without this list "why is my game not shown" has no answer."""
    out = []
    for c in cands:
        if c.get("error") or not c.get("title_id"):
            out.append(dict(source=c.get("source", ""), origin=c.get("origin", ""), error=str(c.get("error") or "no title ID found")[:300]))
    return sorted(out, key=lambda x: x["source"])


def choose(cfg, cands, ledger, failures=None, titles=None):
    """The plan: one entry per title. `failures` (state/failures.json) makes a title that failed, with nothing
    changed since, action "failed" instead of "build": preparing it again would copy the same hundreds of GiB
    to fail the same way. `titles` (state/titles.json) names the games whose scan only found a folder name."""
    failures = failures or {}
    by = {}
    for c in cands:
        if c.get("error") or not c.get("title_id"):
            continue
        by.setdefault(c["title_id"], []).append(c)
    plan = []
    for tid, lst in sorted(by.items(), key=lambda kv: clean_title(kv[1][0].get("title", "")).lower()):
        ov = cfg["overrides"].get(tid, {})
        if ov.get("source"):
            pick = next((c for c in lst if c["source"] == ov["source"]), None)
            if pick is None:
                plan.append(dict(title_id=tid, action="error", why=f"override source not found: {ov['source']}"))
                continue
        else:
            usable = [c for c in lst if not c.get("packed")] or lst
            pick = sorted(usable, key=lambda c: (tuple(-x for x in vkey(c["version"])), KIND_PREF.get(c["kind"], 9)))[0]
        done = ledger.get(tid)
        pick = dict(pick, title=best_title(tid, lst, pick, ledger, titles))
        lst = [pick if c["source"] == pick["source"] else c for c in lst]
        shown = pick                                                         # what the package will say: the patch's param.json wins
        if not pick.get("ps4") and ov.get("patch"):
            pi = patched_identity(ov["patch"])
            if pi and pi["title_id"] == tid:
                shown = dict(pick, title=pi["title"] or pick["title"], version=pi["version"], fw=pi["fw"])
        name = pkg_name(shown)
        ps4 = bool(pick.get("ps4"))
        if ov.get("skip"):                                                  # an override always wins
            action, why = "skip", "skipped by override"
        elif ps4 and pick["kind"] == "ps4-folder":
            action, why = "skip", "PS4 folder: not handled (only PS4 package sets are copied)"
        elif ps4 and cfg.get("ps4_mode") == "skip":
            action, why = "skip", "PS4: skipped (ps4_mode is skip)"
        elif tid.startswith("CUSA") and not ps4:
            action, why = "skip", "PS4 title that is not a set of packages: not handled"
        elif pick.get("packed"):
            action, why = "skip", (f"only {pick['packed']}-packed copies: UltraPack builds from unpacked files, so this game is skipped "
                                   f"until an unpacked copy is found")
        elif done and vkey(done.get("source_version") or done.get("version")) >= vkey(pick["version"]) and done.get("output") and nas_exists(done["output"]):
            action, why = "done", f"built {done.get('built', '')}: {Path(done['output']).name}"
        elif ps4:
            action, why = "copy", "PS4: copy packages as they are" + (" (newer version)" if done else "")
        else:
            action, why = "build", "newer version" if done else "new"
        twin = next((c for c in lst if c["kind"] in ("rar", "zip", "7z") and c["version"] == pick["version"]
                     and not c.get("packed") and c["source"] != pick["source"]), None)
        plan.append(dict(title_id=tid, title=clean_title(shown["title"]), version=shown["version"], fw=shown.get("fw", ""),
                         source=pick["source"], kind=pick["kind"], size=pick.get("size", 0),
                         **(dict(patched=True, source_version=pick["version"], source_fw=pick.get("fw", ""), source_name=pkg_name(pick))
                            if shown is not pick else {}),
                         verify_archive=twin["source"] if twin and pick["kind"] not in ("rar", "zip", "7z", "folder") else None,
                         verify_root=twin.get("archive_root") if twin else None,
                         archive_root=pick.get("archive_root"), inner=pick.get("inner"), patch=ov.get("patch"),
                         name=name if shown["version"] else f"{clean_title(shown['title'])} [{tid}] (version read after extraction)",
                         action=action, why=why,
                         candidates=explain_candidates(lst, pick, ov, twin),
                         backport=backport_info(cfg, pick, twin if twin and pick["kind"] not in ("rar", "zip", "7z", "folder") else None, ov),
                         **(dict(help="packed") if pick.get("packed") and action == "skip" else {}),
                         skipped_duplicates=[f"{c['source']} (v{c['version']}{', ' + c['packed'] if c.get('packed') else ''})"
                                             for c in lst if c["source"] != pick["source"]]))
        entry = plan[-1]
        if ps4 and pick["kind"] == "archive-ps4":
            entry.update(ps4_plan(cfg, pick))
        entry["fingerprint"] = ps5_failures.fingerprint(cfg, entry)
        rec = failures.get(tid)
        if action in ("build", "copy") and ps5_failures.blocks(cfg, entry, rec):
            entry["action"] = "failed"
            entry["why"] = (f"failed on {rec.get('at_text', '?')}, unchanged: {ps5_failures.short_reason(rec)} "
                            f"It is retried when its source or options change, the app is updated, or you press Retry.")
    return plan


# ------------------------------------------------------------------ job steps

def guarded_rmtree(p: Path, base: Path):
    p, base = Path(p).resolve(), Path(base).resolve()
    if p == base or base not in p.parents:
        raise RuntimeError(f"refusing to delete {p}: not inside {base}")
    shutil.rmtree(p, ignore_errors=True)


EXEC_RE = re.compile(r"(^|/)(eboot\.bin|[^/]+\.(prx|sprx|self|elf))$|^fakelib/", re.I)


def image_crc(uw, e):
    crc, off = 0, 0
    while off < e.size:
        chunk = uw.fs.read(e, off, min(16 << 20, e.size - off))
        if not chunk:
            raise RuntimeError("short read")
        crc = zlib.crc32(chunk, crc)
        off += len(chunk)
    return crc


def archive_param(cfg, archive, root):
    r = seven(cfg, ["e", "-so", "-p-", archive, (root or "") + "sce_sys/param.json"], timeout=900, stdin=subprocess.DEVNULL)
    return parse_param(r.stdout)


FW_KEYS = {"requiredSystemSoftwareVersion", "sdkVersion"}   # what a backport lowers in param.json


def json_diff(a, b, path=""):
    out = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k), b.get(k)
        if isinstance(x, dict) and isinstance(y, dict):
            out += json_diff(x, y, path + k + ".")
        elif x != y:
            out.append(path + k)
    return out


def backport_param(cfg, job, uw, files, want, log):
    """True when the image's param.json differs from the original only in firmware fields (a backport)."""
    e = files.get("sce_sys/param.json")
    w = want.get("sce_sys/param.json")
    if e is None or w is None or image_crc(uw, e) == w:
        return False
    try:
        img = json.loads(uw.fs.read(e, 0, e.size).decode("utf-8-sig"))
        r = seven(cfg, ["e", "-so", "-p-", job["verify_archive"], (job.get("verify_root") or "") + "sce_sys/param.json"],
                  timeout=900, stdin=subprocess.DEVNULL)
        keys = json_diff(img, json.loads(r.stdout.decode("utf-8-sig")))
    except Exception as ex:
        log(f"  could not compare param.json: {ex}")
        return False
    if keys and set(keys) <= FW_KEYS:
        return True
    log(f"  param.json differs beyond firmware fields: {', '.join(keys[:8])}")
    return False


def resolve_modified_exec(cfg, job, want, log):
    """Image vs RAR twin: if only executables differ (backport / patched eboot), keep the copy that runs on
    cfg['console_fw']: the original when it runs there, else the image. Returns the CRCs still to enforce."""
    uw, base = A.open_game_fs(job["source"])
    try:
        root = uw.root or ""
        files = {k[len(root):]: e for k, e in uw.fs.files.items() if k.startswith(root)}
        diff = [rel for rel, e in files.items() if EXEC_RE.search(rel) and rel.lower() in want
                and image_crc(uw, e) != want[rel.lower()]]
        extra = [rel for rel in files if rel.lower().startswith("fakelib/") and rel.lower() not in want]
        if backport_param(cfg, job, uw, {k.lower(): e for k, e in files.items()}, want, log):
            diff.append("sce_sys/param.json (firmware fields only)")
    finally:
        if base:
            base.close()
    if not diff and not extra:
        job["backport_outcome"] = dict(differs=False, used="image", image_fw=job.get("fw"))
        return want
    ov = cfg["overrides"].get(job["title_id"], {})
    a = archive_param(cfg, job["verify_archive"], job.get("verify_root"))
    console = cfg.get("console_fw", "")
    outcome = dict(differs=True, files=len(diff) + len(extra), examples=(diff + extra)[:4], image_fw=job.get("fw"), original_fw=a["fw"], console_fw=console)
    log(f"  image differs from the original in {len(diff)} file(s){f' + {len(extra)} fakelib file(s)' if extra else ''}: "
        f"{', '.join((diff + extra)[:6])}{' ...' if len(diff) + len(extra) > 6 else ''}; image fw {job.get('fw')}, "
        f"original fw {a['fw']}, console fw {console or '?'}")
    want_exec = ov.get("modified_exec")                  # your option comes first (the Library shows the decision by backport_decision(), the same rules)
    if want_exec == "image" or (want_exec != "original" and console and a["fw"] and vkey(a["fw"]) > vkey(console)
                                and vkey(job.get("fw") or "99") <= vkey(console)):
        log("  keeping the image's modified executables (original needs newer firmware than the console)")
        job["backport_outcome"] = dict(outcome, used="image", decided_by="your option" if ov.get("modified_exec") == "image" else "your console's firmware")
        return {k: v for k, v in want.items() if not EXEC_RE.search(k) and not
                (k == "sce_sys/param.json" and any(d.startswith("sce_sys/param.json") for d in diff))}
    if want_exec == "original" or (console and a["fw"] and vkey(a["fw"]) <= vkey(console)):
        log(f"  switching to the original archive (runs on fw {console})")
        job["backport_outcome"] = dict(outcome, used="original", decided_by="your option" if ov.get("modified_exec") == "original" else "your console's firmware")
        job.update(kind="rar", source=job["verify_archive"], archive_root=job.get("verify_root"), verify_archive=None,
                   fw=a["fw"], name=pkg_name(dict(job, fw=a["fw"])))
        return None
    raise ps5_failures.GameError(
        "modified_exec_unknown", "image has modified executables and neither copy is known to run on the console "
        "(set console_fw or overrides.<id>.modified_exec)", files=(diff + extra)[:10], image_fw=job.get("fw"), original_fw=a["fw"])


def extract_image_files(image, app: Path, want: dict, log, against=""):
    """Copy every file out of an image with the read-only readers; CRC32-check against `want` when given."""
    checked = mism = 0
    differing = []
    uw, base = A.open_game_fs(image)
    try:
        root = uw.root or ""
        for k, e in uw.fs.files.items():
            if not k.startswith(root):
                continue
            rel = k[len(root):]
            out = app / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            crc = 0
            with open(out, "wb") as fh:
                off = 0
                while off < e.size:
                    chunk = uw.fs.read(e, off, min(16 << 20, e.size - off))
                    if not chunk:
                        raise RuntimeError(f"short read in {k}")
                    fh.write(chunk)
                    crc = zlib.crc32(chunk, crc)
                    off += len(chunk)
            w = want.get(rel.lower())
            if w is not None and rel.lower() != "ampr_emu.index":
                checked += 1
                if w != crc:
                    mism += 1
                    differing.append(rel)
                    log(f"  CRC MISMATCH {rel}: image {crc:08X} vs archive {w:08X}")
    finally:
        if base:
            base.close()
    if want:
        log(f"  CRC32 check vs {against}: {checked} files matched, {mism} mismatched")
        if mism:
            raise ps5_failures.GameError("crc_mismatch", f"{mism} extracted file(s) differ from the archive CRCs",
                                         files=differing[:50], count=mism, checked=checked, against=str(against))
        if checked == 0:
            raise ps5_failures.GameError("crc_no_common_files", "CRC check found no common files", against=str(against))


def materialise(cfg, job, jobdir: Path, log) -> Path:
    """Put the game's /app0 on the SSD and return its folder."""
    dest = jobdir / "src"
    dest.mkdir(parents=True)
    t0 = time.time()
    want = None
    skip_verify = (cfg["overrides"].get(job["title_id"]) or {}).get("skip_verify")     # "keep this copy anyway"
    if job.get("verify_archive") and skip_verify:
        log("  not checking against the original archive (override skip_verify)")
    elif job["kind"] not in ("rar", "zip", "7z", "folder", "archive-pkg", "archive-image") and job.get("verify_archive"):
        crcs = {}
        archive_listing(cfg, job["verify_archive"], crcs, timeout=4 * 3600)
        r = job.get("verify_root") or ""
        want = {p[len(r):].lower(): c for p, c in crcs.items() if p.startswith(r)}
        want = resolve_modified_exec(cfg, job, want, log)     # may switch the job to the archive
    if job["kind"] in ("rar", "zip", "7z"):
        r = seven(cfg, ["x", "-y", "-bso0", "-bsp0", f"-o{dest}", job["source"]])
        if r.returncode != 0:
            raise ps5_failures.GameError("extract_failed", f"7zz extract failed ({r.returncode}): {r.stderr.decode(errors='replace')[-400:]}",
                                         rc=r.returncode, detail=r.stderr.decode(errors="replace")[-300:])
        app = dest / job["archive_root"] if job.get("archive_root") else dest
    elif job["kind"] == "archive-pkg":
        r = seven(cfg, ["x", "-y", "-bso0", "-bsp0", f"-o{jobdir / 'pkg'}", job["source"], job["inner"]])
        if r.returncode != 0:
            raise ps5_failures.GameError("extract_failed", f"7zz extract of {job['inner']} failed ({r.returncode})", rc=r.returncode)
        pkg = next((jobdir / "pkg").rglob("*.pkg"))
        r = subprocess.run([cfg["python"], "-B", "backend/cli.py", "placeholder", str(dest / "pkgx"), "--fpkg-extract", str(pkg)],
                           cwd=cfg["ultrapack_src"], capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        if r.returncode != 0:
            raise ps5_failures.GameError("extract_failed", f"--fpkg-extract failed: {(r.stdout + r.stderr).decode(errors='replace')[-400:]}",
                                         rc=r.returncode, detail=(r.stdout + r.stderr).decode(errors="replace")[-300:])
        shutil.rmtree(jobdir / "pkg", ignore_errors=True)
        pjs = sorted((dest / "pkgx").rglob("param.json"), key=lambda q: len(q.parts))
        pjs = [q for q in pjs if q.parent.name.lower() == "sce_sys"]
        if not pjs:
            raise ps5_failures.GameError("no_param_json", "extracted .pkg has no sce_sys/param.json")
        app = pjs[0].parent.parent
    elif job["kind"] == "archive-image":
        nas_tmp = Path(cfg["output"]) / ".staging" / f"{job['title_id']}-image"
        if nas_tmp.exists():
            guarded_rmtree(nas_tmp, Path(cfg["output"]))
        nas_tmp.mkdir(parents=True)
        try:
            log(f"  extracting wrapped image {job['inner']} to {nas_tmp} (NAS scratch; 7-Zip verifies its CRC)")
            r = seven(cfg, ["x", "-y", "-bso0", "-bsp0", f"-o{nas_tmp}", job["source"], job["inner"]])
            if r.returncode != 0:
                raise ps5_failures.GameError("extract_failed", f"7zz extract of {job['inner']} failed ({r.returncode})", rc=r.returncode)
            img = next(q for q in nas_tmp.rglob("*") if q.is_file() and q.suffix.lower() in (".exfat", ".ffpfsc", ".ffpfs", ".ffpkg"))
            app = dest / "app"
            extract_image_files(str(img), app, {}, log)
        finally:
            guarded_rmtree(nas_tmp, Path(cfg["output"]))
    elif job["kind"] == "folder":
        app = dest / "app"
        shutil.copytree(job["source"], app, ignore=shutil.ignore_patterns("._*", ".DS_Store"))
    else:
        app = dest / "app"
        want = want or {}
        extract_image_files(job["source"], app, want, log, Path(job["verify_archive"]).name if want else "")
    if not (app / "sce_sys" / "param.json").exists():
        raise ps5_failures.GameError("no_param_json", f"no sce_sys/param.json under {app}")
    got = sum(f.stat().st_size for f in app.rglob("*") if f.is_file())
    log(f"  materialised {got / 2**30:.1f} GiB on SSD in {(time.time() - t0) / 60:.1f} min "
        f"({got / 2**20 / max(time.time() - t0, 1):.0f} MiB/s)")
    return app


def clean(app: Path, patch: str | None, log):
    removed = []
    for f in sorted(app.rglob("*")):
        if f.is_file() and JUNK_FILE and JUNK_FILE.search(f.name):
            removed.append(str(f.relative_to(app)))
            f.unlink()
    pj = app / "sce_sys" / "param.json"
    raw = pj.read_bytes()
    j = json.loads(raw.decode("utf-8-sig"))
    fixed = 0
    for lang, d in (j.get("localizedParameters") or {}).items():
        if JUNK_TITLE and isinstance(d, dict) and isinstance(d.get("titleName"), str):
            new = re.sub(r"\s+", " ", JUNK_TITLE.sub(" ", d["titleName"])).strip(" -_")
            if new != d["titleName"]:
                d["titleName"] = new
                fixed += 1
    if fixed:
        pj.write_text(json.dumps(j, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    patched = []
    if patch:
        for f in sorted(Path(patch).rglob("*")):
            if f.is_file() and not f.name.startswith("._") and f.name != ".DS_Store":
                rel = f.relative_to(patch)
                (app / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, app / rel)
                patched.append(str(rel))
    log(f"  cleaned: {len(removed)} junk file(s) removed, {fixed} title(s) fixed"
        + (f", patch overlaid: {', '.join(patched)}" if patched else ""))
    return dict(removed=removed, titles_fixed=fixed, patched=patched)


def host_mapping(cfg):
    """[(container prefix, host prefix)] for the folders whose host location is configured (see ps5_hostpaths);
    empty when none is, or when it can't be worked out (a notification must never fail over this)."""
    try:
        return ps5_hostpaths.folders_for(cfg, state=STATE, logs=LOGS, environ=os.environ)["mapping"]
    except Exception:
        return []


def notification_text(cfg, name, status, why=None, output=None):
    """What a push notification says: the package, the reason when there is one, and for a built package the
    folder it is in, on the host when that is known (never a container path nobody could find)."""
    mapping = host_mapping(cfg)
    text = ps5_hostpaths.display_text(f"{name}" + (f": {why}" if why else ""), mapping)
    if status in ("built", "copied") and output:
        folder = ps5_hostpaths.display_path(str(Path(output).parent), mapping)
        if folder:
            text += f"\nSaved in {folder}"
    return text


def notify(cfg, title, msg):
    url = (cfg.get("notify_url") or "").strip()
    if not url or not NOTIFY_URL_RE.match(url):
        return
    try:
        import urllib.request
        req = urllib.request.Request(url, data=msg.encode("utf-8"), method="POST",
                                     headers={"Title": title.encode("ascii", "replace").decode()})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


RESULT_KEYS = ("title_id", "name", "title", "status", "why", "validate", "prep_min", "build_min", "gib", "code", "phase", "data", "size", "at", "backport")


def build_progress(logf):
    """Stage and percentage of a running UltraPack build, from the tail of its log."""
    tr = PG.LogTracker(time.time(), exact=False)
    if not tr.read(logf, time.time()):
        return {}
    return dict(stage=tr.stage, stage_text=tr.stage_text, pct=tr.pct, part=tr.part)


def stop_requested():
    return (STATE / "stop.request").exists()


def jobdir_of(cfg, job):
    return Path(cfg["temp"]) / job["title_id"]


def usable_ssd(cfg):
    Path(cfg["temp"]).mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(cfg["temp"]).free - (cfg["ssd_reserve_gb"] << 30)


def name_after_patch(job, app, cleaned, log):
    """A patch that replaces sce_sys/param.json can change the firmware, version or title the package carries (the
    usual way to lower requiredSystemSoftwareVersion for an older console). The package is named from what the
    work copy says after the patch, because the [fwNN.NN] tag is what people read to know whether it runs on their
    console. Returns {key: (before, after)} of what changed in the name's parts; {} when nothing did."""
    if PATCHED_PARAM not in [str(x).replace("\\", "/").lower() for x in (cleaned or {}).get("patched") or []]:
        return {}
    ident = parse_param((app / "sce_sys" / "param.json").read_bytes())
    if ident["title_id"] != job["title_id"]:
        raise ps5_failures.GameError("title_mismatch", f"after the patch the game is {ident['title_id']}, expected {job['title_id']}",
                                     found=ident["title_id"], expected=job["title_id"], patch=True)
    if not ident["version"]:                                           # a param.json that says no version: the name is kept
        log(f"  the patch's sce_sys/param.json has no contentVersion: the name stays {job.get('name')}")
        return {}
    ident = dict(ident, title=ident["title"] or job.get("title") or "")
    new = dict(version=ident["version"], fw=ident["fw"], title=clean_title(ident["title"]), name=pkg_name(ident))
    changed = {k: (job.get(k), new[k]) for k in ("fw", "version", "title") if job.get(k) != new[k]}
    if new["name"] == job.get("name") and not changed:
        return {}
    old_name = job.get("name")
    job.setdefault("source_version", job.get("version"))             # what the source said: a newer source is what makes it a rebuild
    job.update(new)
    if new["name"] != old_name:
        log(f"  name after patch: {new['name']}" + (f" ({', '.join(f'{k} {a} -> {b}' for k, (a, b) in changed.items())})" if changed else ""))
    return changed


def prepare(cfg, job, log, headroom=0):
    """Extract + clean the game on the SSD. headroom = bytes the running build may still grow."""
    log = job_log(log, job["title_id"])
    _JOB_LINES[job["title_id"]] = []
    temp_root = Path(cfg["temp"])
    temp_root.mkdir(parents=True, exist_ok=True)
    jd = jobdir_of(cfg, job)
    marker = jd / ".prepared.json"
    if marker.exists():
        try:
            m = json.loads(marker.read_text())
            if m.get("source") == job["source"] and (m["app"] and (Path(m["app"]) / "sce_sys" / "param.json").exists()):
                log(f"PREPARE {job['name']}: reusing verified copy on SSD from {m.get('at')}")
                if not job.get("version") and m.get("job"):
                    job.update({k: m["job"][k] for k in ("version", "fw", "title", "name", "source_version") if k in m["job"] and m["job"][k] is not None})
                name_after_patch(job, Path(m["app"]), m.get("clean"), log)         # a copy prepared before this was done: its marker may have the old name
                return dict(app=Path(m["app"]), clean=m.get("clean"), prep_min=m.get("prep_min"))
        except Exception as ex:
            log(f"  ignoring unusable prepare marker: {ex}")
    if jd.exists():
        guarded_rmtree(jd, temp_root)
    need = int((job["size"] or 0) * 1.10)
    usable = usable_ssd(cfg) - headroom
    if need > usable:
        raise ps5_failures.GameError("no_space", f"needs ~{need / 2**30:.0f} GiB on the SSD, {usable / 2**30:.0f} GiB usable",
                                     need=need, usable=usable, reserve=int(cfg["ssd_reserve_gb"]) << 30)
    jd.mkdir(parents=True)
    t0 = time.time()
    log(f"PREPARE {job['name']}  <- {job['source']}")
    app = materialise(cfg, job, jd, log)
    ident = parse_param((app / "sce_sys" / "param.json").read_bytes())
    if ident["title_id"] != job["title_id"]:
        raise ps5_failures.GameError("title_mismatch", f"extracted game is {ident['title_id']}, expected {job['title_id']}",
                                     found=ident["title_id"], expected=job["title_id"])
    if not job.get("version"):
        job.update(version=ident["version"], fw=ident["fw"], title=clean_title(ident["title"]), name=pkg_name(ident))
        remember_title(job["title_id"], job["title"])
        log(f"  identified after extraction: {job['name']}")
    res = dict(app=app, clean=clean(app, job.get("patch"), log), prep_min=round((time.time() - t0) / 60, 1))
    name_after_patch(job, app, res["clean"], log)
    marker.write_text(json.dumps(dict(res, app=str(app), source=job["source"], at=time.strftime("%Y-%m-%d %H:%M"),
                                      job={k: job.get(k) for k in ("version", "fw", "title", "name", "source_version")}),
                                 ensure_ascii=False, default=str))
    return res


def start_build(cfg, job, prep, log):
    out = Path(cfg["output"])
    jd = jobdir_of(cfg, job)
    # build_on_ssd: UltraPack writes the package to the SSD (its outer-PFS writes are small and slow over
    # SMB); finish_build validates it there, then copies it to the NAS in large writes and verifies the copy
    stage = jd / "out" if cfg.get("build_on_ssd") else out / ".staging" / job["title_id"]
    stage.mkdir(parents=True, exist_ok=True)
    image_in_temp = usable_ssd(cfg) > int(job["size"] * 0.25)
    (jd / "t").mkdir(exist_ok=True)
    cmd = (["caffeinate", "-i"] if sys.platform == "darwin" else []) + [
           "nice", "-n", str(cfg["nice"]), cfg["python"], "-B", "backend/cli.py",
           str(prep["app"]), str(stage), "--to", "pkg", "--stage-in-place", "--temp-dir", str(jd / "t"),
           "--cpu-count", str(cfg["cpu_count"]), "--compression-level", str(cfg["compression_level"]),
           "--if-exists", "overwrite"] + (["--fpkg-image-in-temp"] if image_in_temp else [])
    logf = LOGS / "pkg" / f"{time.strftime('%Y%m%d-%H%M%S')}_{job['title_id']}.log"
    logf.parent.mkdir(parents=True, exist_ok=True)
    log(f"BUILD {job['name']} (image_in_temp={image_in_temp}, level {cfg['compression_level']}); log {logf.name}")
    proc = subprocess.Popen(cmd, cwd=cfg["ultrapack_src"], stdout=open(logf, "wb"), stderr=subprocess.STDOUT,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), start_new_session=True)
    t0 = time.time()
    (jd / ".building").write_text(json.dumps(dict(pid=proc.pid, logf=str(logf), stage=str(stage), t0=t0,
                                                  prep=dict(prep, app=str(prep["app"])), job=job),
                                             ensure_ascii=False, default=str))   # a started build consumes its source
    (jd / ".prepared.json").unlink(missing_ok=True)
    return dict(job=job, prep=prep, proc=proc, logf=logf, stage=stage, t0=t0, tracker=PG.LogTracker(t0))


class PidProc:
    """Popen-like handle for a build started by an earlier scheduler (own session, not our child)."""
    def __init__(self, pid, logf):
        self.pid, self.logf, self.returncode = pid, Path(logf), None

    def poll(self):
        if self.returncode is None:
            try:
                os.kill(self.pid, 0)
                return None
            except ProcessLookupError:
                pass
            except PermissionError:
                return None
            with open(self.logf, "rb") as fh:
                fh.seek(max(0, os.path.getsize(self.logf) - (4 << 20)))
                self.returncode = 0 if b"[SUCCESS] fPKG built." in fh.read() else 1
        return self.returncode

    def wait(self):
        while self.poll() is None:
            time.sleep(10)
        return self.returncode


def adopt_build(cfg, job, log):
    """Re-attach to a build that survived a scheduler restart; None if there is none."""
    jd = jobdir_of(cfg, job)
    bf = jd / ".building"
    if not bf.exists():
        return None
    raw = bf.read_text().strip()
    info = json.loads(raw) if raw.startswith("{") else dict(pid=int(raw))
    pid = int(info["pid"])
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    logs = sorted((LOGS / "pkg").glob(f"*_{job['title_id']}.log"))
    logf = Path(info.get("logf") or (logs[-1] if logs else ""))
    if str(jd) not in cmd and not (logf.exists() and b"[SUCCESS] fPKG built." in logf.read_bytes()[-(4 << 20):]):
        log(f"  {job['title_id']}: stale build marker (pid {pid} gone, no finished package); starting over")
        guarded_rmtree(jd, Path(cfg["temp"]))
        return None
    prep = info.get("prep") or {}
    if prep.get("app"):
        prep["app"] = Path(prep["app"])
    job.update({k: v for k, v in (info.get("job") or {}).items() if k in ("version", "fw", "title", "name", "source", "source_version") and v is not None})
    log(f"ADOPT running build of {job['name']} (pid {pid}); log {logf.name}")
    t0 = float(info.get("t0") or bf.stat().st_mtime)
    return dict(job=job, prep=prep, proc=PidProc(pid, logf), logf=logf,
                stage=Path(info.get("stage") or Path(cfg["output"]) / ".staging" / job["title_id"]),
                t0=t0, tracker=PG.LogTracker(t0, exact=False))


def past_compression(b) -> bool:
    """True once UltraPack has left stage 1 (Kraken) - from then on it is hashing/writing (I/O-bound)."""
    if b["proc"].poll() is not None:
        return True
    try:
        with open(b["logf"], "rb") as fh:
            fh.seek(max(0, os.path.getsize(b["logf"]) - (1 << 20)))
            tail = fh.read()
            return b"[stage 3/5]" in tail or b"Inner image complete" in tail
    except OSError:
        return False


F_NOCACHE = 48     # macOS fcntl: bypass the unified buffer cache (read-back must hit the disk, not RAM)


def _nocache(fd):
    if sys.platform == "darwin":
        import fcntl
        fcntl.fcntl(fd, F_NOCACHE, 1)


def _drop_cache(fd):
    if hasattr(os, "posix_fadvise"):
        os.fsync(fd)
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)


def copy_verified(src: Path, dst: Path, log, progress=None):
    """Copy src -> dst (dst + '.partial' first) in 64 MiB writes; SHA-256 of the source while writing, then a
    cache-bypassing read-back of the NAS copy must match before the rename. `progress("copying" | "verifying",
    bytes done, total)` is told after every chunk."""
    part = dst.parent / ".staging" / (dst.name + ".partial")
    part.parent.mkdir(exist_ok=True)
    t0 = time.time()
    h = hashlib.sha256()
    total, wrote = src.stat().st_size, 0
    with open(src, "rb") as fi, open(part, "wb") as fo:
        _nocache(fi.fileno())
        _nocache(fo.fileno())
        while True:
            chunk = fi.read(64 << 20)
            if not chunk:
                break
            fo.write(chunk)
            h.update(chunk)
            wrote += len(chunk)
            if progress:
                progress("copying", wrote, total)
        fo.flush()
        os.fsync(fo.fileno())
        _drop_cache(fo.fileno())
    t1 = time.time()
    h2 = hashlib.sha256()
    with open(part, "rb") as fr:
        _nocache(fr.fileno())
        _drop_cache(fr.fileno())
        read = 0
        while True:
            chunk = fr.read(64 << 20)
            if not chunk:
                break
            h2.update(chunk)
            read += len(chunk)
            if progress:
                progress("verifying", read, total)
    size = src.stat().st_size
    if h.digest() != h2.digest() or part.stat().st_size != size:
        part.unlink(missing_ok=True)
        raise ps5_failures.GameError("copy_failed", f"NAS copy of {src.name} failed verification", phase="copy")
    part.rename(dst)
    t2 = time.time()
    log(f"  copied to NAS: {size / 2**30:.1f} GiB, write {size / 2**20 / max(t1 - t0, 1):.0f} MiB/s, "
        f"verify {size / 2**20 / max(t2 - t1, 1):.0f} MiB/s, sha256 {h.hexdigest()[:16]}")
    return h.hexdigest()


def dir_size(path):
    try:
        return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file())
    except OSError:
        return 0


def ultrapack_failure_data(logf, rc, packages):
    """What to tell someone whose build stopped: the stage it was in and the error lines of its log."""
    try:
        with open(logf, "rb") as fh:
            fh.seek(max(0, os.path.getsize(logf) - 32768))
            raw = fh.read().decode("utf-8", "replace").replace("\r", "\n")
    except OSError:
        raw = ""
    lines = [ln.strip()[:300] for ln in raw.split("\n") if ln.strip()]
    prog = build_progress(logf)
    return dict(rc=rc, packages=packages, stage=prog.get("stage"), stage_text=prog.get("stage_text"),
                errors=[ln for ln in lines if re.search(r"\[ERROR\]|Traceback|Error:|No module named", ln)][-5:], tail=lines[-12:])


def finish_build(cfg, b, log, ledger_p, ledger, ledger_lock):
    job, prep, stage, logf = b["job"], b["prep"], b["stage"], b["logf"]
    log = job_log(log, job["title_id"])
    out = Path(cfg["output"])
    temp_root = Path(cfg["temp"])
    jd = jobdir_of(cfg, job)
    res = dict(title=job["title"], title_id=job["title_id"], name=job["name"], source=job["source"], size=job.get("size"),
               status="failed", clean=prep.get("clean"), prep_min=prep.get("prep_min"), log=str(logf))
    tid, tr, validate_secs, copy_secs = job["title_id"], b.get("tracker"), 0.0, 0.0
    try:
        rc = b["proc"].wait()
        res.update(rc=rc, build_min=round((time.time() - b["t0"]) / 60, 1))
        if tr is not None:
            tr.read(logf, time.time())                      # the last lines: the stages that ended just now
        pkgs = sorted(stage.glob("*.pkg"), key=lambda p: -p.stat().st_size)
        if rc != 0 or not pkgs:
            res["why"] = f"UltraPack exit {rc}, {len(pkgs)} package(s)"
            res.update(phase="build", code="ultrapack_failed", data=ultrapack_failure_data(logf, rc, len(pkgs)))
            return res
        FINISH[tid] = dict(phase="validating", frac=0.0)
        t_val = time.time()
        v = subprocess.run([cfg["python"], "-B", "backend/cli.py", "--fpkg-validate", str(pkgs[0])],
                           cwd=cfg["ultrapack_src"], capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        validate_secs = time.time() - t_val
        vtxt = (v.stdout + v.stderr).decode("utf-8", "replace")
        with open(logf, "a") as fh:
            fh.write("\n==== --fpkg-validate ====\n" + vtxt)
        summ = next((ln for ln in vtxt.splitlines() if ln.startswith("summary:")), "")
        res["validate"] = summ
        if v.returncode != 0:
            res["why"] = f"fpkg-validate failed ({summ}); package left in {stage}"
            m = re.search(r"(\d+)\s+fail", summ)
            res.update(phase="validate", code="validate_failed",
                       data=dict(summary=summ, failed_checks=int(m.group(1)) if m else None, stage=str(stage)))
            return res
        final = out / job["name"]
        if final.exists():
            final.rename(final.with_name(final.stem + f" (replaced {time.strftime('%Y%m%d%H%M')}).pkg"))
        if Path(cfg["temp"]).resolve() in pkgs[0].resolve().parents:
            marks = {}

            def copy_progress(phase, done, total):
                now_ = time.time()
                t0_ = marks.setdefault(phase, now_)
                FINISH[tid] = dict(phase=phase, done=done, total=total, rate=(done / (now_ - t0_)) if now_ - t0_ > 2 else None,
                                   frac=(0.5 if phase == "verifying" else 0.0) + 0.5 * (done / total if total else 1.0))
            t_cp = time.time()
            res["sha256"] = copy_verified(pkgs[0], final, log, progress=copy_progress)
            copy_secs = time.time() - t_cp
        else:
            pkgs[0].rename(final)
        res.update(status="built", output=str(final), gib=round(final.stat().st_size / 2**30, 2), build_secs=round(time.time() - b["t0"], 1),
                   backport=job.get("backport_outcome"))
        secs = tr.stage_secs() if tr is not None else {}
        if all(str(n) in secs for n in range(1, 6)):         # a build seen from its first line: its stage times teach the next estimate
            secs.update(validate=round(validate_secs, 1), copy=round(copy_secs, 1))
        else:
            secs = {}
        with ledger_lock:
            ledger[job["title_id"]] = dict(version=job["version"], output=str(final), source=job["source"],
                                           source_version=job.get("source_version") or job["version"],
                                           built=time.strftime("%Y-%m-%d %H:%M"), built_ts=time.time(), prep_min=prep.get("prep_min"),
                                           build_min=res["build_min"], bytes=final.stat().st_size, source_bytes=job.get("size"),
                                           level=cfg["compression_level"], validate=summ, clean=prep.get("clean"),
                                           sha256=res.get("sha256"), **({"stage_secs": secs} if secs else {}),
                                           **({"backport": job["backport_outcome"]} if job.get("backport_outcome") else {}))
            ledger_p.write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
        return res
    except Exception as ex:
        code, phase, data = ps5_failures.classify(ex)
        res.update(why=f"{type(ex).__name__}: {ex}", code=code, data=data, phase="build" if code == "unknown" else phase)
        return res
    finally:
        FINISH.pop(tid, None)
        res["at"] = time.time()
        if jd.exists():
            guarded_rmtree(jd, temp_root)
        shutil.rmtree(stage / "_ultrapack_temp", ignore_errors=True)
        if res.get("status") == "built":
            shutil.rmtree(stage, ignore_errors=True)
            try:
                (out / ".staging").rmdir()
            except OSError:
                pass
        elif res.get("code") == "validate_failed":            # the package is still there, unless it was on the SSD
            here = stage.exists()               # the stamp lets the server be sure, before deleting it, that this is still it
            res["data"] = dict(res["data"], staged=str(stage) if here else None, staged_bytes=dir_size(stage) if here else 0,
                               staged_stamp=ps5_failures.dir_stamp(stage) if here else None)
        log(f"  -> {job['title']}: {res['status']} (prep {res.get('prep_min')} min, build {res.get('build_min')} min). "
            f"{res.get('why', '')} {res.get('validate', '')}  SSD free {shutil.disk_usage(temp_root).free / 2**30:.0f} GiB")
        if res.get("status") == "built":
            try:
                failure_store().clear(job["title_id"])
            except Exception:
                pass
            notify(cfg, "PS5 pkg: built", notification_text(cfg, job["name"], "built", None, res.get("output")))
        else:
            res.setdefault("code", "unknown")
            res.setdefault("phase", "build")
            res.setdefault("data", {})
            if record_failure(cfg, job, log, phase=res["phase"], code=res["code"], message=res.get("why", ""),
                              data=res["data"], logf=logf):       # one notification per distinct failure, not per retry
                notify(cfg, "PS5 pkg: failed", notification_text(
                    cfg, job["name"], "failed", ps5_failures.short_reason(dict(code=res["code"], data=res["data"], message=res.get("why", "")))))


def copy_ps4(cfg, job, log):
    """Copy a PS4 title's .pkg members out of its archive into <output>/PS4/<title> [CUSA...]/, as they are: through
    <output>/.staging, each file extracted by 7-Zip (which checks its CRC and fails on a mismatch) and checked
    against the size in the listing, then renamed into place. UltraPack is never involved and no byte is changed.
    Returns {output, files, bytes, validate}."""
    tid = job["title_id"]
    log = job_log(log, tid)
    _JOB_LINES[tid] = []
    out, dest = Path(cfg["output"]), Path(job["dest_dir"])
    stage = out / ".staging" / f"{tid}-ps4"
    exe = str(Path(cfg["ultrapack_src"]) / SEVENZ)
    todo = [f for f in job["files"] if f.get("dest")]
    total = sum(f["size"] for f in todo)
    free = shutil.disk_usage(out).free
    if free < total + (64 << 20):
        raise ps5_failures.GameError("output_no_space", f"needs ~{total / 2**30:.1f} GiB in the output folder, {free / 2**30:.1f} GiB free",
                                     phase="copy", need=total, free=free)
    if stage.exists():
        guarded_rmtree(stage, out)
    stage.mkdir(parents=True)
    done = []
    try:
        nested = {}
        # packages inside a nested archive that was too big to open while scanning: open it now
        for f in [f for f in job["files"] if f.get("kind") == "nested archive"]:
            np_, listing, why = ps4_open_nested(cfg, job["source"], f["path"], stage / "nested" / str(len(nested)))
            if np_ is None:
                log(f"  {f['name']}: {why}; nothing is copied from it")
                continue
            nested[f["path"]] = np_
            pk = sorted(q for q in listing if q.lower().endswith(".pkg"))
            if not pk:
                log(f"  {f['name']}: no .pkg inside, nothing is copied from it")
            for q in pk:
                m = ps4_describe(cfg, str(np_), q, listing[q], via=f["path"])
                if m["ps4"] is not False:
                    todo.append(dict(name=Path(q).name, size=m["size"], kind=m["kind"], path=q, via=f["path"], dest=ps4_clean_name(Path(q).name)))
        for i, f in enumerate(todo):
            src, member = job["source"], f["path"]
            if f.get("via"):
                if f["via"] not in nested:
                    np_, _, why = ps4_open_nested(cfg, job["source"], f["via"], stage / "nested" / str(len(nested)))
                    if np_ is None:
                        raise ps5_failures.GameError("extract_failed", f"{Path(f['via']).name}: {why}", phase="copy", detail=why)
                    nested[f["via"]] = np_
                src = str(nested[f["via"]])
            work = stage / "x" / str(i)
            work.mkdir(parents=True)
            log(f"  copying {f['name']} ({f['size'] / 2**30:.2f} GiB, {f['kind']})")
            r = subprocess.run([exe, "x", "-y", "-bso0", "-bsp0", f"-o{work}", "-p-", src, member], capture_output=True,
                               stdin=subprocess.DEVNULL, env=seven_env())
            got = work / member
            if r.returncode != 0 or not got.is_file():
                tail = (r.stdout + r.stderr).decode("utf-8", "replace")[-300:]
                raise ps5_failures.GameError("extract_failed", f"7-Zip failed on {f['name']} ({r.returncode}): {tail}", phase="copy",
                                             rc=r.returncode, detail=tail)
            if f["size"] and got.stat().st_size != f["size"]:
                raise ps5_failures.GameError("extract_failed", f"{f['name']}: {got.stat().st_size} bytes extracted, the listing says {f['size']}",
                                             phase="copy", detail="size differs from the archive listing")
            (stage / "final").mkdir(exist_ok=True)
            os.replace(got, stage / "final" / f["dest"])
            done.append(f)
        dest.mkdir(parents=True, exist_ok=True)
        for f in done:
            target = dest / f["dest"]
            if target.exists():
                target.rename(target.with_name(f"{target.stem} (replaced {time.strftime('%Y%m%d%H%M')}){target.suffix}"))
            os.replace(stage / "final" / f["dest"], target)
        log(f"  -> {len(done)} package(s) in {dest}")
        return dict(output=str(dest), files=[f["dest"] for f in done], bytes=sum(f["size"] for f in done),
                    validate=f"CRC verified by 7-Zip: {len(done)} package(s)")
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        try:
            (out / ".staging").rmdir()
        except OSError:
            pass


# ------------------------------------------------------------------ what the page shows of a run

STATUS_INTERVAL = float(os.environ.get("PS5PKG_STATUS_INTERVAL") or 5)    # seconds between writes of status.json
FINISH = {}          # title_id -> {phase: validating|copying|verifying, frac, done, total}: a build UltraPack is done with


def tree_bytes(*paths):
    """Bytes in the files under these folders (one that isn't there counts 0); never raises."""
    total = 0
    for p in paths:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
    return total


def run_counts(results):
    return {s: sum(1 for r in results if r.get("status") == s) for s in ("built", "copied", "failed", "deferred")}


class RunStatus:
    """state/status.json for one run. It is written when the run starts (so the page knows at once), every
    STATUS_INTERVAL seconds while it works, and at the end. It says when the run started and why (manual or
    scheduled), how many games it covers and how many are done, each build with its stage, progress, timings and
    estimate, what is being prepared (bytes, percentage, rate) or copied (PS4), what is waiting, and the results.
    The estimates come from ps5_progress: this run's own log and pace first, earlier builds in the ledger second."""

    def __init__(self, cfg, trigger="manual", ledger=None):
        self.cfg = cfg
        self.trigger = trigger if trigger in ("manual", "scheduled") else "manual"
        self.started = time.time()
        self.hist = PG.history(ledger or {}, cfg.get("compression_level"))
        self.shares = PG.normalise(PG.shares_from(self.hist), with_copy=bool(cfg.get("build_on_ssd")))
        self.ps4_jobs, self.ps4_results, self.ps4_current = [], [], None
        self.jobs = []                      # the builds of this run
        self.q = {}                         # the queue's live containers, see bind()
        self.samples, self.scans = {}, {}    # per title: (time, bytes) of its prepare or copy; the last folder scan
        self.smoothers, self.paced = {}, set()
        self.pace, self.prep_pace, self.ps4_pace = [], [], []     # seconds per source GiB of what finished in this run
        self.progress = 0.0
        self.state = STATE                  # the folder it writes to: fixed now, so a writer that outlives its run can never write into another one
        self.stop_evt, self.thread, self.finished, self.last_error = threading.Event(), None, False, None
        self.lock = threading.Lock()

    # -- what the engine tells it
    def plan(self, todo, copies):
        self.jobs, self.ps4_jobs = list(todo), list(copies)

    def bind(self, **containers):
        self.q = containers

    def ps4_begin(self, job):
        self.ps4_current = dict(job=job, t0=time.time())

    def ps4_end(self, res):
        self.ps4_current = None
        self.ps4_results.append(res)

    def start(self):
        self.write()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_evt.wait(STATUS_INTERVAL):
            self.write()

    # -- assembling it
    def _scan(self, key, now, fn):
        """A folder size, not re-measured more often than it costs: a game of 100k files is not walked every tick."""
        last = self.scans.get(key)
        if last and now - last["at"] < max(STATUS_INTERVAL, 4 * last["cost"]):
            return last["value"]
        t0 = time.time()
        value = fn()
        self.scans[key] = dict(at=now, value=value, cost=time.time() - t0)
        return value

    def _rate(self, tid, now, got):
        sm = self.samples.setdefault(tid, deque(maxlen=120))
        if not sm or sm[-1][1] != got or now - sm[-1][0] >= STATUS_INTERVAL:
            sm.append((now, got))
        return PG.rate_of(sm, now, window=90)

    def _build_rows(self, now):
        rows, kept = [], set()
        pairs = list(zip(list(self.q.get("all_builds", [])), list(self.q.get("finishing", []))))
        for b, fut in pairs:
            if fut.done():
                continue
            job, tid, tr = b["job"], b["job"]["title_id"], b.get("tracker")
            running = b["proc"].poll() is None
            row = dict(title_id=tid, name=job["name"], since=b["t0"], log=Path(b["logf"]).name, size=job.get("size"), elapsed=now - b["t0"])
            raw, kraken_left = None, 0.0
            if tr is not None and running:
                tr.read(b["logf"], now)
                per_gib = self.hist.get("build_per_gib")
                est = PG.estimate_build(tr, self.shares, now, hist_total=(per_gib * job["size"] / PG.GIB) if per_gib and job.get("size") else None)
                raw = est["remaining"]
                kraken_left = (tr.local_remaining(now) or 0.0) if tr.stage == 1 and raw is not None else 0.0
                row.update(stage=tr.stage, stage_text=tr.stage_text, stage_name=est["stage_name"], part=tr.part, pct=tr.pct,
                           stage_pct=est["stage_pct"], progress=round(est["progress"], 4), rate=est["rate"], phase="building",
                           stages=tr.timings())
            elif running:                                        # a build without a tracker: only the tail of its log
                row.update(build_progress(b["logf"]), phase="building", progress=None, stage_name="")
            else:
                fin = FINISH.get(tid) or {}
                phase = fin.get("phase") or "validating"
                frac = float(fin.get("frac") or 0.0)
                scale = None
                if tr is not None and tr.exact and tr.stage_secs():
                    scale = sum(tr.stage_secs().values()) / max(1e-9, sum(self.shares[k] for k in PG.STAGE_KEYS[:5]))
                elif self.hist.get("build_per_gib") and job.get("size"):
                    scale = self.hist["build_per_gib"] * job["size"] / PG.GIB
                if scale is not None:
                    raw = scale * (self.shares["validate"] * 0.5 + self.shares["copy"]) if phase == "validating" else scale * self.shares["copy"] * (1.0 - frac)
                row.update(stage=5, stage_text="", stage_name=PG.STAGE_NAMES["validate" if phase == "validating" else "copy"], part="", pct=0,
                           stage_pct=round(frac * 100), phase=phase, rate=fin.get("rate"),
                           progress=round(PG.finishing_progress(self.shares, "validating" if phase == "validating" else "copying", frac), 4),
                           stages=tr.timings() if tr is not None else [])
                if fin.get("total"):
                    row.update(bytes=fin.get("done"), total_bytes=fin.get("total"))
            sm = self.smoothers.setdefault(tid, PG.Smoother(tick=STATUS_INTERVAL))
            row["remaining"] = sm.update(raw, now)
            row["kraken_remaining"] = kraken_left
            kept.add(tid)
            rows.append(row)
        for tid in [t for t in self.smoothers if t not in kept and t != "run"]:
            self.smoothers.pop(tid, None)
        return rows

    @staticmethod
    def _prep_row(job, since, got, size, rate, ps4=False):
        return dict(title_id=job["title_id"], name=job["name"], since=since, bytes=got, size=size, ps4=ps4,
                    pct=(min(99, int(got * 100 / size)) if size else None), rate=rate,
                    remaining=((max(0, size - got) / rate) if rate and size else None))

    def _prepare_rows(self, now):
        cfg, rows = self.cfg, []
        todo, preparing = self.q.get("todo", []), self.q.get("preparing", {})
        if self.ps4_current:                                    # a PS4 title being copied (before the queue starts)
            job = self.ps4_current["job"]
            tid = job["title_id"]
            stage = Path(cfg["output"]) / ".staging" / f"{tid}-ps4"
            got = self._scan(("ps4", tid), now, lambda: tree_bytes(stage / "x", stage / "final"))
            rows.append(self._prep_row(job, self.ps4_current["t0"], got, job.get("size") or 0, self._rate(tid, now, got), ps4=True))
        for k, (t, f) in list(preparing.items()):
            if f.done() or k >= len(todo):
                continue
            job = todo[k]
            tid = job["title_id"]
            jd = jobdir_of(cfg, job)
            got = self._scan(("prep", tid), now, lambda: tree_bytes(jd / "src", jd / "pkg", Path(cfg["output"]) / ".staging" / f"{tid}-image"))
            rows.append(self._prep_row(job, t, got, job.get("size") or 0, self._rate(tid, now, got)))
        return rows

    def _progress(self, results, builds, prep_rows):
        """The whole run as one number: each game weighs its size; a finished one counts fully, one being built by
        how far its build is (after its prepare), one being prepared or copied by how many bytes are in. It only grows."""
        rho, h = 0.12, self.hist                              # rho: the part of a game's time that is its prepare
        if h.get("prep_per_gib") and h.get("build_per_gib"):
            rho = h["prep_per_gib"] / (h["prep_per_gib"] + h["build_per_gib"])
        done = {r.get("title_id") for r in results}
        building = {b["title_id"]: b for b in builds}
        prepping = {p["title_id"]: p for p in prep_rows}
        total = got = 0.0
        for j in list(self.ps4_jobs) + list(self.jobs):
            w, tid = float(j.get("size") or 1), j["title_id"]
            total += w
            if tid in done:
                got += w
            elif tid in building:
                got += w * (rho + (1 - rho) * float(building[tid].get("progress") or 0.0))
            elif tid in prepping:
                p = prepping[tid]
                frac = (p["bytes"] / p["size"]) if p.get("size") else 0.0
                got += w * (frac if p.get("ps4") else rho * min(0.99, frac))
        self.progress = max(self.progress, min(1.0, (got / total) if total else 0.0))
        return self.progress

    def _eta(self, now, results, builds, prep_rows):
        """Seconds until the run is done, or None while that can't be said. In flight: each build's own estimate.
        Waiting: the pace this run has shown (or earlier builds), sent through the same pipeline the engine runs
        (one prepare at a time, max_compress compressions, max_builds processes)."""
        cfg, h = self.cfg, self.hist
        done = {r.get("title_id") for r in results}
        building = {b["title_id"] for b in builds}
        stopping = stop_requested()                               # what is waiting will not be started
        ps4_left, ps4_rate = 0.0, None
        cur = next((p for p in prep_rows if p.get("ps4")), None)
        if cur is not None:
            ps4_rate = cur.get("rate")
            ps4_left += max(0, (cur.get("size") or 0) - (cur.get("bytes") or 0))
        if not stopping:
            ps4_left += sum(float(j.get("size") or 0) for j in self.ps4_jobs
                            if j["title_id"] not in done and not (cur and cur["title_id"] == j["title_id"]))
        if ps4_left > 0:
            if not ps4_rate and self.ps4_pace:
                ps4_rate = PG.GIB / PG.median_of(self.ps4_pace)
            if not ps4_rate and h.get("ps4_per_gib"):
                ps4_rate = PG.GIB / h["ps4_per_gib"]
            if not ps4_rate:
                return None
        t0 = now + (ps4_left / ps4_rate if ps4_left > 0 else 0.0)
        running = []
        for b in builds:
            if b.get("remaining") is None:
                return None                                       # a build in flight with no signal yet
            running.append(dict(end=now + b["remaining"], kraken_end=now + (b.get("kraken_remaining") or 0.0)))
        todo, preparing = (self.q.get("todo") or self.jobs), self.q.get("preparing", {})
        waiting = [] if stopping else [(k, j) for k, j in enumerate(todo) if j["title_id"] not in done and j["title_id"] not in building]
        queue = []
        if waiting:
            obs = list(self.pace) + [(b["elapsed"] + b["remaining"]) / (b["size"] / PG.GIB) for b in builds
                                     if b.get("size") and b["elapsed"] >= PG.MIN_SIGNAL_SECS and b["phase"] == "building"]
            per_gib = PG.median_of(obs) if obs else h.get("build_per_gib")
            if not per_gib:
                return None
            prep_obs = [PG.GIB / p["rate"] for p in prep_rows if not p.get("ps4") and p.get("rate")] + list(self.prep_pace)
            prep_per_gib = PG.median_of(prep_obs) if prep_obs else (h.get("prep_per_gib") or 0.0)
            by_tid = {p["title_id"]: p for p in prep_rows}
            for k, j in waiting:
                gib = float(j.get("size") or 0) / PG.GIB
                total = per_gib * gib
                p, fut = by_tid.get(j["title_id"]), (preparing.get(k) or (None, None))[1]
                if fut is not None and fut.done():
                    prep = 0.0                                    # prepared and waiting for a slot
                elif p is not None and p.get("remaining") is not None:
                    prep = p["remaining"]
                else:
                    prep = prep_per_gib * gib
                queue.append(dict(prep=prep, total=total, kraken=total * self.shares["1"]))
        end = PG.simulate(t0, running, queue, max_builds=int(cfg.get("max_builds", 3)), max_compress=int(cfg.get("max_compress", 2)))
        return None if end is None else max(0.0, end - now)

    def _paced(self, results):
        """What the games that finished in this run say about the pace of the next ones."""
        for r in results:
            tid = r.get("title_id")
            if tid in self.paced or not r.get("size"):
                continue
            gib = r["size"] / PG.GIB
            if r.get("status") == "built" and r.get("build_secs"):
                self.pace.append(r["build_secs"] / gib)
                if r.get("prep_min"):
                    self.prep_pace.append(r["prep_min"] * 60 / gib)
                self.paced.add(tid)
            elif r.get("status") == "copied" and r.get("copy_secs"):
                self.ps4_pace.append(r["copy_secs"] / gib)
                self.paced.add(tid)

    def snapshot(self, now=None):
        now = now or time.time()
        q, bound = self.q, bool(self.q)
        todo, preparing, done_idx = q.get("todo", []), q.get("preparing", {}), q.get("done_idx", set())
        results = list(q["results"]) if bound else list(self.ps4_results)
        results += [dict(f.result()) for f in q.get("finishing", []) if f.done()]
        builds, prep_rows = self._build_rows(now), self._prepare_rows(now)
        shown = {p["title_id"] for p in prep_rows}
        finished = {r.get("title_id") for r in results}
        waiting = [j for k, j in enumerate(todo) if k not in preparing and k not in done_idx] if bound else list(self.jobs)
        queue = [dict(title_id=j["title_id"], name=j.get("name") or j["title_id"], size=j.get("size"), ps4=True) for j in self.ps4_jobs
                 if j["title_id"] not in finished and j["title_id"] not in shown]
        queue += [dict(title_id=j["title_id"], name=j["name"], size=j["size"]) for j in waiting]
        self._paced(results)
        progress = self._progress(results, builds, prep_rows)
        eta = self._eta(now, results, builds, prep_rows) if (self.jobs or self.ps4_jobs) else None
        eta = self.smoothers.setdefault("run", PG.Smoother(tick=STATUS_INTERVAL)).update(eta, now)
        run = dict(started=self.started, trigger=self.trigger, phase="running" if (self.jobs or self.ps4_jobs or bound) else "scanning",
                   total=len(self.jobs) + len(self.ps4_jobs), done=len(results), progress=round(progress, 4), remaining=eta,
                   eta_at=(now + eta) if eta is not None else None, queue_bytes=sum(int(x.get("size") or 0) for x in queue), **run_counts(results))
        return dict(updated=now, pid=os.getpid(), stopping=stop_requested(), run=run, queue=queue, preparing=prep_rows, builds=builds,
                    results=[{k: r.get(k) for k in RESULT_KEYS} for r in results])

    def write(self):
        try:
            with self.lock:
                if self.finished:
                    return
                tmp = self.state / "status.json.part"
                tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, default=str))
                tmp.replace(self.state / "status.json")
        except Exception as ex:                                 # a run must not stop over its status; but say so, once per kind of error
            msg = f"status.json could not be written: {type(ex).__name__}: {ex}"
            if msg != self.last_error:
                self.last_error = msg
                print(msg, flush=True)

    def finish(self, results, interrupted=False):
        """The last write: the run is over."""
        self.stop_evt.set()
        with self.lock:
            self.finished = True
            now = time.time()
            run = dict(started=self.started, finished=now, trigger=self.trigger, phase="finished",
                       total=max(len(results), len(self.jobs) + len(self.ps4_jobs)), done=len(results), progress=1.0, **run_counts(results))
            if interrupted:
                run["interrupted"] = True
            tmp = self.state / "status.json.part"
            tmp.write_text(json.dumps(dict(updated=now, pid=None, finished=True, run=run,
                                           results=[{k: r.get(k) for k in RESULT_KEYS} for r in results]), ensure_ascii=False, default=str))
            tmp.replace(self.state / "status.json")


def run_ps4(cfg, jobs, log, ledger_p, ledger, rs=None):
    """Copy the planned PS4 titles one after the other (it is only disk to disk). Returns their results."""
    results = []
    for job in jobs:
        if stop_requested():
            log(f"=== stop requested: not copying the remaining {len(jobs) - len(results)} PS4 title(s)")
            break
        tid = job["title_id"]
        log(f"PS4 copy {job['name']}  <- {job['source']}")
        res = dict(title=job.get("title") or job["name"], title_id=tid, name=job["name"], status="failed", source=job["source"], size=job.get("size"))
        if rs:
            rs.ps4_begin(job)
        t_copy = time.time()
        try:
            r = copy_ps4(cfg, job, log)
            ledger[tid] = dict(version=job["version"], output=r["output"], source=job["source"], built=time.strftime("%Y-%m-%d %H:%M"),
                               built_ts=time.time(), bytes=r["bytes"], ps4=True, kind="ps4", files=r["files"], validate=r["validate"],
                               source_bytes=job.get("size"), copy_secs=round(time.time() - t_copy, 1))
            ledger_p.write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
            res.update(status="copied", output=str(Path(r["output"]) / r["files"][0]) if r["files"] else r["output"],
                       gib=round(r["bytes"] / 2**30, 2), validate=r["validate"], copy_secs=round(time.time() - t_copy, 1))
            try:
                failure_store().clear(tid)
            except Exception:
                pass
            notify(cfg, "PS4: copied", notification_text(cfg, job["name"], "copied", None, res["output"]))
        except Exception as ex:
            code, phase, data = ps5_failures.classify(ex)
            if code == "output_no_space":                    # not a failure: it is copied when there is room
                res.update(status="deferred", why=str(ex), code=code, phase="copy", data=data)
                log(f"  -> deferred: {ex}")
                record_failure(cfg, job, log, phase="copy", code=code, message=str(ex), data=data, state=ps5_failures.WAITING)
            else:
                res.update(status="failed", why=f"{type(ex).__name__}: {ex}", code=code, phase="copy", data=data)
                job_log(log, tid)(f"  -> {job['name']}: failed: {type(ex).__name__}: {ex}")
                if record_failure(cfg, job, log, phase="copy", code=code, message=f"{type(ex).__name__}: {ex}", data=data):
                    notify(cfg, "PS5 pkg: failed", notification_text(
                        cfg, job["name"], "failed", ps5_failures.short_reason(dict(code=code, data=data, message=str(ex)))))
        res["at"] = time.time()
        results.append(res)
        if rs:
            rs.ps4_end(res)
    return results


def run_queue(cfg, todo, log, ledger_p, ledger, preset=None, rs=None):
    """Three-stage pipeline:  prepare (extract+verify+clean) game N+2  |  compress game N+1  |
    hash/write/validate game N.  At most max_compress builds in the Kraken stage and max_builds UltraPack
    processes; prepares only start when the SSD can hold them on top of what is in flight. `rs` is the run's
    RunStatus (main() makes it); without one this call is the whole run as far as the status goes, and a queue
    that dies leaves a finished (interrupted) status behind instead of a writer that goes on for ever."""
    if rs is not None:
        return _run_queue(cfg, todo, log, ledger_p, ledger, preset, rs)
    rs = RunStatus(cfg, ledger=ledger)
    rs.plan(todo, [])
    rs.ps4_results = list(preset or [])
    rs.start()
    try:
        return _run_queue(cfg, todo, log, ledger_p, ledger, preset, rs)
    except BaseException:
        if not rs.finished:
            try:
                rs.finish(rs.snapshot()["results"], interrupted=True)
            except Exception:
                rs.stop_evt.set()
        raise


def _run_queue(cfg, todo, log, ledger_p, ledger, preset, rs):
    import threading
    results = list(preset or [])          # what ran before the queue (PS4 copies) is part of this run's results
    ledger_lock = threading.Lock()
    prep_pool = ThreadPoolExecutor(max_workers=1)
    fin_pool = ThreadPoolExecutor(max_workers=cfg.get("max_builds", 3))
    prepared = {}            # index -> Future(prepare)
    active = []              # running builds
    finishing = []           # Futures(finish_build)

    for job in list(todo):
        b = adopt_build(cfg, job, log)
        if b:
            active.append(b)
            finishing.append(fin_pool.submit(finish_build, cfg, b, log, ledger_p, ledger, ledger_lock))
            todo.remove(job)

    def in_flight_growth():
        g = 0.55 if cfg.get("build_on_ssd") else 0.25
        return sum(int(b["job"]["size"] * g) for b in active if b["proc"].poll() is None)

    def try_prefetch(i):
        if i < len(todo) and i not in prepared and not stop_requested():
            need = int((todo[i]["size"] or 0) * 1.10)
            if need <= usable_ssd(cfg) - in_flight_growth():
                prepared[i] = prep_pool.submit(prepare, cfg, todo[i], log, in_flight_growth())
                return True
        return False

    preparing = {}           # index -> (start time, Future(prepare))
    all_builds = list(active)
    done_idx = set()
    rs.bind(todo=todo, preparing=preparing, done_idx=done_idx, all_builds=all_builds, finishing=finishing, results=results)
    _orig_submit = prep_pool.submit

    def _submit(fn, *a, **kw):
        fut = _orig_submit(fn, *a, **kw)
        k = next((k for k, j in enumerate(todo) if j is a[1]), None)
        if k is not None:
            preparing[k] = (time.time(), fut)
        return fut
    prep_pool.submit = _submit

    try_prefetch(0)
    for i, job in enumerate(todo):
        if stop_requested():
            log(f"=== stop requested: not starting the remaining {len(todo) - i} job(s); "
                f"running builds finish normally, finished copies are kept for the next run")
            break
        done_idx.add(i)
        log(f"[{i + 1}/{len(todo)}] {job['title']} ({job['size'] / 2**30:.0f} GiB)")
        while i not in prepared and not try_prefetch(i):
            active[:] = [b for b in active if b["proc"].poll() is None]
            if not active and not any(not f.done() for f in finishing):
                break                    # nothing will free space; let prepare() report it
            time.sleep(20)
        fut = prepared.pop(i, None) or prep_pool.submit(prepare, cfg, job, log, in_flight_growth())
        try:
            prep = fut.result()
        except Exception as ex:
            jlog = job_log(log, job["title_id"])
            code, phase, data = ps5_failures.classify(ex)
            if code == "no_space":            # not a failure: it will be tried again when there is room
                results.append(dict(job, status="deferred", why=str(ex), code=code, phase="prepare", data=data, at=time.time()))
                jlog(f"  -> deferred: {ex}")
                record_failure(cfg, job, log, phase="prepare", code=code, message=str(ex), data=data,
                               state=ps5_failures.WAITING)
                continue
            jd = jobdir_of(cfg, job)
            if jd.exists():
                guarded_rmtree(jd, Path(cfg["temp"]))
            results.append(dict(job, status="failed", why=f"prepare: {type(ex).__name__}: {ex}", code=code, phase="prepare", data=data, at=time.time()))
            jlog(f"  -> {job['title']}: failed while preparing: {type(ex).__name__}: {ex}")
            if record_failure(cfg, job, log, phase="prepare", code=code, message=f"{type(ex).__name__}: {ex}", data=data):
                notify(cfg, "PS5 pkg: failed", notification_text(
                    cfg, job["name"], "failed", ps5_failures.short_reason(dict(code=code, data=data, message=str(ex)))))
            continue
        try:                                  # it fits now: the wait for space is over
            failure_store().clear(job["title_id"], only_state=ps5_failures.WAITING)
        except Exception:
            pass
        # wait for a free compression slot (max_compress) and a free process slot (max_builds)
        while True:
            active[:] = [b for b in active if b["proc"].poll() is None]
            compressing = sum(1 for b in active if not past_compression(b))
            if len(active) < cfg.get("max_builds", 3) and compressing < cfg.get("max_compress", 2):
                break
            time.sleep(15)
        b = start_build(cfg, job, prep, log)
        active.append(b)
        all_builds.append(b)
        finishing.append(fin_pool.submit(finish_build, cfg, b, log, ledger_p, ledger, ledger_lock))
        try_prefetch(i + 1)
    for k, f in list(prepared.items()):          # a stop leaves verified copies (with markers) for next run
        try:
            f.result()
        except Exception:
            pass
    # `results` holds only prepare failures; the status writer adds finished builds itself, so the full list
    # goes into a new name (the writer's closure sees any rebinding of `results`) (appending showed every finished build twice)
    final = results + [dict(f.result()) for f in finishing]
    prep_pool.shutdown(wait=True)
    fin_pool.shutdown(wait=True)
    rs.finish(final)
    (STATE / "stop.request").unlink(missing_ok=True)
    (STATE / "last_run.json").write_text(json.dumps(final, indent=2, ensure_ascii=False, default=str))
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["plan", "run"])
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rescan", action="store_true")
    ap.add_argument("--trigger", choices=["manual", "scheduled"], default="manual", help="why this run was started (for the status)")
    a = ap.parse_args()
    LOGS.mkdir(parents=True, exist_ok=True)
    log = log_to(open(LOGS / "pkg_sync.log", "a", encoding="utf-8"))
    info = load_cfg_info()
    if info.error:       # no last good settings to fall back on in a fresh process: say why and stop
        log(f"=== not starting: {CFG_PATH.name} can't be used ({info.error.describe()}). "
            f"Fix the file, restore a backup, or delete it to go back to the defaults.")
        sys.exit(EXIT_BAD_CONFIG)
    for note in info.problems:
        log(f"config: {note}")
    cfg = info.cfg
    p = cfg["library"]          # the downloads folder is optional: discover() skips it when it isn't there
    if not os.path.isdir(p):
        log(f"not mounted: {p}" + (f"  (mounted: {', '.join(os.listdir('/Volumes'))})" if os.path.isdir("/Volumes") else ""))
        sys.exit(EXIT_NOT_MOUNTED)
    try:
        os.listdir(p)
    except OSError as ex:
        log(f"can't read {p}: {ex.strerror or ex}")
        sys.exit(EXIT_NOT_MOUNTED)
    lock = STATE / "run.lock"
    rs = None
    if a.mode == "run":
        failing = ps5_setup.blocking(ps5_setup.run_checks(cfg, state_dir=STATE))    # not 30 minutes into a game
        for row in failing:
            log(f"setup problem: {row['title']}: {row['message']}. {row['fix']}")
        if failing:
            sys.exit(EXIT_SETUP)
        if lock.exists():
            log(f"another run holds {lock}; exiting")
            sys.exit(EXIT_LOCKED)
        lock.write_text(str(os.getpid()))
        (STATE / "stop.request").unlink(missing_ok=True)
    try:
        cache_p = STATE / "ident_cache.json"
        cache = {} if a.rescan or not cache_p.exists() else json.loads(cache_p.read_text())
        if cache.get("__version__") != IDENT_VERSION:     # identified by an older version: look at everything once more
            cache = {"__version__": IDENT_VERSION}
        ledger_p = STATE / "ledger.json"
        ledger = json.loads(ledger_p.read_text()) if ledger_p.exists() else {}
        if a.mode == "run":
            rs = RunStatus(cfg, a.trigger, ledger)             # the page knows the run has started while it is still scanning
            rs.start()
        log(f"=== {a.mode}: scanning")
        cands = discover(cfg, log, cache)
        cache_p.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
        failures = failure_store().load()
        plan = choose(cfg, cands, ledger, failures, titles=load_titles())
        write_json_atomic(STATE / "unrecognised.json", unrecognised(cands))
        for j in plan:                                  # built since (by hand, or by another copy): nothing to remember
            if j["action"] == "done" and j["title_id"] in failures:
                failure_store().clear(j["title_id"])
        (STATE / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False))
        log("=== plan")
        for j in plan:
            log(f"  {j['action']:6} {j.get('name', j['title_id'])}  <- {j.get('source', '')} ({j['why']})"
                + (f" [patch {j['patch']}]" if j.get("patch") else ""))
        if a.mode == "plan":
            return
        todo = [j for j in plan if (j["action"] == "build" or (j["action"] == "failed" and a.only))     # --only always retries
                and not j.get("ps4") and (not a.only or j["title_id"] in a.only)]
        copies = [j for j in plan if (j["action"] == "copy" or (j["action"] == "failed" and j.get("ps4") and a.only))
                  and (not a.only or j["title_id"] in a.only)]
        for j in plan:
            if j["action"] == "failed" and not a.only:
                rec = failures.get(j["title_id"]) or {}
                log(f"=== skipped: failed on {rec.get('at_text', '?')}, unchanged: {j['title_id']} {j.get('name', '')} - "
                    f"{ps5_failures.short_reason(rec)} (press Retry in the Library, or change its source or options)")
        todo.sort(key=lambda j: j["size"])            # small first: early results, early failures
        if a.limit:
            todo = todo[: a.limit]
        log(f"=== run: {len(todo)} build(s); level {cfg['compression_level']}, {cfg['cpu_count']} workers, nice {cfg['nice']}, "
            f"{cfg.get('max_compress', 2)} concurrent compressions")
        if copies:
            log(f"=== PS4: {len(copies)} title(s) to copy as they are")
        rs.plan(todo, copies)
        results = run_queue(cfg, todo, log, ledger_p, ledger, preset=run_ps4(cfg, copies, log, ledger_p, ledger, rs=rs), rs=rs)
        log("=== summary: " + ", ".join(f"{s} {sum(1 for r in results if r['status'] == s)}"
                                        for s in ("built", "copied", "failed", "deferred")))
    finally:
        if rs is not None and not rs.finished:          # the run did not get to its end: say so instead of leaving it "running"
            try:
                rs.finish(rs.snapshot()["results"], interrupted=True)
            except Exception:
                pass
        if a.mode == "run" and lock.exists():
            lock.unlink()


if __name__ == "__main__":
    main()
