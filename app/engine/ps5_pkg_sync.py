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
import time
import zlib
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
}
SEVENZ = "backend/native/7zz"
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
    "schedule": (dict,),
}
FILE_ONLY = {"ultrapack_src": (str,), "python": (str,)}   # valid in the file, not on the Settings page
EXIT_BAD_CONFIG = 4
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
    fields = (("source", is_str), ("patch", is_str), ("note", is_str), ("skip", is_bool),
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
    path = Path(path)
    tmp = path.with_name(path.name + ".part")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
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
        if raw is None:
            raw = copy.deepcopy(DEFAULT_CFG)
        mutate(raw)
        write_json_atomic(CFG_PATH, raw)
    cfg, problems = validate_cfg(raw)
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


def parse_param(raw: bytes) -> dict:
    j = json.loads(raw.decode("utf-8-sig"))
    lp = j.get("localizedParameters", {}) or {}
    title = (lp.get("en-US") or {}).get("titleName") or (lp.get(lp.get("defaultLanguage", "")) or {}).get("titleName")
    return dict(title=title or "", title_id=j.get("titleId", ""), version=j.get("contentVersion", ""),
                fw=fw_from_param(j.get("requiredSystemSoftwareVersion")))


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
    return log


# ------------------------------------------------------------------ discovery

def ident_image_or_folder(path):
    uw, base = A.open_game_fs(path)
    try:
        root = uw.root or ""
        files = {k[len(root):].lower(): e for k, e in uw.fs.files.items() if k.startswith(root)}
        pj = files.get("sce_sys/param.json")
        if not pj:
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


def ident_archive(cfg, path):
    paths = archive_listing(cfg, path)
    pj = [p for p in paths if p.lower().endswith("sce_sys/param.json")]
    if not pj:
        inner = [p for p in paths if p.lower().endswith((".exfat", ".ffpfsc", ".ffpfs", ".ffpkg", ".pkg"))
                 and paths[p] > (1 << 30)]
        if len(inner) != 1:
            return None                 # patch / backport / DLC / unlocker archive
        m = re.search(r"(PPSA|CUSA)\d{5}", inner[0]) or re.search(r"(PPSA|CUSA)\d{5}", path)
        if not m:
            return None
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
    for top in sorted(Path(cfg["downloads"]).iterdir()):
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
             "archive-image": 2, "archive-pkg": 2, "folder": 3}


def nas_exists(p) -> bool:
    """smbfs keeps names as stored (NFD here): check both Unicode normal forms."""
    import unicodedata
    return any(Path(unicodedata.normalize(f, str(p))).exists() for f in ("NFC", "NFD"))


def choose(cfg, cands, ledger):
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
        name = pkg_name(pick)
        if ov.get("skip"):
            action, why = "skip", "skipped by override"
        elif pick.get("packed"):
            action, why = "skip", f"only {pick['packed']}-packed sources"
        elif done and vkey(done["version"]) >= vkey(pick["version"]) and nas_exists(done["output"]):
            action, why = "done", f"built {done.get('built', '')}: {Path(done['output']).name}"
        else:
            action, why = "build", "newer version" if done else "new"
        twin = next((c for c in lst if c["kind"] in ("rar", "zip", "7z") and c["version"] == pick["version"]
                     and not c.get("packed") and c["source"] != pick["source"]), None)
        plan.append(dict(title_id=tid, title=clean_title(pick["title"]), version=pick["version"], fw=pick.get("fw", ""),
                         source=pick["source"], kind=pick["kind"], size=pick.get("size", 0),
                         verify_archive=twin["source"] if twin and pick["kind"] not in ("rar", "zip", "7z", "folder") else None,
                         verify_root=twin.get("archive_root") if twin else None,
                         archive_root=pick.get("archive_root"), inner=pick.get("inner"), patch=ov.get("patch"),
                         name=name if pick["version"] else f"{clean_title(pick['title'])} [{tid}] (version read after extraction)",
                         action=action, why=why,
                         candidates=[dict(source=c["source"], kind=c["kind"], version=c["version"],
                                          packed=c.get("packed", ""), size=c.get("size", 0)) for c in lst],
                         skipped_duplicates=[f"{c['source']} (v{c['version']}{', ' + c['packed'] if c.get('packed') else ''})"
                                             for c in lst if c["source"] != pick["source"]]))
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
        return want
    ov = cfg["overrides"].get(job["title_id"], {})
    a = archive_param(cfg, job["verify_archive"], job.get("verify_root"))
    console = cfg.get("console_fw", "")
    log(f"  image differs from the original in {len(diff)} file(s){f' + {len(extra)} fakelib file(s)' if extra else ''}: "
        f"{', '.join((diff + extra)[:6])}{' ...' if len(diff) + len(extra) > 6 else ''}; image fw {job.get('fw')}, "
        f"original fw {a['fw']}, console fw {console or '?'}")
    if ov.get("modified_exec") == "image" or (console and a["fw"] and vkey(a["fw"]) > vkey(console)
                                               and vkey(job.get("fw") or "99") <= vkey(console)):
        log("  keeping the image's modified executables (original needs newer firmware than the console)")
        return {k: v for k, v in want.items() if not EXEC_RE.search(k) and not
                (k == "sce_sys/param.json" and any(d.startswith("sce_sys/param.json") for d in diff))}
    if ov.get("modified_exec") == "original" or (console and a["fw"] and vkey(a["fw"]) <= vkey(console)):
        log(f"  switching to the original archive (runs on fw {console})")
        job.update(kind="rar", source=job["verify_archive"], archive_root=job.get("verify_root"), verify_archive=None,
                   fw=a["fw"], name=pkg_name(dict(job, fw=a["fw"])))
        return None
    raise RuntimeError("image has modified executables and neither copy is known to run on the console "
                       "(set console_fw or overrides.<id>.modified_exec)")


def extract_image_files(image, app: Path, want: dict, log, against=""):
    """Copy every file out of an image with the read-only readers; CRC32-check against `want` when given."""
    checked = mism = 0
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
                    log(f"  CRC MISMATCH {rel}: image {crc:08X} vs archive {w:08X}")
    finally:
        if base:
            base.close()
    if want:
        log(f"  CRC32 check vs {against}: {checked} files matched, {mism} mismatched")
        if mism:
            raise RuntimeError(f"{mism} extracted file(s) differ from the archive CRCs")
        if checked == 0:
            raise RuntimeError("CRC check found no common files")


def materialise(cfg, job, jobdir: Path, log) -> Path:
    """Put the game's /app0 on the SSD and return its folder."""
    dest = jobdir / "src"
    dest.mkdir(parents=True)
    t0 = time.time()
    want = None
    if job["kind"] not in ("rar", "zip", "7z", "folder", "archive-pkg", "archive-image") and job.get("verify_archive"):
        crcs = {}
        archive_listing(cfg, job["verify_archive"], crcs, timeout=4 * 3600)
        r = job.get("verify_root") or ""
        want = {p[len(r):].lower(): c for p, c in crcs.items() if p.startswith(r)}
        want = resolve_modified_exec(cfg, job, want, log)     # may switch the job to the archive
    if job["kind"] in ("rar", "zip", "7z"):
        r = seven(cfg, ["x", "-y", "-bso0", "-bsp0", f"-o{dest}", job["source"]])
        if r.returncode != 0:
            raise RuntimeError(f"7zz extract failed ({r.returncode}): {r.stderr.decode(errors='replace')[-400:]}")
        app = dest / job["archive_root"] if job.get("archive_root") else dest
    elif job["kind"] == "archive-pkg":
        r = seven(cfg, ["x", "-y", "-bso0", "-bsp0", f"-o{jobdir / 'pkg'}", job["source"], job["inner"]])
        if r.returncode != 0:
            raise RuntimeError(f"7zz extract of {job['inner']} failed ({r.returncode})")
        pkg = next((jobdir / "pkg").rglob("*.pkg"))
        r = subprocess.run([cfg["python"], "-B", "backend/cli.py", "placeholder", str(dest / "pkgx"), "--fpkg-extract", str(pkg)],
                           cwd=cfg["ultrapack_src"], capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        if r.returncode != 0:
            raise RuntimeError(f"--fpkg-extract failed: {(r.stdout + r.stderr).decode(errors='replace')[-400:]}")
        shutil.rmtree(jobdir / "pkg", ignore_errors=True)
        pjs = sorted((dest / "pkgx").rglob("param.json"), key=lambda q: len(q.parts))
        pjs = [q for q in pjs if q.parent.name.lower() == "sce_sys"]
        if not pjs:
            raise RuntimeError("extracted .pkg has no sce_sys/param.json")
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
                raise RuntimeError(f"7zz extract of {job['inner']} failed ({r.returncode})")
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
        raise RuntimeError(f"no sce_sys/param.json under {app}")
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


def notify(cfg, title, msg):
    url = (cfg.get("notify_url") or "").strip()
    if not url:
        return
    try:
        import urllib.request
        req = urllib.request.Request(url, data=msg.encode("utf-8"), method="POST",
                                     headers={"Title": title.encode("ascii", "replace").decode()})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


PROGRESS_RE = re.compile(rb"(\d+)% (inner image|outer PFS|finalize)[^\n\r]*")
STAGE_RE = re.compile(rb"\[stage (\d)/5\] ([^\n\r(:]+)")


def build_progress(logf):
    """Stage and percentage of a running UltraPack build, from the tail of its log."""
    try:
        with open(logf, "rb") as fh:
            fh.seek(max(0, os.path.getsize(logf) - (256 << 10)))
            tail = fh.read()
    except OSError:
        return {}
    st = STAGE_RE.findall(tail)
    pr = PROGRESS_RE.findall(tail)
    return dict(stage=int(st[-1][0]) if st else 1, stage_text=st[-1][1].decode(errors="replace").strip() if st else "",
                pct=int(pr[-1][0]) if pr else 0, part=pr[-1][1].decode() if pr else "")


def stop_requested():
    return (STATE / "stop.request").exists()


def jobdir_of(cfg, job):
    return Path(cfg["temp"]) / job["title_id"]


def usable_ssd(cfg):
    Path(cfg["temp"]).mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(cfg["temp"]).free - (cfg["ssd_reserve_gb"] << 30)


def prepare(cfg, job, log, headroom=0):
    """Extract + clean the game on the SSD. headroom = bytes the running build may still grow."""
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
                    job.update({k: m["job"][k] for k in ("version", "fw", "title", "name") if k in m["job"]})
                return dict(app=Path(m["app"]), clean=m.get("clean"), prep_min=m.get("prep_min"))
        except Exception as ex:
            log(f"  ignoring unusable prepare marker: {ex}")
    if jd.exists():
        guarded_rmtree(jd, temp_root)
    need = int((job["size"] or 0) * 1.10)
    if need > usable_ssd(cfg) - headroom:
        raise MemoryError(f"needs ~{need / 2**30:.0f} GiB on the SSD, "
                          f"{(usable_ssd(cfg) - headroom) / 2**30:.0f} GiB usable")
    jd.mkdir(parents=True)
    t0 = time.time()
    log(f"PREPARE {job['name']}  <- {job['source']}")
    app = materialise(cfg, job, jd, log)
    ident = parse_param((app / "sce_sys" / "param.json").read_bytes())
    if ident["title_id"] != job["title_id"]:
        raise RuntimeError(f"extracted game is {ident['title_id']}, expected {job['title_id']}")
    if not job.get("version"):
        job.update(version=ident["version"], fw=ident["fw"], title=clean_title(ident["title"]), name=pkg_name(ident))
        log(f"  identified after extraction: {job['name']}")
    res = dict(app=app, clean=clean(app, job.get("patch"), log), prep_min=round((time.time() - t0) / 60, 1))
    marker.write_text(json.dumps(dict(res, app=str(app), source=job["source"], at=time.strftime("%Y-%m-%d %H:%M"),
                                      job={k: job.get(k) for k in ("version", "fw", "title", "name")}),
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
    return dict(job=job, prep=prep, proc=proc, logf=logf, stage=stage, t0=t0)


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
    job.update({k: v for k, v in (info.get("job") or {}).items() if k in ("version", "fw", "title", "name", "source")})
    log(f"ADOPT running build of {job['name']} (pid {pid}); log {logf.name}")
    return dict(job=job, prep=prep, proc=PidProc(pid, logf), logf=logf,
                stage=Path(info.get("stage") or Path(cfg["output"]) / ".staging" / job["title_id"]),
                t0=float(info.get("t0") or bf.stat().st_mtime))


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


def copy_verified(src: Path, dst: Path, log):
    """Copy src -> dst (dst + '.partial' first) in 64 MiB writes; SHA-256 of the source while writing, then a
    cache-bypassing read-back of the NAS copy must match before the rename."""
    part = dst.parent / ".staging" / (dst.name + ".partial")
    part.parent.mkdir(exist_ok=True)
    t0 = time.time()
    h = hashlib.sha256()
    with open(src, "rb") as fi, open(part, "wb") as fo:
        _nocache(fi.fileno())
        _nocache(fo.fileno())
        while True:
            chunk = fi.read(64 << 20)
            if not chunk:
                break
            fo.write(chunk)
            h.update(chunk)
        fo.flush()
        os.fsync(fo.fileno())
        _drop_cache(fo.fileno())
    t1 = time.time()
    h2 = hashlib.sha256()
    with open(part, "rb") as fr:
        _nocache(fr.fileno())
        _drop_cache(fr.fileno())
        while True:
            chunk = fr.read(64 << 20)
            if not chunk:
                break
            h2.update(chunk)
    size = src.stat().st_size
    if h.digest() != h2.digest() or part.stat().st_size != size:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"NAS copy of {src.name} failed verification")
    part.rename(dst)
    t2 = time.time()
    log(f"  copied to NAS: {size / 2**30:.1f} GiB, write {size / 2**20 / max(t1 - t0, 1):.0f} MiB/s, "
        f"verify {size / 2**20 / max(t2 - t1, 1):.0f} MiB/s, sha256 {h.hexdigest()[:16]}")
    return h.hexdigest()


def finish_build(cfg, b, log, ledger_p, ledger, ledger_lock):
    job, prep, stage, logf = b["job"], b["prep"], b["stage"], b["logf"]
    out = Path(cfg["output"])
    temp_root = Path(cfg["temp"])
    jd = jobdir_of(cfg, job)
    res = dict(title=job["title"], title_id=job["title_id"], name=job["name"], source=job["source"],
               status="failed", clean=prep.get("clean"), prep_min=prep.get("prep_min"), log=str(logf))
    try:
        rc = b["proc"].wait()
        res.update(rc=rc, build_min=round((time.time() - b["t0"]) / 60, 1))
        pkgs = sorted(stage.glob("*.pkg"), key=lambda p: -p.stat().st_size)
        if rc != 0 or not pkgs:
            res["why"] = f"UltraPack exit {rc}, {len(pkgs)} package(s)"
            return res
        v = subprocess.run([cfg["python"], "-B", "backend/cli.py", "--fpkg-validate", str(pkgs[0])],
                           cwd=cfg["ultrapack_src"], capture_output=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        vtxt = (v.stdout + v.stderr).decode("utf-8", "replace")
        with open(logf, "a") as fh:
            fh.write("\n==== --fpkg-validate ====\n" + vtxt)
        summ = next((ln for ln in vtxt.splitlines() if ln.startswith("summary:")), "")
        res["validate"] = summ
        if v.returncode != 0:
            res["why"] = f"fpkg-validate failed ({summ}); package left in {stage}"
            return res
        final = out / job["name"]
        if final.exists():
            final.rename(final.with_name(final.stem + f" (replaced {time.strftime('%Y%m%d%H%M')}).pkg"))
        if Path(cfg["temp"]).resolve() in pkgs[0].resolve().parents:
            res["sha256"] = copy_verified(pkgs[0], final, log)
        else:
            pkgs[0].rename(final)
        res.update(status="built", output=str(final), gib=round(final.stat().st_size / 2**30, 2))
        with ledger_lock:
            ledger[job["title_id"]] = dict(version=job["version"], output=str(final), source=job["source"],
                                           built=time.strftime("%Y-%m-%d %H:%M"), prep_min=prep.get("prep_min"),
                                           build_min=res["build_min"], bytes=final.stat().st_size,
                                           level=cfg["compression_level"], validate=summ, clean=prep.get("clean"),
                                           sha256=res.get("sha256"))
            ledger_p.write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
        return res
    except Exception as ex:
        res["why"] = f"{type(ex).__name__}: {ex}"
        return res
    finally:
        if jd.exists():
            guarded_rmtree(jd, temp_root)
        shutil.rmtree(stage / "_ultrapack_temp", ignore_errors=True)
        if res.get("status") == "built":
            shutil.rmtree(stage, ignore_errors=True)
            try:
                (out / ".staging").rmdir()
            except OSError:
                pass
        notify(cfg, f"PS5 pkg: {res['status']}", f"{job['name']}" + (f": {res.get('why')}" if res.get("why") else ""))
        log(f"  -> {job['title']}: {res['status']} (prep {res.get('prep_min')} min, build {res.get('build_min')} min). "
            f"{res.get('why', '')} {res.get('validate', '')}  SSD free {shutil.disk_usage(temp_root).free / 2**30:.0f} GiB")


def run_queue(cfg, todo, log, ledger_p, ledger):
    """Three-stage pipeline:  prepare (extract+verify+clean) game N+2  |  compress game N+1  |
    hash/write/validate game N.  At most max_compress builds in the Kraken stage and max_builds UltraPack
    processes; prepares only start when the SSD can hold them on top of what is in flight."""
    import threading
    results = []
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

    preparing = {}           # index -> start time

    def status_writer(stop_evt):
        while not stop_evt.wait(5):
            try:
                st = dict(updated=time.time(), pid=os.getpid(), stopping=stop_requested(),
                          queue=[dict(title_id=j["title_id"], name=j["name"], size=j["size"])
                                 for k, j in enumerate(todo) if k not in preparing and k not in done_idx],
                          preparing=[dict(title_id=todo[k]["title_id"], name=todo[k]["name"], since=t)
                                     for k, (t, f) in list(preparing.items()) if not f.done()],
                          builds=[dict(title_id=b["job"]["title_id"], name=b["job"]["name"], since=b["t0"],
                                       log=Path(b["logf"]).name, **build_progress(b["logf"]))
                                  for b in list(all_builds) if b["proc"].poll() is None],
                          results=[{k: r.get(k) for k in ("title_id", "name", "title", "status", "why", "validate",
                                                          "prep_min", "build_min", "gib")} for r in results]
                                  + [{k: f.result().get(k) for k in ("title_id", "name", "title", "status", "why",
                                                                     "validate", "prep_min", "build_min", "gib")}
                                     for f in finishing if f.done()])
                tmp = STATE / "status.json.part"
                tmp.write_text(json.dumps(st, ensure_ascii=False, default=str))
                tmp.replace(STATE / "status.json")
            except Exception:
                pass

    all_builds = list(active)
    done_idx = set()
    _orig_submit = prep_pool.submit

    def _submit(fn, *a, **kw):
        fut = _orig_submit(fn, *a, **kw)
        k = next((k for k, j in enumerate(todo) if j is a[1]), None)
        if k is not None:
            preparing[k] = (time.time(), fut)
        return fut
    prep_pool.submit = _submit
    stop_evt = threading.Event()
    threading.Thread(target=status_writer, args=(stop_evt,), daemon=True).start()

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
        except MemoryError as ex:
            results.append(dict(job, status="deferred", why=str(ex)))
            log(f"  -> deferred: {ex}")
            continue
        except Exception as ex:
            jd = jobdir_of(cfg, job)
            if jd.exists():
                guarded_rmtree(jd, Path(cfg["temp"]))
            results.append(dict(job, status="failed", why=f"prepare: {type(ex).__name__}: {ex}"))
            log(f"  -> {job['title']}: failed while preparing: {type(ex).__name__}: {ex}")
            notify(cfg, "PS5 pkg: failed", f"{job['name']}: {type(ex).__name__}: {ex}")
            continue
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
    stop_evt.set()
    (STATE / "status.json").write_text(json.dumps(dict(updated=time.time(), pid=None, finished=True, results=[
        {k: r.get(k) for k in ("title_id", "name", "title", "status", "why", "validate", "prep_min", "build_min", "gib")}
        for r in final]), ensure_ascii=False, default=str))
    (STATE / "stop.request").unlink(missing_ok=True)
    (STATE / "last_run.json").write_text(json.dumps(final, indent=2, ensure_ascii=False, default=str))
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["plan", "run"])
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rescan", action="store_true")
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
    for p in (cfg["library"], cfg["downloads"]):
        if not os.path.isdir(p):
            log(f"not mounted: {p}" + (f"  (mounted: {', '.join(os.listdir('/Volumes'))})" if os.path.isdir("/Volumes") else ""))
            sys.exit(2)
    lock = STATE / "run.lock"
    if a.mode == "run":
        if lock.exists():
            log(f"another run holds {lock}; exiting")
            sys.exit(3)
        lock.write_text(str(os.getpid()))
        (STATE / "stop.request").unlink(missing_ok=True)
    try:
        cache_p = STATE / "ident_cache.json"
        cache = {} if a.rescan or not cache_p.exists() else json.loads(cache_p.read_text())
        ledger_p = STATE / "ledger.json"
        ledger = json.loads(ledger_p.read_text()) if ledger_p.exists() else {}
        log(f"=== {a.mode}: scanning")
        cands = discover(cfg, log, cache)
        cache_p.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
        plan = choose(cfg, cands, ledger)
        (STATE / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False))
        log("=== plan")
        for j in plan:
            log(f"  {j['action']:6} {j.get('name', j['title_id'])}  <- {j.get('source', '')} ({j['why']})"
                + (f" [patch {j['patch']}]" if j.get("patch") else ""))
        if a.mode == "plan":
            return
        todo = [j for j in plan if j["action"] == "build" and (not a.only or j["title_id"] in a.only)]
        todo.sort(key=lambda j: j["size"])            # small first: early results, early failures
        if a.limit:
            todo = todo[: a.limit]
        log(f"=== run: {len(todo)} build(s); level {cfg['compression_level']}, {cfg['cpu_count']} workers, nice {cfg['nice']}, "
            f"{cfg.get('max_compress', 2)} concurrent compressions")
        results = run_queue(cfg, todo, log, ledger_p, ledger)
        log("=== summary: " + ", ".join(f"{s} {sum(1 for r in results if r['status'] == s)}"
                                        for s in ("built", "failed", "deferred")))
    finally:
        if a.mode == "run" and lock.exists():
            lock.unlink()


if __name__ == "__main__":
    main()
