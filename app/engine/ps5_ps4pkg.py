"""Reading the header of a PS4 package (.pkg), to name and classify it by what it says rather than by its file name.

A PS4 package starts with the magic 0x7F 'C' 'N' 'T' and holds, big-endian, a content ID at 0x40
("UP9000-CUSA12345_00-GAMENAME0000000A": region and publisher, the CUSA title ID, a counter, a label) and an entry
table; the entry 0x1000 is PARAM.SFO, whose TITLE, TITLE_ID, CONTENT_ID, CATEGORY (gd: game, gp: patch, ac: add-on
content) and APP_VER say what the package is. Only the first few MiB are needed, so a caller can stream just the head
of a member out of an archive. This module only parses bytes; it never writes. Standard library only."""
from __future__ import annotations

import re
import struct

MAGIC = b"\x7fCNT"
PS5_MAGIC = b"\x7fFIH"                      # PS5 packages: not ours
CONTENT_ID_AT, ENTRY_TABLE = 0x40, 0x18
ENTRY_PARAM_SFO = 0x1000
CONTENT_ID_RE = re.compile(r"^[A-Z]{2}\d{4}-((?:CUSA|PPSA)\d{5})_\d{2}-[A-Z0-9]{16}$")
TITLE_ID_RE = re.compile(r"(CUSA\d{5})")
HEAD_BYTES = 4 << 20


def parse_sfo(data):
    """PARAM.SFO as {key: str | int}; {} when it isn't one."""
    if len(data) < 0x14 or data[:4] != b"\x00PSF":
        return {}
    key_start, data_start, count = struct.unpack_from("<III", data, 0x08)
    out = {}
    for i in range(min(count, 512)):
        base = 0x14 + 16 * i
        if base + 16 > len(data):
            break
        key_off, fmt, length, _max, data_off = struct.unpack_from("<HHIII", data, base)
        kend = data.find(b"\x00", key_start + key_off)
        if kend < 0:
            continue
        key = data[key_start + key_off:kend].decode("ascii", "replace")
        raw = data[data_start + data_off:data_start + data_off + length]
        if fmt in (0x0004, 0x0204):
            out[key] = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        elif fmt == 0x0404 and len(raw) >= 4:
            out[key] = struct.unpack("<I", raw[:4])[0]
    return out


def parse_header(buf):
    """What the first bytes of a package say: {"ps4": True, content_id, title_id, content_type, sfo} for a PS4
    package, {"ps4": False} for another kind of package or any other file, None when there are too few bytes to tell.
    `sfo` is {} when PARAM.SFO is not within the bytes given."""
    if len(buf) < 0x80:
        return None
    if buf[:4] != MAGIC:
        return {"ps4": False}
    cid = buf[CONTENT_ID_AT:CONTENT_ID_AT + 0x24].split(b"\x00", 1)[0].decode("ascii", "replace")
    m = CONTENT_ID_RE.match(cid)
    sfo = {}
    count, table = struct.unpack_from(">I", buf, 0x10)[0], struct.unpack_from(">I", buf, ENTRY_TABLE)[0]
    for i in range(min(count, 1024)):
        at = table + 32 * i
        if at + 32 > len(buf):
            break
        eid, _name, _f1, _f2, off, size = struct.unpack_from(">IIIIII", buf, at)
        if eid == ENTRY_PARAM_SFO:
            if 0 < size <= 1 << 20 and off + size <= len(buf):
                sfo = parse_sfo(buf[off:off + size])
            break
    tid = (m.group(1) if m and m.group(1).startswith("CUSA") else None) or str(sfo.get("TITLE_ID") or "") or None
    return {"ps4": True, "content_id": cid, "title_id": tid if tid and tid.startswith("CUSA") else None,
            "content_type": struct.unpack_from(">I", buf, 0x74)[0], "sfo": sfo}


def version_of(sfo):
    """The version a package carries ("01.04"): APP_VER, else VERSION; "" when it says none."""
    for k in ("APP_VER", "VERSION"):
        v = str(sfo.get(k) or "").strip()
        if re.fullmatch(r"\d{1,2}\.\d{2}", v):
            return v
    return ""


def category_kind(category):
    """game / patch / dlc / other from PARAM.SFO's CATEGORY."""
    c = str(category or "").lower()
    if c.startswith("gd"):
        return "game"
    if c == "gp":
        return "patch"
    if c.startswith("ac"):
        return "dlc"
    return "other"


def kind_from_name(name, size=0):
    """The same by the file name, for a package whose header could not be read: PS4 update packages are named
    ...-A0104-V0100.pkg (the A is the app version: A0100 is the base game, anything above is an update)."""
    m = re.search(r"-A(\d{2})(\d{2})-V\d{4}", name)
    if m:
        return "game" if (m.group(1), m.group(2)) == ("01", "00") else "patch"
    if re.search(r"patch|update|upd\b", name, re.I):
        return "patch"
    if re.search(r"dlc|addon|add-on", name, re.I):
        return "dlc"
    return "other"
