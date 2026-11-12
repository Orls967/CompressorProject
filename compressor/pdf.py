"""Kompresi PDF.

Beberapa strategi dijalankan, lalu yang terkecil DAN lolos validasi dipakai:

  pikepdf        kompres ulang tiap gambar langsung di dalam PDF (teks, vektor,
                 font, tautan, form, tag tidak disentuh) + optimasi struktur
  pikepdf+gs     hasil di atas dilewatkan ke Ghostscript untuk subset font &
                 buang duplikat (gambar diteruskan apa adanya)
  gs-downsample  penyelamat: Ghostscript men-downsample semua gambar, dipakai
                 kalau dua strategi di atas belum banyak mengecilkan file

Teks dan vektor tidak pernah dirasterisasi oleh strategi mana pun.
"""
from __future__ import annotations

import hashlib
import io
import math
import shutil
import subprocess
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pikepdf
from pikepdf import Array, Dictionary, Name, Stream
from PIL import Image

from . import deps, images, validate
from .presets import Settings

pikepdf.settings.set_flate_compression_level(9)

Progress = Callable[[float, str], None]
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_PASSTHROUGH_FILTERS = {"/JPXDecode", "/JBIG2Decode", "/CCITTFaxDecode"}


@dataclass
class ImageStats:
    total: int = 0
    to_jpeg: int = 0
    kept_png: int = 0
    recompressed: int = 0
    downsampled: int = 0
    lossless: int = 0
    untouched: int = 0

    def describe(self) -> str:
        if not self.total:
            return "tanpa gambar"
        parts = [f"{self.total} gambar"]
        if self.downsampled:
            parts.append(f"{self.downsampled} di-downsample")
        if self.recompressed:
            parts.append(f"{self.recompressed} JPEG dikompres ulang")
        if self.to_jpeg:
            parts.append(f"{self.to_jpeg} foto → JPEG")
        if self.kept_png:
            parts.append(f"{self.kept_png} grafis/teks tetap lossless-palet")
        if self.lossless:
            parts.append(f"{self.lossless} dioptimasi lossless")
        return ", ".join(parts)


# ----------------------------------------------------- ukuran tampil tiap gambar

def _mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


def _inherited(page_obj, key: str):
    node, hops = page_obj, 0
    while node is not None and hops < 50:
        val = node.get(key)
        if val is not None:
            return val
        node = node.get("/Parent")
        hops += 1
    return None


def image_display_sizes(pdf: pikepdf.Pdf) -> dict[tuple[int, int], tuple[float, float]]:
    """objgen gambar → ukuran tampil terbesar (lebar, tinggi) dalam point.

    Dihitung dengan mengikuti matriks transformasi (cm) di content stream tiap
    halaman, termasuk Form XObject bersarang. Gambar yang tidak ditemukan
    (dipakai di pola/anotasi) tidak akan di-downsample.
    """
    sizes: dict[tuple[int, int], tuple[float, float]] = {}

    def record(obj, ctm):
        w = math.hypot(ctm[0], ctm[1])
        h = math.hypot(ctm[2], ctm[3])
        targets = [obj]
        for key in ("/SMask", "/Mask"):
            m = obj.get(key)
            if isinstance(m, Stream):
                targets.append(m)
        for t in targets:
            pw, ph = sizes.get(t.objgen, (0.0, 0.0))
            sizes[t.objgen] = (max(pw, w), max(ph, h))

    def walk(container, resources, ctm, depth):
        if depth > 10 or resources is None:
            return
        xobjs = resources.get("/XObject")
        if xobjs is None:
            return
        try:
            ops = pikepdf.parse_content_stream(container, "q Q cm Do")
        except Exception:
            return
        stack, cur = [], ctm
        for operands, operator in ops:
            op = str(operator)
            if op == "q":
                stack.append(cur)
            elif op == "Q":
                if stack:
                    cur = stack.pop()
            elif op == "cm":
                try:
                    cur = _mul(tuple(float(x) for x in operands), cur)
                except Exception:
                    pass
            elif op == "Do" and operands:
                xo = xobjs.get(str(operands[0]))
                if not isinstance(xo, Stream):
                    continue
                sub = xo.get("/Subtype")
                if sub == Name.Image:
                    record(xo, cur)
                elif sub == Name.Form:
                    m = xo.get("/Matrix")
                    try:
                        fm = tuple(float(x) for x in m) if m is not None else _IDENTITY
                    except Exception:
                        fm = _IDENTITY
                    walk(xo, xo.get("/Resources", resources), _mul(fm, cur), depth + 1)

    for page in pdf.pages:
        try:
            unit = float(page.obj.get("/UserUnit", 1.0))
            walk(page, _inherited(page.obj, "/Resources"), (unit, 0, 0, unit, 0, 0), 0)
        except Exception:
            continue
    return sizes


# --------------------------------------------------------------- gambar di PDF

def _filters(obj) -> list[str]:
    f = obj.get("/Filter")
    if f is None:
        return []
    if isinstance(f, Array):
        return [str(x) for x in f]
    return [str(f)]


def _colorspace(obj) -> tuple[str, int, object]:
    """→ (jenis, jumlah komponen, basis). jenis: gray/rgb/cmyk/indexed/other."""
    cs = obj.get("/ColorSpace")
    if cs is None:
        return "other", 0, None

    def simple(c):
        if isinstance(c, Name):
            return {"/DeviceGray": ("gray", 1), "/DeviceRGB": ("rgb", 3),
                    "/DeviceCMYK": ("cmyk", 4), "/G": ("gray", 1), "/RGB": ("rgb", 3),
                    "/CMYK": ("cmyk", 4)}.get(str(c), ("other", 0))
        if isinstance(c, Array) and len(c) >= 2:
            kind = str(c[0])
            if kind == "/ICCBased":
                n = int(c[1].get("/N", 0))
                return ({1: "gray", 3: "rgb", 4: "cmyk"}.get(n, "other"), n)
            if kind == "/CalRGB":
                return "rgb", 3
            if kind == "/CalGray":
                return "gray", 1
            if kind == "/Separation":
                return "other", 1
            if kind == "/DeviceN":
                return "other", len(c[1])
            if kind == "/Lab":
                return "other", 3
        return "other", 0

    if isinstance(cs, Array) and len(cs) == 4 and str(cs[0]) in ("/Indexed", "/I"):
        return "indexed", 1, cs
    kind, n = simple(cs)
    return kind, n, cs


def _write_png_stream(obj, png: dict, colorspace=None) -> None:
    """Tanam aliran IDAT PNG ke stream PDF (FlateDecode + predictor PNG)."""
    colors = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[png["color"]]
    obj.write(png["idat"], filter=Name.FlateDecode,
              decode_parms=Dictionary(Predictor=15, Colors=colors,
                                      BitsPerComponent=png["depth"], Columns=png["width"]))
    obj.Width, obj.Height = png["width"], png["height"]
    obj.BitsPerComponent = png["depth"]
    if colorspace is not None:
        obj.ColorSpace = colorspace


def _png_stream(img: Image.Image, quantized: bytes | None = None) -> dict | None:
    data = quantized if quantized is not None else images.png_bytes(img, 1)
    data = images.optimize_png_lossless(data, keep_structure=True)
    png = images.parse_png(data)
    if not png or png.get("interlace") or png["color"] not in (0, 2, 3, 4, 6):
        return None
    return png


class _ImagePass:
    """Kompres ulang semua gambar di satu PDF. Hasil encode di-cache supaya
    strategi berikutnya (yang gambarnya identik) tidak mengulang kerja berat."""

    def __init__(self, settings: Settings, cache: dict):
        self.s = settings
        self.cache = cache

    def run(self, pdf: pikepdf.Pdf, lossy: bool, progress: Progress | None = None) -> ImageStats:
        stats = ImageStats()
        sizes = image_display_sizes(pdf) if lossy else {}
        imgs, masks = [], set()
        for obj in pdf.objects:
            if isinstance(obj, Stream) and obj.get("/Subtype") == Name.Image:
                imgs.append(obj)
                for key in ("/SMask", "/Mask"):
                    m = obj.get(key)
                    if isinstance(m, Stream):
                        masks.add(m.objgen)
        self.touched: set[tuple[int, int]] = set()
        for i, obj in enumerate(imgs):
            if progress:
                progress(i / max(1, len(imgs)), f"gambar {i + 1}/{len(imgs)}")
            stats.total += 1
            try:
                self._one(obj, sizes.get(obj.objgen), lossy, obj.objgen in masks, stats)
            except Exception:
                stats.untouched += 1
        return stats

    # -- satu gambar
    def _one(self, obj, display_pt, lossy: bool, is_mask: bool, stats: ImageStats) -> None:
        if obj.get("/ImageMask"):
            stats.untouched += 1
            return
        filters = _filters(obj)
        if any(f in _PASSTHROUGH_FILTERS for f in filters):
            stats.untouched += 1
            return
        raw_len = len(obj.read_raw_bytes())
        w, h = int(obj.Width), int(obj.Height)
        bpc = int(obj.get("/BitsPerComponent", 8))
        has_decode = obj.get("/Decode") is not None
        color_key = isinstance(obj.get("/Mask"), Array)
        sm = obj.get("/SMask")
        matte = isinstance(sm, Stream) and sm.get("/Matte") is not None
        can_resize = display_pt is not None and not matte and not (is_mask and obj.get("/Matte") is not None)
        display_in = (display_pt[0] / 72.0, display_pt[1] / 72.0) if can_resize else None

        if filters and filters[-1] == "/DCTDecode":
            jpeg = obj.read_bytes(decode_level=pikepdf.StreamDecodeLevel.generalized) \
                if len(filters) > 1 else obj.read_raw_bytes()
            self._jpeg(obj, jpeg, raw_len, w, h, display_in, lossy and not has_decode and not color_key, stats)
            return
        if bpc != 8:
            stats.untouched += 1
            return
        kind, ncomp, cs = _colorspace(obj)
        try:
            raw = obj.read_bytes()
        except Exception:
            stats.untouched += 1
            return
        if kind == "other" and ncomp == 0:
            ncomp = len(raw) // (w * h) if w * h else 0
        if ncomp not in (1, 2, 3, 4) or len(raw) != w * h * ncomp:
            stats.untouched += 1
            return
        lossy_ok = lossy and not has_decode and not color_key and kind in ("gray", "rgb", "indexed")
        self._raster(obj, raw, raw_len, w, h, kind, ncomp, cs, display_in, lossy_ok, is_mask, stats)

    def _scale(self, w: int, h: int, display_in, kind: str) -> float:
        if not display_in or display_in[0] <= 0 or display_in[1] <= 0:
            return 1.0
        dpi = images.target_dpi(kind, self.s)
        if dpi <= 0:
            return 1.0
        return max(display_in[0] * dpi / w, display_in[1] * dpi / h)

    def _key(self, tag: str, payload: bytes, *extra) -> tuple:
        return (tag, hashlib.sha1(payload).digest(), *extra)

    # -- JPEG
    def _jpeg(self, obj, jpeg: bytes, raw_len: int, w, h, display_in, lossy: bool, stats: ImageStats):
        disp = tuple(round(x, 2) for x in display_in) if display_in else None
        key = self._key("jpeg", jpeg, disp, lossy)
        res = self.cache.get(key)
        if res is None:
            res = self._encode_jpeg(jpeg, w, h, display_in, lossy)
            self.cache[key] = res
        mode, data, nw, nh, gray = res
        if data is None or len(data) >= raw_len:
            stats.untouched += 1
            return
        obj.write(data, filter=Name.DCTDecode)
        self.touched.add(obj.objgen)
        if mode == "lossless":
            stats.lossless += 1
            return
        obj.Width, obj.Height, obj.BitsPerComponent = nw, nh, 8
        if gray:
            obj.ColorSpace = Name.DeviceGray
        stats.recompressed += 1
        if (nw, nh) != (w, h):
            stats.downsampled += 1

    def _encode_jpeg(self, jpeg: bytes, w, h, display_in, lossy: bool):
        lossless = images.optimize_jpeg_lossless(jpeg, strip_all=True)
        best = ("lossless", lossless if len(lossless) < len(jpeg) else jpeg, w, h, False)
        if not lossy:
            return best
        try:
            img = Image.open(io.BytesIO(jpeg))
            q_in = images.estimate_jpeg_quality(img)
            img.load()
        except Exception:
            return best
        if img.mode not in ("L", "RGB") or img.size != (w, h):
            return best
        kind = images.classify(img)
        scale = self._scale(w, h, display_in, kind)
        q = images.jpeg_quality_for(kind, self.s)
        to_gray = self.s.grayscale and img.mode == "RGB"
        if scale >= 0.9 and not to_gray and q_in is not None and q_in <= q + 3:
            return best
        if scale < 0.9:
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
        if to_gray:
            img = img.convert("L")
        out = images.encode_jpeg(img, q)
        if len(out) < len(best[1]) * 0.95:
            return ("lossy", out, img.size[0], img.size[1], to_gray)
        return best

    # -- raster (Flate/LZW/…)
    def _raster(self, obj, raw, raw_len, w, h, kind, ncomp, cs, display_in, lossy, is_mask, stats):
        disp = tuple(round(x, 2) for x in display_in) if display_in else None
        pal = _lookup_bytes(cs) if kind == "indexed" and lossy else b""
        key = self._key("raster", raw, w, h, ncomp, kind, pal, disp, lossy, is_mask)
        res = self.cache.get(key)
        if res is None:
            res = self._encode_raster(raw, raw_len, w, h, kind, ncomp, cs, display_in, lossy, is_mask)
            self.cache[key] = res
        mode, payload, meta = res
        if payload is None:
            stats.untouched += 1
            return
        size = len(payload) if mode == "jpeg" else len(payload["idat"])
        if size >= raw_len:
            stats.untouched += 1
            return
        self.touched.add(obj.objgen)
        base = cs[1] if kind == "indexed" else cs
        if mode == "lossless":
            _write_png_stream(obj, payload)          # sampel identik, colorspace tetap
            stats.lossless += 1
            return
        if mode == "jpeg":
            obj.write(payload, filter=Name.DCTDecode)
            obj.Width, obj.Height, obj.BitsPerComponent = meta["w"], meta["h"], 8
            obj.ColorSpace = Name.DeviceGray if meta["gray"] else base
            stats.to_jpeg += 1
        else:  # png lossy / resize
            if payload["color"] == 3:
                hival = len(payload["plte"]) // 3 - 1
                pal_base = Name.DeviceRGB if (meta["gray"] or kind == "gray") else base
                colorspace = Array([Name.Indexed, pal_base, hival, pikepdf.String(payload["plte"])])
            elif payload["color"] == 0:
                colorspace = base if kind == "gray" else Name.DeviceGray
            else:
                colorspace = base
            _write_png_stream(obj, payload, colorspace)
            stats.kept_png += 1
        if (meta["w"], meta["h"]) != (w, h):
            stats.downsampled += 1

    def _encode_raster(self, raw, raw_len, w, h, kind, ncomp, cs, display_in, lossy, is_mask):
        mode = {1: "L", 2: "LA", 3: "RGB", 4: "RGBA"}[ncomp]
        plain = Image.frombytes(mode, (w, h), raw)

        def lossless():
            return ("lossless", _png_stream(plain), None)

        if not lossy:
            return lossless()
        lossy_res = self._encode_raster_lossy(plain, w, h, kind, cs, display_in, is_mask)
        if lossy_res is None:
            return lossless()
        size = len(lossy_res[1]) if lossy_res[0] == "jpeg" else len(lossy_res[1]["idat"])
        if size < raw_len * 0.6:
            return lossy_res  # jauh lebih kecil → tidak perlu menghitung kandidat lossless (lambat)
        ll = lossless()
        ll_size = len(ll[1]["idat"]) if ll[1] else float("inf")
        return lossy_res if size < ll_size * 0.95 else ll

    def _encode_raster_lossy(self, plain, w, h, kind, cs, display_in, is_mask):
        if kind == "indexed":
            base_kind, base_n = _colorspace_of(cs[1])
            if base_kind not in ("gray", "rgb"):
                return None
            lookup = _lookup_bytes(cs)
            if base_n == 1:
                lookup = b"".join(bytes([v, v, v]) for v in lookup)
            img = plain.convert("P")
            img.putpalette(lookup[:768])
            img = img.convert("RGB")
            if base_n == 1:
                img = img.convert("L")
        else:
            img = plain
        pic_kind = images.GRAPHIC if is_mask else images.classify(img)
        scale = self._scale(w, h, display_in, pic_kind)
        work = img
        if scale < 0.9:
            work = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.Resampling.LANCZOS)
        to_gray = self.s.grayscale and work.mode == "RGB" and not is_mask
        if to_gray:
            work = work.convert("L")
        meta = {"w": work.size[0], "h": work.size[1], "gray": to_gray}

        if is_mask:
            # Kanal alpha: resize saja, tetap lossless (tanpa JPEG/kuantisasi).
            if work is img:
                return None
            png = _png_stream(work)
            return ("png", png, meta) if png else None
        if pic_kind != images.GRAPHIC:
            return ("jpeg", images.encode_jpeg(work, images.jpeg_quality_for(pic_kind, self.s)), meta)
        # Grafis / teks: tetap lossless-palet (tanpa artefak JPEG di tepi huruf).
        # Dicoba pada ukuran hasil resize DAN ukuran asli; ambil yang terkecil.
        variants = [(work, meta)]
        if work.size != img.size:
            variants.append((img.convert("L") if to_gray else img, {"w": w, "h": h, "gray": to_gray}))
        cands = []
        for im, m in variants:
            if im.mode == "RGB":
                qd = images.quantize_png(im, self.s.png_colors, self.s.png_quality, flat_art=True)
                png = _png_stream(im, qd) if qd else None
            else:
                png = _png_stream(im)
            if png:
                cands.append((len(png["idat"]), png, m))
        if not cands:
            return None
        _, png, m = min(cands, key=lambda c: c[0])
        return ("png", png, m)


def _colorspace_of(cs) -> tuple[str, int]:
    kind, n, _ = _colorspace({"/ColorSpace": cs})
    return kind, n


def _lookup_bytes(cs) -> bytes:
    """Tabel palet dari colorspace /Indexed (bisa berupa string atau stream)."""
    table = cs[3]
    return table.read_bytes() if isinstance(table, Stream) else bytes(table)


# ---------------------------------------------------------------- struktur PDF

def _dedupe_images(pdf: pikepdf.Pdf) -> int:
    """Gambar yang byte-nya identik cukup disimpan sekali."""
    seen: dict[bytes, object] = {}
    replaced = 0

    def fingerprint(xo) -> bytes | None:
        try:
            hsh = hashlib.sha1(xo.read_raw_bytes())
            for k in sorted(xo.keys()):
                if k in ("/Length",):
                    continue
                v = xo[k]
                hsh.update(k.encode())
                hsh.update(v.unparse() if isinstance(v, pikepdf.Object) else repr(v).encode())
            return hsh.digest()
        except Exception:
            return None

    def scan(resources, depth=0):
        nonlocal replaced
        if resources is None or depth > 10:
            return
        xobjs = resources.get("/XObject")
        if xobjs is None:
            return
        for name in list(xobjs.keys()):
            xo = xobjs[name]
            if not isinstance(xo, Stream):
                continue
            sub = xo.get("/Subtype")
            if sub == Name.Image:
                fp = fingerprint(xo)
                if fp is None:
                    continue
                first = seen.setdefault(fp, xo)
                if first.objgen != xo.objgen:
                    xobjs[name] = first
                    replaced += 1
            elif sub == Name.Form:
                scan(xo.get("/Resources"), depth + 1)

    for page in pdf.pages:
        try:
            scan(_inherited(page.obj, "/Resources"))
        except Exception:
            continue
    return replaced


def _strip_metadata(pdf: pikepdf.Pdf) -> None:
    root = pdf.Root
    for key in ("/Metadata", "/PieceInfo"):
        if key in root:
            del root[key]
    for page in pdf.pages:
        for key in ("/Thumb", "/PieceInfo", "/Metadata"):
            if key in page.obj:
                del page.obj[key]
    for obj in pdf.objects:
        if isinstance(obj, Stream) and obj.get("/Subtype") in (Name.Image, Name.Form):
            for key in ("/Metadata", "/PieceInfo"):
                if key in obj:
                    del obj[key]


def _recompress_flate(pdf: pikepdf.Pdf, skip: set) -> None:
    """Deflate ulang level 9 untuk stream tanpa predictor (content stream, font, dll.)."""
    for obj in pdf.objects:
        if not isinstance(obj, Stream) or obj.objgen in skip:
            continue
        if obj.get("/Filter") != Name.FlateDecode or obj.get("/DecodeParms") is not None:
            continue
        if obj.get("/Type") in (Name.XRef, Name.ObjStm):
            continue
        try:
            raw = obj.read_raw_bytes()
            new = zlib.compress(zlib.decompress(raw), 9)
        except Exception:
            continue
        if len(new) < len(raw):
            obj.write(new, filter=Name.FlateDecode)


def pikepdf_pass(src: Path, dst: Path, settings: Settings, lossy: bool, cache: dict,
                 progress: Progress | None = None) -> ImageStats:
    with pikepdf.open(src) as pdf:
        encrypted = pdf.is_encrypted
        ip = _ImagePass(settings, cache)
        stats = ip.run(pdf, lossy, progress)
        _dedupe_images(pdf)
        _strip_metadata(pdf)
        try:
            pdf.remove_unreferenced_resources()
        except Exception:
            pass
        _recompress_flate(pdf, ip.touched)
        if progress:
            progress(0.98, "menulis PDF")
        pdf.save(dst,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate,
                 compress_streams=True,
                 recompress_flate=False,
                 linearize=settings.linearize,
                 encryption=True if encrypted else None)
    return stats


# ------------------------------------------------------------------ Ghostscript

def ghostscript(src: Path, dst: Path, settings: Settings, downsample: bool, timeout: int = 900) -> bool:
    gs = deps.path("gs")
    if not gs:
        return False
    cmd = [gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
           "-dCompatibilityLevel=1.7", "-dAutoRotatePages=/None", "-dDetectDuplicateImages=true",
           "-dCompressFonts=true", "-dSubsetFonts=true", "-dEmbedAllFonts=true",
           "-dCompressPages=true", "-dPrinted=false", f"-sOutputFile={dst}"]
    if settings.grayscale:
        cmd += ["-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray"]
    if downsample and settings.dpi > 0:
        # Ghostscript HANYA men-downsample (keluaran lossless/Flate); pemilihan
        # JPEG vs palet dan kualitasnya tetap dilakukan langkah pikepdf, supaya
        # semua strategi memakai aturan kualitas yang sama dan gambar berisi
        # teks tidak pernah dijadikan JPEG. Resolusi yang dipakai = batas untuk
        # gambar grafis; foto diturunkan lagi ke DPI foto oleh langkah pikepdf.
        dpi = max(settings.dpi, settings.graphic_dpi)
        cmd += [
            "-dDownsampleColorImages=true", "-dColorImageDownsampleType=/Bicubic",
            f"-dColorImageResolution={dpi}", "-dColorImageDownsampleThreshold=1.15",
            "-dDownsampleGrayImages=true", "-dGrayImageDownsampleType=/Bicubic",
            f"-dGrayImageResolution={dpi}", "-dGrayImageDownsampleThreshold=1.15",
            "-dDownsampleMonoImages=true", "-dMonoImageDownsampleType=/Subsample",
            f"-dMonoImageResolution={max(300, dpi * 2)}", "-dMonoImageDownsampleThreshold=1.5",
        ]
    else:
        cmd += ["-dDownsampleColorImages=false", "-dDownsampleGrayImages=false",
                "-dDownsampleMonoImages=false"]
    cmd += ["-dAutoFilterColorImages=false", "-dAutoFilterGrayImages=false",
            "-dColorImageFilter=/FlateEncode", "-dGrayImageFilter=/FlateEncode",
            "-dPassThroughJPEGImages=true", "-dPassThroughJPXImages=true",
            "-f", str(src)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        return False
    return p.returncode == 0 and dst.exists() and dst.stat().st_size > 200


# -------------------------------------------------------------------- orkestrasi

@dataclass
class Candidate:
    label: str
    path: Path
    size: int
    validation: validate.Validation | None = None
    stats: ImageStats | None = None


def compress_pdf(src: Path, dst: Path, settings: Settings, progress: Progress | None = None) -> dict:
    """Tulis ``dst`` hanya kalau ada kandidat valid yang lebih kecil dari ``src``.

    Mengembalikan dict: written, strategy, validation, candidates, notes, detail.
    """
    def report(frac: float, msg: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, frac)), msg)

    src_size = src.stat().st_size
    notes: list[str] = []
    try:
        with pikepdf.open(src) as pdf:
            encrypted = pdf.is_encrypted
            signed = bool("/AcroForm" in pdf.Root
                          and int(pdf.Root.AcroForm.get("/SigFlags", 0)) & 1)
    except pikepdf.PasswordError:
        raise RuntimeError("PDF dilindungi password — buka kuncinya dulu sebelum dikompres")
    if signed:
        notes.append("PDF bertanda tangan digital: tanda tangan tidak lagi valid setelah dikompres")

    lossy = not settings.lossless
    use_gs = lossy and deps.has("gs") and not encrypted
    if lossy and not deps.has("gs"):
        notes.append("Ghostscript belum terpasang → hanya strategi pikepdf (pasang: brew install ghostscript)")
        if settings.grayscale:
            notes.append("Tanpa Ghostscript, grayscale hanya berlaku untuk gambar (teks/vektor tetap berwarna)")
    elif lossy and encrypted:
        notes.append("PDF terenkripsi: Ghostscript dilewati supaya proteksi tetap ada")

    report(0.02, "menganalisis PDF")
    reference = validate.pdf_reference(src)
    cache: dict = {}
    candidates: list[Candidate] = []

    with tempfile.TemporaryDirectory(prefix="compressor_pdf_") as tmp:
        tmpd = Path(tmp)

        def add(label: str, path: Path, stats: ImageStats, frac: float) -> Candidate:
            cand = Candidate(label, path, path.stat().st_size, stats=stats)
            report(frac, f"{label}: validasi")
            cand.validation = validate.validate_pdf(src, path, reference=reference)
            if not cand.validation.ok:
                notes.append(f"{label}: ditolak — {cand.validation.reason}")
            candidates.append(cand)
            return cand

        # 1. pikepdf: gambar dikompres in-place, struktur dioptimasi.
        first = tmpd / "pikepdf.pdf"
        first_stats: ImageStats | None = None
        try:
            first_stats = pikepdf_pass(src, first, settings, lossy, cache,
                                       lambda f, m: report(0.05 + f * 0.45, f"pikepdf: {m}"))
            add("pikepdf", first, first_stats, 0.52)
        except Exception as e:
            notes.append(f"pikepdf: gagal ({type(e).__name__}: {e})")

        # 2. pikepdf+gs: Ghostscript merapikan font (subset, duplikat) pada PDF
        #    yang gambarnya SUDAH kecil → cepat. Gambar dilewatkan apa adanya.
        if use_gs and first.exists():
            try:
                report(0.55, "pikepdf+gs: Ghostscript")
                mid = tmpd / "gs_mid.pdf"
                if ghostscript(first, mid, settings, downsample=False):
                    out = tmpd / "pikepdf_gs.pdf"
                    stats = pikepdf_pass(mid, out, settings, lossy, cache,
                                         lambda f, m: report(0.65 + f * 0.1, f"pikepdf+gs: {m}"))
                    add("pikepdf+gs", out, first_stats or stats, 0.78)
                else:
                    notes.append("pikepdf+gs: Ghostscript gagal memproses file ini")
            except Exception as e:
                notes.append(f"pikepdf+gs: gagal ({type(e).__name__}: {e})")

        # 3. gs-downsample (penyelamat): kalau hasil sejauh ini masih besar,
        #    kemungkinan ada gambar yang ukuran tampilnya tidak terbaca (inline
        #    image, pola, anotasi). Ghostscript men-downsample semuanya.
        valid = [c for c in candidates if c.validation and c.validation.ok]
        best_so_far = min((c.size for c in valid), default=src_size)
        if use_gs and settings.dpi > 0 and best_so_far > src_size * 0.6:
            try:
                report(0.8, "gs-downsample: Ghostscript")
                mid = tmpd / "gs_full.pdf"
                if ghostscript(src, mid, settings, downsample=True):
                    out = tmpd / "gs_downsample.pdf"
                    stats = pikepdf_pass(mid, out, settings, lossy, cache,
                                         lambda f, m: report(0.88 + f * 0.06, f"gs-downsample: {m}"))
                    add("gs-downsample", out, stats, 0.95)
            except Exception as e:
                notes.append(f"gs-downsample: gagal ({type(e).__name__}: {e})")

        valid = [c for c in candidates if c.validation and c.validation.ok]
        if settings.grayscale and use_gs:
            # Hanya kandidat yang lewat Ghostscript yang teks/vektornya ikut abu-abu.
            gray = [c for c in valid if c.label != "pikepdf"]
            if gray:
                valid = gray
            elif valid:
                notes.append("Grayscale hanya diterapkan ke gambar (konversi Ghostscript gagal divalidasi)")
        result = {
            "written": False, "strategy": None, "validation": None, "notes": notes,
            "candidates": [{"label": c.label, "size": c.size,
                            "ok": bool(c.validation and c.validation.ok),
                            "reason": c.validation.reason if c.validation else ""} for c in candidates],
            "detail": "",
        }
        if valid:
            best = min(valid, key=lambda c: c.size)
            result["strategy"] = best.label
            result["validation"] = best.validation
            result["detail"] = best.stats.describe() if best.stats else ""
            if best.size < src_size:
                shutil.copyfile(best.path, dst)
                result["written"] = True
        report(1.0, "selesai")
        return result
