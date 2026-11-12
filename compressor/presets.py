"""Preset kompresi dan pengaturan custom."""
from __future__ import annotations

from dataclasses import dataclass, replace, asdict


@dataclass(frozen=True)
class Settings:
    name: str = "balanced"
    # Lossless: piksel gambar tidak berubah sama sekali, hanya optimasi struktur.
    lossless: bool = False
    # DPI target untuk FOTO, dihitung dari ukuran tampil gambar di dokumen.
    dpi: int = 150
    # DPI target untuk screenshot/diagram/gambar berisi teks. Sengaja lebih
    # tinggi daripada foto supaya teks di dalam gambar tetap tajam.
    graphic_dpi: int = 220
    jpeg_quality: int = 75
    # Jumlah warna maksimum saat PNG dikuantisasi (pngquant).
    png_colors: int = 256
    # Rentang kualitas pngquant (min-max).
    png_quality: tuple[int, int] = (65, 90)
    grayscale: bool = False
    # Batas sisi terpanjang (piksel) untuk file gambar mandiri; 0 = tidak di-resize.
    max_photo_px: int = 2560
    max_graphic_px: int = 0
    # Linearize ("fast web view") menambah beberapa KB, jadi default mati.
    linearize: bool = False

    def describe(self) -> str:
        if self.lossless:
            return "Lossless (struktur saja)"
        g = ", grayscale" if self.grayscale else ""
        return f"{self.name}: {self.dpi} DPI, JPEG q{self.jpeg_quality}{g}"

    def to_dict(self) -> dict:
        return asdict(self)


PRESETS: dict[str, Settings] = {
    "lossless": Settings(name="lossless", lossless=True, dpi=0, graphic_dpi=0,
                         jpeg_quality=100, max_photo_px=0, max_graphic_px=0),
    "balanced": Settings(name="balanced", dpi=150, graphic_dpi=220, jpeg_quality=75,
                         png_colors=256, png_quality=(65, 90),
                         max_photo_px=2560, max_graphic_px=0),
    "maximum": Settings(name="maximum", dpi=100, graphic_dpi=150, jpeg_quality=50,
                        png_colors=128, png_quality=(45, 80),
                        max_photo_px=1600, max_graphic_px=0),
}

PRESET_LABELS = {
    "lossless": "Lossless — kualitas tidak berubah, hanya optimasi struktur",
    "balanced": "Balanced — 150 DPI, JPEG q75 (default)",
    "maximum": "Maximum — 100 DPI, JPEG q50, teks tetap tajam",
}


def make_settings(
    preset: str = "balanced",
    dpi: int | None = None,
    quality: int | None = None,
    grayscale: bool | None = None,
    linearize: bool | None = None,
) -> Settings:
    """Ambil preset lalu timpa dengan pengaturan custom (kalau ada)."""
    key = (preset or "balanced").lower()
    if key not in PRESETS:
        raise ValueError(f"Preset tidak dikenal: {preset!r}. Pilihan: {', '.join(PRESETS)}")
    s = PRESETS[key]
    custom = False
    if dpi is not None and dpi > 0 and dpi != s.dpi:
        dpi = max(36, min(int(dpi), 1200))
        # Gambar berisi teks selalu diberi resolusi ~1.5x foto.
        s = replace(s, dpi=dpi, graphic_dpi=round(dpi * 1.5), lossless=False)
        custom = True
    if quality is not None and quality > 0 and quality != s.jpeg_quality:
        q = max(5, min(int(quality), 95))
        lo = max(20, q - 15)
        s = replace(s, jpeg_quality=q, png_quality=(lo, min(98, q + 15)), lossless=False)
        custom = True
    if grayscale is not None and grayscale != s.grayscale:
        s = replace(s, grayscale=bool(grayscale), lossless=False if grayscale else s.lossless)
        custom = custom or bool(grayscale)
    if linearize is not None:
        s = replace(s, linearize=bool(linearize))
    if custom:
        if s.dpi <= 0:  # lossless + custom quality/grayscale → tanpa resize
            s = replace(s, dpi=0, graphic_dpi=0)
        s = replace(s, name=f"custom({key})")
    return s
