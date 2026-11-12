"""Deflate sekecil mungkin (lossless).

Zopfli menghasilkan aliran deflate yang 100% kompatibel dengan zlib tetapi
±5–10% lebih kecil daripada zlib level 9, dengan waktu kompresi lebih lama.
Dipakai untuk bagian XML di DOCX/PPTX/XLSX dan stream teks/font di PDF.
Kalau paket ``zopfli`` tidak terpasang, otomatis memakai zlib level 9.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

try:
    import zopfli.zlib as _zopfli
except Exception:  # pragma: no cover - fallback
    _zopfli = None

HAS_ZOPFLI = _zopfli is not None
_MAX_ZOPFLI = 96 * 1024 * 1024   # di atas ini zopfli terlalu lama → zlib saja


def _iterations(n: int) -> int:
    if n <= 256 * 1024:
        return 15
    if n <= 4 * 1024 * 1024:
        return 8
    return 4


def zlib_best(data: bytes) -> bytes:
    """Aliran format zlib terkecil (untuk /FlateDecode di PDF)."""
    best = zlib.compress(data, 9)
    if _zopfli is not None and 64 < len(data) <= _MAX_ZOPFLI:
        try:
            alt = _zopfli.compress(data, numiterations=_iterations(len(data)))
            if len(alt) < len(best) and zlib.decompress(alt) == data:
                best = alt
        except Exception:
            pass
    return best


def raw_best(data: bytes) -> bytes:
    """Aliran deflate mentah terkecil (untuk entri ZIP)."""
    return zlib_best(data)[2:-4]


# ----------------------------------------------------------------- penulis ZIP

_DOS_TIME, _DOS_DATE = 0, 0x0021  # 1980-01-01 00:00 → arsip deterministik


def write_zip(dst: Path, entries: list[tuple[str, bytes]]) -> None:
    """Tulis arsip ZIP dengan deflate terbaik per entri (atau STORED kalau
    deflate tidak membantu). Python ``zipfile`` tidak bisa menerima data yang
    sudah dikompres, jadi strukturnya ditulis langsung di sini."""
    total = sum(len(d) for _, d in entries)
    if total >= 0xFFFFFFF0 or len(entries) >= 0xFFFF:
        _write_zip_stdlib(dst, entries)   # butuh ZIP64 → serahkan ke zipfile
        return
    central = []
    with open(dst, "wb") as fh:
        for name, data in entries:
            raw_name = name.encode("utf-8")
            flags = 0 if raw_name.isascii() else 0x0800
            crc = zlib.crc32(data) & 0xFFFFFFFF
            method, payload = 0, data
            if data:
                packed = raw_best(data)
                if len(packed) < len(data):
                    method, payload = 8, packed
            offset = fh.tell()
            fh.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, method, _DOS_TIME, _DOS_DATE,
                                 crc, len(payload), len(data), len(raw_name), 0))
            fh.write(raw_name)
            fh.write(payload)
            central.append(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, method,
                                       _DOS_TIME, _DOS_DATE, crc, len(payload), len(data),
                                       len(raw_name), 0, 0, 0, 0, 0, offset) + raw_name)
        cd_offset = fh.tell()
        cd = b"".join(central)
        fh.write(cd)
        fh.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries),
                             len(cd), cd_offset, 0))


def _write_zip_stdlib(dst: Path, entries: list[tuple[str, bytes]]) -> None:
    import zipfile
    with zipfile.ZipFile(dst, "w", allowZip64=True) as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            z.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
