"""Konversi lewat LibreOffice headless (100% lokal).

- DOC/PPT/XLS → DOCX/PPTX/XLSX
- DOCX/PPTX/XLSX/DOC/PPT/XLS → PDF

Ekspor PDF memakai kompresi gambar LOSSLESS dan tanpa penurunan resolusi:
JPEG asli diteruskan byte-per-byte dan PNG tetap utuh, sehingga seluruh
keputusan kompresi (DPI, JPEG vs PNG, kualitas) diambil sekali saja oleh
pipeline PDF — tidak ada kompresi ganda.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from . import deps

_LOCK = threading.Lock()  # satu instance soffice per profil
_PROFILE: Path | None = None

LEGACY_TO_MODERN = {".doc": "docx", ".ppt": "pptx", ".xls": "xlsx",
                    ".rtf": "docx", ".odt": "docx", ".odp": "pptx", ".ods": "xlsx"}

_EXPORT_FILTER = {
    "docx": 'docx:MS Word 2007 XML',
    "pptx": 'pptx:Impress MS PowerPoint 2007 XML',
    "xlsx": 'xlsx:Calc MS Excel 2007 XML',
    "doc": "doc", "ppt": "ppt", "xls": "xls",
}
_PDF_FILTER = {
    "writer": "writer_pdf_Export", "impress": "impress_pdf_Export", "calc": "calc_pdf_Export",
}
_FAMILY = {
    ".doc": "writer", ".docx": "writer", ".rtf": "writer", ".odt": "writer",
    ".ppt": "impress", ".pptx": "impress", ".odp": "impress",
    ".xls": "calc", ".xlsx": "calc", ".ods": "calc",
}
_PDF_OPTIONS = {
    "UseLosslessCompression": {"type": "boolean", "value": "true"},
    "ReduceImageResolution": {"type": "boolean", "value": "false"},
    "ExportBookmarks": {"type": "boolean", "value": "true"},
    "ExportNotes": {"type": "boolean", "value": "false"},
}


def _profile_dir() -> Path:
    """Profil LibreOffice khusus proses ini, supaya tidak bentrok dengan
    LibreOffice yang sedang dibuka pengguna atau proses Compressor lain."""
    global _PROFILE
    if _PROFILE is None:
        _PROFILE = Path(tempfile.mkdtemp(prefix="compressor_lo_"))
        atexit.register(shutil.rmtree, _PROFILE, True)
    return _PROFILE


def _filter_for(src: Path, fmt: str) -> str:
    if fmt == "pdf":
        family = _FAMILY.get(src.suffix.lower(), "writer")
        return f"pdf:{_PDF_FILTER[family]}:{json.dumps(_PDF_OPTIONS, separators=(',', ':'))}"
    return _EXPORT_FILTER[fmt]


def convert(src: Path, out_dir: Path, fmt: str, timeout: int = 600) -> Path:
    """Konversi ``src`` ke ``fmt``; hasil ditaruh di ``out_dir`` dengan nama
    ``<stem>.<fmt>``. Melempar RuntimeError berisi penjelasan kalau gagal."""
    soffice = deps.require("soffice")
    src = Path(src)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with _LOCK, tempfile.TemporaryDirectory(prefix="compressor_conv_") as tmp:
        tmpd = Path(tmp)
        work = tmpd / f"in{src.suffix.lower()}"   # nama aman tanpa spasi/karakter aneh
        shutil.copyfile(src, work)
        cmd = [soffice, f"-env:UserInstallation=file://{_profile_dir()}",
               "--headless", "--norestore", "--nolockcheck", "--nodefault", "--nofirststartwizard",
               "--convert-to", _filter_for(src, fmt), "--outdir", str(tmpd / "out"), str(work)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, "HOME": os.environ.get("HOME", str(tmpd))})
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"LibreOffice tidak selesai dalam {timeout} detik saat mengonversi {src.name}")
        produced = tmpd / "out" / f"in.{fmt}"
        if not produced.exists() or produced.stat().st_size == 0:
            detail = (p.stderr or p.stdout or "").strip().splitlines()
            raise RuntimeError(
                f"LibreOffice gagal mengonversi {src.name} ke {fmt.upper()}"
                + (f": {detail[-1]}" if detail else ""))
        target = out_dir / f"{src.stem}.{fmt}"
        if target.exists():
            target.unlink()
        shutil.move(str(produced), target)
        return target
