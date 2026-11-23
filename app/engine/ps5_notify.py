"""Notifications: what is sent, to which kind of service, and what became of it (standard library plus ps5_failures).

Events: `summary` (a run finished), `built` / `copied` (a game finished), `failed` (a game failed for a reason it had not failed for
before), `attention` (something needs a person: a run could not start, waiting for space, a folder nearly full, the settings file broken),
`test` (the Send test button). Which of them are sent follows three settings: notify_summary, notify_each_game, notify_attention.

Types: `ntfy` posts the message as the body of a plain POST with the Title, Priority, Tags (and Click, the link back) headers an ntfy server
reads; any server that ignores the headers still gets the text. `webhook` posts JSON, the same for every event (see README).

Every delivery's outcome is kept (state/notify_last.json) and every failure is logged, in words; the notification URL itself never is
(it may contain a private topic or a token): errors are scrubbed of it and of anything that looks like one."""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import ps5_failures

TYPES = ("ntfy", "webhook")
URL_RE = re.compile(r"https?://[^\s/?#]+", re.I)
EVENT_OF_TITLE = {"PS5 pkg: built": "built", "PS4: copied": "copied", "PS5 pkg: failed": "failed"}      # the titles of the per-game messages
TAGS = {"built": "white_check_mark", "copied": "white_check_mark", "failed": "x", "summary": "package", "attention": "warning", "test": "bell"}
HIGH = ("failed", "attention")
SCHEMA = 1
TIMEOUT = 10
LAST = "notify_last.json"
STATE_FILE = "notify_state.json"
GIB = 2 ** 30


# ------------------------------------------------------------------ what is wanted

def url_of(cfg):
    url = (cfg.get("notify_url") or "").strip()
    return url if url and URL_RE.match(url) else ""


def wants(cfg, event):
    """Whether this kind of message is on. A config without the toggles (written before they existed) that has a notification URL keeps
    sending a message per game, as it always did; a new install sends the summary and what needs attention."""
    each = cfg.get("notify_each_game")
    if each is None:
        each = bool(cfg.get("notify_url"))
    attention, summary = cfg.get("notify_attention", True), cfg.get("notify_summary", True)
    return {"test": True, "summary": bool(summary), "attention": bool(attention), "built": bool(each), "copied": bool(each),
            "failed": bool(attention or each)}.get(event, bool(each))


# ------------------------------------------------------------------ never the URL

TOKEN_PARAM = re.compile(r"(?i)([?&;](?:[\w.-]*(?:token|key|secret|auth|pass(?:word)?|pwd|sig(?:nature)?)[\w.-]*)=)[^&\s\"'<>]+")
ANY_URL = re.compile(r"https?://[^\s\"'<>]+", re.I)


def redact(text, url=""):
    """`text` without the notification URL: the URL itself, the parts of its path that could be a topic or a token, any other web address, and
    the value of anything in a query that looks like a token."""
    t = str(text)
    url = (url or "").strip()
    if url:
        bits = {url, url.rstrip("/")}
        try:
            parts = urllib.parse.urlsplit(url)
            bits.update(seg for seg in parts.path.split("/") if len(seg) >= 6)
            bits.update(x for x in (parts.query, parts.password) if x)
        except ValueError:                                              # an address that does not even parse: the whole of it is what goes
            pass
        for b in sorted(bits, key=len, reverse=True):
            t = t.replace(b, "<notification URL>" if b in (url, url.rstrip("/")) else "<redacted>")
    t = ANY_URL.sub("<URL>", t)
    return TOKEN_PARAM.sub(r"\1<redacted>", t)


# ------------------------------------------------------------------ the message

def header_text(text):
    """A header value: one line, and RFC 2047 for anything outside ASCII (what ntfy reads), so a title in any language arrives."""
    t = " ".join(str(text).split())
    if t.isascii():
        return t
    return "=?UTF-8?B?" + base64.b64encode(t.encode("utf-8")).decode("ascii") + "?="


def build(cfg, event, title, message, *, games=None, run=None, priority=None, now=None):
    """(headers, body bytes) for this event and the configured type."""
    link = (cfg.get("notify_link") or "").strip()
    pri = priority or ("high" if event in HIGH else "default")
    if (cfg.get("notify_type") or "ntfy") == "webhook":
        doc = {"schema": SCHEMA, "event": event, "title": title, "message": message, "priority": pri, "url": link or None,
               "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now or time.time())), "games": list(games or [])}
        if run:
            doc["run"] = run
        return {"Content-Type": "application/json; charset=utf-8"}, json.dumps(doc, ensure_ascii=False).encode("utf-8")
    headers = {"Title": header_text(title), "Priority": pri, "Tags": TAGS.get(event, ""), "Content-Type": "text/plain; charset=utf-8"}
    if link:
        headers["Click"] = header_text(link)
    return {k: v for k, v in headers.items() if v}, message.encode("utf-8")


def post(url, headers, body, timeout=None):
    """Deliver it: {ok, status, error}. The error is in words and has no URL in it."""
    timeout = timeout or TIMEOUT
    status, error = None, None
    try:
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        resp = urllib.request.urlopen(req, timeout=timeout)
        got = getattr(resp, "status", None)
        status = got if isinstance(got, int) else 200
        resp.read()
    except urllib.error.HTTPError as ex:
        status, error = ex.code, f"HTTP {ex.code} {ex.reason}".strip()
    except urllib.error.URLError as ex:
        r = ex.reason
        error = ("timed out" if "timed out" in str(r) else r if isinstance(r, str) else (getattr(r, "strerror", None) if isinstance(r, OSError) else None)
                 or f"{type(r).__name__}: {r}")
    except Exception as ex:
        error = "timed out" if "timed out" in str(ex) else f"{type(ex).__name__}: {ex}"
    ok = error is None and status is not None and status < 400
    return {"ok": ok, "status": status, "error": None if ok else redact(error or f"HTTP {status}", url)}


# ------------------------------------------------------------------ what was last delivered, and what was last said

def _atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(obj, ensure_ascii=False))
    os.replace(tmp, path)


def record_last(state, res):
    try:
        _atomic(Path(state) / LAST, {k: res.get(k) for k in ("at", "ok", "status", "error", "event", "type")})
    except OSError:
        pass


def read_last(state):
    try:
        d = json.loads((Path(state) / LAST).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def once(state, key, active):
    """True when `key` became active just now (it was not before): a condition that lasts is announced once, and again after it has cleared."""
    p = Path(state) / STATE_FILE
    try:
        d = json.loads(p.read_text())
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        d = {}
    was = bool(d.get(key))
    if was != bool(active):
        d[key] = bool(active)
        try:
            _atomic(p, d)
        except OSError:
            pass
    return bool(active) and not was


def send(cfg, event, title, message, *, state=None, log=None, games=None, run=None, priority=None, record=True):
    """Send one message if this event is wanted and there is somewhere to send it. Returns {ok, status, error, ...} or None when nothing was
    sent. A failed delivery is logged (without the URL) and remembered."""
    url = url_of(cfg)
    if not url or not wants(cfg, event):
        return None
    headers, body = build(cfg, event, title, message, games=games, run=run, priority=priority)
    res = post(url, headers, body)
    res.update(at=time.time(), event=event, type=cfg.get("notify_type") or "ntfy")
    if record and state:
        record_last(state, res)
    if log:
        try:
            log(f"notification {event} ({res['type']}): " + ("delivered" + (f" (HTTP {res['status']})" if res["status"] else "") if res["ok"] else f"NOT delivered: {res['error']}"))
        except Exception:
            pass
    return res


# ------------------------------------------------------------------ words

def duration(seconds):
    s = max(0, int(round(seconds or 0)))
    if s < 90:
        return f"{s} s"
    m = round(s / 60)
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    return f"{h} h {r:02d} min" if r else f"{h} h"


def size_text(b):
    return f"{b / 2**40:.1f} TiB" if b >= 2**40 else f"{b / GIB:.0f} GiB" if b >= 10 * GIB else f"{b / GIB:.1f} GiB"


def game_row(res):
    """One result of a run as the webhook lists it."""
    why = None
    if res.get("status") in ("failed", "deferred") and res.get("code"):
        try:
            why = ps5_failures.short_reason(dict(code=res["code"], data=res.get("data") or {}, message=str(res.get("why") or "")))
        except Exception:
            why = str(res.get("why") or "")
    elif res.get("status") == "cancelled":
        why = str(res.get("why") or "")
    secs = res.get("build_secs")
    if secs is None and res.get("build_min") is not None:
        secs = res["build_min"] * 60
    return {k: v for k, v in dict(title_id=res.get("title_id"), title=res.get("title"), name=res.get("name"), status=res.get("status"), reason=why or None,
                                  output=res.get("output"), bytes=res.get("bytes"), seconds=round(secs) if secs else None).items() if v is not None}


COUNT_ORDER = (("built", "built"), ("copied", "copied"), ("failed", "failed"), ("cancelled", "cancelled"), ("deferred", "waiting for space"))


def summary_text(results, seconds, free=None):
    """"Run finished: 4 built, 1 failed (Example Game: differs from the archive in 1 file). 3 h 12 min. 1.3 TiB free in the output folder." """
    rows = [game_row(r) for r in results]
    counts = {k: sum(1 for r in rows if r.get("status") == k) for k, _ in COUNT_ORDER}
    parts = []
    for key, word in COUNT_ORDER:
        if not counts[key]:
            continue
        piece = f"{counts[key]} {word}"
        if key == "failed":
            bad = [f"{r.get('title') or r.get('name') or r.get('title_id')}: {r.get('reason') or 'failed'}" for r in rows if r.get("status") == "failed"]
            piece += " (" + "; ".join(bad[:3]) + (f"; and {len(bad) - 3} more" if len(bad) > 3 else "") + ")"
        parts.append(piece)
    text = "Run finished: " + (", ".join(parts) or "nothing to do") + f". {duration(seconds)}."
    if free is not None:
        text += f" {size_text(free)} free in the output folder."
    return text, rows, counts


def space_findings(cfg, out_free, work_free, biggest):
    """{"output": message | None, "work": message | None}: a folder is low when the biggest game still to build would not fit (the same rule as
    the Dashboard's folder line); a folder with nothing waiting is low under 10 GiB."""
    floor = 10 * GIB
    res = {"output": None, "work": None}
    if out_free is not None and out_free < max(biggest, floor):
        res["output"] = (f"The output folder is nearly full: {size_text(out_free)} free" +
                         (f", and the biggest game still to build is {size_text(biggest)}." if biggest else "."))
    reserve = int(cfg.get("ssd_reserve_gb") or 0) * GIB
    if work_free is not None and (work_free - reserve <= 0 or (biggest and biggest * 1.1 > work_free - reserve)):
        need = int(biggest * 1.1) + reserve
        res["work"] = f"The work drive is nearly full: {size_text(work_free)} free" + (f", the biggest game still to build needs about {size_text(need)} with what is kept free." if biggest else ".")
    return res
