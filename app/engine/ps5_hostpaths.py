"""Where the container's folders are on the host.

Docker gives a container no way to ask where a bind mount comes from, so the app has to be told, and this
module works out what to show. Standard library only; the engine uses it to validate the `host_paths`
setting, the web server to build the Dashboard's Folders card.

Where a folder's host path comes from, first match wins:
  1. PS5PKG_HOST_LIBRARY / _DOWNLOADS / _OUTPUT / _WORK / _CONFIG: the host folder that is mounted at /library,
     /downloads, /output, /work and /config (the same value as the volume, so the two can't drift). When the
     folder the app uses is below that mount (the work folder is /work/tmp) the sub-folder is added.
  2. PS5PKG_HOST_PATHS, a JSON object {"library": "...", "temp": "..."}: the host path of the folder itself,
     exactly as the app shows it. The original mechanism, still supported unchanged.
  3. The `host_paths` setting in config.json (Settings > Folders > Shown as): the same as 2.
  4. A hint read from /proc/self/mountinfo: the tail of the host path (what the mount's root is inside its
     filesystem, without a btrfs subvolume like Synology's "@syno"). Never complete, so `host` is then written
     with a leading "…" (not an absolute path) and `partial` is true, and it is also used to flag a configured
     path whose tail doesn't match ("mismatch"); overlay set-ups can add prefixes, so a mismatch is a hint, not
     an error."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

FOLDERS = ("library", "downloads", "output", "temp", "config")      # "temp" is the work folder, as in config.json
MOUNTS = {"library": "/library", "downloads": "/downloads", "output": "/output", "temp": "/work", "config": "/config"}
ENV_VARS = {"library": "PS5PKG_HOST_LIBRARY", "downloads": "PS5PKG_HOST_DOWNLOADS", "output": "PS5PKG_HOST_OUTPUT",
            "temp": "PS5PKG_HOST_WORK", "config": "PS5PKG_HOST_CONFIG"}
LEGACY_ENV = "PS5PKG_HOST_PATHS"
ALIASES = {"work": "temp"}                 # PS5PKG_HOST_PATHS may say "work" for the work folder
NOT_REAL_DISKS = {"overlay", "tmpfs", "proc", "sysfs", "devtmpfs", "devpts", "mqueue", "cgroup", "cgroup2", "shm",
                  "squashfs", "ramfs"}


def is_absolute(p):
    """A host path: /unix/style, C:\\windows\\style or \\\\server\\share."""
    return bool(re.match(r"^(/|[A-Za-z]:[\\/]|\\\\)", p))


def clean(p):
    """Trim blanks and trailing separators ("/" itself stays)."""
    p = p.strip()
    q = p.rstrip("/\\")
    if re.fullmatch(r"[A-Za-z]:", q):
        return q + "\\"                            # a drive root keeps its separator
    return q or p[:1]


def check_value(p):
    """The cleaned path, or raises ValueError(why)."""
    if not isinstance(p, str):
        raise ValueError("must be text")
    p = clean(p)
    if not is_absolute(p):
        raise ValueError(f"must be an absolute path such as /srv/games/ps5 (found {p!r})")
    if "\n" in p or "\x00" in p:
        raise ValueError("must be a single line")
    return p


def validate_setting(value):
    """The `host_paths` setting: {folder: path}. Empty values are dropped. Raises ValueError(why)."""
    out = {}
    for k, v in value.items():
        if k not in FOLDERS:
            raise ValueError(f"host_paths: unknown folder {k!r} (use {', '.join(FOLDERS)})")
        if not isinstance(v, str):
            raise ValueError(f"host_paths.{k} must be text")
        if not v.strip():
            continue
        try:
            out[k] = check_value(v)
        except ValueError as ex:
            raise ValueError(f"host_paths.{k} {ex}")
    return out


def read_env(environ):
    """Host paths from the environment: ({folder: {"path", "via", "mount"}}, problems).
    `mount` is True when the path is that of the volume (PS5PKG_HOST_<FOLDER>) rather than of the folder itself.
    Bad values are reported in `problems` and ignored, never silently dropped."""
    found, problems = {}, []
    raw = (environ.get(LEGACY_ENV) or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"expected a JSON object like {{\"library\": \"/srv/games/ps5\"}}, found {type(data).__name__}")
        except (ValueError, TypeError) as ex:
            problems.append(f"{LEGACY_ENV} is not valid JSON ({ex}); ignoring it. Use {ENV_VARS['library']} and "
                            f"friends instead, or fix the quoting.")
            data = {}
        for k, v in data.items():
            key = ALIASES.get(k, k)
            if key not in FOLDERS:
                problems.append(f"{LEGACY_ENV}: unknown folder {k!r} (use {', '.join(FOLDERS)}); ignoring it")
                continue
            try:
                found[key] = dict(path=check_value(v), via=LEGACY_ENV, mount=False)
            except ValueError as ex:
                problems.append(f"{LEGACY_ENV}.{k} {ex}; ignoring it")
    for key, name in ENV_VARS.items():
        v = (environ.get(name) or "").strip()
        if not v:
            continue                              # unset, or compose's empty ${VAR}
        try:
            found[key] = dict(path=check_value(v), via=name, mount=True)
        except ValueError as ex:
            problems.append(f"{name} {ex}; ignoring it")
    return found, problems


# ------------------------------------------------------------------ /proc/self/mountinfo

def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_mountinfo(text):
    """[{mount_point, root, options, fstype, source}] from the text of /proc/self/mountinfo."""
    mounts = []
    for line in text.splitlines():
        f = line.split(" ")
        try:
            dash = f.index("-")
        except ValueError:
            continue
        if dash < 6 or len(f) < dash + 3:
            continue
        mounts.append(dict(root=_unescape(f[3]), mount_point=_unescape(f[4]), options=f[5].split(","),
                           fstype=f[dash + 1], source=_unescape(f[dash + 2])))
    return mounts


def read_mountinfo(path="/proc/self/mountinfo"):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_mountinfo(fh.read())
    except OSError:
        return []


def without_subvolume(root, fstype):
    """A btrfs mount's root starts with the subvolume's own path ("/@syno/share/games" on Synology, "/@home/..."
    on Ubuntu), which is not part of any path a person can open: drop a leading component that starts with @."""
    parts = root.split("/")
    if fstype == "btrfs" and len(parts) > 1 and parts[1].startswith("@"):
        return "/" + "/".join(parts[2:])
    return root


def detect(container_path, mounts):
    """What the mount table says about a folder inside the container: {tail, mode, mount_point} or None. `tail`
    is where the folder is inside the filesystem it lives on (for a bind mount of /srv/games/ps5, "/games/ps5"
    when /srv is the mount point of the disk), without a leading btrfs subvolume."""
    p = container_path.rstrip("/") or "/"
    best = None
    for m in mounts:
        mp = m["mount_point"].rstrip("/") or "/"
        if p == mp or p.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best[0]):
                best = (mp, m)
    if best is None or best[0] == "/" or best[1]["fstype"] in NOT_REAL_DISKS:
        return None                                # the container's own filesystem: not a mounted folder
    mp, m = best
    tail = (without_subvolume(m["root"], m["fstype"]).rstrip("/") + p[len(mp):]) or "/"
    if tail == "/":
        return None                                # a whole filesystem: nothing to tell
    return dict(tail=tail, mode="ro" if "ro" in m["options"] else "rw", mount_point=mp)


def same_tail(host, tail):
    h = host.replace("\\", "/").rstrip("/").lower()
    return h == tail.lower() or h.endswith(tail.lower())


def resolve(containers, env, settings, mounts):
    """One entry per folder: {key, container, host, source, via, partial, detected, mismatch, mode, setting}.
    `containers` maps folder -> its path in the container, `env` is read_env()'s first result, `settings` the
    `host_paths` setting. source: env / settings / detected / none."""
    out = {}
    for key in FOLDERS:
        container = containers[key]
        det = detect(container, mounts)
        host, source, via = None, "none", None
        if key in env:
            e = env[key]
            host, source, via = e["path"], "env", e["via"]
            mount = MOUNTS[key]
            if e["mount"] and (container == mount or container.startswith(mount + "/")):
                host = e["path"].rstrip("/") + container[len(mount):]
        elif settings.get(key):
            host, source = settings[key], "settings"
        elif det:                                  # only the tail is known: say so, don't pass it off as a path
            host, source = "…" + det["tail"], "detected"
        out[key] = dict(key=key, container=container, host=host, source=source, via=via, partial=source == "detected",
                        detected=det["tail"] if det else None, mode=det["mode"] if det else None,
                        mismatch=bool(det and source in ("env", "settings") and not same_tail(host, det["tail"])),
                        setting=settings.get(key, ""))
    return out


# ------------------------------------------------------------------ the whole picture, and path translation

def config_root(state, logs):
    """The folder mounted at /config: the parent that the state and logs folders share."""
    try:
        return os.path.commonpath([str(state), str(logs)])
    except ValueError:
        return str(Path(str(state)).parent)


def path_map(folders, env):
    """[(container prefix, host prefix)], longest container prefix first, for display_path & co. Only folders
    whose host location is configured count: a detected hint is partial and must never stand in for a path.
    The volume behind a PS5PKG_HOST_<FOLDER> variable (/work) is included next to the folder (/work/tmp)."""
    pairs = {}
    for key, f in folders.items():
        if f["source"] not in ("env", "settings"):
            continue
        pairs[f["container"].rstrip("/") or "/"] = f["host"]
        e = env.get(key)
        if e and e["mount"]:
            pairs[MOUNTS[key]] = e["path"]
    return sorted(pairs.items(), key=lambda kv: -len(kv[0]))


def folders_for(cfg, *, state, logs, environ, mountinfo="/proc/self/mountinfo"):
    """Everything the web server and the engine need to know about folder locations: {folders (see resolve),
    configured ({folder: host path} where one is configured), problems (what is wrong with the environment),
    mapping (see path_map)}."""
    env, problems = read_env(environ)
    containers = {k: cfg[k] for k in FOLDERS if k != "config"}
    containers["config"] = config_root(state, logs)
    folders = resolve(containers, env, cfg.get("host_paths") or {}, read_mountinfo(mountinfo))
    return dict(folders=folders, problems=problems, mapping=path_map(folders, env),
                configured={k: f["host"] for k, f in folders.items() if f["source"] in ("env", "settings")})


def _under(path, prefix):
    """"" when path is prefix itself, "/rest" when it is below it, None otherwise: whole segments only, so
    /output doesn't match /output2/x."""
    prefix = prefix.rstrip("/") or "/"
    if path == prefix:
        return ""
    if prefix == "/":
        return path
    return path[len(prefix):] if path.startswith(prefix + "/") else None


def _join(host, rest):
    if not rest:
        return host
    if "\\" in host:                              # a Windows host: its separator, not ours
        return host.rstrip("\\/") + rest.replace("/", "\\")
    return host.rstrip("/") + rest


def display_path(path, mapping):
    """The host form of a container path, or None when its folder has no configured host location (so a
    caller shows the container path, marked as one). The longest matching prefix wins."""
    if not isinstance(path, str) or not path:
        return None
    p = path.rstrip("/") or "/"
    for container, host in mapping:
        rest = _under(p, container)
        if rest is not None:
            return _join(host, rest)
    return None


def to_container(path, mapping):
    """A path someone typed, as a container path: unchanged when it already is one, translated when it is
    below a configured host location, None when it maps to neither (it is then kept as typed)."""
    q = (path or "").strip()
    if not q:
        return None
    n = q.replace("\\", "/").rstrip("/") or "/"
    for container, _ in mapping:
        if _under(n, container) is not None:
            return q
    for container, host in sorted(mapping, key=lambda m: -len(m[1])):
        h = host.replace("\\", "/").rstrip("/") or "/"
        windows = "\\" in host or bool(re.match(r"^[A-Za-z]:", host))
        if (_under(n.lower(), h.lower()) if windows else _under(n, h)) is not None:
            return container.rstrip("/") + n[len(h):] if h != "/" else container.rstrip("/") + n
    return None


def display_text(text, mapping):
    """Free text (a log line, a failure reason) with every container folder prefix swapped for its host
    location. Display only: callers never write the result back. Whole segments only, longest prefix first."""
    if not mapping or not text:
        return text
    hosts = dict(mapping)
    pat = re.compile(r"(?<![\w./\-])(" + "|".join(re.escape(c) for c, _ in mapping) + r")(?![\w\-])(?!\.\w)")
    return pat.sub(lambda m: hosts[m.group(1)], text)
