"""Games that failed: what happened, in words, and whether trying again can make a difference.

A failure is stored per title in state/failures.json (written by the engine, and by the web server when
someone presses Retry or Dismiss, so every change goes through a file lock and an atomic rename). Preparing a
big game copies hundreds of GiB, so a title that failed is not tried again on every scheduled run: it is
retried when its fingerprint changes (the source file, the twin archive, the options set for it, the console
firmware, the junk patterns, the app's own version: an image update can fix what failed) or when someone presses
Retry. A game that was only deferred for lack of space is "waiting", not failed, and is always retried.

Standard library only; shared by the engine and the web server."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:          # not POSIX: writes stay atomic, only the lock shared between processes is lost
    fcntl = None

HERE = Path(__file__).resolve().parent
FAILED, WAITING = "failed", "waiting"
# the override fields that change what is done with a title (a note doesn't)
OVERRIDE_KEYS = ("source", "patch", "modified_exec", "skip", "skip_verify")
EXCERPT_LINES, HISTORY_LINES = 60, 20


class GameError(RuntimeError):
    """A game could not be prepared or built, for a reason that has a name (`code`) and numbers (`data`), so
    the UI can explain it and offer what to do instead of showing an exception string."""

    def __init__(self, code, message, phase="prepare", **data):
        super().__init__(message)
        self.code, self.phase, self.data = code, phase, data


def classify(exc):
    """(code, phase, data) of any exception: the typed ones say it themselves."""
    if isinstance(exc, GameError):
        return exc.code, exc.phase, exc.data
    return "unknown", "prepare", {}


def app_version():
    """The version the app was released as (the VERSION file next to the app in the image, or at the top of a
    checkout). A new version can fix what failed, so it is part of what a failure is tied to."""
    for p in (HERE.parent / "VERSION", HERE.parent.parent / "VERSION"):
        try:
            v = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if v:
            return v
    return "unknown"


# ------------------------------------------------------------------ fingerprint

def _sig(path):
    """What changes when a file is replaced or edited: modification time and size (a folder: its time)."""
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return [int(st.st_mtime), st.st_size if stat.S_ISREG(st.st_mode) else None]


def fingerprint_parts(cfg, entry, version=None):
    """Everything that, when it changes, makes another try worthwhile: for the plan entry of a title."""
    ov = (cfg.get("overrides") or {}).get(entry.get("title_id"), {}) or {}
    return dict(
        source=entry.get("source"), source_sig=_sig(entry.get("source")), size=entry.get("size"),
        verify_archive=entry.get("verify_archive"), verify_sig=_sig(entry.get("verify_archive")),
        override={k: ov[k] for k in OVERRIDE_KEYS if ov.get(k) not in (None, "", False)},
        patch_sig=_sig(ov.get("patch")) if ov.get("patch") else None,
        console_fw=cfg.get("console_fw") or "",
        junk=[cfg.get("junk_file_patterns") or [], cfg.get("junk_title_patterns") or []],
        version=version or app_version())


def fingerprint(cfg, entry, version=None):
    raw = json.dumps(fingerprint_parts(cfg, entry, version), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def blocks(cfg, entry, rec, version=None):
    """True when the stored failure `rec` means this title should not be queued again: it failed (not just
    waiting for space), nobody asked for a retry, and nothing it depends on has changed since."""
    return bool(rec and rec.get("state") == FAILED and not rec.get("retry")
                and rec.get("fingerprint") == fingerprint(cfg, entry, version))


# ------------------------------------------------------------------ the store

def _atomic_write(path, obj):
    tmp = path.with_name(path.name + ".part")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Store:
    """state/failures.json: {title_id: record}. Reading never writes; every change is a locked read-modify-write
    (the engine's threads and the web server share it), so nothing is lost and no reader sees half a file."""
    _thread_lock = threading.Lock()

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def update(self, fn):
        """fn(dict) edits the stored failures in place; returns whatever fn returns."""
        with self._thread_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path.with_name(self.path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o666) if fcntl else None
            try:
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                data = self.load()
                before = json.dumps(data, sort_keys=True, default=str)
                out = fn(data)
                if json.dumps(data, sort_keys=True, default=str) != before:      # a no-op never touches the file
                    _atomic_write(self.path, data)
                return out
            finally:
                if fd is not None:
                    os.close(fd)

    def record(self, tid, *, state=FAILED, title="", name="", phase="prepare", code="unknown", message="", data=None,
               log="", excerpt=None, fingerprint="", version="", now=None):
        """Store a failure (or a wait for space). Returns True when it is a new kind of failure for this title,
        False when it is the same one again (same code, same fingerprint): that is counted, not announced."""
        now = now or time.time()

        def apply(all_):
            old = all_.get(tid) or {}
            same = (old.get("state") == state and old.get("code") == code and old.get("fingerprint") == fingerprint)
            history = (old.get("history") or [])[-(HISTORY_LINES - 1):] + [dict(at=now, code=code, message=message[:300])]
            all_[tid] = dict(
                title_id=tid, title=title or old.get("title", ""), name=name or old.get("name", ""), state=state,
                phase=phase, code=code, message=message, data=data or {}, log=log, excerpt=list(excerpt or [])[-EXCERPT_LINES:],
                fingerprint=fingerprint, version=version, at=now, at_text=time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
                first_at=old.get("first_at") or now, attempts=int(old.get("attempts") or 0) + 1, retry=False,
                dismissed=bool(old.get("dismissed")) if same else False, history=history)
            return not same
        return self.update(apply)

    def clear(self, tid, only_state=None):
        def apply(all_):
            if tid in all_ and (only_state is None or all_[tid].get("state") == only_state):
                del all_[tid]
                return True
            return False
        return self.update(apply)

    def set_flag(self, tid, **flags):
        """Retry / dismiss: change a flag on a stored failure; False when there is none."""
        def apply(all_):
            if tid not in all_:
                return False
            all_[tid].update(flags)
            return True
        return self.update(apply)


# ------------------------------------------------------------------ words

def _gib(n):
    try:
        return f"{float(n) / 2**30:.0f} GiB"
    except (TypeError, ValueError):
        return "?"


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def explain(rec):
    """{summary, detail[]}: what happened to the game, in words, from a stored failure (or its code and data)."""
    code, d, msg = rec.get("code") or "unknown", rec.get("data") or {}, rec.get("message") or ""
    detail = []
    if code == "crc_mismatch":
        files = list(d.get("files") or [])
        n = int(d.get("count") or len(files))
        named = ", ".join(files[:5]) + (f" and {n - 5} more" if n > 5 else "")
        summary = (f"The library copy differs from the original archive in {_plural(n, 'file')}"
                   + (f": {named}" if named else "") + ". It may be modified or damaged.")
    elif code == "crc_no_common_files":
        summary = "The copy could not be checked against the original archive: they have no files in common."
    elif code == "modified_exec_unknown":
        summary = ("This copy has modified executables (a backport) and the console firmware is not set, so the app "
                   "cannot tell which copy will run.")
        if d.get("files"):
            detail.append("Differs in: " + ", ".join(list(d["files"])[:6]))
    elif code == "no_space":
        summary = (f"Needs about {_gib(d.get('need'))} on the work drive; {_gib(d.get('usable'))} usable. "
                   f"Will be retried automatically.")
    elif code == "ultrapack_failed":
        stage = d.get("stage_text")
        summary = f"UltraPack stopped with an error{f' during {stage}' if stage else ''}."
        detail += list(d.get("errors") or d.get("tail") or [])[-5:]
        if any("No module named" in x for x in detail):
            detail.append("This looks like a problem in the app itself rather than in the game. "
                          "Updating the app tries the game again.")
    elif code == "validate_failed":
        n = d.get("failed_checks")
        summary = (f"The package was built but failed {_plural(n, 'validation check') if n else 'validation'}. "
                   f"It was not moved to the output folder.")
        if d.get("summary"):
            detail.append(str(d["summary"]))
    elif code == "title_mismatch":
        summary = f"The source contains {d.get('found') or 'another game'}, not {d.get('expected') or rec.get('title_id')}."
    elif code == "copy_failed":
        summary = "The package was built, but copying it to the output folder failed verification."
    elif code == "extract_failed":
        summary = "Extracting the game from its source failed."
        detail += [x for x in [d.get("detail")] if x]
    elif code == "no_param_json":
        summary = "The game has no sce_sys/param.json after extraction: the source may be incomplete or damaged."
    else:
        summary = msg or "The game failed for a reason the app doesn't know."
    return dict(summary=summary, detail=detail)


def short_reason(rec):
    return explain(rec)["summary"]
