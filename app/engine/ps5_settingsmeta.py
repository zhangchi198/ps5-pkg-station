"""What the Settings page shows and checks, in one place (standard library plus the engine module).

* `FIELDS`: every setting the page edits, in the order of its groups, with a plain label, a one-line help, an example,
  and its kind. The ranges and defaults are not repeated here: they come from the engine (`SETTINGS`, `DEFAULT_CFG`), so
  the number fields carry exactly the `min` / `max` the server checks.
* `check()`: validates a set of changes the way the page sends them and says what is wrong in plain words, field by
  field: `{field, message}`. Nothing is dropped silently, and the cross-field rules (games compressing at once can't be
  more than games in progress; a schedule that is on needs a time) are errors; over-committing the CPU or a reserve bigger
  than the work drive are warnings that can still be saved.
* `preview_patterns()`: which titles or file names of the last scan a cleanup pattern would match.
* `rescan_needed()`: the settings that only show in the Library after a scan, and which of them changed since the last one."""
from __future__ import annotations

import re

import ps5_pkg_sync as E

GROUPS = [("console", "Console"), ("performance", "Performance"), ("cleanup", "Cleanup"), ("schedule", "Schedule"),
          ("notifications", "Notifications"), ("folders", "Folders")]

NICE_CHOICES = [(0, "Normal"), (10, "Low (builds give way to other programs)"), (19, "Lowest")]

FIELDS = [
    dict(key="console_fw", group="console", kind="text", label="Console firmware", example="13.20",
         help="The system software version of the console that will run the packages, like 13.20. When a game has a backport "
              "this decides which copy is built: the original if it runs on this firmware, else the backport. Empty means "
              "unknown: a game with a backport then stops and asks."),
    dict(key="ps4_mode", group="console", kind="choice", label="PS4 titles",
         choices=[("copy", "Copy their ready-made packages as they are (no conversion)"), ("skip", "Skip them (listed as skipped)")],
         help="PS4 games (CUSA...) come as finished .pkg files; they are copied, never converted."),
    dict(key="max_builds", group="performance", kind="int", label="Games in progress at once (max)", unit="games",
         help="How many games the app works on together: one compressing while others are being prepared, written or checked. "
              "More games in progress need more room on the work drive. 2 or 3 is typical."),
    dict(key="max_compress", group="performance", kind="int", label="Games compressing at once (max)", unit="games",
         help="How many of those may be in the compression stage, the slow and CPU-heavy one, at the same time. It can't be more "
              "than the games in progress. 1 or 2 suits most machines."),
    dict(key="cpu_count", group="performance", kind="int", label="CPU threads per game", unit="threads",
         help="The threads UltraPack uses to compress one game. Games compressing at once times this should not be more than the "
              "threads of this machine."),
    dict(key="compression_level", group="performance", kind="int", label="Compression level",
         help="1 is fastest, 9 makes the smallest packages. Each step up takes longer for a smaller package, and the gain gets "
              "smaller with every step: 4 is fast, 7 is a good balance."),
    dict(key="nice", group="performance", kind="nice", label="Run builds at low priority",
         help="At a lower priority builds give way to other programs on the same machine, so it stays responsive; they take "
              "longer when something else is busy."),
    dict(key="ssd_reserve_gb", group="performance", kind="int", label="Keep this much free on the work drive", unit="GB",
         help="Space the app never uses for building, so the work drive can't fill up. A game only starts when it fits with this "
              "much left; a game that never fits is skipped on every run."),
    dict(key="build_on_ssd", group="performance", kind="bool", label="Build on the work drive, then copy to the output folder",
         help="Helps when the output folder is slow or on the network: the package is written on the fast work drive, checked, then "
              "copied once in large writes. It needs noticeably more room on the work drive for each game in progress."),
    dict(key="skip_download_prefixes", group="cleanup", kind="lines", label="Skip download folders starting with", example="PC_",
         help="One per line. Folders in Downloads whose name starts with one of these are not looked at, for example PC_ for PC releases."),
    dict(key="junk_file_patterns", group="cleanup", kind="regex", subject="file", label="Delete files whose name matches", example=r"\.url$",
         help="Regular expressions, one per line, matched against file names. Matching files are deleted from the work copy before "
              "building (release-group watermarks, .url and .txt files). Your source folders are never changed."),
    dict(key="junk_title_patterns", group="cleanup", kind="regex", subject="title", label="Cut from game titles", example=r"\[.*?\]",
         help="Regular expressions, one per line. Matching text is cut out of the title in param.json and out of the package name."),
    dict(key="schedule.enabled", group="schedule", kind="bool", label="Run automatically",
         help="Start a run at the times below, on this server's clock."),
    dict(key="schedule.times", group="schedule", kind="times", label="Run at", example="03:00",
         help="24-hour times as HH:MM, like 03:00 or 14:30."),
    dict(key="notify_url", group="notifications", kind="text", label="Notification URL (optional)", example="https://ntfy.sh/my-private-topic",
         help="A web address that gets a short message when a game is built, copied or fails (an ntfy topic works). It must start with "
              "http:// or https://; leave it empty for no notifications."),
]
BY_KEY = {f["key"]: f for f in FIELDS}
LABEL = {**{k: f["label"] for k, f in BY_KEY.items()},
         "library": "Game library folder", "downloads": "Downloads folder", "output": "Finished packages folder", "temp": "Work folder",
         "host_paths": "Folder locations", "schedule": "Schedule"}
RESCAN_LABEL = {"library": "Game library folder", "downloads": "Downloads folder", "output": "Finished packages folder", "ps4_mode": "PS4 titles",
                "skip_download_prefixes": "Download folders to skip", "junk_file_patterns": "File cleanup patterns", "junk_title_patterns": "Title cleanup patterns"}
RESCAN_NOTE = ("These show in the Library only after a rescan: the library, downloads and output folders, PS4 titles, the download folders to skip and the "
               "cleanup patterns. The Library says so when one of them changed, with a button to rescan.")


def default_of(key):
    cur = E.DEFAULT_CFG
    for part in key.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def schema():
    """The fields for the page: the definitions above with the ranges and defaults of the engine."""
    out = []
    for f in FIELDS:
        d = dict(f)
        key = f["key"].split(".")[0]
        spec = E.SETTINGS.get(key)
        if f["kind"] in ("int", "nice") and spec and spec[0] is int:
            d.update(min=spec[1], max=spec[2], step=1)
        d["default"] = default_of(f["key"])
        if f["kind"] == "nice":
            d["choices"] = NICE_CHOICES
        out.append(d)
    return out


# ---------------------------------------------------------------- plain-language errors

def _err(field, message, **kw):
    return dict(field=field, message=message, **kw)


def _lines(key, value, label):
    """Errors of a list setting, one per bad pattern, with its line."""
    out = []
    for i, x in enumerate(value, 1):
        if not isinstance(x, str):
            out.append(_err(key, f"Line {i}: text expected.", line=i))
        elif key in ("junk_file_patterns", "junk_title_patterns") and x.strip():
            try:
                re.compile(f"(?:{x})")
            except re.error as ex:
                out.append(_err(key, f"Line {i}: {x!r} is not a valid pattern: {ex}.", line=i))
    return out


def friendly(key, value, tech):
    """The errors for one setting that failed E.validate_setting (`tech` is its message), in plain words."""
    label = LABEL.get(key, key)
    spec = E.SETTINGS.get(key)
    if key == "schedule":
        out = []
        enabled = value.get("enabled", False) if isinstance(value, dict) else None
        if not isinstance(value, dict):
            return [_err("schedule.enabled", "The schedule is not valid.")]
        if not isinstance(enabled, bool):
            out.append(_err("schedule.enabled", "Run automatically must be on or off."))
        times = value.get("times", [])
        if not isinstance(times, list):
            return out + [_err("schedule.times", "The times are not a list.")]
        for t in times:
            if not (isinstance(t, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t)):
                shown = t if isinstance(t, str) else repr(t)
                out.append(_err("schedule.times", f"{shown!r} is not a time. Use 24-hour HH:MM, like 03:00 or 14:30.", value=shown))
        return out
    if key in ("junk_file_patterns", "junk_title_patterns", "skip_download_prefixes"):
        if not isinstance(value, list):
            return [_err(key, f"{label}: a list of lines is expected.")]
        return _lines(key, value, label) or [_err(key, f"{label}: {tech}")]
    if key == "host_paths":
        m = re.match(r"host_paths\.(\w+) (.*)", tech)
        if m:
            return [_err(f"host_paths.{m.group(1)}", f"{LABEL.get(m.group(1), m.group(1))}: " + {
                "must be an absolute path": "enter the full path, starting with / (or a drive letter such as D:\\).",
                "must be text": "enter text."}.get(m.group(2), m.group(2) + "."))]
        text = re.sub(r"^host_paths\b", "Folder locations", tech)
        return [_err("host_paths", text[0].upper() + text[1:] + ("" if text.endswith(".") else "."))]
    if key == "console_fw":
        return [_err(key, "Enter the firmware like 13.20 (digits, a dot, two digits), or leave it empty if you don't know it.")]
    if key == "notify_url":
        return [_err(key, "Enter a web address that starts with http:// or https://, or leave it empty.")]
    if key == "ps4_mode":
        return [_err(key, "Choose copy or skip.")]
    if spec and spec[0] is int:
        return [_err(key, f"{label} must be a whole number from {spec[1]} to {spec[2]}.")]
    if spec and spec[0] is bool:
        return [_err(key, f"{label} must be on or off.")]
    if spec and spec[0] is str:
        return [_err(key, f"{label}: enter text.")]
    return [_err(key, f"{label}: {tech}")]


def cross_errors(cfg):
    out = []
    if isinstance(cfg.get("max_compress"), int) and isinstance(cfg.get("max_builds"), int) and cfg["max_compress"] > cfg["max_builds"]:
        out.append(_err("max_compress", f"Games compressing at once ({cfg['max_compress']}) can't be more than games in progress at once ({cfg['max_builds']}). "
                                        f"Lower it, or raise games in progress."))
    sch = cfg.get("schedule")
    if isinstance(sch, dict) and sch.get("enabled") and not sch.get("times"):
        out.append(_err("schedule.times", "Run automatically is on but there is no time. Add at least one time, or turn it off."))
    return out


def warnings(cfg, machine):
    """Things worth a second look that can still be saved: [{field, message}]. `machine`: {cpus, work_total}."""
    out = []
    cpus = machine.get("cpus") or 0
    mc, cc = cfg.get("max_compress"), cfg.get("cpu_count")
    if cpus and isinstance(mc, int) and isinstance(cc, int) and mc * cc > cpus:
        out.append(_err("cpu_count", f"This machine has {cpus} CPU threads. With these values up to {mc} × {cc} = {mc * cc} threads compress at once, "
                                     f"{'twice' if mc * cc == 2 * cpus else f'{mc * cc / cpus:.1f} times'} what it has: the games compete for CPU and each takes longer."))
    total, reserve = machine.get("work_total"), cfg.get("ssd_reserve_gb")
    if total and isinstance(reserve, int) and reserve << 30 >= total:
        out.append(_err("ssd_reserve_gb", f"Keeping {reserve} GB free is more than the whole work drive ({total / 2**30:.0f} GB): no game would fit, "
                                          f"so every game would be deferred. Lower it."))
    return out


def check(current, body, machine=None):
    """(clean changes, errors, warnings) for the settings `body` the page sends, on top of the current ones. Every field
    is checked (not the first one only); a value is only changed when the whole set is fine."""
    clean, errors = {}, []
    for k, v in (body or {}).items():
        if k not in E.SETTINGS:
            errors.append(_err(k, f"{k!r} is not a setting this version has."))
            continue
        try:
            clean[k] = E.validate_setting(k, v)
        except ValueError as ex:
            errors += friendly(k, v, str(ex))
        else:
            if k in ("junk_file_patterns", "junk_title_patterns") and any(not isinstance(x, str) for x in v):
                errors += _lines(k, v, LABEL[k])
    merged = {**current, **clean}
    if "max_compress" in clean or "max_builds" in clean:
        errors += [e for e in cross_errors(merged) if e["field"] == "max_compress"]
    if "schedule" in clean:
        errors += [e for e in cross_errors(merged) if e["field"] == "schedule.times"]
    return clean, errors, warnings(merged, machine or {})


# ---------------------------------------------------------------- what a pattern would match

def titles_seen(state):
    """The raw titles the last scan read from game files (state/ident_cache.json), de-duplicated."""
    import json
    try:
        cache = json.loads((state / "ident_cache.json").read_text())
    except (OSError, ValueError):
        return []
    seen, out = set(), []
    for v in cache.values() if isinstance(cache, dict) else []:
        t = v.get("title") if isinstance(v, dict) else None
        if isinstance(t, str) and t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def file_names_seen(state, limit=200000):
    """(names, archives): the file names inside the archives the scans listed (state/listing_cache), one per file."""
    names, archives = [], 0
    folder = state / "listing_cache"
    try:
        files = sorted(folder.glob("*.txt"))
    except OSError:
        return [], 0
    for f in files:
        try:
            paths = E._parse_listing(f.read_text(encoding="utf-8"), None)
        except (OSError, ValueError):
            continue
        archives += 1
        names += [p.rsplit("/", 1)[-1] for p in paths]
        if len(names) >= limit:
            break
    return names, archives


def preview_patterns(state, kind, patterns, limit=60):
    """Which titles ("title") or file names ("file") of the last scan the patterns would match: {errors, matches: [{text,
    after?}], total, looked_at, note}. A bad pattern is reported by line and nothing is matched."""
    patterns = [p for p in patterns or [] if isinstance(p, str) and p.strip()]
    errors = []
    for i, x in enumerate(patterns, 1):
        try:
            re.compile(f"(?:{x})")
        except re.error as ex:
            errors.append(_err("pattern", f"Line {i}: {x!r} is not a valid pattern: {ex}.", line=i))
    if errors or not patterns:
        return dict(errors=errors, matches=[], total=0, looked_at=0, note="" if errors else "Add a pattern to test.")
    if kind == "title":
        rx, titles = E.junk_title_regex(patterns), titles_seen(state)
        hits = [dict(text=t, after=E.clean_title(t, rx)) for t in titles if rx.search(t)]
        return dict(errors=[], matches=hits[:limit], total=len(hits), looked_at=len(titles),
                    note=f"Looked at {len(titles)} title{'' if len(titles) == 1 else 's'} found by the last scan.")
    rx = E.junk_file_regex(patterns)
    names, archives = file_names_seen(state)
    hits = sorted({n for n in names if rx.search(n)})
    return dict(errors=[], matches=[dict(text=n) for n in hits[:limit]], total=len(hits), looked_at=len(names),
                note=(f"Looked at {len(names)} file names inside {archives} archive{'' if archives == 1 else 's'} listed by the last scans. "
                      f"Files in extracted folders and images are checked when a game is built."))


# ---------------------------------------------------------------- what needs a rescan

def rescan_needed(snapshot, cfg):
    """Labels of the settings that changed since the plan was made (`snapshot`: what plan_settings() recorded), [] when
    nothing did or when there is no snapshot to compare with."""
    if not isinstance(snapshot, dict):
        return []
    return [RESCAN_LABEL.get(k, k) for k in E.RESCAN_KEYS if k in snapshot and snapshot[k] != cfg.get(k)]
