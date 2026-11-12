"""Kompresi DOCX / PPTX / XLSX.

File OOXML adalah arsip ZIP. Yang dilakukan:
1. Tiap gambar di folder media dikompres ulang; resolusinya disesuaikan dengan
   ukuran tampil gambar itu di dokumen (dibaca dari XML) dan DPI preset.
2. Foto PNG tanpa transparansi → JPEG. Screenshot/diagram/gambar berisi teks
   tetap PNG (pngquant) supaya teks tidak blur.
3. Media yang tidak direferensikan, duplikat, dan thumbnail dibuang.
4. Arsip di-zip ulang dengan deflate level 9.

Semua bagian XML dokumen (teks, tata letak) disalin byte-per-byte; hanya file
relasi dan [Content_Types].xml yang disesuaikan saat nama media berubah.
"""
from __future__ import annotations

import hashlib
import posixpath
import re
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from lxml import etree

from . import images
from .presets import Settings

Progress = Callable[[float, str], None]

NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
EMU_PER_INCH = 914400.0

_MEDIA_DIR = re.compile(r"^(word|ppt|xl|visio)/media/[^/]+$")
_RASTER_MAGIC = ((b"\xff\xd8", "jpeg"), (b"\x89PNG\r\n\x1a\n", "png"), (b"BM", "bmp"),
                 (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"))
_CONTENT_TYPE = {"jpeg": "image/jpeg", "png": "image/png"}
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)


@dataclass
class OfficeStats:
    images: int = 0
    recompressed: int = 0
    resized: int = 0
    to_jpeg: int = 0
    kept_png: int = 0
    unused_removed: int = 0
    duplicates_removed: int = 0
    thumbnail_removed: bool = False
    media_before: int = 0
    media_after: int = 0
    details: list[str] = field(default_factory=list)

    def describe(self) -> str:
        parts = []
        if self.images:
            parts.append(f"{self.images} gambar")
        if self.resized:
            parts.append(f"{self.resized} di-resize")
        if self.to_jpeg:
            parts.append(f"{self.to_jpeg} foto PNG → JPEG")
        if self.kept_png:
            parts.append(f"{self.kept_png} grafis/teks tetap PNG")
        if self.unused_removed:
            parts.append(f"{self.unused_removed} media tak terpakai dibuang")
        if self.duplicates_removed:
            parts.append(f"{self.duplicates_removed} duplikat digabung")
        if self.thumbnail_removed:
            parts.append("thumbnail dibuang")
        return ", ".join(parts) if parts else "zip ulang deflate maksimum"


def _local(el) -> str:
    tag = el.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _sniff(data: bytes) -> str | None:
    for magic, kind in _RASTER_MAGIC:
        if data.startswith(magic):
            return kind
    return None


def _rels_source(rels_name: str) -> str:
    """'word/_rels/document.xml.rels' → 'word/document.xml'; '_rels/.rels' → ''."""
    d, f = posixpath.split(rels_name)
    return posixpath.join(posixpath.dirname(d), f[:-5])


def _resolve(source_part: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source_part), target))


# ----------------------------------------------------------- ukuran tampil gambar

def _int(el, attr: str) -> int:
    try:
        return int(el.get(attr, "0"))
    except (TypeError, ValueError):
        return 0


def _xfrm_ext(node) -> tuple[int, int] | None:
    """Cari <a:xfrm><a:ext cx cy> milik sebuah shape/gambar."""
    for child in node:
        if _local(child) in ("spPr", "grpSpPr", "xfrm"):
            xfrm = child if _local(child) == "xfrm" else child.find(f"{{{NS_A}}}xfrm")
            if xfrm is not None:
                ext = xfrm.find(f"{{{NS_A}}}ext")
                if ext is not None and _int(ext, "cx") > 0 and _int(ext, "cy") > 0:
                    return _int(ext, "cx"), _int(ext, "cy")
    return None


_UNIT = {"pt": 1 / 72, "in": 1.0, "cm": 1 / 2.54, "mm": 1 / 25.4, "px": 1 / 96, "pc": 1 / 6, "": 1 / 96}


def _css_inches(style: str, prop: str) -> float | None:
    m = re.search(rf"(?:^|;)\s*{prop}\s*:\s*([\d.]+)\s*([a-z]*)", style or "")
    if not m:
        return None
    try:
        return float(m.group(1)) * _UNIT.get(m.group(2), 1 / 96)
    except ValueError:
        return None


def _usage_size(el, fallback: tuple[float, float] | None) -> tuple[float, float] | None:
    """Ukuran tampil (inci) GAMBAR UTUH untuk satu pemakaian. None = tidak
    diketahui → gambar tidak akan di-resize."""
    local = _local(el)
    if local == "imagedata":  # VML (dokumen lama, header, OLE preview)
        shape = el.getparent()
        style = shape.get("style", "") if shape is not None else ""
        w, h = _css_inches(style, "width"), _css_inches(style, "height")
        return (w, h) if w and h else fallback
    if local != "blip":
        return fallback
    fill = el.getparent()
    if fill is None:
        return fallback
    crop = [0.0, 0.0, 0.0, 0.0]
    for child in fill:
        cl = _local(child)
        if cl == "tile":
            return None  # isian berulang: ukuran tampil tidak bisa ditentukan
        if cl == "srcRect":
            crop = [max(0.0, _int(child, k) / 100000.0) for k in ("l", "t", "r", "b")]
    ext = None
    container = None
    node = fill.getparent()
    while node is not None:
        ln = _local(node)
        if ln in ("pic", "sp", "cxnSp", "graphicFrame"):
            container = node
            ext = _xfrm_ext(node)
            break
        if ln in ("bg", "bgPr"):
            return fallback  # latar slide = seukuran slide
        if ln in ("rPr", "pPr", "defRPr", "lvl1pPr", "buBlip", "ln"):
            return None      # bullet gambar / isian teks / garis
        node = node.getparent()
    # XLSX: <xdr:oneCellAnchor>/<xdr:absoluteAnchor><xdr:ext cx cy>
    if ext is None and container is not None and container.getparent() is not None \
            and _local(container.getparent()) in ("oneCellAnchor", "absoluteAnchor"):
        for child in container.getparent():
            if _local(child) == "ext" and _int(child, "cx") > 0 and _int(child, "cy") > 0:
                ext = (_int(child, "cx"), _int(child, "cy"))
    # DOCX: <wp:inline>/<wp:anchor><wp:extent cx cy>
    node = el
    while node is not None:
        if _local(node) in ("inline", "anchor"):
            e = node.find(f"{{{NS_WP}}}extent")
            if e is not None and _int(e, "cx") > 0 and _int(e, "cy") > 0:
                w = (_int(e, "cx"), _int(e, "cy"))
                ext = w if ext is None else (max(ext[0], w[0]), max(ext[1], w[1]))
            break
        node = node.getparent()
    if ext is None:
        return fallback
    sx = sy = 1.0
    node = container.getparent() if container is not None else None
    while node is not None:  # shape di dalam grup: skala grup ikut berlaku
        if _local(node) == "grpSp":
            for child in node:
                if _local(child) == "grpSpPr":
                    xfrm = child.find(f"{{{NS_A}}}xfrm")
                    if xfrm is not None:
                        e, ch = xfrm.find(f"{{{NS_A}}}ext"), xfrm.find(f"{{{NS_A}}}chExt")
                        if e is not None and ch is not None and _int(ch, "cx") > 0 and _int(ch, "cy") > 0:
                            sx *= _int(e, "cx") / _int(ch, "cx")
                            sy *= _int(e, "cy") / _int(ch, "cy")
        node = node.getparent()
    w = ext[0] * sx / EMU_PER_INCH
    h = ext[1] * sy / EMU_PER_INCH
    # Gambar di-crop: bagian yang terlihat hanya sebagian, jadi gambar utuhnya
    # perlu resolusi lebih besar.
    fw = max(0.05, 1.0 - crop[0] - crop[2])
    fh = max(0.05, 1.0 - crop[1] - crop[3])
    return w / fw, h / fh


def _fallback_size(parts: dict[str, bytes]) -> tuple[float, float] | None:
    """Batas atas ukuran tampil kalau ukuran gambar tidak tertulis: seukuran
    slide (PPTX) atau halaman (DOCX). XLSX: tidak ada → jangan resize."""
    try:
        if "ppt/presentation.xml" in parts:
            root = etree.fromstring(parts["ppt/presentation.xml"], _PARSER)
            sz = root.find(f"{{{NS_P}}}sldSz")
            if sz is not None:
                return _int(sz, "cx") / EMU_PER_INCH, _int(sz, "cy") / EMU_PER_INCH
            return 13.333, 7.5
        if "word/document.xml" in parts:
            root = etree.fromstring(parts["word/document.xml"], _PARSER)
            best = None
            for pg in root.iter(f"{{{NS_W}}}pgSz"):
                w = _int(pg, f"{{{NS_W}}}w") / 1440.0
                h = _int(pg, f"{{{NS_W}}}h") / 1440.0
                if w > 0 and h > 0:
                    best = (max(w, best[0]), max(h, best[1])) if best else (w, h)
            return best or (8.5, 11.0)
    except Exception:
        pass
    return None


# ------------------------------------------------------------------------ paket

class _Package:
    def __init__(self, src: Path):
        with zipfile.ZipFile(src) as z:
            bad = z.testzip()
            if bad:
                raise RuntimeError(f"arsip rusak di bagian {bad}")
            self.order = [i.filename for i in z.infolist() if not i.is_dir()]
            self.parts = {n: z.read(n) for n in self.order}
        if "[Content_Types].xml" not in self.parts:
            raise RuntimeError("bukan dokumen Office (tidak ada [Content_Types].xml)")
        self.ct = etree.fromstring(self.parts["[Content_Types].xml"], _PARSER)
        self.ct_dirty = False
        self.rels: dict[str, etree._Element] = {}
        self.rels_dirty: set[str] = set()
        for name in self.order:
            if name.endswith(".rels") and "/_rels/" in "/" + name:
                try:
                    self.rels[name] = etree.fromstring(self.parts[name], _PARSER)
                except etree.XMLSyntaxError:
                    continue
        self._trees: dict[str, etree._Element | None] = {}

    def relationships(self):
        """→ (nama file rels, elemen Relationship, part sumber, target ter-resolve)."""
        for name, root in self.rels.items():
            source = _rels_source(name)
            for rel in root:
                if _local(rel) != "Relationship" or rel.get("TargetMode") == "External":
                    continue
                target = rel.get("Target")
                if target:
                    yield name, rel, source, _resolve(source, target)

    def tree(self, part: str):
        if part not in self._trees:
            try:
                self._trees[part] = etree.fromstring(self.parts[part], _PARSER) if part in self.parts else None
            except etree.XMLSyntaxError:
                self._trees[part] = None
        return self._trees[part]

    def ensure_default(self, ext: str) -> None:
        for el in self.ct:
            if _local(el) == "Default" and (el.get("Extension") or "").lower() == ext:
                return
        el = etree.Element(f"{{{NS_CT}}}Default")
        el.set("Extension", ext)
        el.set("ContentType", _CONTENT_TYPE[ext])
        self.ct.insert(0, el)
        self.ct_dirty = True

    def drop_override(self, part: str, rename_to: str | None = None, ext: str | None = None) -> None:
        for el in list(self.ct):
            if _local(el) == "Override" and (el.get("PartName") or "").lstrip("/") == part:
                if rename_to:
                    el.set("PartName", "/" + rename_to)
                    el.set("ContentType", _CONTENT_TYPE[ext])
                else:
                    self.ct.remove(el)
                self.ct_dirty = True

    def serialize(self, root) -> bytes:
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def compress_office(src: Path, dst: Path, settings: Settings, progress: Progress | None = None) -> dict:
    """Tulis versi terkompresi ke ``dst`` (selalu ditulis; pemanggil yang
    membandingkan ukurannya dengan file asli)."""
    def report(frac: float, msg: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, frac)), msg)

    report(0.02, "membaca dokumen")
    pkg = _Package(src)
    stats = OfficeStats()
    rels = list(pkg.relationships())
    referenced: dict[str, list[tuple[str, etree._Element, str]]] = {}
    for rels_name, rel, source, target in rels:
        referenced.setdefault(target, []).append((rels_name, rel, source))

    removed: set[str] = set()

    # 1. thumbnail
    for rels_name, rel, source, target in rels:
        if (rel.get("Type") or "").endswith("/metadata/thumbnail") and source == "":
            rel.getparent().remove(rel)
            pkg.rels_dirty.add(rels_name)
            if target in pkg.parts and len(referenced.get(target, [])) <= 1:
                removed.add(target)
                pkg.drop_override(target)
            stats.thumbnail_removed = True

    # 2. media yang tidak direferensikan relasi mana pun
    for name in pkg.order:
        if _MEDIA_DIR.match(name) and name not in referenced:
            removed.add(name)
            pkg.drop_override(name)
            stats.unused_removed += 1

    # 3. ukuran tampil tiap gambar
    media = [n for n in pkg.order if n not in removed and n in referenced
             and _sniff(pkg.parts[n]) and (_MEDIA_DIR.match(n) or any(
                 (r.get("Type") or "").endswith("/image") for _, r, _ in referenced[n]))]
    stats.images = len(media)
    stats.media_before = sum(len(pkg.parts[n]) for n in media)
    fallback = _fallback_size(pkg.parts)
    display: dict[str, tuple[float, float]] = {}
    r_attrs = [f"{{{NS_R}}}embed", f"{{{NS_R}}}id", f"{{{NS_R}}}pict", f"{{{NS_R}}}link"]
    usage_index: dict[str, dict[str, list]] = {}

    def usages(source: str, rid: str) -> list:
        if source not in usage_index:
            idx: dict[str, list] = {}
            root = pkg.tree(source)
            if root is not None:
                for el in root.iter():
                    if not isinstance(el.tag, str):
                        continue
                    for attr in r_attrs:
                        v = el.get(attr)
                        if v:
                            idx.setdefault(v, []).append(el)
            usage_index[source] = idx
        return usage_index[source].get(rid, [])

    if not settings.lossless and settings.dpi > 0:
        for name in media:
            w = h = 0.0
            unknown = False
            for _, rel, source in referenced[name]:
                els = usages(source, rel.get("Id") or "")
                if not els:
                    unknown = True
                    break
                for el in els:
                    size = _usage_size(el, fallback)
                    if size is None or size[0] <= 0 or size[1] <= 0:
                        unknown = True
                        break
                    w, h = max(w, size[0]), max(h, size[1])
                if unknown:
                    break
            display[name] = (0.0, 0.0) if unknown or w <= 0 else (w, h)

    # 4. kompres ulang gambar (paralel: sebagian besar waktu ada di mozjpeg/pngquant)
    done = 0

    def work(name: str):
        return name, images.recompress(pkg.parts[name], settings,
                                       display_in=display.get(name, (0.0, 0.0)),
                                       allow_jpeg_convert=True, exif_mode="keep")

    renamed: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for name, enc in pool.map(work, media):
            done += 1
            report(0.05 + 0.8 * done / max(1, len(media)), f"gambar {done}/{len(media)}")
            if enc is None or len(enc.data) >= len(pkg.parts[name]):
                continue
            old_kind = _sniff(pkg.parts[name])
            stats.recompressed += 1
            if "→" in enc.note and "×" in enc.note:
                stats.resized += 1
            base = posixpath.basename(name)
            stats.details.append(f"{base}: {len(pkg.parts[name]) / 1024:.0f} KB → "
                                 f"{len(enc.data) / 1024:.0f} KB ({enc.note})")
            if enc.ext == "png" and enc.kind == images.GRAPHIC and "pngquant" in enc.note:
                stats.kept_png += 1
            if enc.ext != old_kind:
                # format berubah (PNG/BMP/TIFF → JPEG/PNG): ganti nama part + relasinya
                if enc.ext == "jpeg" and old_kind == "png":
                    stats.to_jpeg += 1
                stem = name.rsplit(".", 1)[0] if "." in base else name
                new_name = f"{stem}.{enc.ext}"
                n = 1
                while new_name in pkg.parts or new_name in renamed.values():
                    new_name = f"{stem}_c{n}.{enc.ext}"
                    n += 1
                renamed[name] = new_name
                pkg.parts[new_name] = enc.data
                pkg.ensure_default(enc.ext)
                pkg.drop_override(name, rename_to=new_name, ext=enc.ext)
            else:
                pkg.parts[name] = enc.data

    def retarget(old: str, new: str) -> None:
        for rels_name, rel, source in referenced.get(old, []):
            target = rel.get("Target")
            if target.startswith("/"):
                rel.set("Target", "/" + new)
            else:
                rel.set("Target", posixpath.relpath(new, posixpath.dirname(source) or "."))
            pkg.rels_dirty.add(rels_name)
        referenced.setdefault(new, []).extend(referenced.pop(old, []))

    for old, new in renamed.items():
        retarget(old, new)
        removed.add(old)

    # 5. gambar identik cukup satu
    current = [renamed.get(n, n) for n in media]
    seen: dict[bytes, str] = {}
    for name in current:
        digest = hashlib.sha1(pkg.parts[name]).digest()
        first = seen.setdefault(digest, name)
        if first != name and posixpath.splitext(first)[1].lower() == posixpath.splitext(name)[1].lower():
            retarget(name, first)
            removed.add(name)
            pkg.drop_override(name)
            stats.duplicates_removed += 1
    stats.media_after = sum(len(pkg.parts[n]) for n in current if n not in removed)

    # 6. tulis ulang zip, deflate level 9
    report(0.9, "menulis arsip")
    for rels_name in pkg.rels_dirty:
        pkg.parts[rels_name] = pkg.serialize(pkg.rels[rels_name])
    if pkg.ct_dirty:
        pkg.parts["[Content_Types].xml"] = pkg.serialize(pkg.ct)
    order = ["[Content_Types].xml"] + [n for n in pkg.order if n != "[Content_Types].xml"]
    position = {old: i for i, old in enumerate(order)}
    final: list[tuple[int, str]] = []
    for old in order:
        name = renamed.get(old, old)
        if name in removed:
            continue  # tak terpakai / thumbnail / duplikat
        final.append((position[old], name))
    names = [n for _, n in sorted(final)]
    write_zip(dst, [(n, pkg.parts[n]) for n in names])
    report(1.0, "selesai")
    return {"stats": stats, "detail": stats.describe()}


def write_zip(dst: Path, entries: list[tuple[str, bytes]]) -> None:
    """ZIP deterministik: deflate level 9, atau STORED kalau deflate tidak membantu."""
    with zipfile.ZipFile(dst, "w") as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o600 << 16
            method = zipfile.ZIP_DEFLATED
            if len(data) > 256 and _sniff(data) in ("jpeg", "png"):
                if len(zlib.compress(data, 9)) >= len(data):
                    method = zipfile.ZIP_STORED
            elif not data:
                method = zipfile.ZIP_STORED
            info.compress_type = method
            z.writestr(info, data, compress_type=method,
                       compresslevel=9 if method == zipfile.ZIP_DEFLATED else None)
