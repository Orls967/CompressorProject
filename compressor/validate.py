"""Validasi hasil kompresi: file bisa dibuka, jumlah halaman/slide/sheet sama,
teks masih bisa diekstrak, dan tampilan halaman tidak berubah drastis."""
from __future__ import annotations

import hashlib
import io
import re
import threading
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

# pdfium tidak thread-safe → semua pemanggilan diserialkan.
_PDFIUM_LOCK = threading.RLock()

TEXT_MIN_RATIO = 0.995     # minimal porsi karakter yang masih bisa diekstrak (ligatur hilang = ditolak)
SSIM_MIN = 0.72            # di bawah ini tampilan halaman dianggap rusak
MAX_TEXT_PAGES = 400
VISUAL_PAGES = 5


@dataclass
class Validation:
    ok: bool = True
    reason: str = ""
    units: str = "halaman"
    count_before: int | None = None
    count_after: int | None = None
    text_before: int | None = None
    text_after: int | None = None
    text_ratio: float | None = None
    ssim: float | None = None
    notes: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> "Validation":
        self.ok = False
        self.reason = reason
        return self

    def summary(self) -> str:
        if not self.ok:
            return f"GAGAL: {self.reason}"
        parts = []
        if self.count_after is not None:
            parts.append(f"{self.count_after} {self.units}")
        if self.text_ratio is not None and self.text_before:
            parts.append(f"teks {self.text_ratio * 100:.0f}%")
        elif self.text_before == 0 and self.units == "halaman":
            parts.append("tanpa lapisan teks")
        if self.ssim is not None:
            parts.append(f"kemiripan {self.ssim:.3f}")
        parts.extend(self.notes)
        return "OK" + (" — " + ", ".join(parts) if parts else "")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "ok", "reason", "units", "count_before", "count_after", "text_before",
            "text_after", "text_ratio", "ssim", "notes")} | {"summary": self.summary()}


# ------------------------------------------------------------------------- PDF

def _sample_indices(n: int, k: int) -> list[int]:
    if n <= k:
        return list(range(n))
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def pdf_summary(path: Path, render: list[int] | None = None, render_px: int = 900) -> dict:
    """Jumlah halaman, hitungan karakter teks, dan render beberapa halaman (grayscale)."""
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            n = len(pdf)
            chars: Counter = Counter()
            for i in _sample_indices(n, MAX_TEXT_PAGES):
                page = pdf[i]
                tp = page.get_textpage()
                try:
                    text = tp.get_text_bounded()
                finally:
                    tp.close()
                    page.close()
                chars.update(c for c in text if not c.isspace())
            renders: dict[int, np.ndarray] = {}
            sizes: dict[int, tuple[float, float]] = {}
            for i in render or []:
                if i >= n:
                    continue
                page = pdf[i]
                try:
                    w, h = page.get_size()
                    sizes[i] = (w, h)
                    scale = render_px / max(w, h, 1)
                    bitmap = page.render(scale=scale, grayscale=True)
                    renders[i] = np.array(bitmap.to_numpy(), dtype=np.float32).reshape(
                        bitmap.height, bitmap.width, -1)[:, :, 0]
                    bitmap.close()
                finally:
                    page.close()
            return {"pages": n, "chars": chars, "renders": renders, "sizes": sizes}
        finally:
            pdf.close()


def ssim(a: np.ndarray, b: np.ndarray, block: int = 8) -> float:
    """SSIM sederhana per blok (tanpa tumpang tindih)."""
    h = min(a.shape[0], b.shape[0]) // block * block
    w = min(a.shape[1], b.shape[1]) // block * block
    if h == 0 or w == 0:
        return 0.0

    def blocks(x: np.ndarray) -> np.ndarray:
        return (x[:h, :w].astype(np.float64)
                .reshape(h // block, block, w // block, block)
                .transpose(0, 2, 1, 3).reshape(-1, block * block))

    A, B = blocks(a), blocks(b)
    mu_a, mu_b = A.mean(1), B.mean(1)
    var_a, var_b = A.var(1), B.var(1)
    cov = ((A - mu_a[:, None]) * (B - mu_b[:, None])).mean(1)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    s = ((2 * mu_a * mu_b + c1) * (2 * cov + c2)) / ((mu_a ** 2 + mu_b ** 2 + c1) * (var_a + var_b + c2))
    return float(s.mean())


def validate_pdf(orig: Path, new: Path, visual: bool = True, reference: dict | None = None) -> Validation:
    """``reference`` = hasil pdf_summary(orig) kalau sudah dihitung sebelumnya."""
    v = Validation(units="halaman")
    try:
        import pikepdf
        with pikepdf.open(new) as pdf:
            n_new = len(pdf.pages)
    except Exception as e:
        return v.fail(f"output tidak bisa dibuka ({type(e).__name__}: {e})")
    try:
        if reference is None:
            import pypdfium2 as pdfium
            with _PDFIUM_LOCK:
                d = pdfium.PdfDocument(str(orig))
                n0 = len(d)
                d.close()
            reference = pdf_summary(orig, _sample_indices(n0, VISUAL_PAGES) if visual else None)
        idx = sorted(reference["renders"].keys())
        after = pdf_summary(new, idx if visual else None)
    except Exception as e:
        return v.fail(f"output tidak bisa dibaca ({type(e).__name__}: {e})")
    v.count_before, v.count_after = reference["pages"], after["pages"]
    if v.count_before != v.count_after or n_new != v.count_before:
        return v.fail(f"jumlah halaman berubah ({v.count_before} → {v.count_after})")
    c0, c1 = reference["chars"], after["chars"]
    v.text_before, v.text_after = sum(c0.values()), sum(c1.values())
    if v.text_before:
        v.text_ratio = sum((c0 & c1).values()) / v.text_before
        if v.text_ratio < TEXT_MIN_RATIO:
            return v.fail(f"teks yang bisa diekstrak tinggal {v.text_ratio * 100:.0f}%")
    if visual and reference["renders"]:
        scores = []
        for i, a in reference["renders"].items():
            b = after["renders"].get(i)
            if b is None:
                return v.fail(f"halaman {i + 1} gagal dirender")
            if abs(a.shape[0] - b.shape[0]) > 4 or abs(a.shape[1] - b.shape[1]) > 4:
                return v.fail(f"ukuran halaman {i + 1} berubah")
            scores.append(ssim(a, b))
        v.ssim = min(scores)
        if v.ssim < SSIM_MIN:
            return v.fail(f"tampilan halaman berubah terlalu jauh (kemiripan {v.ssim:.2f})")
    return v


def pdf_reference(orig: Path, visual: bool = True) -> dict:
    import pypdfium2 as pdfium
    with _PDFIUM_LOCK:
        d = pdfium.PdfDocument(str(orig))
        n = len(d)
        d.close()
    return pdf_summary(orig, _sample_indices(n, VISUAL_PAGES) if visual else None)


# ---------------------------------------------------------------------- Office

_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_TEXT_TAGS = {
    "word": "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t",
    "ppt": "{http://schemas.openxmlformats.org/drawingml/2006/main}t",
}
_SKIP_COMPARE = re.compile(r"(^\[Content_Types\]\.xml$|\.rels$|^docProps/thumbnail\.|/media/)")


def office_summary(path: Path) -> dict:
    """Jenis dokumen, jumlah slide/sheet/paragraf, jumlah karakter teks, dan
    hash tiap bagian XML."""
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError(f"arsip rusak di {bad}")
        names = [i.filename for i in z.infolist() if not i.is_dir()]
        nameset = set(names)
        kind = "word" if "word/document.xml" in nameset else \
            "ppt" if "ppt/presentation.xml" in nameset else \
            "xl" if "xl/workbook.xml" in nameset else "?"
        hashes, chars, broken, images_bad = {}, 0, [], []
        count, units = None, "bagian"
        for n in names:
            data = z.read(n)
            if not _SKIP_COMPARE.search(n):
                hashes[n] = hashlib.sha1(data).hexdigest()
            if n.endswith(".rels"):
                try:
                    root = etree.fromstring(data, parser)
                except etree.XMLSyntaxError:
                    broken.append(f"{n} bukan XML yang valid")
                    continue
                d, f = n.rsplit("/", 1) if "/" in n else ("", n)
                src_dir = d.rsplit("/_rels", 1)[0] if d.endswith("_rels") else d
                src_dir = "" if src_dir == "_rels" else src_dir
                for rel in root:
                    if rel.get("TargetMode") == "External":
                        continue
                    target = rel.get("Target") or ""
                    if target.startswith("/"):
                        full = target.lstrip("/")
                    else:
                        import posixpath
                        full = posixpath.normpath(posixpath.join(src_dir, target))
                    if full and full not in nameset and "#" not in target and target != "NULL":
                        broken.append(f"{n}: target hilang {target}")
            elif n == "[Content_Types].xml" or n.endswith(".xml"):
                try:
                    root = etree.fromstring(data, parser)
                except etree.XMLSyntaxError:
                    broken.append(f"{n} bukan XML yang valid")
                    continue
                tag = _TEXT_TAGS.get(kind)
                if tag and (n == "word/document.xml" or n.startswith("ppt/slides/slide")):
                    chars += sum(len(e.text or "") for e in root.iter(tag))
                if n == "word/document.xml":
                    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
                    count, units = sum(1 for _ in root.iter(w)), "paragraf"
                if n == "xl/sharedStrings.xml" or n.startswith("xl/worksheets/sheet"):
                    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                    chars += sum(len(e.text or "") for e in root.iter(f"{ns}t", f"{ns}v"))
            elif "/media/" in n and data[:2] in (b"\xff\xd8", b"\x89P"):
                try:
                    with Image.open(io.BytesIO(data)) as im:
                        im.verify()
                except Exception:
                    images_bad.append(n)
        if kind == "ppt":
            count = sum(1 for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
            units = "slide"
        elif kind == "xl":
            count = sum(1 for n in names if re.fullmatch(r"xl/(work|chart)sheets/sheet\d+\.xml", n))
            units = "sheet"
    return {"kind": kind, "count": count, "units": units, "chars": chars, "hashes": hashes,
            "broken": broken, "images_bad": images_bad}


def _open_with_library(path: Path, kind: str) -> str | None:
    """Coba buka dengan pustaka Office; kembalikan pesan error kalau gagal."""
    try:
        if kind == "word":
            import docx
            docx.Document(str(path))
        elif kind == "ppt":
            import pptx
            pptx.Presentation(str(path))
        elif kind == "xl":
            import openpyxl
            openpyxl.load_workbook(str(path), read_only=True).close()
    except ImportError:
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


def validate_office(orig: Path, new: Path) -> Validation:
    v = Validation()
    try:
        before = office_summary(orig)
        after = office_summary(new)
    except Exception as e:
        return v.fail(f"output tidak bisa dibuka ({type(e).__name__}: {e})")
    v.units = after["units"]
    v.count_before, v.count_after = before["count"], after["count"]
    v.text_before, v.text_after = before["chars"], after["chars"]
    v.text_ratio = (after["chars"] / before["chars"]) if before["chars"] else None
    if after["kind"] != before["kind"]:
        return v.fail("jenis dokumen berubah")
    new_broken = [b for b in after["broken"] if b not in before["broken"]]
    if new_broken:
        return v.fail(new_broken[0])
    if after["images_bad"]:
        return v.fail(f"gambar rusak: {after['images_bad'][0]}")
    if before["count"] != after["count"]:
        return v.fail(f"jumlah {v.units} berubah ({before['count']} → {after['count']})")
    if before["chars"] != after["chars"]:
        return v.fail("teks dokumen berubah")
    changed = [n for n, h in before["hashes"].items() if after["hashes"].get(n) != h]
    if changed:
        return v.fail(f"isi dokumen berubah di {changed[0]}")
    err = _open_with_library(new, after["kind"])
    if err and not _open_with_library(orig, before["kind"]):
        return v.fail(f"output tidak bisa dibuka ({err})")
    v.notes.append("isi identik dengan aslinya")
    return v


# ---------------------------------------------------------------------- gambar

def _gray_thumb(im: Image.Image, size: int = 256) -> np.ndarray:
    """Thumbnail abu-abu di atas latar putih (piksel transparan tidak ikut dibandingkan)."""
    rgba = im.convert("RGBA")
    flat = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    flat.alpha_composite(rgba)
    return np.asarray(flat.convert("L").resize((size, size), Image.Resampling.BILINEAR), dtype=np.float32)


def validate_image(orig: Path, new: Path) -> Validation:
    from PIL import ImageOps
    v = Validation(units="gambar")
    try:
        with Image.open(new) as im:
            im.load()
            size_new, fmt_new = im.size, im.format
            thumb_new = _gray_thumb(im)
        with Image.open(orig) as im:
            fmt_old = im.format
            thumb_old = _gray_thumb(ImageOps.exif_transpose(im))
    except Exception as e:
        return v.fail(f"output tidak bisa dibuka ({type(e).__name__}: {e})")
    if fmt_new != fmt_old:
        return v.fail(f"format berubah ({fmt_old} → {fmt_new})")
    v.count_before = v.count_after = 1
    v.ssim = ssim(thumb_old, thumb_new)
    v.notes.append(f"{size_new[0]}×{size_new[1]} px")
    if v.ssim < SSIM_MIN:
        return v.fail(f"gambar berubah terlalu jauh (kemiripan {v.ssim:.2f})")
    return v
