#!/usr/bin/env python3
"""Read-only PS5 library audit for AMPR LZ4 / Zstd asset packs.

Every source file is opened with os.O_RDONLY only.  Nothing on the source
volume is ever written, renamed or touched.  Images are parsed in user space
(exFAT, PFS, PFSC) so no image has to be mounted or copied: only directory
clusters, small metadata files, AMPR manifests, data-volume headers and a few
sampled chunks are read over the network.

Formats implemented from these references (fetched 2026-11-22):
  * AMPRPAK4 / AMPRDAT3 / AMPRCRC1 / AMPRIDX3 - drakmor/ampr_emu
    tools/ampr_pack_format.py @ cfa85df (2026-09-06)
  * AMPRZPK1 / AMPRZDT1 - quer3q/ampr-emu-ng crates/ampr-zstd + ampr-format
    @ d20210e (2026-11-20)
  * PFS / PFSC (.ffpfsc) - PSBrew/MkPFS mkpfs/consts.py + pfs.py
  * exFAT - Microsoft exFAT specification
  * LZ4 raw block - decoded with liblz4 LZ4_decompress_safe (same call the
    ampr_emu packer's verify uses); Zstandard - RFC 8878 frame magic +
    libzstd ZSTD_decompress.
"""
from __future__ import annotations

import binascii
import ctypes
import ctypes.util
import json
import os
import re
import struct
import sys
import time
import traceback
import zlib
from collections import OrderedDict

# --------------------------------------------------------------------------
# byte sources (all read-only)
# --------------------------------------------------------------------------

PAGE = 1 << 16


class Stats:
    network_bytes = 0


class FileSource:
    """Random access over a file opened O_RDONLY, with a small page cache."""

    def __init__(self, path: str, cache_pages: int = 256):
        self.path = path
        self.fd = os.open(path, os.O_RDONLY)
        self.size = os.fstat(self.fd).st_size
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.cache_pages = cache_pages

    def _page(self, idx: int) -> bytes:
        page = self.cache.get(idx)
        if page is not None:
            self.cache.move_to_end(idx)
            return page
        page = os.pread(self.fd, PAGE, idx * PAGE)
        Stats.network_bytes += len(page)
        self.cache[idx] = page
        if len(self.cache) > self.cache_pages:
            self.cache.popitem(last=False)
        return page

    def read(self, off: int, n: int) -> bytes:
        if n <= 0 or off >= self.size:
            return b""
        n = min(n, self.size - off)
        if n > 4 * PAGE:  # large read: bypass cache
            out = os.pread(self.fd, n, off)
            Stats.network_bytes += len(out)
            return out
        out = bytearray()
        while n > 0:
            idx, inner = divmod(off, PAGE)
            page = self._page(idx)
            take = min(n, len(page) - inner)
            if take <= 0:
                break
            out += page[inner:inner + take]
            off += take
            n -= take
        return bytes(out)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class SliceSource:
    def __init__(self, base, off: int, size: int):
        self.base, self.off, self.size = base, off, size

    def read(self, off: int, n: int) -> bytes:
        if off >= self.size:
            return b""
        return self.base.read(self.off + off, min(n, self.size - off))


class PfscSource:
    """Logical view of a PFSC-compressed PFS file (zlib blocks)."""

    MAGIC = 0x43534650  # "PFSC"

    def __init__(self, base, off: int, stored: int, logical: int):
        self.base, self.off, self.stored = base, off, stored
        hdr = base.read(off, 0x30)
        (magic, _u4, _u8, bs, bs2, boo, data_off, data_len) = struct.unpack("<iiiiqqQq", hdr)
        if magic != self.MAGIC:
            raise ValueError("PFSC magic missing on compressed inode")
        if bs <= 0 or bs != bs2:
            raise ValueError(f"unexpected PFSC block size {bs}/{bs2}")
        self.bs, self.boo, self.data_len = bs, boo, data_len
        self.size = logical
        self.nblocks = (data_len + bs - 1) // bs
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.raw_blocks = self.zlib_blocks = self.zero_blocks = 0

    def _block(self, i: int) -> bytes:
        b = self.cache.get(i)
        if b is not None:
            self.cache.move_to_end(i)
            return b
        o0, o1 = struct.unpack("<QQ", self.base.read(self.off + self.boo + 8 * i, 16))
        span = o1 - o0
        if span == 0:
            b = bytes(self.bs)
            self.zero_blocks += 1
        elif span == self.bs:
            b = self.base.read(self.off + o0, span)
            self.raw_blocks += 1
        elif span < self.bs:
            data = self.base.read(self.off + o0, span)
            try:
                b = zlib.decompress(data)
            except zlib.error:
                b = zlib.decompress(data, -15)
            self.zlib_blocks += 1
            if len(b) < self.bs:
                b = b + bytes(self.bs - len(b))
        else:
            raise ValueError(f"PFSC block {i} span {span} exceeds block size")
        self.cache[i] = b
        if len(self.cache) > 96:
            self.cache.popitem(last=False)
        return b

    def read(self, off: int, n: int) -> bytes:
        if off >= self.size:
            return b""
        n = min(n, self.size - off)
        out = bytearray()
        while n > 0:
            i, inner = divmod(off, self.bs)
            blk = self._block(i)
            take = min(n, self.bs - inner)
            out += blk[inner:inner + take]
            off += take
            n -= take
        return bytes(out)


# --------------------------------------------------------------------------
# file systems: each exposes .files {relpath: entry} and .read(entry, off, n)
# --------------------------------------------------------------------------


class Entry:
    __slots__ = ("path", "size", "loc")

    def __init__(self, path, size, loc):
        self.path, self.size, self.loc = path, size, loc


class DirFS:
    kind = "folder"

    def __init__(self, root: str):
        self.root = root
        self.files: dict[str, Entry] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                self.files[rel] = Entry(rel, st.st_size, full)
        self._open: dict[str, FileSource] = {}

    def source(self, e: Entry):
        src = self._open.get(e.loc)
        if src is None:
            src = FileSource(e.loc, cache_pages=32)
            self._open[e.loc] = src
        return src

    def read(self, e: Entry, off: int, n: int) -> bytes:
        return self.source(e).read(off, n)

    def close(self):
        for s in self._open.values():
            s.close()


class ExfatFS:
    kind = "exFAT"

    def __init__(self, src):
        self.src = src
        bs = src.read(0, 512)
        if bs[3:11] != b"EXFAT   ":
            raise ValueError("not an exFAT boot sector")
        (self.fat_off, self.fat_len, heap_off, self.ccount, self.root) = struct.unpack_from("<IIIII", bs, 0x50)
        bps = 1 << bs[0x6C]
        spc = 1 << bs[0x6D]
        self.cs = bps * spc
        self.fat_base = self.fat_off * bps
        self.heap = heap_off * bps
        self.files: dict[str, Entry] = {}
        self.dirs = 0
        self._walk(self.root, False, None, "", 0)

    def _next(self, c: int) -> int:
        return struct.unpack("<I", self.src.read(self.fat_base + 4 * c, 4))[0]

    def _chain(self, first: int, limit_bytes: int | None):
        clusters = []
        c = first
        maxc = None if limit_bytes is None else (limit_bytes + self.cs - 1) // self.cs
        while 2 <= c < 0xFFFFFFF7:
            clusters.append(c)
            if maxc is not None and len(clusters) >= maxc:
                break
            if len(clusters) > self.ccount:
                raise ValueError("exFAT FAT chain loop")
            c = self._next(c)
        return clusters

    def _coff(self, c: int) -> int:
        return self.heap + (c - 2) * self.cs

    def _read_extent(self, first, nofat, size, off, n, chain_cache=None):
        if off >= size:
            return b""
        n = min(n, size - off)
        if first == 0:
            return b""
        if nofat:
            out = self.src.read(self._coff(first) + off, n)
            if len(out) != n:
                raise ValueError("exFAT short read (beyond container end)")
            return out
        chain = chain_cache if chain_cache is not None else self._chain_cached(first, size)
        out = bytearray()
        while n > 0:
            ci, inner = divmod(off, self.cs)
            # coalesce physically contiguous clusters into one read
            j = ci
            need_clusters = (inner + n + self.cs - 1) // self.cs
            while j + 1 < len(chain) and j - ci + 1 < need_clusters and chain[j + 1] == chain[j] + 1:
                j += 1
            take = min(n, (j - ci + 1) * self.cs - inner)
            piece = self.src.read(self._coff(chain[ci]) + inner, take)
            if len(piece) != take:
                raise ValueError("exFAT short read (beyond container end)")
            out += piece
            off += take
            n -= take
        return bytes(out)

    def _chain_cached(self, first, size):
        cache = self.__dict__.setdefault("_chains", OrderedDict())
        key = (first, size)
        ch = cache.get(key)
        if ch is None:
            ch = self._chain(first, size)
            cache[key] = ch
            if len(cache) > 64:
                cache.popitem(last=False)
        return ch

    def _walk(self, first, nofat, size, prefix, depth):
        if depth > 64:
            raise ValueError("exFAT directory depth exceeded")
        self.dirs += 1
        if size is None:  # root: follow FAT chain
            chain = self._chain(first, None)
            data = b"".join(self.src.read(self._coff(c), self.cs) for c in chain)
        else:
            data = self._read_extent(first, nofat, size, 0, size)
        i, n = 0, len(data) // 32
        while i < n:
            e = data[i * 32:(i + 1) * 32]
            t = e[0]
            if t == 0x00:
                break
            if t != 0x85:
                i += 1
                continue
            sc = e[1]
            attr = struct.unpack_from("<H", e, 4)[0]
            grp = data[(i + 1) * 32:(i + 1 + sc) * 32]
            i += 1 + sc
            if len(grp) < 64 or grp[0] != 0xC0:
                continue
            flags, namelen = grp[1], grp[3]
            valid = struct.unpack_from("<Q", grp, 8)[0]
            fc = struct.unpack_from("<I", grp, 20)[0]
            dlen = struct.unpack_from("<Q", grp, 24)[0]
            name_u16 = b""
            for k in range(1, sc):
                ne = grp[k * 32:(k + 1) * 32]
                if ne[0] == 0xC1:
                    name_u16 += ne[2:32]
            name = name_u16[: namelen * 2].decode("utf-16-le", "replace")
            path = f"{prefix}{name}"
            nofat_e = bool(flags & 0x02)
            if attr & 0x10:
                self._walk(fc, nofat_e, dlen, path + "/", depth + 1)
            else:
                self.files[path] = Entry(path, valid, (fc, nofat_e, valid))

    def source(self, e: Entry):
        fc, nofat, size = e.loc
        fs = self
        chain = None if nofat else self._chain(fc, size)

        class _S:
            def __init__(s):
                s.size = size

            def read(s, off, n):
                return fs._read_extent(fc, nofat, size, off, n, chain)

        return _S()

    def read(self, e: Entry, off: int, n: int) -> bytes:
        fc, nofat, size = e.loc
        return self._read_extent(fc, nofat, size, off, n)

    def close(self):
        pass


class PfsFS:
    kind = "PFS"
    MAGIC = 20130315

    def __init__(self, src):
        self.src = src
        h = src.read(0, 0x58)
        version, magic = struct.unpack_from("<QQ", h, 0)
        if magic != self.MAGIC:
            raise ValueError("not a PFS image")
        self.mode = struct.unpack_from("<H", h, 0x1C)[0]
        self.bs = struct.unpack_from("<I", h, 0x20)[0]
        self.ninodes = struct.unpack_from("<Q", h, 0x30)[0]
        self.superroot = struct.unpack_from("<Q", h, 0x48)[0]
        if self.mode & 0x4:
            raise ValueError("PFS image is encrypted (mode 0x%x)" % self.mode)
        if self.mode & 0x3:
            raise ValueError("PFS signed/64-bit inode layout (mode 0x%x) not supported by this reader" % self.mode)
        self.isz = 0xA8
        self.per_block = self.bs // self.isz
        self.files: dict[str, Entry] = {}
        self.top: dict[str, Entry] = {}
        self._walk(self.superroot, "", 0, True)

    def inode(self, ino: int):
        blk, idx = divmod(ino, self.per_block)
        raw = self.src.read((1 + blk) * self.bs + idx * self.isz, self.isz)
        mode, nlink, flags = struct.unpack_from("<HHI", raw, 0)
        size, csize = struct.unpack_from("<qq", raw, 8)
        blocks = struct.unpack_from("<I", raw, 0x60)[0]
        db = struct.unpack_from("<12i", raw, 0x64)
        return dict(mode=mode, flags=flags, size=size, csize=csize, blocks=blocks, db=db)

    def _data(self, ino_d, n=None):
        # For compressed inodes the two size fields hold stored and logical
        # lengths; MkPFS output was observed with them in the opposite order to
        # LibOrbisPkg's naming, so take stored = smaller, logical = larger and
        # confirm logical against the PFSC header in source().
        if ino_d["flags"] & 1:
            stored = min(ino_d["size"], ino_d["csize"])
        else:
            stored = ino_d["size"]
        start = ino_d["db"][0] * self.bs
        db = ino_d["db"]
        for k in range(1, min(12, ino_d["blocks"])):
            if db[k] not in (0, -1) and db[k] != db[0] + k:
                raise ValueError("non-contiguous PFS file; reader assumes contiguous blocks")
        return start, stored

    def _walk(self, ino, prefix, depth, is_super):
        if depth > 64:
            raise ValueError("PFS depth exceeded")
        d = self.inode(ino)
        start, stored = self._data(d)
        data = self.src.read(start, max(stored, d["blocks"] * self.bs))
        for b in range(0, len(data), self.bs):
            blk = data[b:b + self.bs]
            p = 0
            while p + 16 <= len(blk):
                cino, typ, nlen, esz = struct.unpack_from("<Iiii", blk, p)
                if esz <= 0:
                    break
                name = blk[p + 16:p + 16 + nlen].decode("utf-8", "replace")
                p += esz
                if typ in (4, 5) or name in (".", ".."):
                    continue
                path = prefix + name
                if typ == 3:
                    self._walk(cino, path + "/", depth + 1, False)
                elif typ == 2:
                    cd = self.inode(cino)
                    logical = max(cd["size"], cd["csize"]) if cd["flags"] & 1 else cd["size"]
                    e = Entry(path, logical, (cino, cd))
                    self.files[path] = e

    def source(self, e: Entry):
        _ino, d = e.loc
        start, stored = self._data(d)
        if d["flags"] & 1:
            logical = max(d["size"], d["csize"])
            src = PfscSource(self.src, start, stored, logical)
            if src.data_len != logical:
                raise ValueError(f"PFSC data length {src.data_len} != inode logical size {logical}")
            return src
        return SliceSource(self.src, start, d["size"])

    def read(self, e: Entry, off, n):
        return self.source(e).read(off, n)

    def close(self):
        pass


class UfsFS:
    """FreeBSD UFS2 (little-endian) reader: superblock at 64 KiB, 256-byte dinodes."""

    kind = "UFS2"

    def __init__(self, src):
        self.src = src
        sb = src.read(65536, 1536)
        if struct.unpack_from("<I", sb, 0x55C)[0] != 0x19540119:
            raise ValueError("UFS2 superblock magic missing")
        g = lambda o: struct.unpack_from("<i", sb, o)[0]
        self.iblkno, self.bsize, self.fsize, self.frag = g(16), g(48), g(52), g(56)
        self.nindir, self.inopb, self.ipg, self.fpg = g(116), g(120), g(184), g(188)
        if self.bsize <= 0 or self.fsize <= 0 or self.inopb != self.bsize // 256 or self.nindir != self.bsize // 8:
            raise ValueError("UFS2 superblock geometry unexpected")
        self.ind_cache: OrderedDict[int, tuple] = OrderedDict()
        self.files: dict[str, Entry] = {}
        self._walk(2, "", 0)

    def inode(self, ino):
        cg, rem = divmod(ino, self.ipg)
        fsba = cg * self.fpg + self.iblkno + (rem // self.inopb) * self.frag
        raw = self.src.read(fsba * self.fsize + (rem % self.inopb) * 256, 256)
        mode = struct.unpack_from("<H", raw, 0)[0]
        size = struct.unpack_from("<Q", raw, 16)[0]
        flags = struct.unpack_from("<I", raw, 88)[0]
        db = struct.unpack_from("<12q", raw, 112)
        ib = struct.unpack_from("<3q", raw, 208)
        return dict(mode=mode, size=size, db=db, ib=ib, flags=flags)

    def _ind(self, fragaddr):
        t = self.ind_cache.get(fragaddr)
        if t is None:
            t = struct.unpack(f"<{self.nindir}q", self.src.read(fragaddr * self.fsize, self.bsize))
            self.ind_cache[fragaddr] = t
            if len(self.ind_cache) > 64:
                self.ind_cache.popitem(last=False)
        return t

    def bmap(self, d, lbn):
        if lbn < 12:
            return d["db"][lbn]
        lbn -= 12
        n = self.nindir
        if lbn < n:
            return self._ind(d["ib"][0])[lbn] if d["ib"][0] else 0
        lbn -= n
        if lbn < n * n:
            if not d["ib"][1]:
                return 0
            l1 = self._ind(d["ib"][1])[lbn // n]
            return self._ind(l1)[lbn % n] if l1 else 0
        lbn -= n * n
        if not d["ib"][2]:
            return 0
        l1 = self._ind(d["ib"][2])[lbn // (n * n)]
        if not l1:
            return 0
        l2 = self._ind(l1)[(lbn // n) % n]
        return self._ind(l2)[lbn % n] if l2 else 0

    def _read(self, d, off, n):
        size = d["size"]
        if off >= size:
            return b""
        n = min(n, size - off)
        out = bytearray()
        while n > 0:
            lbn, inner = divmod(off, self.bsize)
            take = min(n, self.bsize - inner)
            pa = self.bmap(d, lbn)
            if pa == 0:
                out += bytes(take)
            else:
                piece = self.src.read(pa * self.fsize + inner, take)
                if len(piece) != take:
                    raise ValueError(f"UFS2 short read at fragment {pa} (beyond container end)")
                out += piece
            off += take
            n -= take
        return bytes(out)

    def _walk(self, ino, prefix, depth):
        if depth > 64:
            raise ValueError("UFS2 depth exceeded")
        d = self.inode(ino)
        data = self._read(d, 0, d["size"])
        p = 0
        while p + 8 <= len(data):
            cino, reclen, typ, nlen = struct.unpack_from("<IHBB", data, p)
            if reclen == 0:
                break
            name = data[p + 8:p + 8 + nlen].decode("utf-8", "replace")
            p += reclen
            if cino == 0 or name in (".", ".."):
                continue
            path = prefix + name
            if typ == 4:
                self._walk(cino, path + "/", depth + 1)
            elif typ == 8:
                cd = self.inode(cino)
                self.files[path] = Entry(path, cd["size"], (cino, cd))

    def source(self, e):
        fs, d = self, e.loc[1]

        class _S:
            size = d["size"]

            def read(s, off, n):
                return fs._read(d, off, n)

        return _S()

    def read(self, e, off, n):
        return self._read(e.loc[1], off, n)

    def close(self):
        pass


def sniff(src) -> str:
    head = src.read(0, 512)
    if head[3:11] == b"EXFAT   ":
        return "exfat"
    if len(head) >= 16 and struct.unpack_from("<Q", head, 8)[0] == PfsFS.MAGIC:
        return "pfs"
    if head[:4] == b"\x7fCNT" or head[:4] == b"\x7fFIH":
        return "pkg"
    # UFS2 superblock magic 0x19540119 at 0x10000+0x55c
    sb = src.read(0x10000 + 0x55C, 4)
    if len(sb) == 4 and struct.unpack("<I", sb)[0] == 0x19540119:
        return "ufs2"
    return "unknown"


class Unwrapped:
    def __init__(self, fs, chain, root_prefix, pfsc_src=None):
        self.fs, self.chain, self.root, self.pfsc = fs, chain, root_prefix, pfsc_src


def open_game_fs(path: str):
    """Return (fs, chain description, root prefix within fs, base source)."""
    if os.path.isdir(path):
        return Unwrapped(DirFS(path), ["folder"], ""), None
    base = FileSource(path)
    kind = sniff(base)
    chain = []
    src = base
    pfsc = None
    for _ in range(4):
        if kind == "exfat":
            fs = ExfatFS(src)
            chain.append("exFAT")
            return Unwrapped(fs, chain, find_game_root(fs), pfsc), base
        if kind == "ufs2":
            fs = UfsFS(src)
            chain.append("UFS2")
            return Unwrapped(fs, chain, find_game_root(fs), pfsc), base
        if kind == "pfs":
            pfs = PfsFS(src)
            chain.append(f"PFS(mode=0x{pfs.mode:x})")
            root = find_game_root(pfs)
            if root is not None:
                return Unwrapped(pfs, chain, root, pfsc), base
            # nested image: the largest file in the container
            nested = sorted(pfs.files.values(), key=lambda e: -e.size)
            if not nested:
                raise ValueError("PFS container is empty")
            ne = nested[0]
            src = pfs.source(ne)
            if isinstance(src, PfscSource):
                pfsc = src
                chain.append(f"PFSC-zlib nested file '{ne.path}' ({ne.size} B logical)")
            else:
                chain.append(f"nested file '{ne.path}'")
            kind = sniff(src)
            continue
        raise ValueError(f"unsupported image type: {kind}")
    raise ValueError("too many nested containers")


def find_game_root(fs):
    keys = {k.lower(): k for k in fs.files}
    cands = [k for k in keys if k.endswith("sce_sys/param.json") or k.endswith("sce_sys/param.sfo")]
    if not cands:
        return None
    best = min(cands, key=lambda k: k.count("/"))
    return keys[best][: len(best) - len("sce_sys/param.json")]


# --------------------------------------------------------------------------
# codecs (reference libraries)
# --------------------------------------------------------------------------


def _load(name, fallback):
    p = ctypes.util.find_library(name) or fallback
    return ctypes.CDLL(p)


LZ4 = _load("lz4", "/opt/homebrew/lib/liblz4.dylib")
LZ4.LZ4_decompress_safe.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
LZ4.LZ4_decompress_safe.restype = ctypes.c_int
LZ4.LZ4_versionString.restype = ctypes.c_char_p
ZSTD = _load("zstd", "/opt/homebrew/lib/libzstd.dylib")
ZSTD.ZSTD_decompress.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]
ZSTD.ZSTD_decompress.restype = ctypes.c_size_t
ZSTD.ZSTD_isError.argtypes = [ctypes.c_size_t]
ZSTD.ZSTD_isError.restype = ctypes.c_uint
ZSTD.ZSTD_versionString.restype = ctypes.c_char_p


def lz4_block_decode(data: bytes, raw_size: int) -> bytes:
    dst = ctypes.create_string_buffer(raw_size)
    got = LZ4.LZ4_decompress_safe(data, dst, len(data), raw_size)
    if got != raw_size:
        raise ValueError(f"LZ4_decompress_safe returned {got}, expected {raw_size}")
    return dst.raw[:got]


ZSTD_FRAME_MAGIC = b"\x28\xb5\x2f\xfd"  # RFC 8878 0xFD2FB528 LE


def zstd_decode(data: bytes, raw_size: int) -> bytes:
    if data[:4] != ZSTD_FRAME_MAGIC:
        raise ValueError("chunk does not start with a Zstandard frame magic")
    dst = ctypes.create_string_buffer(raw_size)
    got = ZSTD.ZSTD_decompress(dst, raw_size, data, len(data))
    if ZSTD.ZSTD_isError(got) or got != raw_size:
        raise ValueError(f"ZSTD_decompress returned {got}, expected {raw_size}")
    return dst.raw[:got]


def crc32(b) -> int:
    return binascii.crc32(b) & 0xFFFFFFFF


# --------------------------------------------------------------------------
# AMPR parsers
# --------------------------------------------------------------------------

INDEX_HEADER = struct.Struct("<8sIIII16sQQIIIIQQQQQIIQ")
FILE_RECORD = struct.Struct("<QQqIIIIIBBH")
PACK_RECORD = struct.Struct("<QQIIII")
DATA_HEADER = struct.Struct("<8sIIII16sQQII")
CRC_HEADER = struct.Struct("<8sII16sQII")


def parse_ampridx3(data: bytes):
    magic, ver, esz, cnt, pbytes, hoff, hss, hsc = struct.unpack_from("<8sIIQQQII", data)
    if magic != b"AMPRIDX3" or ver != 3 or esz != 24:
        raise ValueError("bad AMPRIDX3 header")
    poff = 48 + cnt * 24
    out = []
    for i in range(cnt):
        pa, pl, size, mt = struct.unpack_from("<IIQq", data, 48 + i * 24)
        out.append((data[poff + pa:poff + pa + pl].decode("utf-8", "replace"), size))
    return out


def parse_amprpak4(data: bytes):
    """Return dict with files/chunks/packs per drakmor ampr_pack_format.py."""
    if len(data) < 128:
        raise ValueError("AMPRPAK4 truncated")
    v = INDEX_HEADER.unpack_from(data)
    (magic, version, hsize, flags, endian, build_id, fcount, ccount, pcount, frs, crs, prs,
     foff, coff, poff, soff, ssize, pcrc, hcrc, reserved) = v
    errs = []
    if magic != b"AMPRPAK4" or version != 4:
        raise ValueError(f"unsupported manifest {magic!r} v{version}")
    if hsize != 128 or endian != 0x01020304 or reserved != 0 or flags != 0:
        errs.append("header fields invalid")
    if (frs, crs, prs) != (48, 12, 32):
        raise ValueError("unexpected record sizes")
    hc = bytearray(data[:128])
    struct.pack_into("<I", hc, 116, 0)
    hdr_crc_ok = crc32(hc) == hcrc
    pay_crc_ok = crc32(data[128:]) == pcrc
    if not hdr_crc_ok:
        errs.append("manifest header CRC mismatch")
    if not pay_crc_ok:
        errs.append("manifest payload CRC mismatch")
    if not (foff == 128 and coff == foff + fcount * 48 and poff == coff + ccount * 12
            and soff == poff + pcount * 32 and soff + ssize == len(data)):
        errs.append("manifest section layout invalid")
    strings = data[soff:]

    def s(o, l):
        return strings[o:o + l].decode("utf-8", "replace")

    files = []
    for i in range(fcount):
        (ph, lsize, mt, first, cnt, po, pl, fflags, bshift, pclass, _r) = FILE_RECORD.unpack_from(data, foff + i * 48)
        files.append(dict(id=i + 1, path=s(po, pl), size=lsize, first=first, count=cnt,
                          flags=fflags, shift=bshift))
    chunk_raw = data[coff:coff + ccount * 12]
    packs = []
    for i in range(pcount):
        pb, fsz, no, nl, pf, iop = PACK_RECORD.unpack_from(data, poff + i * 32)
        packs.append(dict(id=i, name=s(no, nl), payload=pb, file_size=fsz, flags=pf, io_page=iop))
    return dict(build_id=build_id, files=files, chunks=chunk_raw, ccount=ccount, packs=packs,
                errors=errs, crc_ok=hdr_crc_ok and pay_crc_ok)


def pak4_chunk(chunks: bytes, idx: int):
    loc, desc = struct.unpack_from("<QI", chunks, idx * 12)
    return dict(offset=loc & ((1 << 48) - 1), pack=(loc >> 48) & 0xFFFF,
                stored=(desc & 0xFFFFF) + 1, codec=(desc >> 20) & 3, cflags=(desc >> 22) & 0xFF)


def parse_amprzpk1(data: bytes):
    if len(data) < 128 or data[:8] != b"AMPRZPK1":
        raise ValueError("not AMPRZPK1")
    errs = []
    ver, hsize = struct.unpack_from("<II", data, 8)
    shift, level = data[16], data[17]
    vcount = struct.unpack_from("<I", data, 20)[0]
    archive_id = data[24:40]
    fcount, ccount, foff, coff, voff, soff, slen, total = struct.unpack_from("<QQQQQQQQ", data, 40)
    pcrc, hcrc = struct.unpack_from("<II", data, 112)
    if ver != 1 or hsize != 128 or shift != 18:
        errs.append(f"header fields invalid (v{ver}, size {hsize}, shift {shift})")
    hc = data[:112] + bytes(8) + data[120:128]
    hdr_ok = crc32(hc) == hcrc
    pay_ok = crc32(data[128:]) == pcrc
    if not hdr_ok:
        errs.append("index header CRC mismatch")
    if not pay_ok:
        errs.append("index payload CRC mismatch")
    if not (foff == 128 and coff == foff + fcount * 32 and voff == coff + ccount * 8
            and soff == voff + vcount * 8 and soff + slen <= len(data)):
        errs.append("index section layout invalid")
    files = []
    CH = 1 << 18
    for i in range(fcount):
        r = data[foff + i * 32: foff + (i + 1) * 32]
        ph, size, first, po, pl, fflags = struct.unpack_from("<QQIIHH", r, 0)
        cnt = size // CH + (1 if size % CH else 0)
        files.append(dict(id=i + 1, path=data[soff + po: soff + po + pl].decode("utf-8", "replace"),
                          size=size, first=first, count=cnt, store_only=bool(fflags & 1)))
    vols = [struct.unpack_from("<Q", data, voff + i * 8)[0] for i in range(vcount)]
    return dict(level=level, archive_id=archive_id, files=files, chunks=data[coff:coff + ccount * 8],
                ccount=ccount, volumes=vols, errors=errs, crc_ok=hdr_ok and pay_ok, total=total)


def zpk_chunk(chunks: bytes, idx: int):
    w = struct.unpack_from("<Q", chunks, idx * 8)[0]
    return dict(offset=w & 0xFFFFFFFF, pack=(w >> 32) & 0xFFF, stored=((w >> 44) & 0x3FFFF) + 1, codec=w >> 62)


# --------------------------------------------------------------------------
# audit of one source
# --------------------------------------------------------------------------

AMPR_MAGICS = {
    b"AMPRIDX3": "AMPRIDX3 runtime path index (no compression info)",
    b"AMPRPAK4": "AMPRPAK4 LZ4 pack manifest",
    b"AMPRPAK3": "AMPRPAK3 LZ4 pack manifest (legacy)",
    b"AMPRPAK2": "AMPRPAK2 LZ4 pack manifest (legacy)",
    b"AMPRPAK1": "AMPRPAK1 LZ4 pack manifest (legacy)",
    b"AMPRDAT3": "AMPRDAT3 LZ4-pack data volume",
    b"AMPRCRC1": "AMPRCRC1 decoded-chunk CRC sidecar",
    b"AMPRCFG1": "AMPRCFG1 runtime settings",
    b"AMPRZPK1": "AMPRZPK1 Zstd pack index",
    b"AMPRZDT1": "AMPRZDT1 Zstd-pack data volume",
}

SAMPLE_COMPRESSED = 16
SAMPLE_RAW = 4


def title_from_param(raw: bytes):
    try:
        j = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return None, None, None
    lp = j.get("localizedParameters", {}) or {}
    lang = lp.get("defaultLanguage", "en-US")
    title = (lp.get(lang) or {}).get("titleName") or (lp.get("en-US") or {}).get("titleName")
    return title, j.get("titleId"), j.get("contentVersion")


def spread(seq, k):
    if len(seq) <= k:
        return list(seq)
    return [seq[(i * (len(seq) - 1)) // (k - 1)] for i in range(k)]


def audit_source(path: str, log):
    t0 = time.time()
    net0 = Stats.network_bytes
    g = dict(source_path=path, source_type="folder" if os.path.isdir(path) else os.path.splitext(path)[1].lstrip("."),
             title="", title_id="", content_version="", container="", pack_formats="", category="",
             evidence=[], status="", errors=[], limitations=[], file_count=0, ampr_runtime="")
    rows = []
    base = None
    try:
        uw, base = open_game_fs(path)
    except Exception as ex:
        g["status"] = "could not inspect"
        g["category"] = "Could not inspect"
        g["errors"].append(f"open failed: {type(ex).__name__}: {ex}")
        log(traceback.format_exc())
        if base:
            base.close()
        return g, rows
    fs = uw.fs
    g["container"] = " > ".join(uw.chain)
    try:
        root = uw.root
        if root is None:
            g["limitations"].append("no sce_sys/param.json found; treating image root as /app0")
            root = ""
        files = {k[len(root):]: e for k, e in fs.files.items() if k.startswith(root)}
        g["file_count"] = len(files)
        lower = {k.lower(): k for k in files}
        log(f"container: {g['container']}; app root '{root}'; {len(files)} files")

        def rd(rel, off=0, n=None):
            e = files[rel]
            return fs.read(e, off, e.size if n is None else n)

        pj = lower.get("sce_sys/param.json")
        if pj:
            t, tid, cv = title_from_param(rd(pj))
            g["title"], g["title_id"], g["content_version"] = t or "", tid or "", cv or ""
        if not g["title_id"]:
            m = re.search(r"(PPSA|CUSA)\d{5}", os.path.basename(path))
            if m:
                g["title_id"] = m.group(0)
                g["limitations"].append("title ID taken from file name (param.json unreadable)")

        # ---- discover AMPR artefacts by magic ----
        found = {}
        for rel in sorted(files):
            base_l = rel.rsplit("/", 1)[-1].lower()
            if "ampr" in base_l or base_l.endswith(".index") or base_l.endswith(".crc") or base_l.endswith(".runtime"):
                e = files[rel]
                if base_l.endswith(".sprx") or base_l.endswith(".prx"):
                    continue
                head = rd(rel, 0, 8) if e.size else b""
                if head in AMPR_MAGICS:
                    found.setdefault(head, []).append(rel)
        runtime = []
        for rel in sorted(files):
            if rel.rsplit("/", 1)[-1].lower() == "libsceampr.sprx":
                e = files[rel]
                info = f"{rel} ({e.size} B"
                if e.size <= 32 << 20:
                    blob = rd(rel)
                    vers = sorted(set(m.decode("latin-1") for m in re.findall(
                        rb"ampr[-_ ]?emu[-_ a-zA-Z]*v?\d+\.\d+(?:\.\d+)*(?:\.\d+)?|ampr-emu-ng[^\x00]{0,24}", blob, re.I)))[:3]
                    if vers:
                        info += "; strings: " + " | ".join(vers)
                    if blob[:4] == b"\x7fELF":
                        info += "; plain ELF"
                    elif blob[:4] in (b"\x4f\x15\x3d\x1d", b"\x54\x14\xf5\xee"):
                        info += "; SELF"
                runtime.append(info + ")")
        g["ampr_runtime"] = "; ".join(runtime)
        for m, lst in found.items():
            if m not in (b"AMPRDAT3", b"AMPRZDT1"):
                g["evidence"].append(f"{AMPR_MAGICS[m]}: {', '.join(lst[:3])}" + (" ..." if len(lst) > 3 else ""))
        if runtime:
            g["evidence"].append("AMPR runtime: " + g["ampr_runtime"])

        formats = []
        verified_lz4 = verified_zstd = False
        unverified = []

        # ---- LZ4 (AMPRPAK4) ----
        for mrel in found.get(b"AMPRPAK4", []):
            mdir = mrel.rsplit("/", 1)[0] + "/" if "/" in mrel else ""
            data = rd(mrel)
            try:
                man = parse_amprpak4(data)
            except Exception as ex:
                g["errors"].append(f"{mrel}: {ex}")
                unverified.append(mrel)
                continue
            formats.append("AMPR LZ4 (AMPRPAK4/AMPRDAT3)")
            g["errors"] += [f"{mrel}: {x}" for x in man["errors"]]
            chunks = man["chunks"]
            # volumes
            vol_ok = {}
            for p in man["packs"]:
                cand = [p["name"], mdir + p["name"], p["name"].lstrip("/")]
                if p["name"].lower().startswith("/app0/"):
                    cand.append(p["name"][6:])
                vrel = next((lower[c.lower()] for c in cand if c.lower() in lower), None)
                if vrel is None:
                    g["errors"].append(f"pack volume {p['name']} missing")
                    vol_ok[p["id"]] = None
                    continue
                hdr = rd(vrel, 0, 64)
                try:
                    magic, ver, hs, pid, pfl, bid, poff, pbytes, hcrc, res = DATA_HEADER.unpack(hdr)
                    c = bytearray(hdr)
                    struct.pack_into("<I", c, 56, 0)
                    problems = []
                    if magic != b"AMPRDAT3" or ver != 3 or hs != 64:
                        problems.append("bad magic/version")
                    if crc32(c) != hcrc:
                        problems.append("header CRC mismatch")
                    if bid != man["build_id"]:
                        problems.append("build ID differs from manifest")
                    if pid != p["id"]:
                        problems.append("pack id mismatch")
                    if files[vrel].size != p["file_size"] or poff + pbytes != files[vrel].size:
                        problems.append(f"size mismatch (file {files[vrel].size}, manifest {p['file_size']})")
                    if problems:
                        g["errors"].append(f"{vrel}: " + "; ".join(problems))
                    vol_ok[p["id"]] = (vrel, not problems)
                except Exception as ex:
                    g["errors"].append(f"{vrel}: header parse failed {ex}")
                    vol_ok[p["id"]] = None
            # optional decoded-CRC sidecar
            crcs = None
            crel = lower.get((mrel + ".crc").lower())
            if crel:
                cdat = rd(crel)
                try:
                    mg, cv, chs, cbid, ccnt, ppcrc, hhcrc = CRC_HEADER.unpack_from(cdat)
                    if mg == b"AMPRCRC1" and cbid == man["build_id"] and ccnt == man["ccount"] \
                            and len(cdat) == 48 + 4 * ccnt and crc32(cdat[48:]) == ppcrc:
                        crcs = cdat[48:]
                    else:
                        g["limitations"].append(f"{crel}: CRC sidecar does not match manifest; not used")
                except Exception as ex:
                    g["limitations"].append(f"{crel}: unreadable ({ex})")
            # per-file classification
            lz4_chunk_ids, raw_chunk_ids = [], []
            packed_files = loose_files = 0
            tot_logical = tot_stored = 0
            for f in man["files"]:
                rel_path = f["path"]
                rel_path = rel_path[6:] if rel_path.lower().startswith("/app0/") else rel_path.lstrip("/")
                if not f["flags"] & 1:
                    loose_files += 1
                    rows.append(dict(source_path=path, pack_name="(loose - not in pack)", file=rel_path,
                                     entry_id=f["id"], compression_status="loose file (indexed, not packed)",
                                     original_bytes=f["size"], stored_bytes=files[lower[rel_path.lower()]].size if rel_path.lower() in lower else "",
                                     chunks=0, lz4_chunks=0, raw_chunks=0, manifest=mrel))
                    continue
                packed_files += 1
                nl = nr = st = 0
                packs_used = set()
                bsz = 1 << f["shift"]
                for li in range(f["count"]):
                    ci = f["first"] + li
                    ch = pak4_chunk(chunks, ci)
                    raw_sz = min(bsz, f["size"] - li * bsz)
                    st += ch["stored"]
                    packs_used.add(ch["pack"])
                    if ch["codec"] == 1:
                        nl += 1
                        lz4_chunk_ids.append((ci, ch, raw_sz))
                    elif ch["codec"] == 0:
                        nr += 1
                        raw_chunk_ids.append((ci, ch, raw_sz))
                if not man["crc_ok"]:
                    status = "in LZ4 pack; compression unverified"
                elif f["count"] == 0:
                    status = "empty file (no chunks)"
                elif nl == f["count"]:
                    status = "fully LZ4-compressed"
                elif nl == 0:
                    status = "stored raw"
                else:
                    status = "partly LZ4-compressed"
                tot_logical += f["size"]
                tot_stored += st
                pnames = ";".join(man["packs"][i]["name"] if i < len(man["packs"]) else f"pack#{i}" for i in sorted(packs_used))
                rows.append(dict(source_path=path, pack_name=pnames, file=rel_path, entry_id=f["id"],
                                 compression_status=status, original_bytes=f["size"], stored_bytes=st,
                                 chunks=f["count"], lz4_chunks=nl, raw_chunks=nr, manifest=mrel))
            # sample decode
            ok_l = bad_l = ok_r = bad_r = crc_hits = 0
            for kind, pool, kmax in (("lz4", lz4_chunk_ids, SAMPLE_COMPRESSED), ("raw", raw_chunk_ids, SAMPLE_RAW)):
                for ci, ch, raw_sz in spread(pool, kmax):
                    v = vol_ok.get(ch["pack"])
                    if not v:
                        continue
                    stored = rd(v[0], ch["offset"], ch["stored"])
                    try:
                        if kind == "lz4":
                            out = lz4_block_decode(stored, raw_sz)
                        else:
                            if len(stored) != raw_sz:
                                raise ValueError("raw chunk size mismatch")
                            out = stored
                        if crcs is not None:
                            want = struct.unpack_from("<I", crcs, ci * 4)[0]
                            if crc32(out) != want:
                                raise ValueError("decoded CRC differs from AMPRCRC1 sidecar")
                            crc_hits += 1
                        if kind == "lz4":
                            ok_l += 1
                        else:
                            ok_r += 1
                    except Exception as ex:
                        if kind == "lz4":
                            bad_l += 1
                        else:
                            bad_r += 1
                        g["errors"].append(f"chunk {ci} ({kind}) in {v[0]}: {ex}")
            ratio = f"{tot_stored / tot_logical:.1%}" if tot_logical else "n/a"
            ev = (f"{mrel}: AMPRPAK4 header+payload CRC {'OK' if man['crc_ok'] else 'FAIL'}; "
                  f"{len(man['files'])} records ({packed_files} packed, {loose_files} loose), "
                  f"{man['ccount']} chunks ({len(lz4_chunk_ids)} LZ4, {len(raw_chunk_ids)} RAW), "
                  f"{len(man['packs'])} AMPRDAT3 volumes ({sum(1 for x in vol_ok.values() if x and x[1])} headers valid); "
                  f"packed bytes {tot_logical} -> stored {tot_stored} ({ratio}); "
                  f"sample decode: LZ4 {ok_l}/{ok_l + bad_l} OK via liblz4 {LZ4.LZ4_versionString().decode()}, "
                  f"RAW {ok_r}/{ok_r + bad_r} OK"
                  + (f"; {crc_hits} matched AMPRCRC1 decoded CRCs" if crcs is not None else "; no CRC sidecar"))
            g["evidence"].append(ev)
            vols_good = all(x and x[1] for x in vol_ok.values()) if vol_ok else False
            if man["crc_ok"] and vols_good and bad_l == 0 and bad_r == 0 and (ok_l > 0 or not lz4_chunk_ids):
                verified_lz4 = True
                if not lz4_chunk_ids:
                    g["limitations"].append(f"{mrel}: valid LZ4 pack but every chunk is stored RAW")
            else:
                unverified.append(mrel)

        for m in (b"AMPRPAK1", b"AMPRPAK2", b"AMPRPAK3"):
            for mrel in found.get(m, []):
                formats.append(f"AMPR LZ4 ({m.decode()} legacy)")
                unverified.append(mrel)
                g["limitations"].append(f"{mrel}: legacy {m.decode()} manifest; per-chunk parser not implemented")
                rows.append(dict(source_path=path, pack_name=mrel, file="(entries not enumerated)", entry_id="",
                                 compression_status="in LZ4 pack; compression unverified", original_bytes="",
                                 stored_bytes="", chunks="", lz4_chunks="", raw_chunks="", manifest=mrel))

        # ---- Zstd (AMPRZPK1) ----
        for mrel in found.get(b"AMPRZPK1", []):
            mdir = mrel.rsplit("/", 1)[0] + "/" if "/" in mrel else ""
            try:
                man = parse_amprzpk1(rd(mrel))
            except Exception as ex:
                g["errors"].append(f"{mrel}: {ex}")
                unverified.append(mrel)
                continue
            formats.append("AMPR Zstd (AMPRZPK1/AMPRZDT1)")
            g["errors"] += [f"{mrel}: {x}" for x in man["errors"]]
            vol_ok = {}
            for vid, vsize in enumerate(man["volumes"]):
                name = f"ampr_zstd-{vid:03d}.pak"
                vrel = lower.get((mdir + name).lower())
                if vrel is None:
                    g["errors"].append(f"Zstd volume {name} missing")
                    vol_ok[vid] = None
                    continue
                hdr = rd(vrel, 0, 64)
                c = hdr[:60] + bytes(4)
                probs = []
                if hdr[:8] != b"AMPRZDT1" or struct.unpack_from("<II", hdr, 8) != (1, 64):
                    probs.append("bad magic/version")
                if crc32(c) != struct.unpack_from("<I", hdr, 60)[0]:
                    probs.append("header CRC mismatch")
                if struct.unpack_from("<I", hdr, 16)[0] != vid:
                    probs.append("volume id mismatch")
                if hdr[24:40] != man["archive_id"]:
                    probs.append("archive id differs from index")
                if files[vrel].size != vsize:
                    probs.append("size mismatch")
                if probs:
                    g["errors"].append(f"{vrel}: " + "; ".join(probs))
                vol_ok[vid] = (vrel, not probs)
            z_ids, r_ids = [], []
            tl = ts = 0
            CH = 1 << 18
            for f in man["files"]:
                nz = nr = st = 0
                used = set()
                for li in range(f["count"]):
                    ci = f["first"] + li
                    ch = zpk_chunk(man["chunks"], ci)
                    raw_sz = min(CH, f["size"] - li * CH)
                    st += ch["stored"]
                    used.add(ch["pack"])
                    if ch["codec"] == 1:
                        nz += 1
                        z_ids.append((ci, ch, raw_sz))
                    else:
                        nr += 1
                        r_ids.append((ci, ch, raw_sz))
                if not man["crc_ok"]:
                    status = "in Zstd pack; compression unverified"
                elif f["count"] == 0:
                    status = "empty file (no chunks)"
                elif nz == f["count"]:
                    status = "fully Zstd-compressed"
                elif nz == 0:
                    status = "stored raw"
                else:
                    status = "partly Zstd-compressed"
                tl += f["size"]
                ts += st
                rows.append(dict(source_path=path, pack_name=";".join(f"ampr_zstd-{i:03d}.pak" for i in sorted(used)),
                                 file=f["path"], entry_id=f["id"], compression_status=status,
                                 original_bytes=f["size"], stored_bytes=st, chunks=f["count"], lz4_chunks="",
                                 raw_chunks=nr, manifest=mrel, zstd_chunks=nz))
            okz = badz = okr = badr = 0
            for kind, pool, kmax in (("zstd", z_ids, SAMPLE_COMPRESSED), ("raw", r_ids, SAMPLE_RAW)):
                for ci, ch, raw_sz in spread(pool, kmax):
                    v = vol_ok.get(ch["pack"])
                    if not v:
                        continue
                    stored = rd(v[0], ch["offset"], ch["stored"])
                    try:
                        if kind == "zstd":
                            zstd_decode(stored, raw_sz)
                            okz += 1
                        else:
                            if len(stored) != raw_sz:
                                raise ValueError("raw chunk size mismatch")
                            okr += 1
                    except Exception as ex:
                        if kind == "zstd":
                            badz += 1
                        else:
                            badr += 1
                        g["errors"].append(f"chunk {ci} ({kind}) in {v[0]}: {ex}")
            ratio = f"{ts / tl:.1%}" if tl else "n/a"
            g["evidence"].append(
                f"{mrel}: AMPRZPK1 level {man['level']} CRC {'OK' if man['crc_ok'] else 'FAIL'}; {len(man['files'])} files, "
                f"{man['ccount']} chunks ({len(z_ids)} zstd, {len(r_ids)} RAW), {len(man['volumes'])} AMPRZDT1 volumes; "
                f"bytes {tl} -> {ts} ({ratio}); sample decode: zstd {okz}/{okz + badz} OK via libzstd "
                f"{ZSTD.ZSTD_versionString().decode()}, RAW {okr}/{okr + badr} OK")
            if man["crc_ok"] and vol_ok and all(x and x[1] for x in vol_ok.values()) and badz == 0 and badr == 0 \
                    and (okz > 0 or not z_ids):
                verified_zstd = True
            else:
                unverified.append(mrel)

        # orphan data volumes without a manifest
        if (found.get(b"AMPRDAT3") and not found.get(b"AMPRPAK4")) or (found.get(b"AMPRZDT1") and not found.get(b"AMPRZPK1")):
            g["limitations"].append("AMPR data volumes present without a readable manifest")
            unverified.append("orphan volumes")

        g["pack_formats"] = "; ".join(dict.fromkeys(formats)) or "none"
        if unverified:
            g["category"] = "AMPR pack present - unverified"
            g["status"] = "inspected; pack verification incomplete"
        elif verified_lz4 and verified_zstd:
            g["category"] = "LZ4 + Zstd packs verified"
            g["status"] = "inspected"
        elif verified_lz4:
            g["category"] = "LZ4 pack verified"
            g["status"] = "inspected"
        elif verified_zstd:
            g["category"] = "Zstd pack verified"
            g["status"] = "inspected"
        else:
            g["category"] = "No AMPR pack detected"
            g["status"] = "inspected"
            note = "no AMPRPAK/AMPRZPK manifest or AMPR data volume found by magic in any file"
            if found.get(b"AMPRIDX3"):
                note += "; ampr_emu.index (AMPRIDX3) is only a path/size index"
            g["evidence"].append(note)
        if uw.pfsc is not None:
            p = uw.pfsc
            g["limitations"].append(
                f"read through PFSC (zlib) container: {p.zlib_blocks} zlib + {p.raw_blocks} raw + {p.zero_blocks} zero 64 KiB blocks decoded for metadata")
    except Exception as ex:
        g["status"] = "could not inspect"
        g["category"] = "Could not inspect"
        g["errors"].append(f"{type(ex).__name__}: {ex}")
        log(traceback.format_exc())
    finally:
        try:
            fs.close()
        except Exception:
            pass
        if base:
            base.close()
    g["seconds"] = round(time.time() - t0, 1)
    g["network_mib_read"] = round((Stats.network_bytes - net0) / 2**20, 1)
    return g, rows


# --------------------------------------------------------------------------
# library inventory + CSV
# --------------------------------------------------------------------------

IMAGE_EXT = (".exfat", ".ffpfsc", ".ffpfs", ".ffpkg", ".img", ".ufs", ".ufs2", ".pfs")


def inventory(lib: str):
    """Images (by extension or sniffed signature) and extracted game folders."""
    out = []
    for dirpath, dirnames, filenames in os.walk(lib):
        dirnames.sort()
        dirnames[:] = [d for d in dirnames if not d.startswith(("@", "#recycle", "."))]  # DSM metadata
        rel = os.path.relpath(dirpath, lib)
        names_l = {n.lower() for n in filenames}
        if "eboot.bin" in names_l and any(d.lower() == "sce_sys" for d in dirnames):
            out.append(("folder", dirpath))
            dirnames[:] = []  # do not descend into a game folder
            continue
        for fn in sorted(filenames):
            if fn.startswith("._") or fn == ".DS_Store":
                continue
            full = os.path.join(dirpath, fn)
            low = fn.lower()
            if low.endswith(IMAGE_EXT):
                out.append(("image", full))
            elif low.endswith((".pkg",)):
                out.append(("pkg", full))
            else:
                try:
                    if os.path.getsize(full) > (1 << 30):
                        s = FileSource(full, 4)
                        k = sniff(s)
                        s.close()
                        if k in ("exfat", "pfs", "ufs2"):
                            out.append(("image", full))
                except OSError:
                    pass
    return out


GAME_COLS = ["source_path", "source_type", "title", "title_id", "content_version", "category", "pack_formats",
             "evidence", "inspection_status", "errors_or_limitations", "container", "file_count", "ampr_runtime",
             "seconds", "network_mib_read"]
FILE_COLS = ["source_path", "pack_name", "original_path_or_entry_id", "compression_status", "original_bytes",
             "stored_bytes", "entry_id", "chunks", "lz4_chunks", "zstd_chunks", "raw_chunks", "manifest"]


def game_row(g):
    return {
        "source_path": g["source_path"], "source_type": g["source_type"], "title": g["title"],
        "title_id": g["title_id"], "content_version": g["content_version"], "category": g["category"],
        "pack_formats": g["pack_formats"], "evidence": " || ".join(g["evidence"]),
        "inspection_status": g["status"],
        "errors_or_limitations": " || ".join([f"ERROR: {e}" for e in g["errors"]] + [f"LIMIT: {l}" for l in g["limitations"]]),
        "container": g["container"], "file_count": g["file_count"], "ampr_runtime": g["ampr_runtime"],
        "seconds": g.get("seconds", ""), "network_mib_read": g.get("network_mib_read", ""),
    }


def file_row(r):
    return {
        "source_path": r["source_path"], "pack_name": r["pack_name"],
        "original_path_or_entry_id": r["file"] if r["file"] else f"entry#{r['entry_id']}",
        "compression_status": r["compression_status"], "original_bytes": r["original_bytes"],
        "stored_bytes": r["stored_bytes"], "entry_id": r["entry_id"], "chunks": r["chunks"],
        "lz4_chunks": r.get("lz4_chunks", ""), "zstd_chunks": r.get("zstd_chunks", ""),
        "raw_chunks": r["raw_chunks"], "manifest": r["manifest"],
    }


def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", action="append", default=[], help="substring filter on source path")
    ap.add_argument("--inventory-only", action="store_true")
    a = ap.parse_args(argv)
    os.makedirs(os.path.join(a.out, "logs"), exist_ok=True)
    os.makedirs(os.path.join(a.out, "reports"), exist_ok=True)
    os.makedirs(os.path.join(a.out, "work", "results"), exist_ok=True)
    runlog = open(os.path.join(a.out, "logs", "run.log"), "a", encoding="utf-8")

    def log(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        runlog.write(line + "\n")
        runlog.flush()

    inv = inventory(a.library)
    with open(os.path.join(a.out, "reports", "inventory.txt"), "w", encoding="utf-8") as fh:
        for k, p in inv:
            fh.write(f"{k}\t{p}\n")
    log(f"inventory: {len(inv)} sources under {a.library}")
    if a.inventory_only:
        for k, p in inv:
            print(k, p)
        return
    todo = [(k, p) for k, p in inv if not a.only or any(s in p for s in a.only)]
    for i, (kind, p) in enumerate(todo, 1):
        log(f"[{i}/{len(todo)}] {kind}: {p}")
        if kind == "pkg":
            g = dict(source_path=p, source_type="pkg", title="", title_id="", content_version="", container="pkg",
                     pack_formats="", category="Could not inspect", evidence=[], status="could not inspect",
                     errors=[], limitations=["PKG inspection not implemented"], file_count=0, ampr_runtime="")
            rows = []
        else:
            g, rows = audit_source(p, log)
        log(f"    -> {g['category']} | {g['title']} {g['title_id']} | {g['pack_formats']} | "
            f"{len(rows)} file rows | {g.get('seconds')}s, {g.get('network_mib_read')} MiB read")
        for e in g["errors"][:10]:
            log(f"    ERROR {e}")
        key = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.relpath(p, a.library))[:150]
        with open(os.path.join(a.out, "work", "results", key + ".json"), "w", encoding="utf-8") as fh:
            json.dump(dict(game=g, files=rows), fh, ensure_ascii=False)
    # assemble CSVs from all stored results (lets partial reruns merge)
    write_reports(a.library, a.out, inv)
    log("reports written")


def write_reports(lib, out, inv):
    import csv
    resdir = os.path.join(out, "work", "results")
    results = {}
    for fn in os.listdir(resdir):
        with open(os.path.join(resdir, fn), encoding="utf-8") as fh:
            d = json.load(fh)
        results[d["game"]["source_path"]] = d
    order = [p for _, p in inv if p in results]
    with open(os.path.join(out, "reports", "games.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, GAME_COLS)
        w.writeheader()
        for p in order:
            w.writerow(game_row(results[p]["game"]))
    with open(os.path.join(out, "reports", "files.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, FILE_COLS)
        w.writeheader()
        for p in order:
            for r in results[p]["files"]:
                w.writerow(file_row(r))


if __name__ == "__main__":
    main(sys.argv[1:])
