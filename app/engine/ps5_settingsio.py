"""Exporting and importing the settings: one JSON document {format, app_version, exported_at, settings, overrides}.

Export holds only what can be imported again safely: the settings the Settings page can change (not the paths of
UltraPack's files, not keys this version doesn't know) and the per-game overrides, from the effective values, so an
invalid value in the file is exported as the default that is actually in use. The notification URL can be left
out: it may be a private topic. State (build history, failed-game records, caches) is not settings and is never in
this document.

Import checks everything with the same rules as the Settings page (ps5_pkg_sync.validate_setting) before anything
is written, names the key of every problem, previews what would change, and then only sets what the file says:
settings and overrides that are not in the file, and keys it doesn't know, stay as they are. Standard library
only apart from the engine module it validates with."""
from __future__ import annotations

import copy
import re
import time

import ps5_pkg_sync as E

FORMAT = 1
TID_RE = re.compile(r"(PPSA|CUSA)\d{5}")
TEXT_FIELDS, BOOL_FIELDS = ("source", "patch", "note"), ("skip", "skip_verify")
KNOWN_OVERRIDE_FIELDS = TEXT_FIELDS + BOOL_FIELDS + ("modified_exec",)
CONTAINER_PATHS = ("library", "downloads", "output", "temp")
PATH_NOTE = "a folder inside the container: only apply it if this install mounts the same folders"
PARTS = ("settings", "overrides")


class ImportProblem(ValueError):
    """The document can't be imported; `problems` names every key at fault."""

    def __init__(self, problems):
        super().__init__("Not imported: " + "; ".join(problems))
        self.problems = list(problems)


def export_doc(cfg, *, include_notify_url=True, app_version="", now=None):
    settings = {k: copy.deepcopy(cfg.get(k)) for k in E.SETTINGS if include_notify_url or k != "notify_url"}
    return dict(format=FORMAT, app_version=app_version,
                exported_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time())),
                includes_notify_url=bool(include_notify_url), settings=settings,
                overrides=copy.deepcopy(cfg.get("overrides") or {}))


def parse_import(doc, patch_problem=None):
    """(settings, overrides) of an exported document, every value checked; raises ImportProblem listing all
    problems (each starts with the key: `settings.compression_level: ...`) and nothing else. `patch_problem(tid,
    folder)` says why a patch folder may not be used (None when it may): the web server knows where they belong."""
    problems = []
    if not isinstance(doc, dict):
        raise ImportProblem(["the file is not a settings export (a JSON object was expected)"])
    fmt = doc.get("format", FORMAT)
    if not isinstance(fmt, int) or isinstance(fmt, bool) or fmt > FORMAT:
        problems.append(f"format: {fmt!r} was made by a newer version of the app")
    if "settings" not in doc and "overrides" not in doc:
        problems.append("the file has neither settings nor overrides")
    settings, overrides = {}, {}
    raw_settings = doc.get("settings", {})
    if not isinstance(raw_settings, dict):
        problems.append("settings: must be an object")
        raw_settings = {}
    for k, v in raw_settings.items():
        if k not in E.SETTINGS:
            problems.append(f"settings.{k}: not a setting this version has (it is not exported, so the file was edited or "
                            f"comes from another version); remove it from the file")
            continue
        try:
            settings[k] = E.validate_setting(k, v)
        except ValueError as ex:
            problems.append(f"settings.{k}: {ex}")
    raw_ovs = doc.get("overrides", {})
    if not isinstance(raw_ovs, dict):
        problems.append("overrides: must be an object")
        raw_ovs = {}
    for tid, ov in raw_ovs.items():
        if not TID_RE.fullmatch(str(tid)):
            problems.append(f"overrides.{tid}: not a title ID like PPSA01234")
            continue
        if not isinstance(ov, dict):
            problems.append(f"overrides.{tid}: must be an object")
            continue
        for k, v in ov.items():              # fields this version doesn't know are kept, not judged
            if k in TEXT_FIELDS and not isinstance(v, str):
                problems.append(f"overrides.{tid}.{k}: must be text")
            elif k in BOOL_FIELDS and not isinstance(v, bool):
                problems.append(f"overrides.{tid}.{k}: must be true or false")
            elif k == "modified_exec" and v not in ("image", "original"):
                problems.append(f"overrides.{tid}.{k}: must be \"image\" or \"original\"")
            elif k == "patch" and v and patch_problem and patch_problem(tid, v):
                problems.append(f"overrides.{tid}.{k}: {patch_problem(tid, v)}")
        overrides[tid] = copy.deepcopy(ov)
    if problems:
        raise ImportProblem(problems)
    return settings, overrides


def parse_parts(text):
    """The ?parts= choice: settings, overrides, or both (the default)."""
    parts = [p for p in (text or ",".join(PARTS)).split(",") if p]
    bad = [p for p in parts if p not in PARTS]
    if bad or not parts:
        raise ValueError(f"parts must be {' and/or '.join(PARTS)} (got {text!r})")
    return parts


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def diff(cfg, settings, overrides, parts, *, replace_all=False):
    """What applying `settings` / `overrides` to the effective config `cfg` would change. With replace_all (a
    restore: the whole file is swapped) overrides that the other side doesn't have are listed as removed."""
    out = dict(parts=list(parts), settings=dict(changed=[], unchanged=0),
               overrides=dict(added=[], replaced=[], removed=[], unchanged=0))
    if "settings" in parts:
        for k, v in settings.items():
            cur = cfg.get(k)
            if cur == v:
                out["settings"]["unchanged"] += 1
            else:
                out["settings"]["changed"].append(dict({"key": k, "from": cur, "to": v},
                                                       **({"note": PATH_NOTE} if k in CONTAINER_PATHS else {})))
    if "overrides" in parts:
        cur = cfg.get("overrides") or {}
        for tid, ov in overrides.items():
            if tid not in cur:
                out["overrides"]["added"].append(dict(title_id=tid, to=ov))
            elif cur[tid] != ov:
                out["overrides"]["replaced"].append(dict({"title_id": tid, "from": cur[tid], "to": ov}))
            else:
                out["overrides"]["unchanged"] += 1
        if replace_all:
            out["overrides"]["removed"] = [dict(title_id=t, **{"from": v}) for t, v in cur.items() if t not in overrides]
    s, o = out["settings"], out["overrides"]
    bits = []
    if s["changed"]:
        bits.append(f"{_plural(len(s['changed']), 'setting')} change")
    if o["added"]:
        bits.append(f"{_plural(len(o['added']), 'override')} added")
    if o["replaced"]:
        bits.append(f"{len(o['replaced'])} replaced")
    if o["removed"]:
        bits.append(f"{len(o['removed'])} removed")
    out["changes"] = len(s["changed"]) + len(o["added"]) + len(o["replaced"]) + len(o["removed"])
    out["summary"] = ", ".join(bits) if bits else "Nothing would change."
    return out


def apply_import(raw, settings, overrides, parts):
    """Edit the file's own dict in place (call inside E.update_cfg): set what the document says. Everything else
    (other settings, keys this version doesn't know, other overrides, fields of a replaced override that the
    document doesn't know) stays."""
    if "settings" in parts:
        for k, v in settings.items():
            raw[k] = copy.deepcopy(v)
    if "overrides" in parts:
        cur = raw.get("overrides")
        if not isinstance(cur, dict):
            cur = raw["overrides"] = {}
        for tid, ov in overrides.items():
            old = cur.get(tid) if isinstance(cur.get(tid), dict) else {}
            keep = {k: v for k, v in old.items() if k not in KNOWN_OVERRIDE_FIELDS and k not in ov}
            cur[tid] = dict(copy.deepcopy(ov), **keep)
