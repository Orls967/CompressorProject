"""Kompresi gambar: dipakai untuk file JPG/PNG mandiri, media di dalam
DOCX/PPTX/XLSX, dan gambar di dalam PDF.

Aturan penting:
- FOTO tanpa transparansi boleh dijadikan JPEG.
- Screenshot / diagram / gambar berisi teks ("graphic") TETAP PNG dan
  dikuantisasi dengan pngquant, supaya tepi teks tidak blur oleh artefak JPEG.
"""
from __future__ import annotations

import io
import struct
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

from . import deps
from .presets import Settings

Image.MAX_IMAGE_PIXELS = 600_000_000  # scan besar tidak dianggap "bom dekompresi"

PHOTO = "photo"
GRAPHIC = "graphic"   # screenshot, diagram, gambar berisi teks → tetap PNG
SCAN = "scan"         # halaman teks hasil scan/foto dokumen → JPEG, tapi DPI lebih tinggi


# --------------------------------------------------------------------------- util

def _run(cmd: list[str], data: bytes, timeout: int = 600, ok_codes=(0,)) -> tuple[int, bytes]:
    try:
        p = subprocess.run(cmd, input=data, capture_output=True, timeout=timeout)
    except Exception:
        return -1, b""
    if p.returncode not in ok_codes:
        return p.returncode, b""
    return p.returncode, p.stdout


def has_real_alpha(img: Image.Image) -> bool:
    """True kalau ada piksel yang benar-benar transparan (bukan sekadar punya kanal alpha)."""
    if img.mode in ("RGBA", "LA", "PA"):
        return img.getchannel("A").getextrema()[0] < 255
    if img.mode == "P" and "transparency" in img.info:
        return img.convert("RGBA").getchannel("A").getextrema()[0] < 255
    if img.mode in ("RGB", "L") and "transparency" in img.info:
        return True  # tRNS color-key
    return False


def image_metrics(img: Image.Image) -> dict:
    """Ciri untuk membedakan foto dari gambar grafis, diukur pada potongan
    resolusi asli (tanpa resize, supaya interpolasi tidak mengaburkan cirinya).

    flat      : porsi piksel yang identik dengan tetangga kanan & bawahnya
    sharp_nf  : di antara piksel yang TIDAK rata, porsi yang loncatannya tajam
    sharp_all : porsi piksel bertepi tajam dari seluruh piksel
    """
    w, h = img.size
    rgb = img.convert("RGB")
    tile = 192
    nx = min(4, max(1, w // tile))
    ny = min(4, max(1, h // tile))
    total = flat = nonflat = sharp = bright = 0
    for j in range(ny):
        for i in range(nx):
            x = max(0, min(w - tile, int((i + 0.5) * w / nx - tile / 2))) if w > tile else 0
            y = max(0, min(h - tile, int((j + 0.5) * h / ny - tile / 2))) if h > tile else 0
            a = np.asarray(rgb.crop((x, y, min(w, x + tile), min(h, y + tile))), dtype=np.int16)
            if a.shape[0] < 2 or a.shape[1] < 2:
                continue
            dr = np.abs(a[:, 1:, :] - a[:, :-1, :]).max(axis=2)[:-1, :]
            dd = np.abs(a[1:, :, :] - a[:-1, :, :]).max(axis=2)[:, :-1]
            m = np.maximum(dr, dd)
            total += m.size
            flat += int((m == 0).sum())
            nonflat += int((m > 0).sum())
            sharp += int((m >= 48).sum())
            bright += int((a[:-1, :-1, :].mean(axis=2) > 160).sum())
    if not total:
        return {"flat": 1.0, "sharp_nf": 1.0, "sharp_all": 0.0, "bright": 1.0}
    return {"flat": flat / total, "sharp_nf": sharp / max(1, nonflat),
            "sharp_all": sharp / total, "bright": bright / total}


def classify(img: Image.Image) -> str:
    """Bedakan foto vs gambar grafis (screenshot, diagram, gambar berisi teks).

    Gambar grafis = banyak area berwarna rata DAN tepinya tajam (huruf, garis,
    kotak). Foto = hampir tidak ada area yang benar-benar rata, atau kalau ada
    (langit, latar polos) peralihannya halus.
    """
    w, h = img.size
    if w * h < 96 * 96:
        return GRAPHIC
    m = image_metrics(img)
    if m["flat"] >= 0.9:
        return GRAPHIC
    if m["flat"] >= 0.35 and m["sharp_nf"] >= 0.06:
        return GRAPHIC
    if m["flat"] >= 0.2 and m["sharp_all"] >= 0.02:
        return GRAPHIC
    if m["sharp_all"] >= 0.03 and m["bright"] >= 0.6:
        return SCAN  # latar terang ber-noise + banyak goresan tajam = teks hasil scan
    return PHOTO


def jpeg_quality_for(kind: str, settings: Settings) -> int:
    """Gambar berisi teks diberi kualitas JPEG lebih tinggi supaya tidak muncul
    ringing di sekitar huruf."""
    q = settings.jpeg_quality
    if kind == GRAPHIC:
        return min(90, q + 15)
    if kind == SCAN:
        return max(q, 60)
    return q


def target_dpi(kind: str, settings: Settings) -> int:
    """DPI target per jenis gambar (0 = jangan di-resize)."""
    if settings.dpi <= 0:
        return 0
    if kind == GRAPHIC:
        return settings.graphic_dpi
    if kind == SCAN:  # teks hasil scan: jangan di bawah 150 DPI supaya tetap terbaca
        return max(settings.dpi, min(settings.graphic_dpi, 150))
    return settings.dpi


_STD_LUMA_SUM = 3929  # jumlah tabel kuantisasi luminance standar JPEG (kualitas 50)


def estimate_jpeg_quality(img: Image.Image) -> int | None:
    q = getattr(img, "quantization", None)
    if not q:
        return None
    try:
        table = q[min(q.keys())]
        scale = sum(table) / _STD_LUMA_SUM * 100.0
    except Exception:
        return None
    if scale <= 0:
        return 100
    quality = (200 - scale) / 2 if scale <= 100 else 5000 / scale
    return int(max(1, min(100, round(quality))))


def _to_srgb(img: Image.Image) -> Image.Image:
    """Konversi profil warna non-sRGB (mis. Display P3) ke sRGB supaya profil
    ICC bisa dibuang tanpa menggeser warna."""
    icc = img.info.get("icc_profile")
    if not icc or img.mode not in ("RGB", "RGBA"):
        return img
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        if "srgb" in (ImageCms.getProfileDescription(src) or "").lower():
            return img
        dst = ImageCms.createProfile("sRGB")
        if img.mode == "RGBA":
            alpha = img.getchannel("A")
            rgb = ImageCms.profileToProfile(img.convert("RGB"), src, dst, outputMode="RGB")
            rgb.putalpha(alpha)
            return rgb
        return ImageCms.profileToProfile(img, src, dst, outputMode="RGB")
    except Exception:
        return img


def _resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    if img.mode == "RGBA":
        # Premultiply supaya tepi transparan tidak berhalo gelap.
        return img.convert("RGBa").resize(size, Image.Resampling.LANCZOS).convert("RGBA")
    if img.mode == "LA":
        return _resize(img.convert("RGBA"), size).convert("LA")
    return img.resize(size, Image.Resampling.LANCZOS)


# ----------------------------------------------------------------------- encoder

def encode_jpeg(img: Image.Image, quality: int, exif: bytes | None = None) -> bytes:
    """JPEG lewat mozjpeg (trellis + progressive); fallback ke Pillow."""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    quality = int(max(5, min(95, quality)))
    cjpeg = deps.path("cjpeg")
    if cjpeg and not exif:
        w, h = img.size
        magic = b"P5" if img.mode == "L" else b"P6"
        ppm = magic + f"\n{w} {h}\n255\n".encode() + img.tobytes()
        cmd = [cjpeg, "-quality", str(quality), "-optimize", "-progressive"]
        if img.mode == "RGB":
            cmd += ["-sample", "2x2" if quality < 90 else "1x1"]
        code, out = _run(cmd, ppm)
        if code == 0 and out[:2] == b"\xff\xd8":
            return out
    buf = io.BytesIO()
    kw = dict(quality=quality, optimize=True, progressive=True)
    if img.mode == "RGB":
        kw["subsampling"] = 2 if quality < 90 else 0
    if exif:
        kw["exif"] = exif
    img.save(buf, "JPEG", **kw)
    return buf.getvalue()


def optimize_jpeg_lossless(data: bytes, keep_all_markers: bool = False,
                           strip_all: bool = False) -> bytes:
    """Optimasi Huffman + progressive tanpa mengubah satu piksel pun.

    Default menyimpan profil ICC saja; ``strip_all`` membuang semua marker
    (dipakai di PDF, yang mengabaikan ICC/EXIF di dalam JPEG).
    """
    jpegtran = deps.path("jpegtran")
    if not jpegtran:
        return data
    copy = "all" if keep_all_markers else ("none" if strip_all else "icc")
    code, out = _run([jpegtran, "-copy", copy, "-optimize", "-progressive"], data)
    if code != 0 and copy == "icc":  # jpegtran lama tidak kenal "-copy icc"
        code, out = _run([jpegtran, "-copy", "all", "-optimize", "-progressive"], data)
    if code == 0 and out[:2] == b"\xff\xd8" and len(out) < len(data):
        return out
    return data


def png_bytes(img: Image.Image, level: int = 1) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=level)
    return buf.getvalue()


def optimize_png_lossless(data: bytes, keep_structure: bool = False, zopfli: bool = False) -> bytes:
    """oxipng; ``keep_structure`` melarang perubahan tipe warna/bit depth
    (dibutuhkan saat aliran IDAT ditanam ke PDF). ``zopfli`` = lintasan
    tambahan yang lebih lambat, hanya untuk PNG yang sudah kecil."""
    oxipng = deps.path("oxipng")
    if oxipng:
        level = "4" if len(data) < 1_500_000 else "2"
        cmd = [oxipng, "-o", level, "--strip", "all", "-q", "--stdout"]
        if zopfli and len(data) < 600_000:
            cmd += ["-z", "--zi", "5"]
        if keep_structure:
            cmd.append("--nx")
        cmd.append("-")
        code, out = _run(cmd, data)
        if code == 0 and out[:8] == b"\x89PNG\r\n\x1a\n" and len(out) < len(data):
            return out
        return data
    if keep_structure:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue() if buf.tell() < len(data) else data
    except Exception:
        return data


def quantize_png(img: Image.Image, colors: int, quality: tuple[int, int],
                 flat_art: bool = False) -> bytes | None:
    """PNG palet (lossy tapi tepi tetap tajam). None kalau gagal.

    ``flat_art`` (screenshot/diagram): coba juga tanpa dithering — pada area
    berwarna rata dithering justru menambah noise dan memperbesar file.
    """
    pngquant = deps.path("pngquant")
    colors = int(max(2, min(256, colors)))
    if pngquant:
        src = png_bytes(img, 1)
        lo, hi = quality
        speed = "1" if img.size[0] * img.size[1] <= 4_000_000 else "3"
        best = None
        for extra in ([], ["--nofs"]) if flat_art else ([],):
            for q in (f"{lo}-{hi}", f"0-{hi}"):
                code, out = _run([pngquant, f"--quality={q}", "--speed", speed, "--strip",
                                  *extra, str(colors), "-"], src, ok_codes=(0, 98, 99))
                if code == 0 and out[:8] == b"\x89PNG\r\n\x1a\n":
                    if best is None or len(out) < len(best):
                        best = out
                    break
        if best:
            return best
    try:
        if img.mode in ("RGBA", "LA"):
            pal = img.convert("RGBA").quantize(colors, method=Image.Quantize.FASTOCTREE)
        else:
            pal = img.convert("RGB").quantize(colors, method=Image.Quantize.MEDIANCUT,
                                              dither=Image.Dither.NONE if flat_art
                                              else Image.Dither.FLOYDSTEINBERG)
        return png_bytes(pal, 9)
    except Exception:
        return None


def parse_png(data: bytes) -> dict | None:
    """Pecah PNG jadi IHDR / PLTE / tRNS / IDAT (untuk ditanam ke PDF)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    info: dict = {"idat": [], "plte": None, "trns": None}
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            w, h, depth, color, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
            info.update(width=w, height=h, depth=depth, color=color, interlace=interlace)
        elif ctype == b"PLTE":
            info["plte"] = body
        elif ctype == b"tRNS":
            info["trns"] = body
        elif ctype == b"IDAT":
            info["idat"].append(body)
        elif ctype == b"IEND":
            break
    if "width" not in info:
        return None
    info["idat"] = b"".join(info["idat"])
    return info


# -------------------------------------------------------------------- recompress

@dataclass
class Encoded:
    data: bytes
    ext: str            # "jpeg" atau "png"
    width: int
    height: int
    kind: str           # photo / graphic
    note: str = ""


def _sniff(data: bytes) -> str:
    if data[:2] == b"\xff\xd8":
        return "JPEG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    return ""


def _lossless(data: bytes, fmt: str, orientation: int, kind: str) -> Encoded | None:
    if fmt == "JPEG":
        out = optimize_jpeg_lossless(data, keep_all_markers=orientation not in (0, 1))
    elif fmt == "PNG":
        out = optimize_png_lossless(data)
    else:
        return None
    if len(out) >= len(data):
        return None
    try:
        w, h = Image.open(io.BytesIO(out)).size
    except Exception:
        return None
    return Encoded(out, "jpeg" if fmt == "JPEG" else "png", w, h, kind, "optimasi lossless")


def recompress(
    data: bytes,
    settings: Settings,
    *,
    display_in: tuple[float, float] | None = None,
    allow_jpeg_convert: bool = True,
    exif_mode: str = "keep",      # keep (Office) / apply (file mandiri) / ignore (PDF)
) -> Encoded | None:
    """Kompres ulang satu gambar. Mengembalikan None kalau tidak ada penghematan
    (pemanggil tetap memakai data asli).

    ``display_in`` = ukuran tampil gambar di dokumen (inci). Gambar di-resize
    sehingga resolusinya pas dengan DPI preset untuk ukuran tampil itu.
    """
    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format or _sniff(data)
        if getattr(img, "n_frames", 1) > 1 or getattr(img, "is_animated", False):
            return None  # animasi / multi-frame: jangan disentuh
        quality_in = estimate_jpeg_quality(img) if fmt == "JPEG" else None
        orientation = 1
        if fmt in ("JPEG", "TIFF", "PNG"):
            try:
                orientation = int(img.getexif().get(0x0112, 1) or 1)
            except Exception:
                orientation = 1
        img.load()
    except Exception:
        return None
    if fmt not in ("JPEG", "PNG", "BMP", "TIFF"):
        return None

    untouchable = img.mode in ("CMYK", "YCbCr", "LAB", "HSV", "1", "F")
    if settings.lossless or untouchable:
        kind = GRAPHIC if img.mode == "1" else PHOTO
        return _lossless(data, fmt, orientation, kind)

    if img.mode in ("I;16", "I;16B", "I;16L", "I"):
        img = img.convert("I").point(lambda v: v / 256).convert("L")
    img = _to_srgb(img)
    if exif_mode == "apply" and orientation != 1:
        img = ImageOps.exif_transpose(img)
        orientation = 1

    alpha = has_real_alpha(img)
    if alpha:
        img = img.convert("RGBA")
    elif img.mode != "L":
        img = img.convert("RGB")
    kind = classify(img)

    # ---- resize sesuai ukuran tampil / batas sisi terpanjang
    w, h = img.size
    scale = 1.0
    if display_in and display_in[0] > 0 and display_in[1] > 0:
        dpi = target_dpi(kind, settings)
        if dpi > 0:
            dw, dh = display_in
            if orientation in (5, 6, 7, 8):  # piksel tersimpan dalam posisi diputar 90°
                dw, dh = dh, dw
            scale = max(dw * dpi / w, dh * dpi / h)
    elif display_in is None:
        cap = settings.max_photo_px if kind == PHOTO else settings.max_graphic_px
        if cap > 0:
            scale = cap / max(w, h)
    resized = False
    original = img
    if scale < 0.9:
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        img = _resize(img, (nw, nh))
        resized = True
    to_gray = settings.grayscale and img.mode != "L"
    if to_gray:
        img = img.convert("LA") if alpha else img.convert("L")

    notes = []
    if resized:
        notes.append(f"{w}×{h} → {img.size[0]}×{img.size[1]}")
    out: bytes | None = None
    ext = "png"

    if fmt == "JPEG":
        ext = "jpeg"
        q = jpeg_quality_for(kind, settings)
        if not resized and not to_gray and quality_in is not None and quality_in <= q + 3:
            return _lossless(data, fmt, orientation, kind)  # sudah sekecil target
        exif = None
        if exif_mode == "keep" and orientation != 1:
            ex = Image.Exif()
            ex[0x0112] = orientation
            exif = ex.tobytes()
        out = encode_jpeg(img, q, exif=exif)
        notes.append(f"JPEG q{q}")
    elif alpha:
        out = quantize_png(img, settings.png_colors, settings.png_quality, flat_art=kind == GRAPHIC)
        if out:
            out = optimize_png_lossless(out)
            notes.append("PNG transparan → pngquant")
    elif kind != GRAPHIC and allow_jpeg_convert:
        q = jpeg_quality_for(kind, settings)
        out = encode_jpeg(img, q)
        ext = "jpeg"
        notes.append(f"{'foto' if kind == PHOTO else 'scan'} {fmt} → JPEG q{q}")
    else:
        label = "grafis/teks" if kind == GRAPHIC else "foto"
        out = quantize_png(img, settings.png_colors, settings.png_quality, flat_art=kind == GRAPHIC)
        if out:
            out = optimize_png_lossless(out)
            notes.append(f"{label} tetap PNG → pngquant")
        if out is None and (resized or fmt != "PNG"):
            out = optimize_png_lossless(png_bytes(img, 6))
            notes.append("PNG lossless")
        if resized and fmt == "PNG" and not to_gray:
            # Mengecilkan gambar grafis menambah piksel antialias (lebih banyak
            # warna), jadi kadang versi resolusi asli justru lebih kecil DAN
            # lebih tajam. Ambil yang terkecil.
            full = quantize_png(original, settings.png_colors, settings.png_quality,
                                flat_art=kind == GRAPHIC)
            if full:
                full = optimize_png_lossless(full)
                if out is None or len(full) <= len(out):
                    out, img = full, original
                    notes = [f"{label} tetap PNG → pngquant (resolusi asli)"]

    best: Encoded | None = None
    if out and len(out) < len(data) * 0.98:
        best = Encoded(out, ext, img.size[0], img.size[1], kind, ", ".join(notes))
    if best is None or len(best.data) > len(data) * 0.7:
        # Hasil lossy tidak jauh lebih kecil → bandingkan dengan optimasi lossless.
        fallback = _lossless(data, fmt, orientation, kind)
        if fallback and (best is None or len(fallback.data) < len(best.data)):
            best = fallback
    if best is not None and best.ext == "png" and settings.png_zopfli:
        best.data = optimize_png_lossless(best.data, zopfli=True)
    return best


# ---------------------------------------------------------------- file mandiri

def compress_image_file(src: Path, dst: Path, settings: Settings) -> dict:
    """JPG→JPG atau PNG→PNG. Menulis ``dst`` hanya kalau lebih kecil."""
    data = src.read_bytes()
    enc = recompress(data, settings, display_in=None, allow_jpeg_convert=False, exif_mode="apply")
    info = {"written": False, "note": "", "kind": "", "size": None}
    try:
        with Image.open(io.BytesIO(data)) as im:
            info["src_size"] = im.size
    except Exception:
        pass
    if enc is None or len(enc.data) >= len(data):
        info["note"] = "sudah optimal"
        return info
    dst.write_bytes(enc.data)
    info.update(written=True, note=enc.note, kind=enc.kind, size=(enc.width, enc.height))
    return info


def image_info(path: Path) -> dict:
    with Image.open(path) as im:
        im.load()
        return {"format": im.format, "size": im.size, "mode": im.mode}
