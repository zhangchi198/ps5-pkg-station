"""Progress and time estimates for a run: what UltraPack's log says about a build, how far along the build is as a
whole, how long it will take, and the same for the run (prepare, PS4 copies, the queue). Standard library only.

Principles:

* Nothing here is a guess about speed. A rate comes from what is happening now (UltraPack's own "@ 71.4 MB/s ETA
  1234s" in its progress lines, or the progress of this build over time) or from builds this install did before
  (the ledger). Until there is such a signal the estimate is None and the page says "estimating".
* UltraPack has five stages, but they are not worth 20 % each: the first (compressing the inner image) is most of the
  build. The overall bar is weighted by how long each stage took in earlier builds (median share per stage, from
  the ledger); defaults stand in until there is history. After the five stages come the engine's own two steps:
  checking the package and copying it to the output folder.
* An estimate that is shown is smoothed (`Smoother`): it moves, it does not jump."""
from __future__ import annotations

import os
import re
import statistics
import threading
from collections import deque

STAGE_KEYS = ("1", "2", "3", "4", "5", "validate", "copy")
# the fraction of a build's time each step takes when this install has no history yet: from one real build (a 233 GiB
# game, level 4, 12 workers: compressing 3094 s, the outer PFS 151 s, stages 2, 4 and 5 together 12 s, then about two
# minutes to check the package). The copy to the output folder only counts with build_on_ssd and is a guess.
DEFAULT_SHARES = {"1": 0.90, "2": 0.002, "3": 0.045, "4": 0.002, "5": 0.006, "validate": 0.03, "copy": 0.015}
# what the page calls each step; UltraPack's own text is kept for a tooltip
STAGE_NAMES = {"1": "Compressing", "2": "Indexing", "3": "Writing the image", "4": "Packaging", "5": "Finalizing",
               "validate": "Checking the package", "copy": "Copying to the output folder"}
MIN_SIGNAL_SECS = float(os.environ.get("PS5PKG_ETA_MIN_SECS") or 120)    # a build gets an estimate once it has run this long (and made some progress)
GIB = 2 ** 30

STAGE_RE = re.compile(r"\[stage (\d)/5\]\s*([^\n\r(:]*)")
# UltraPack prints a bar line for the phase it is in: "[####----] 61% inner image (Kraken) @ 71.4 MB/s ETA 1234s"
BAR_RE = re.compile(r"(\d{1,3})% (source scan|inner files prepared|staging copy|staged in place|inner image|NAPS tables|outer PFS|"
                    r"CNT image|finalize|finalized \.pkg written|validate checklist)\b([^\n\r]*)")
RATE_RE = re.compile(r"@ ([\d.]+) MB/s ETA (\d+)s")
PLAN_RE = re.compile(r"planning nwonly inner image: ([\d,]+) files, ([\d,]+) uncompressed bytes", re.I)
PART_OF = {"inner image": "inner image", "outer PFS": "outer PFS", "finalize": "finalize"}


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- one build's log

class LogTracker:
    """Follows the log of one UltraPack build, reading only what was added since the last look, and keeps what is
    needed to say where the build is: the stage, the progress of the current part, UltraPack's own rate and ETA,
    and when each stage began and ended (to the accuracy of how often it is read).

    `exact` is False for a build this process did not start (adopted after a restart): the stages it went through
    before are not seen, so its timings are not worth keeping."""

    def __init__(self, t0, exact=True):
        self.t0, self.exact = float(t0), exact
        self.offset, self.carry = None, b""
        self.stage, self.stage_text = 1, ""
        self.part, self.pct = "", 0
        self.rate = self.eta = self.bar_at = None          # UltraPack's rate (bytes/s) and ETA (s) as of bar_at
        self.total_bytes = None                            # uncompressed bytes the inner image will hold
        self.started, self.ended, self.complete = {1: self.t0}, {}, set()
        self.samples = deque(maxlen=240)                   # (time, stage, pct) of the current part
        self.finished = False
        self.lock = threading.RLock()

    # -- reading
    def read(self, path, now):
        """Take in what was appended to the log at `path` since the last call. Returns True when anything was new."""
        with self.lock:                                    # the status writer and the build's own thread both look
            try:
                with open(path, "rb") as fh:
                    size = fh.seek(0, 2)
                    if self.offset is None:                # first look: a build we did not start is read from its tail
                        self.offset = 0 if self.exact else max(0, size - (1 << 20))
                    if size < self.offset:                 # replaced: start again
                        self.offset, self.carry = 0, b""
                    fh.seek(self.offset)
                    data = fh.read(8 << 20)
                    self.offset += len(data)
            except OSError:
                return False
            if not data:
                return False
            text = (self.carry + data).replace(b"\r", b"\n")
            text, _, rest = text.rpartition(b"\n")
            self.carry = rest[-65536:]
            self.feed(text.decode("utf-8", "replace"), now)
            return True

    def feed(self, text, now):
        with self.lock:
            for line in text.split("\n"):
                if line.strip():
                    self._line(line, now)

    def _line(self, line, now):
        low = line.lower()
        m = STAGE_RE.search(line)
        if m:
            n = int(m.group(1))
            if n != self.stage:
                self.ended.setdefault(self.stage, now)
                self.stage = n
                self.started.setdefault(n, now)
                self.part, self.pct, self.rate, self.eta, self.bar_at = "", 0, None, None, None
                self.samples.clear()
            if m.group(2).strip():
                self.stage_text = m.group(2).strip()
            if "complete" in low:
                self.complete.add(n)
                self.ended.setdefault(n, now)
        m = PLAN_RE.search(line)
        if m:
            self.total_bytes = int(m.group(2).replace(",", ""))
        m = BAR_RE.search(line)
        if m:
            part = PART_OF.get(m.group(2))
            if part:
                self.part, self.pct = part, min(100, int(m.group(1)))
                self.samples.append((now, self.stage, self.pct))
                r = RATE_RE.search(m.group(3))
                if r:
                    self.rate, self.eta, self.bar_at = float(r.group(1)) * 1e6, int(r.group(2)), now
        if "[success] fpkg built" in low or "build finished" in low:
            self.finished = True
            self.complete.update(range(1, 6))
            for n in range(1, 6):
                self.ended.setdefault(n, now)

    # -- what it says
    def stage_fraction(self):
        """How far the current stage is, 0..1, where the log says so (stages 2 and 4 have no meter)."""
        with self.lock:
            if self.stage in self.complete or self.finished:
                return 1.0
            if self.stage == 1 and self.part == "inner image":
                return self.pct / 100
            if self.stage == 3 and self.part == "outer PFS":
                return self.pct / 100
            if self.stage == 5 and self.part == "finalize":
                return clamp((self.pct - 50) / 50)         # UltraPack scales the last meter to start at 50 %
            return 0.0

    def part_rate(self, now, window=600):
        """Progress of the current part in percent per second, from this build's own history; None without two points."""
        with self.lock:
            pts = [(t, p) for t, s, p in self.samples if s == self.stage and now - t <= window]
            if len(pts) < 2:
                return None
            (t1, p1), (t2, p2) = pts[0], pts[-1]
            if t2 - t1 < 30 or p2 - p1 < 1:
                return None
            return (p2 - p1) / (t2 - t1)

    def local_remaining(self, now):
        """Seconds the current part still needs: UltraPack's own ETA (aged by how long ago it said so), else this
        build's own progress over time; None when neither exists yet."""
        with self.lock:
            frac = self.stage_fraction()
            if self.part and self.eta is not None and self.bar_at is not None and not (self.stage in self.complete):
                return max(0.0, self.eta - (now - self.bar_at))
            pr = self.part_rate(now)
            if pr and self.part and self.stage in (1, 3, 5):
                return max(0.0, (100 - self.pct) / pr)
            return 0.0 if frac >= 1.0 else None

    def timings(self):
        """[{stage, started, ended}] for the stages seen so far."""
        with self.lock:
            return [dict(stage=n, started=self.started[n], ended=self.ended.get(n)) for n in sorted(self.started)]

    def stage_secs(self):
        """{"1": seconds, ...} of the stages that have ended, for the ledger; {} when this tracker did not see them from the start."""
        with self.lock:
            if not self.exact:
                return {}
            return {str(n): round(self.ended[n] - self.started[n], 1) for n in sorted(self.ended) if n in self.started}


# ---------------------------------------------------------------- one build as a whole

def normalise(shares, with_copy=True):
    sh = {k: max(0.0, float(shares.get(k, DEFAULT_SHARES[k]))) for k in STAGE_KEYS}
    if not with_copy:
        sh["copy"] = 0.0
    total = sum(sh.values()) or 1.0
    return {k: v / total for k, v in sh.items()}


def build_progress_fraction(sh, stage, frac, finished=False):
    """Overall progress of the UltraPack part of a build (the five stages, then 'validate' and 'copy' are the
    engine's): the shares of the stages behind it plus the current stage's share times how far it is."""
    if finished:
        return sum(sh[k] for k in STAGE_KEYS[:5])
    stage = min(max(int(stage), 1), 5)
    return clamp(sum(sh[str(n)] for n in range(1, stage)) + sh[str(stage)] * clamp(frac))


HISTORY_FULL_AT = 0.4          # by this much overall progress the estimate is the build's own; before, earlier builds count too
HISTORY_MIN_WEIGHT = 0.3       # what the build's own rate counts for at the very start


def estimate_build(tr, sh, now, hist_total=None):
    """{progress, elapsed, remaining (None until there is signal), rate, stage_name, stage_pct} for a running build.
    `sh` are normalised shares (see `normalise`). `hist_total`: seconds a build of this size took before (this
    install, this compression level), when known. Early in a build the rate has not settled (UltraPack's own ETA
    undershoots while it is still fast), so the history counts for most of the estimate and fades out as the build
    makes progress, to nothing at HISTORY_FULL_AT."""
    stage = tr.stage
    frac = tr.stage_fraction()
    progress = build_progress_fraction(sh, stage, frac, tr.finished)
    elapsed = max(0.0, now - tr.t0)
    out = dict(progress=progress, elapsed=elapsed, remaining=None, rate=tr.rate, stage=stage, stage_pct=round(frac * 100),
               stage_name=STAGE_NAMES.get(str(min(stage, 5)), ""))
    if elapsed < MIN_SIGNAL_SECS or tr.finished:
        return out                                        # not enough signal yet (or UltraPack is done: the caller takes over)
    cur = str(min(stage, 5))
    r_cur = tr.local_remaining(now)
    scale = None                                          # seconds the whole build is worth per unit of share
    if 1 in tr.ended and tr.exact and sh["1"] > 0:
        scale = (tr.ended[1] - tr.started[1]) / sh["1"]
    elif stage == 1 and r_cur is not None and sh["1"] > 0:
        scale = ((now - tr.started[1]) + r_cur) / sh["1"]
    elif progress > 0.05:
        scale = elapsed / progress                         # a build seen from the middle: the average pace so far
    if scale is None:
        return out
    if r_cur is None:
        r_cur = scale * sh[cur] * (1 - frac)
    future = sum(sh[str(n)] for n in range(stage + 1, 6)) + sh["validate"] + sh["copy"]
    remaining = max(0.0, r_cur + scale * future)
    if hist_total and hist_total > 0:
        w = clamp(progress / HISTORY_FULL_AT, HISTORY_MIN_WEIGHT, 1.0)
        remaining = w * remaining + (1 - w) * max(0.0, hist_total - elapsed)
    out["remaining"] = remaining
    return out


def finishing_progress(sh, phase, frac=0.0):
    """Overall progress of a build whose UltraPack has finished: checking the package, then copying it."""
    base = sum(sh[k] for k in STAGE_KEYS[:5])
    if phase == "validating":
        return clamp(base + sh["validate"] * 0.5)
    return clamp(base + sh["validate"] + sh["copy"] * clamp(frac))


# ---------------------------------------------------------------- history (the ledger)

def median_of(values):
    return statistics.median(values) if values else None


def history(ledger, level=None):
    """What earlier builds of this install say: the median share of each step ("shares", None until there is a
    build with its stages recorded), seconds per source GiB to build ("build_per_gib", for this compression level),
    to prepare ("prep_per_gib") and to copy a PS4 title ("ps4_per_gib")."""
    shares_by, build, prep, ps4 = {k: [] for k in STAGE_KEYS}, [], [], []
    n_shares = 0
    for v in (ledger or {}).values():
        if not isinstance(v, dict):
            continue
        size = v.get("source_bytes")
        gib = size / GIB if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0 else None
        if v.get("ps4"):
            secs = v.get("copy_secs")
            if gib and isinstance(secs, (int, float)) and secs > 0:
                ps4.append(secs / gib)
            continue
        ss = v.get("stage_secs")
        if isinstance(ss, dict) and all(isinstance(ss.get(k), (int, float)) for k in ("1", "2", "3", "4", "5")):
            vals = {k: float(ss.get(k) or 0) for k in STAGE_KEYS}
            total = sum(vals.values())
            if total > 0:
                n_shares += 1
                for k in STAGE_KEYS:
                    shares_by[k].append(vals[k] / total)
                if gib and (level is None or v.get("level") == level):
                    build.append(total / gib)
        if gib and isinstance(v.get("prep_min"), (int, float)) and v["prep_min"] > 0:
            prep.append(v["prep_min"] * 60 / gib)
    return dict(shares={k: median_of(shares_by[k]) for k in STAGE_KEYS} if n_shares else None, builds=n_shares,
                build_per_gib=median_of(build), prep_per_gib=median_of(prep), ps4_per_gib=median_of(ps4))


def shares_from(hist):
    sh = dict(DEFAULT_SHARES)
    if hist and hist.get("shares"):
        sh.update({k: v for k, v in hist["shares"].items() if v is not None})
    return sh


# ---------------------------------------------------------------- the whole run

def simulate(now, running, queue, *, max_builds, max_compress, prep_free_at=None):
    """When the last build of the run is done, or None when it can't be said. `running`: [{end, kraken_end}]
    for builds in flight (times); `queue`: [{prep, total, kraken}] seconds for each game still to come, in order;
    `prep_free_at`: when the prepare worker is next free. Prepares run one at a time ahead of the builds, a build
    starts when it is prepared and has a process slot and a compression slot."""
    procs = sorted(r["end"] for r in running)                           # when each process slot frees
    kraken = sorted(r["kraken_end"] for r in running)
    free_p = [now] * max(0, max_builds - len(procs)) + procs
    free_k = [now] * max(0, max_compress - len(kraken)) + kraken
    if not free_p or not free_k:
        return None
    free_p.sort()
    free_k.sort()
    prep_at = max(now, prep_free_at or now)
    end = max([r["end"] for r in running] or [now])
    for job in queue:
        prep_at += job["prep"]
        start = max(prep_at, free_p[0], free_k[0])
        free_p[0] = start + job["total"]
        free_k[0] = start + job["kraken"]
        free_p.sort()
        free_k.sort()
        end = max(end, start + job["total"])
    return end


class Smoother:
    """Shows an estimate that moves, not jumps: the displayed finish time follows the raw one at a fraction of the
    gap, and by at most `slew` seconds per `tick`. With no new raw estimate it keeps counting down."""

    def __init__(self, alpha=0.25, slew=20.0, tick=5.0):
        self.alpha, self.slew, self.tick = alpha, slew, tick
        self.finish = self.at = None

    def update(self, raw_remaining, now):
        """The remaining seconds to show (None until there has been a raw estimate)."""
        if raw_remaining is not None:
            raw = now + raw_remaining
            if self.finish is None:
                self.finish = raw
            else:
                steps = max(0.2, (now - self.at) / self.tick)
                step = (raw - self.finish) * (1 - (1 - self.alpha) ** steps)
                limit = self.slew * steps
                self.finish += max(-limit, min(limit, step))
            self.at = now
        if self.finish is None:
            return None
        return max(0.0, self.finish - now)


def rate_of(samples, now, window=60.0):
    """Bytes per second from [(time, bytes)] over the last `window` seconds; None with less than two points or no growth."""
    pts = [(t, b) for t, b in samples if now - t <= window]
    if len(pts) < 2:
        return None
    (t1, b1), (t2, b2) = pts[0], pts[-1]
    if t2 - t1 < 3 or b2 < b1:
        return None
    return (b2 - b1) / (t2 - t1) if b2 > b1 else None


# ---------------------------------------------------------------- words

def human_duration(secs):
    """'about' is the caller's word: 3 s, 25 min, 1 h 20 min."""
    secs = max(0, int(round(secs)))
    if secs < 90:
        return f"{secs} s"
    mins = round(secs / 60)
    if mins < 60:
        return f"{mins} min"
    h, m = divmod(mins, 60)
    return f"{h} h {m:02d} min" if m else f"{h} h"
