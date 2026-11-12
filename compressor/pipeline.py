"""Orkestrasi satu file: pilih jalur sesuai format & mode, validasi, beri nama
output, dan jatuhkan ke file asli kalau hasilnya tidak lebih kecil.

Aturan yang dijaga di sini:
- File asli TIDAK PERNAH ditimpa atau diubah.
- Output bernama ``<nama>_compressed.<ext>``; kalau sudah ada, diberi nomor.
- Hasil yang gagal validasi tidak pernah dipakai.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

from . import convert, deps, images, office, pdf, validate
from .presets import Settings, make_settings

Progress = Callable[[float, str], None]

PDF_EXT = {".pdf"}
OFFICE_EXT = {".docx", ".pptx", ".xlsx"}
LEGACY_EXT = {".doc", ".ppt", ".xls"}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}
SUPPORTED = PDF_EXT | OFFICE_EXT | LEGACY_EXT | IMAGE_EXT
SUFFIX = "_compressed"

MODES = {"same": "Format sama (dikompres)", "pdf": "Convert ke PDF (lalu dikompres)"}


@dataclass
class Result:
    source: str
    name: str
    mode: str
    preset: str
    size_before: int = 0
    size_after: int = 0
    status: str = "error"        # ok | kept_original | larger | error
    message: str = ""
    output: str | None = None
    output_name: str | None = None
    detail: str = ""
    strategy: str = ""
    validation: dict | None = None
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def saved_bytes(self) -> int:
        return self.size_before - self.size_after

    @property
    def saved_pct(self) -> float:
        return 100.0 * self.saved_bytes / self.size_before if self.size_before else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["saved_bytes"] = self.saved_bytes
        d["saved_pct"] = round(self.saved_pct, 1)
        return d


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}" if n < 100 else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Nama yang belum dipakai: stem.ext, stem-2.ext, stem-3.ext, …"""
    candidate = directory / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{n}{suffix}"
        n += 1
    return candidate


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED


# Memilih nama + memindahkan file harus atomik: dua file bernama sama yang
# diproses bersamaan tidak boleh saling menimpa hasilnya.
_PLACE_LOCK = threading.Lock()


def _place(tmp_file: Path, src: Path, out_dir: Path, ext: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with _PLACE_LOCK:
        target = unique_path(out_dir, src.stem + SUFFIX, ext)
        if target.resolve() == src.resolve():  # mustahil karena ada akhiran, tapi dijaga
            raise RuntimeError("output akan menimpa file asli")
        shutil.move(str(tmp_file), target)
    return target


def _keep_original(res: Result, src: Path, out_dir: Path, why: str) -> Result:
    """Hasil tidak lebih kecil / tidak valid → file asli yang dipakai."""
    res.status = "kept_original"
    res.size_after = res.size_before
    res.message = why
    if out_dir.resolve() != src.parent.resolve():
        out_dir.mkdir(parents=True, exist_ok=True)
        with _PLACE_LOCK:
            target = unique_path(out_dir, src.stem, src.suffix)
            shutil.copy2(src, target)
        res.output, res.output_name = str(target), target.name
        res.message += " — file asli disalin apa adanya"
    else:
        res.output, res.output_name = str(src), src.name
        res.message += " — file asli dipakai (tidak ada file baru)"
    return res


def process_file(
    src: Path,
    mode: str = "same",
    settings: Settings | None = None,
    out_dir: Path | None = None,
    progress: Progress | None = None,
) -> Result:
    src = Path(src)
    settings = settings or make_settings()
    out_dir = Path(out_dir) if out_dir else src.parent
    res = Result(source=str(src), name=src.name, mode=mode, preset=settings.describe())
    started = time.time()

    def report(frac: float, msg: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, frac)), msg)

    try:
        if not src.is_file():
            raise RuntimeError("file tidak ditemukan")
        res.size_before = res.size_after = src.stat().st_size
        ext = src.suffix.lower()
        if ext not in SUPPORTED:
            raise RuntimeError(f"format {ext or '(tanpa ekstensi)'} tidak didukung")
        if mode not in MODES:
            raise RuntimeError(f"mode tidak dikenal: {mode}")
        with tempfile.TemporaryDirectory(prefix="compressor_") as tmp:
            tmpd = Path(tmp)
            if ext in IMAGE_EXT:
                if mode == "pdf":
                    res.notes.append("Gambar tidak dikonversi ke PDF; dikompres dalam format yang sama")
                _image(res, src, tmpd, out_dir, settings, report)
            elif ext in PDF_EXT:
                _pdf(res, src, src, tmpd, out_dir, settings, report)
            elif mode == "pdf":
                _office_to_pdf(res, src, tmpd, out_dir, settings, report)
            else:
                _office(res, src, tmpd, out_dir, settings, report)
    except deps.MissingDependency as e:
        res.status, res.message = "error", str(e)
    except Exception as e:
        res.status = "error"
        res.message = _friendly(e)
    res.seconds = round(time.time() - started, 2)
    report(1.0, "selesai")
    return res


def _friendly(e: Exception) -> str:
    """Pesan error yang bisa dipahami, tanpa path sementara."""
    import zipfile
    import pikepdf
    if isinstance(e, pikepdf.PasswordError):
        return "PDF dilindungi password — buka kuncinya dulu sebelum dikompres"
    if isinstance(e, pikepdf.PdfError):
        return "File PDF rusak atau bukan PDF yang valid"
    if isinstance(e, zipfile.BadZipFile):
        return ("Dokumen Office tidak bisa dibuka: file rusak atau dilindungi password "
                "(hapus password-nya dulu di Word/Excel/PowerPoint)")
    if isinstance(e, MemoryError):
        return "Memori tidak cukup untuk memproses file ini"
    return str(e) or type(e).__name__


# ------------------------------------------------------------------- per jalur

def _finish(res: Result, tmp_out: Path, src: Path, out_dir: Path, ext: str,
            v: validate.Validation | None) -> None:
    res.validation = v.to_dict() if v else None
    target = _place(tmp_out, src, out_dir, ext)
    res.output, res.output_name = str(target), target.name
    res.size_after = target.stat().st_size
    res.status = "ok"
    res.message = f"{human(res.size_before)} → {human(res.size_after)} (hemat {res.saved_pct:.1f}%)"


def _image(res, src, tmpd, out_dir, settings, report) -> None:
    report(0.1, "mengompres gambar")
    try:
        from PIL import Image
        with Image.open(src) as im:
            im.verify()
    except Exception:
        raise RuntimeError("File gambar rusak atau bukan JPG/PNG yang valid")
    tmp_out = tmpd / f"out{src.suffix.lower()}"
    info = images.compress_image_file(src, tmp_out, settings)
    res.detail = info.get("note", "")
    if not info["written"]:
        _keep_original(res, src, out_dir, "Sudah optimal, tidak bisa diperkecil lagi")
        return
    report(0.9, "validasi")
    v = validate.validate_image(src, tmp_out)
    if not v.ok:
        res.validation = v.to_dict()
        _keep_original(res, src, out_dir, f"Hasil gagal validasi ({v.reason})")
        return
    _finish(res, tmp_out, src, out_dir, src.suffix.lower(), v)


def _pdf(res, src, pdf_in, tmpd, out_dir, settings, report, base: float = 0.0) -> bool:
    """Kompres ``pdf_in``; True kalau ada hasil yang lebih kecil dari ``pdf_in``."""
    tmp_out = tmpd / "out.pdf"
    r = pdf.compress_pdf(pdf_in, tmp_out, settings,
                         lambda f, m: report(base + f * (1 - base), m))
    res.notes.extend(r["notes"])
    res.strategy = r["strategy"] or ""
    res.detail = r["detail"]
    v = r["validation"]
    if pdf_in is not src:
        return bool(r["written"]) and _stash(res, v, tmp_out)
    if r["written"]:
        _finish(res, tmp_out, src, out_dir, ".pdf", v)
        return True
    why = "Sudah optimal, tidak bisa diperkecil lagi"
    if r["candidates"] and not any(c["ok"] for c in r["candidates"]):
        why = "Semua hasil kompresi gagal validasi"
    res.validation = v.to_dict() if v else None
    _keep_original(res, src, out_dir, why)
    return False


def _stash(res, v, tmp_out) -> bool:
    res.validation = v.to_dict() if v else None
    return tmp_out.exists()


def _modernize(res, src: Path, tmpd: Path, report) -> Path:
    """DOC/PPT/XLS → DOCX/PPTX/XLSX lewat LibreOffice."""
    ext = src.suffix.lower()
    if ext not in LEGACY_EXT:
        return src
    fmt = convert.LEGACY_TO_MODERN[ext]
    report(0.05, f"LibreOffice: {ext[1:].upper()} → {fmt.upper()}")
    modern = convert.convert(src, tmpd / "modern", fmt)
    res.notes.append(f"Format lama {ext[1:].upper()} diubah ke {fmt.upper()}")
    if ext == ".xls":
        res.notes.append("Konversi XLS → XLSX bisa menggeser sedikit tinggi baris/lebar kolom "
                         "(isi sel tidak berubah); periksa tampilannya kalau tata letak penting")
    return modern


def _office(res, src, tmpd, out_dir, settings, report) -> None:
    modern = _modernize(res, src, tmpd, report)
    ext = modern.suffix.lower()
    tmp_out = tmpd / f"out{ext}"
    base = 0.25 if modern is not src else 0.0
    r = office.compress_office(modern, tmp_out, settings,
                               lambda f, m: report(base + f * (0.9 - base), m))
    res.detail = r["detail"]
    res.notes.extend(r["stats"].details[:40])
    report(0.92, "validasi")
    v = validate.validate_office(modern, tmp_out)
    res.validation = v.to_dict()
    if not v.ok:
        _keep_original(res, src, out_dir, f"Hasil gagal validasi ({v.reason})")
        return
    if tmp_out.stat().st_size >= res.size_before:
        _keep_original(res, src, out_dir, "Sudah optimal, tidak bisa diperkecil lagi")
        return
    _finish(res, tmp_out, src, out_dir, ext, v)


def _office_to_pdf(res, src, tmpd, out_dir, settings, report) -> None:
    report(0.05, "LibreOffice: konversi ke PDF")
    reference = convert.convert(src, tmpd / "pdf", "pdf")
    res.notes.append(f"Dikonversi ke PDF oleh LibreOffice ({human(reference.stat().st_size)} sebelum dikompres)")
    smaller = _pdf(res, src, reference, tmpd, out_dir, settings, report, base=0.3)
    final = tmpd / "out.pdf" if smaller else reference
    if not smaller:
        res.notes.append("PDF hasil konversi sudah optimal")
        v = validate.validate_pdf(reference, reference, visual=False)
        res.validation = v.to_dict()
    target = _place(final, src, out_dir, ".pdf")
    res.output, res.output_name = str(target), target.name
    res.size_after = target.stat().st_size
    if res.size_after < res.size_before:
        res.status = "ok"
        res.message = f"{human(res.size_before)} → {human(res.size_after)} (hemat {res.saved_pct:.1f}%)"
    else:
        res.status = "larger"
        res.message = (f"PDF ({human(res.size_after)}) tidak lebih kecil dari file asli "
                       f"({human(res.size_before)}) — wajar untuk dokumen tanpa gambar besar. "
                       f"PDF tetap disimpan; file asli tidak diubah")
