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

TEXT_MIN_RATIO = 0.95      # minimal porsi karakter yang masih bisa diekstrak
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
        elif self.text_before == 0:
            parts.append("tanpa lapisan teks")
        if self.ssim is not None:
            parts.append(f"kemiripan {self.ssim:.3f}")
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


# ---------------------------------------------------------------------- gambar

def validate_image(orig: Path, new: Path) -> Validation:
    v = Validation(units="gambar")
    try:
        with Image.open(new) as im:
            im.load()
            size_new, fmt_new = im.size, im.format
            thumb_new = np.asarray(im.convert("L").resize((256, 256)), dtype=np.float32)
        with Image.open(orig) as im:
            from PIL import ImageOps
            im2 = ImageOps.exif_transpose(im)
            fmt_old = im.format
            thumb_old = np.asarray(im2.convert("L").resize((256, 256)), dtype=np.float32)
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
