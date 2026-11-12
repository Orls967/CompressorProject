"""Deteksi tool eksternal dan petunjuk install lewat Homebrew.

Tidak ada tool yang membuat aplikasi langsung gagal: tiap tool punya fallback
atau pesan jelas tentang fitur apa yang tidak tersedia dan cara memasangnya.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict

_BREW_PREFIXES = ("/opt/homebrew", "/usr/local")


@dataclass
class Tool:
    key: str
    name: str
    purpose: str
    install: str
    required_for: str
    path: str | None = None
    version: str = ""

    @property
    def ok(self) -> bool:
        return self.path is not None


def _first_existing(*candidates: str | None) -> str | None:
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _version(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        text = (out.stdout or out.stderr).strip().splitlines()
        return text[0].strip() if text else ""
    except Exception:
        return ""


def _find_mozjpeg(binary: str) -> str | None:
    """mozjpeg bersifat keg-only di Homebrew, jadi tidak ada di PATH."""
    return _first_existing(*(f"{p}/opt/mozjpeg/bin/{binary}" for p in _BREW_PREFIXES))


def _find_soffice() -> str | None:
    return _first_existing(
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        os.path.expanduser("~/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        shutil.which("soffice"),
    )


@functools.lru_cache(maxsize=1)
def detect() -> dict[str, Tool]:
    tools = [
        Tool("gs", "Ghostscript", "Downsample gambar & subset font di PDF",
             "brew install ghostscript", "kompresi PDF lossy (Balanced/Maximum)",
             shutil.which("gs")),
        Tool("cjpeg", "mozjpeg (cjpeg)", "Encoder JPEG paling hemat",
             "brew install mozjpeg", "opsional — tanpa ini memakai encoder Pillow (hasil ±5–10% lebih besar)",
             _find_mozjpeg("cjpeg")),
        Tool("jpegtran", "mozjpeg (jpegtran)", "Optimasi JPEG tanpa mengubah piksel",
             "brew install mozjpeg", "opsional — optimasi JPEG lossless",
             _find_mozjpeg("jpegtran") or shutil.which("jpegtran")),
        Tool("pngquant", "pngquant", "Kompresi PNG lossy (palet warna)",
             "brew install pngquant", "opsional — tanpa ini memakai kuantisasi Pillow",
             shutil.which("pngquant")),
        Tool("oxipng", "oxipng", "Optimasi PNG lossless",
             "brew install oxipng", "opsional — tanpa ini memakai optimasi Pillow",
             shutil.which("oxipng")),
        Tool("soffice", "LibreOffice", "Konversi DOC/PPT/XLS dan Office → PDF",
             "brew install --cask libreoffice", "mode Convert ke PDF dan format lama DOC/PPT/XLS",
             _find_soffice()),
    ]
    version_cmd = {
        "gs": ["--version"], "pngquant": ["--version"], "oxipng": ["--version"],
        "cjpeg": ["-version"], "jpegtran": ["-version"],
    }
    for t in tools:
        if t.path and t.key in version_cmd:
            t.version = _version([t.path, *version_cmd[t.key]])
    return {t.key: t for t in tools}


def path(key: str) -> str | None:
    return detect()[key].path


def has(key: str) -> bool:
    return detect()[key].ok


class MissingDependency(RuntimeError):
    """Fitur yang diminta butuh tool yang belum terpasang."""

    def __init__(self, key: str):
        t = detect()[key]
        self.tool = t
        super().__init__(
            f"{t.name} belum terpasang (dibutuhkan untuk {t.required_for}). "
            f"Pasang dengan: {t.install}"
        )


def require(key: str) -> str:
    p = path(key)
    if not p:
        raise MissingDependency(key)
    return p


def status() -> list[dict]:
    return [dict(asdict(t), ok=t.ok) for t in detect().values()]


def missing() -> list[Tool]:
    return [t for t in detect().values() if not t.ok]


def report() -> str:
    lines = ["Status dependency:"]
    for t in detect().values():
        mark = "✓" if t.ok else "✗"
        lines.append(f"  {mark} {t.name:<20} {t.purpose}")
        if not t.ok:
            lines.append(f"      belum terpasang → {t.install}")
            lines.append(f"      dampak: {t.required_for}")
    miss = missing()
    if miss:
        cmds = sorted({t.install for t in miss})
        lines.append("")
        lines.append("Pasang yang kurang dengan:")
        lines.extend(f"  {c}" for c in cmds)
    else:
        lines.append("")
        lines.append("Semua dependency terpasang.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
    sys.exit(0)
