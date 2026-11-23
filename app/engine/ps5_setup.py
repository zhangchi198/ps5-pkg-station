"""First-run setup check: is everything a scan and a build need actually there?

Used by the web server (GET /api/setup, the Dashboard checklist) and by the engine (before a run it stops on
any failing row instead of finding out half an hour into the first game). Standard library only; reads the
folders, and creates and deletes one small temp file in each folder that has to be writable.

Each check is a dict: id, level ("ok" / "warn" / "fail"), title, message, fix (one line, empty when ok),
advisory (a warning that never blocks anything and doesn't keep the Dashboard card open), link (a Settings
field to jump to, or None)."""
from __future__ import annotations

import datetime
import os
import shutil
import time

OK, WARN, FAIL = "ok", "warn", "fail"

SEVENZ = "backend/native/7zz"
ULTRAPACK_FILES = ("backend/cli.py", SEVENZ, "backend/native/ffpfsc-pkg-tool")
ULTRAPACK_EXECUTABLE = (SEVENZ, "backend/native/ffpfsc-pkg-tool")

GROUP_HINT = 'or on Synology add group_add: ["101"] to the compose file'


def fmt_size(n):
    n = max(0, int(n))
    if n >= 2**40:
        return f"{n / 2**40:.1f} TiB"
    return f"{n / 2**30:.0f} GiB" if n >= 2**30 else f"{n / 2**20:.0f} MiB"


def _why(ex):
    return getattr(ex, "strerror", None) or str(ex)


def _write_probe(folder):
    """Create and delete a small hidden file: the only honest test that this user can write there."""
    name = os.path.join(folder, f".ps5pkg-write-test-{os.getpid()}-{time.time_ns()}")
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        os.unlink(name)
    except OSError:
        pass


def probe_dir(path, write=False):
    """(state, detail, item count): state is ok / missing / not_dir / unreadable / unwritable."""
    p = str(path)
    if not os.path.exists(p):
        return "missing", "", 0
    if not os.path.isdir(p):
        return "not_dir", "", 0
    try:
        n = len(os.listdir(p))
    except OSError as ex:
        return "unreadable", _why(ex), 0
    if write:
        try:
            _write_probe(p)
        except OSError as ex:
            return "unwritable", _why(ex), n
    return "ok", "", n


def nearest_existing(path):
    p = os.path.abspath(str(path))
    while not os.path.exists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return p


def clock_info(now=None):
    """The server's clock as the schedule sees it."""
    now = now or datetime.datetime.now().astimezone()
    mins = int((now.utcoffset() or datetime.timedelta(0)).total_seconds() // 60)
    off = f"{'+' if mins >= 0 else '-'}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"
    tz = (os.environ.get("TZ") or "").lstrip(":") or now.tzname() or "UTC"
    return dict(time=now.strftime("%H:%M"), tz=tz, utc_offset=off, text=f"{now:%H:%M} {tz} (UTC{off})")


def biggest_build(plan):
    todo = [p for p in plan or [] if p.get("action") == "build" and p.get("size")]
    return max(todo, key=lambda p: p["size"]) if todo else None


def _row(id, level, title, message, fix="", advisory=False, link=None):
    return dict(id=id, level=level, title=title, message=message, fix=fix if level != OK else "",
                advisory=advisory, link=link)


def _read_only_folder(id, title, p, optional, uid_txt):
    st, why, n = probe_dir(p)
    level = WARN if optional else FAIL
    link = dict(tab="set", field=id)
    if st == "ok":
        return _row(id, OK, title, f"{p} · readable · {n} item{'' if n == 1 else 's'}", link=link)
    if st == "missing" and optional:
        return _row(id, WARN, title, f"{p} · not mounted (optional) - only the library is scanned",
                    f"Only needed for archive sets: mount a folder at {p} in docker-compose.yml, or ignore this.",
                    advisory=True, link=link)
    if st == "missing":
        return _row(id, level, title, f"{p} · not found",
                    f"Mount your game folder at {p} in docker-compose.yml (for example - /path/to/games:{p}:ro), "
                    f"or change the folder under Settings.", link=link)
    if st == "not_dir":
        return _row(id, level, title, f"{p} · is not a folder", "Point this at a folder under Settings.", link=link)
    return _row(id, level, title, f"{p} · not readable by {uid_txt}" + (f": {why}" if why else ""),
                f"Give this user read access to the folder ({GROUP_HINT}).", advisory=optional, link=link)


def _free(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _writable_folder(id, title, field, p, uid_txt, create_ok=False):
    """(failing row or None, the existing folder to measure free space on). With create_ok a folder that doesn't
    exist yet is fine when its nearest existing parent can be written (the engine creates it)."""
    st, why, n = probe_dir(p, write=True)
    link = dict(tab="set", field=field) if field else None
    where = p
    if st == "missing" and create_ok:
        where = nearest_existing(p)
        st, why, n = probe_dir(where, write=True)
    if st == "ok":
        return None, where
    if st == "missing":
        return _row(id, FAIL, title, f"{p} · not found",
                    f"Mount a folder at {p} in docker-compose.yml, or change the folder under Settings.", link=link), None
    if st == "not_dir":
        return _row(id, FAIL, title, f"{p} · is not a folder", "Point this at a folder under Settings.", link=link), None
    if st == "unreadable":
        return _row(id, FAIL, title, f"{where} · not readable by {uid_txt}" + (f": {why}" if why else ""),
                    f"Give this user read and write access to the folder ({GROUP_HINT}).", link=link), None
    return _row(id, FAIL, title, f"{where} · not writable by {uid_txt}" + (f": {why}" if why else ""),
                f"Give this user write access to the folder ({GROUP_HINT}), or mount a writable folder there.",
                link=link), None


def run_checks(cfg, *, state_dir, plan=None, host_paths=None, uid=None):
    """All setup checks for the settings `cfg`. `plan` is plan.json (for the biggest game) or None before the
    first scan; `host_paths` is the optional PS5PKG_HOST_PATHS map."""
    uid = os.getuid() if uid is None and hasattr(os, "getuid") else uid
    uid_txt = f"uid {uid}" if uid is not None else "this user"
    big = biggest_build(plan)
    big_txt = f"{big.get('title') or big['title_id']}" if big else ""
    rows = [_read_only_folder("library", "Game library", cfg["library"], False, uid_txt),
            _read_only_folder("downloads", "Downloads", cfg["downloads"], True, uid_txt)]

    # finished packages
    out = cfg["output"]
    link = dict(tab="set", field="output")
    row, where = _writable_folder("output", "Output", "output", out, uid_txt)
    if row is None:
        free = _free(where)
        msg = f"{out} · writable" + (f" · {fmt_size(free)} free" if free is not None else "")
        if big and free is not None and free < big["size"]:
            row = _row("output", WARN, "Output", f"{msg} · the biggest game ({big_txt}, {fmt_size(big['size'])}) "
                       f"may not fit", "Free up space in the output folder or point it at a bigger drive.", link=link)
        else:
            row = _row("output", OK, "Output", msg, link=link)
    rows.append(row)

    # scratch space: a game is extracted here (the engine creates the folder if only its parent exists)
    temp, reserve = cfg["temp"], int(cfg["ssd_reserve_gb"]) << 30
    link = dict(tab="set", field="temp")
    row, where = _writable_folder("work", "Work drive", "temp", temp, uid_txt, create_ok=True)
    if row is None:
        free = _free(where)
        need = int(big["size"] * 1.10) if big else 0
        fix = ("Free up space on the work drive, point Work folder at a bigger drive, or lower "
               "\"Keep free on work drive\" under Settings.")
        if free is None:
            row = _row("work", OK, "Work drive", f"{temp} · writable")
        elif free - reserve <= 0:
            row = _row("work", FAIL, "Work drive", f"{temp} · {fmt_size(free)} free, less than the "
                       f"{fmt_size(reserve)} that is kept free: no game can be extracted", fix, link=link)
        elif big and need > free - reserve:
            row = _row("work", WARN, "Work drive", f"{temp} · {fmt_size(free)} free · the biggest game ({big_txt}) needs "
                       f"about {fmt_size(need + reserve)} including the reserve and will be deferred on every run",
                       fix, link=link)
        else:
            tail = (f"biggest game needs about {fmt_size(need + reserve)} incl. reserve" if big
                    else "run a scan to check that the biggest game fits")
            row = _row("work", OK, "Work drive", f"{temp} · {fmt_size(free)} free · {tail}", link=link)
    rows.append(row)

    # settings, history and logs
    row, where = _writable_folder("config", "Settings and history", None, str(state_dir), uid_txt)
    rows.append(row or _row("config", OK, "Settings and history", f"{state_dir} · writable"))

    # UltraPack's files
    up = cfg["ultrapack_src"]
    missing = [f for f in ULTRAPACK_FILES if not os.path.isfile(os.path.join(up, f))]
    if not os.path.isfile(cfg["python"]):
        missing.append(cfg["python"])
    noexec = [f for f in ULTRAPACK_EXECUTABLE if os.path.isfile(os.path.join(up, f))
              and not os.access(os.path.join(up, f), os.X_OK)]
    if os.path.isfile(cfg["python"]) and not os.access(cfg["python"], os.X_OK):
        noexec.append(cfg["python"])
    if missing or noexec:
        what = "; ".join(x for x in (("missing: " + ", ".join(missing)) if missing else "",
                                     ("not executable: " + ", ".join(noexec)) if noexec else "") if x)
        rows.append(_row("tools", FAIL, "UltraPack tools", what,
                         "They ship inside the Docker image: pull it again (docker compose pull && docker compose up -d). "
                         "If you changed ultrapack_src or python in config.json, check those paths."))
    else:
        rows.append(_row("tools", OK, "UltraPack tools", f"found in {up}"))

    fw = (cfg.get("console_fw") or "").strip()
    rows.append(_row("console_fw", OK, "Console firmware", fw) if fw else
                _row("console_fw", WARN, "Console firmware", "not set - needed to choose between a backport and the "
                     "original copy of a game", "Enter your console's firmware (for example 9.60) under Settings.",
                     link=dict(tab="set", field="console_fw")))

    clock = clock_info()
    rows.append(_row("time", OK, "Server time", f"{clock['text']} - schedule times use this clock"))

    hp = host_paths or {}
    rows.append(_row("host_paths", OK, "Folder locations", "shown as set in PS5PKG_HOST_PATHS") if hp else
                _row("host_paths", WARN, "Folder locations", "not set - paths are shown as seen inside the container",
                     "Optional: set PS5PKG_HOST_PATHS in the compose file to show where the folders are on the host.",
                     advisory=True))
    return rows


def blocking(rows):
    return [r for r in rows if r["level"] == FAIL]
