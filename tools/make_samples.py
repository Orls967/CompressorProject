#!/usr/bin/env python3
"""Buat file uji di samples/generated/ untuk tiap format yang didukung.

Semua dibuat lokal (tanpa download): foto diambil dari wallpaper bawaan macOS
ditambah foto sintetis; screenshot/diagram digambar dengan Pillow.

    ./run.sh samples            # buat semua
    ./run.sh samples --force    # buat ulang walau sudah ada
"""
from __future__ import annotations

import argparse
import io
import random
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "samples" / "generated"
ASSETS = OUT / "_assets"
RNG = random.Random(20261112)
NP_RNG = np.random.default_rng(20261112)

WALLPAPERS = [
    Path("/System/Library/Desktop Pictures/Sonoma.heic"),
    Path("/System/Library/Desktop Pictures/.wallpapers/Sonoma Horizon/Sonoma Horizon.heic"),
]

KALIMAT = [
    "Laporan ini merangkum hasil pengukuran kinerja selama kuartal ketiga tahun berjalan.",
    "Tim operasional mencatat peningkatan efisiensi sebesar dua belas persen dibanding periode sebelumnya.",
    "Seluruh data dikumpulkan dari sistem internal dan telah diverifikasi oleh bagian audit.",
    "Kompresi dokumen membantu menghemat ruang penyimpanan dan mempercepat pengiriman lewat surel.",
    "Gambar beresolusi tinggi sering menjadi penyebab utama membengkaknya ukuran sebuah berkas.",
    "Teks dan grafik vektor tidak boleh dirasterisasi agar tetap tajam saat diperbesar.",
    "Rekomendasi berikutnya adalah menstandarkan templat presentasi di seluruh divisi.",
    "Anggaran tahun depan disusun dengan asumsi pertumbuhan pendapatan yang konservatif.",
    "Setiap unit kerja wajib menyerahkan rencana kegiatan paling lambat akhir bulan ini.",
    "Hasil survei menunjukkan tingkat kepuasan pelanggan berada pada angka delapan puluh tujuh persen.",
]


def paragraf(n: int = 5) -> str:
    return " ".join(RNG.choice(KALIMAT) for _ in range(n))


def font(size: int, mono: bool = False, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/SFNSMono.ttf"] if mono else
        (["/System/Library/Fonts/Supplemental/Arial Bold.ttf"] if bold else []) +
        ["/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"]
    )
    for c in candidates:
        try:
            return ImageFont.truetype(c, size)
        except Exception:
            continue
    return ImageFont.load_default(size)


# ------------------------------------------------------------------------ aset

def _sips_to_png(src: Path, dst: Path) -> bool:
    try:
        subprocess.run(["sips", "-s", "format", "png", str(src), "--out", str(dst)],
                       capture_output=True, timeout=120, check=True)
        return dst.exists()
    except Exception:
        return False


def synthetic_photo(w: int, h: int, seed: int) -> Image.Image:
    """Foto sintetis: noise fraktal berwarna + grain sensor."""
    rng = np.random.default_rng(seed)
    acc = np.zeros((h, w, 3), dtype=np.float32)
    amp = 1.0
    total = 0.0
    for octave in range(1, 8):
        gw, gh = 2 ** octave + 1, 2 ** octave + 1
        grid = rng.random((gh, gw, 3)).astype(np.float32)
        layer = Image.fromarray((grid * 255).astype(np.uint8)).resize((w, h), Image.Resampling.BICUBIC)
        acc += np.asarray(layer, dtype=np.float32) / 255.0 * amp
        total += amp
        amp *= 0.55
    acc /= total
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    tint = np.array([0.55, 0.75, 1.0], dtype=np.float32) * (1 - yy) + \
        np.array([0.9, 0.6, 0.35], dtype=np.float32) * yy
    img = np.clip((acc * 1.5 - 0.25) * tint, 0, 1)
    img += rng.normal(0, 0.012, img.shape).astype(np.float32)  # grain
    return Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))


def make_photos() -> list[Path]:
    """Kumpulan foto besar (PNG lossless sebagai master)."""
    photos: list[Path] = []
    masters: list[Image.Image] = []
    for i, wp in enumerate(WALLPAPERS):
        tmp = ASSETS / f"_wallpaper{i}.png"
        if wp.exists() and (tmp.exists() or _sips_to_png(wp, tmp)):
            masters.append(Image.open(tmp).convert("RGB"))
    crops = [(0, 0), (1500, 600), (2000, 2800), (300, 2500), (1000, 1500)]
    idx = 0
    for m in masters:
        mw, mh = m.size
        if mw >= 5000:
            for (x, y) in crops:
                c = m.crop((x, y, x + 4000, y + 3000))
                if idx % 2:
                    c = c.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                p = ASSETS / f"photo{idx}.png"
                c.save(p, compress_level=3)
                photos.append(p)
                idx += 1
        else:
            p = ASSETS / f"photo{idx}.png"
            m.save(p, compress_level=3)
            photos.append(p)
            idx += 1
    for s in range(3):
        p = ASSETS / f"photo{idx}.png"
        synthetic_photo(3600, 2400, 100 + s).save(p, compress_level=3)
        photos.append(p)
        idx += 1
    for tmp in ASSETS.glob("_wallpaper*.png"):
        tmp.unlink()
    return photos


def make_screenshot(w: int = 2880, h: int = 1800, with_photo: Image.Image | None = None) -> Image.Image:
    """Tiruan screenshot aplikasi (retina): sidebar, teks, tabel, blok kode."""
    img = Image.new("RGB", (w, h), (250, 250, 252))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 76], fill=(232, 232, 236))
    for i, col in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([28 + i * 44, 24, 56 + i * 44, 52], fill=col)
    d.text((w // 2 - 220, 20), "Laporan Kinerja — Dasbor", font=font(34, bold=True), fill=(60, 60, 67))
    d.rectangle([0, 76, 520, h], fill=(242, 242, 247))
    d.line([520, 76, 520, h], fill=(210, 210, 215), width=2)
    for i, item in enumerate(["Ringkasan", "Penjualan", "Pelanggan", "Inventaris", "Keuangan",
                              "Sumber Daya", "Pengaturan", "Bantuan"]):
        y = 130 + i * 78
        if i == 1:
            d.rounded_rectangle([24, y - 14, 496, y + 50], 14, fill=(0, 122, 255))
        d.text((60, y), item, font=font(32), fill=(255, 255, 255) if i == 1 else (40, 40, 45))
    x0 = 580
    d.text((x0, 120), "Penjualan per Wilayah", font=font(54, bold=True), fill=(20, 20, 25))
    y = 210
    for line in _wrap(paragraf(4), font(28), w - x0 - 80, d):
        d.text((x0, y), line, font=font(28), fill=(70, 70, 78))
        y += 42
    y += 30
    heads = ["Wilayah", "Target", "Realisasi", "Selisih", "Pertumbuhan"]
    colw = (w - x0 - 80) // len(heads)
    d.rectangle([x0, y, w - 80, y + 60], fill=(228, 233, 242))
    for i, hd in enumerate(heads):
        d.text((x0 + 20 + i * colw, y + 12), hd, font=font(28, bold=True), fill=(30, 30, 40))
    y += 60
    for r, wil in enumerate(["Jakarta", "Surabaya", "Bandung", "Medan", "Makassar", "Semarang", "Denpasar"]):
        if r % 2:
            d.rectangle([x0, y, w - 80, y + 54], fill=(246, 247, 250))
        tgt = RNG.randint(800, 2400)
        real = tgt + RNG.randint(-300, 400)
        row = [wil, f"Rp {tgt:,} jt", f"Rp {real:,} jt", f"{real - tgt:+,}", f"{(real - tgt) / tgt * 100:+.1f}%"]
        for i, cell in enumerate(row):
            colr = (30, 30, 40) if i < 3 else ((0, 140, 60) if real >= tgt else (200, 40, 40))
            d.text((x0 + 20 + i * colw, y + 10), cell, font=font(27), fill=colr)
        d.line([x0, y + 54, w - 80, y + 54], fill=(225, 225, 230), width=1)
        y += 54
    y += 40
    code_h = 330
    d.rounded_rectangle([x0, y, w - 80, y + code_h], 16, fill=(30, 33, 40))
    code = [
        "def hitung_pertumbuhan(target, realisasi):",
        "    selisih = realisasi - target",
        "    return round(selisih / target * 100, 1)",
        "",
        "for wilayah in daftar_wilayah:",
        "    print(wilayah.nama, hitung_pertumbuhan(wilayah.target, wilayah.realisasi))",
    ]
    for i, ln in enumerate(code):
        d.text((x0 + 30, y + 26 + i * 46), ln, font=font(27, mono=True),
               fill=[(198, 120, 221), (171, 178, 191), (152, 195, 121)][i % 3])
    y += code_h + 40
    if with_photo is not None:
        ph = with_photo.resize((900, 600), Image.Resampling.LANCZOS)
        img.paste(ph, (x0, min(y, h - 640)))
    else:
        for i in range(6):
            bx = x0 + i * 330
            bh = RNG.randint(80, 300)
            d.rectangle([bx, h - 100 - bh, bx + 220, h - 100], fill=(0, 122, 255) if i % 2 else (88, 86, 214))
            d.text((bx + 60, h - 90), f"Q{i % 4 + 1}", font=font(30), fill=(60, 60, 67))
    return img


def _wrap(text: str, fnt, width: int, d: ImageDraw.ImageDraw) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if d.textlength(trial, font=fnt) <= width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def make_diagram(w: int = 2400, h: int = 1600) -> Image.Image:
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((80, 50), "Alur Proses Kompresi Dokumen", font=font(60, bold=True), fill=(20, 20, 30))
    boxes = [
        ("Unggah berkas", (120, 260), (232, 244, 253), (25, 118, 210)),
        ("Deteksi format", (720, 260), (232, 244, 253), (25, 118, 210)),
        ("Kompres gambar", (1320, 260), (255, 243, 224), (239, 108, 0)),
        ("Optimasi struktur", (1920, 260), (255, 243, 224), (239, 108, 0)),
        ("Validasi hasil", (1320, 760), (232, 245, 233), (46, 125, 50)),
        ("Simpan output", (720, 760), (232, 245, 233), (46, 125, 50)),
        ("Laporan ukuran", (120, 760), (243, 229, 245), (123, 31, 162)),
    ]
    bw, bh = 400, 200
    centers = []
    for label, (x, y), fill, edge in boxes:
        d.rounded_rectangle([x, y, x + bw, y + bh], 24, fill=fill, outline=edge, width=5)
        tw = d.textlength(label, font=font(38, bold=True))
        d.text((x + (bw - tw) / 2, y + 76), label, font=font(38, bold=True), fill=edge)
        centers.append((x, y))
    def arrow(p, q):
        d.line([p, q], fill=(90, 90, 100), width=6)
        ax, ay = q
        dx = 1 if q[0] > p[0] else (-1 if q[0] < p[0] else 0)
        dy = 1 if q[1] > p[1] else (-1 if q[1] < p[1] else 0)
        if dx:
            d.polygon([(ax, ay), (ax - 26 * dx, ay - 16), (ax - 26 * dx, ay + 16)], fill=(90, 90, 100))
        else:
            d.polygon([(ax, ay), (ax - 16, ay - 26 * dy), (ax + 16, ay - 26 * dy)], fill=(90, 90, 100))
    for i in range(3):
        x, y = centers[i]
        arrow((x + bw, y + bh // 2), (centers[i + 1][0], y + bh // 2))
    arrow((centers[3][0] + bw // 2, centers[3][1] + bh), (centers[3][0] + bw // 2, 860))
    d.line([(centers[3][0] + bw // 2, 860), (centers[4][0] + bw, 860)], fill=(90, 90, 100), width=6)
    arrow((centers[4][0], 860), (centers[5][0] + bw, 860))
    arrow((centers[5][0], 860), (centers[6][0] + bw, 860))
    y = 1100
    d.text((120, y), "Keterangan:", font=font(40, bold=True), fill=(20, 20, 30))
    for i, t in enumerate([
        "1. Berkas tidak pernah diunggah ke internet; semua proses berjalan di komputer ini.",
        "2. Gambar foto dikompres sebagai JPEG, sedangkan screenshot dan diagram tetap PNG.",
        "3. Bila hasil tidak lebih kecil, berkas asli yang dipakai.",
        "4. Setiap hasil divalidasi: bisa dibuka, jumlah halaman sama, teks masih terbaca.",
    ]):
        d.text((120, y + 70 + i * 62), t, font=font(34), fill=(60, 60, 70))
    return img


def make_logo(w: int = 1600, h: int = 1200) -> Image.Image:
    """PNG dengan latar transparan."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([200, 150, 900, 850], fill=(0, 122, 255, 255))
    d.ellipse([520, 300, 1220, 1000], fill=(255, 149, 0, 200))
    d.rounded_rectangle([900, 120, 1450, 520], 60, fill=(52, 199, 89, 170))
    d.text((300, 950), "KOMPRESOR", font=font(170, bold=True), fill=(28, 28, 30, 255))
    return img.filter(ImageFilter.GaussianBlur(0.6))


def make_scan_page(page_no: int, w: int = 2480, h: int = 3508) -> Image.Image:
    """Tiruan halaman hasil scan 300 DPI (teks jadi gambar + noise)."""
    img = Image.new("L", (w, h), 244)
    d = ImageDraw.Draw(img)
    d.text((240, 220), f"SURAT KETERANGAN No. {page_no:03d}/XI/2026", font=font(64, bold=True), fill=20)
    y = 380
    for _ in range(9):
        for line in _wrap(paragraf(5), font(44), w - 480, d):
            d.text((240, y), line, font=font(44), fill=30)
            y += 66
        y += 40
        if y > h - 500:
            break
    arr = np.asarray(img, dtype=np.float32)
    arr += NP_RNG.normal(0, 5.0, arr.shape)
    out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7))
    return out.rotate(0.4, resample=Image.Resampling.BICUBIC, fillcolor=244)


def build_assets() -> dict:
    ASSETS.mkdir(parents=True, exist_ok=True)
    print("  aset: foto ...")
    photos_png = make_photos()
    a: dict = {"photo_png": photos_png, "photo_jpg": []}
    for p in photos_png:
        j = p.with_suffix(".jpg")
        Image.open(p).save(j, quality=93, subsampling=0)
        a["photo_jpg"].append(j)
    print("  aset: screenshot, diagram, logo ...")
    a["screenshot"] = ASSETS / "screenshot.png"
    make_screenshot().save(a["screenshot"])
    a["screenshot2"] = ASSETS / "screenshot_dengan_foto.png"
    make_screenshot(with_photo=Image.open(photos_png[0])).save(a["screenshot2"])
    a["diagram"] = ASSETS / "diagram.png"
    make_diagram().save(a["diagram"])
    a["logo"] = ASSETS / "logo_transparan.png"
    make_logo().save(a["logo"])
    a["scan"] = []
    for i in range(4):
        p = ASSETS / f"scan{i}.jpg"
        make_scan_page(i + 1).save(p, quality=90)
        a["scan"].append(p)
    return a


# ------------------------------------------------------------------------- PDF

def build_pdfs(a: dict) -> None:
    from reportlab import rl_config
    rl_config.useA85 = 0  # tanpa ASCII85, seperti PDF dari aplikasi sungguhan
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (Image as RLImage, PageBreak, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    try:
        pdfmetrics.registerFont(TTFont("Arial", "/System/Library/Fonts/Supplemental/Arial.ttf"))
        pdfmetrics.registerFont(TTFont("Georgia", "/System/Library/Fonts/Supplemental/Georgia.ttf"))
        body_font, head_font = "Georgia", "Arial"
    except Exception:
        body_font, head_font = "Times-Roman", "Helvetica"
    styles = getSampleStyleSheet()
    body = styles["BodyText"].clone("body", fontName=body_font, fontSize=11, leading=16)
    head = styles["Heading1"].clone("head", fontName=head_font)

    def doc(name: str, compress: int = 0):
        return SimpleDocTemplate(str(OUT / name), pagesize=A4, pageCompression=compress,
                                 leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
                                 title=name, author="Compressor Samples")

    # 1. teks saja, banyak halaman, stream tidak dikompres
    story = []
    for bab in range(1, 13):
        story.append(Paragraph(f"Bab {bab}. Tinjauan Kinerja", head))
        for _ in range(9):
            story.append(Paragraph(paragraf(7), body))
            story.append(Spacer(1, 6))
        data = [["Bulan", "Target", "Realisasi", "Selisih"]] + [
            [m, str(RNG.randint(100, 900)), str(RNG.randint(100, 900)), str(RNG.randint(-90, 90))]
            for m in ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun"]]
        t = Table(data, colWidths=[4 * cm] * 4)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, (0.6, 0.6, 0.6)),
                               ("BACKGROUND", (0, 0), (-1, 0), (0.9, 0.92, 0.96)),
                               ("FONTNAME", (0, 0), (-1, -1), head_font)]))
        story += [t, PageBreak()]
    doc("pdf_teks.pdf").build(story)

    # 2. banyak foto besar ditampilkan kecil
    story = [Paragraph("Dokumentasi Foto Kegiatan", head)]
    for i, p in enumerate(a["photo_jpg"]):
        story.append(Paragraph(f"Foto {i + 1}. {paragraf(2)}", body))
        story.append(RLImage(str(p), width=16 * cm, height=12 * cm if i < 5 else 9 * cm))
        story.append(Spacer(1, 10))
    doc("pdf_foto.pdf", 1).build(story)

    # 3. screenshot + diagram (PNG → Flate di dalam PDF)
    story = [Paragraph("Panduan Penggunaan Aplikasi", head)]
    for i, p in enumerate([a["screenshot"], a["diagram"], a["screenshot2"], a["screenshot"], a["diagram"]]):
        story.append(Paragraph(f"Langkah {i + 1}. {paragraf(3)}", body))
        w = 17 * cm
        im = Image.open(p)
        story.append(RLImage(str(p), width=w, height=w * im.size[1] / im.size[0]))
        story.append(Spacer(1, 12))
    doc("pdf_screenshot.pdf", 1).build(story)

    # 4. campuran: teks + foto PNG (Flate) + logo transparan + diagram
    story = [Paragraph("Laporan Tahunan", head)]
    lg = Image.open(a["logo"])
    story.append(RLImage(str(a["logo"]), width=6 * cm, height=6 * cm * lg.size[1] / lg.size[0]))
    for i in range(4):
        story.append(Paragraph(paragraf(8), body))
        p = a["photo_png"][i]
        story.append(RLImage(str(p), width=12 * cm, height=9 * cm))
        story.append(Paragraph(paragraf(6), body))
    dg = Image.open(a["diagram"])
    story.append(RLImage(str(a["diagram"]), width=16 * cm, height=16 * cm * dg.size[1] / dg.size[0]))
    doc("pdf_campuran.pdf", 1).build(story)

    # 5. hasil scan: tiap halaman satu gambar penuh, tanpa lapisan teks
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(OUT / "pdf_scan.pdf"), pagesize=A4)
    for p in a["scan"]:
        c.drawImage(str(p), 0, 0, width=A4[0], height=A4[1])
        c.showPage()
    c.save()


# ------------------------------------------------------------------------ DOCX

def _inject_bloat(path: Path, extra: dict[str, bytes]) -> None:
    """Tambahkan media yatim (tidak direferensikan) + thumbnail ke file OOXML."""
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
        for item in zin.infolist():
            if item.filename in extra:
                continue  # diganti dengan versi baru di bawah
            data = zin.read(item.filename)
            if (item.filename == "_rels/.rels" and "docProps/thumbnail.jpeg" in extra
                    and b"thumbnail" not in data):
                data = data.replace(
                    b"</Relationships>",
                    b'<Relationship Id="rIdThumb" Type="http://schemas.openxmlformats.org/package/2006/'
                    b'relationships/metadata/thumbnail" Target="docProps/thumbnail.jpeg"/></Relationships>')
            if item.filename == "[Content_Types].xml":
                for ext, ct in (("jpeg", "image/jpeg"), ("png", "image/png"), ("jpg", "image/jpeg")):
                    if f'Extension="{ext}"'.encode() not in data:
                        data = data.replace(b"</Types>", f'<Default Extension="{ext}" ContentType="{ct}"/></Types>'.encode())
            zout.writestr(item, data)
        for name, blob in extra.items():
            zout.writestr(name, blob)
    tmp.replace(path)


def build_docx(a: dict) -> None:
    from docx import Document
    from docx.shared import Inches

    d = Document()
    d.add_heading("Laporan Kinerja Kuartal III", 0)
    for bab in range(1, 9):
        d.add_heading(f"Bab {bab}", 1)
        for _ in range(8):
            d.add_paragraph(paragraf(7))
        t = d.add_table(rows=6, cols=4)
        t.style = "Table Grid"
        for r in range(6):
            for c in range(4):
                t.cell(r, c).text = str(RNG.randint(100, 9999))
    d.save(OUT / "docx_teks.docx")

    d = Document()
    d.add_heading("Dokumentasi Foto", 0)
    for i in range(6):
        d.add_paragraph(paragraf(3))
        src = a["photo_jpg"][i] if i % 2 == 0 else a["photo_png"][i]
        d.add_picture(str(src), width=Inches(5.5 if i < 4 else 3.0))
    d.save(OUT / "docx_foto.docx")

    # Kasus kunci: screenshot/diagram harus tetap PNG, foto PNG boleh jadi JPEG.
    d = Document()
    d.add_heading("Panduan Aplikasi (screenshot & diagram)", 0)
    for i, src in enumerate([a["screenshot"], a["diagram"], a["screenshot2"], a["photo_png"][2], a["logo"]]):
        d.add_paragraph(f"Gambar {i + 1}. " + paragraf(3))
        d.add_picture(str(src), width=Inches(6.0 if i < 4 else 2.5))
    d.save(OUT / "docx_screenshot.docx")

    # Dokumen "kotor": media yatim, thumbnail, dan gambar duplikat.
    d = Document()
    d.add_heading("Dokumen dengan media tidak terpakai", 0)
    for i in range(3):
        d.add_paragraph(paragraf(5))
        d.add_picture(str(a["photo_jpg"][1]), width=Inches(4.0))
    p = OUT / "docx_kotor.docx"
    d.save(p)
    thumb = io.BytesIO()
    Image.open(a["photo_jpg"][0]).resize((1024, 768)).save(thumb, "JPEG", quality=92)
    _inject_bloat(p, {
        "word/media/tidak_terpakai1.jpg": a["photo_jpg"][3].read_bytes(),
        "word/media/tidak_terpakai2.png": a["screenshot"].read_bytes(),
        "docProps/thumbnail.jpeg": thumb.getvalue(),
    })


# ------------------------------------------------------------------------ PPTX

def build_pptx(a: dict) -> None:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = prs.slide_layouts[6]
    title_only = prs.slide_layouts[5]
    for i in range(8):
        s = prs.slides.add_slide(blank)
        src = a["photo_jpg"][i % len(a["photo_jpg"])] if i % 3 else a["photo_png"][i % len(a["photo_png"])]
        s.shapes.add_picture(str(src), 0, 0, width=prs.slide_width, height=prs.slide_height)
        tb = s.shapes.add_textbox(Inches(0.6), Inches(0.4), Inches(9), Inches(1))
        tb.text_frame.text = f"Slide {i + 1}: Dokumentasi Lapangan"
        tb.text_frame.paragraphs[0].runs[0].font.size = Pt(36)
    prs.save(OUT / "pptx_foto.pptx")

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    title_only = prs.slide_layouts[5]
    items = [
        ("Screenshot aplikasi", a["screenshot"], 8.0),
        ("Diagram alur", a["diagram"], 8.5),
        ("Screenshot berisi foto", a["screenshot2"], 8.0),
        ("Foto PNG (tanpa transparansi)", a["photo_png"][4], 7.0),
        ("Logo transparan", a["logo"], 4.0),
        ("Foto JPEG kecil", a["photo_jpg"][6], 3.5),
    ]
    for title, src, width in items:
        s = prs.slides.add_slide(title_only)
        s.shapes.title.text = title
        pic = s.shapes.add_picture(str(src), Inches(0.8), Inches(1.6), width=Inches(width))
        tb = s.shapes.add_textbox(Inches(9.3), Inches(1.8), Inches(3.6), Inches(4))
        tb.text_frame.word_wrap = True
        tb.text_frame.text = paragraf(3)
        if title.startswith("Foto JPEG"):
            pic.crop_left, pic.crop_right = 0.25, 0.25  # gambar di-crop di dalam slide
    prs.save(OUT / "pptx_campuran.pptx")


# ------------------------------------------------------------------------ XLSX

def build_xlsx(a: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage

    wb = Workbook()
    ws = wb.active
    ws.title = "Transaksi"
    ws.append(["ID", "Tanggal", "Wilayah", "Produk", "Jumlah", "Harga", "Diskon", "Total", "Sales", "Catatan"])
    wilayah = ["Jakarta", "Surabaya", "Bandung", "Medan", "Makassar"]
    produk = ["Laptop", "Monitor", "Keyboard", "Mouse", "Printer", "Proyektor"]
    for i in range(1, 30001):
        q, h = RNG.randint(1, 50), RNG.randint(50, 20000) * 1000
        ws.append([i, f"2026-{RNG.randint(1, 12):02d}-{RNG.randint(1, 28):02d}", RNG.choice(wilayah),
                   RNG.choice(produk), q, h, RNG.choice([0, 5, 10, 15]), f"=E{i + 1}*F{i + 1}*(1-G{i + 1}/100)",
                   f"Sales {RNG.randint(1, 40)}", RNG.choice(KALIMAT)])
    ws2 = wb.create_sheet("Ringkasan")
    for r in range(1, 40):
        ws2.append([f"Baris {r}"] + [RNG.randint(1, 10 ** 6) for _ in range(8)])
    wb.save(OUT / "xlsx_data.xlsx")

    wb = Workbook()
    ws = wb.active
    ws.title = "Dasbor"
    for r in range(1, 200):
        ws.append([RNG.randint(1, 10 ** 5) for _ in range(10)])
    tmp_files = []
    for i, (src, w) in enumerate([(a["photo_jpg"][2], 640), (a["screenshot"], 900), (a["photo_png"][5], 480)]):
        im = XLImage(str(src))
        ratio = im.height / im.width
        im.width, im.height = w, int(w * ratio)
        ws.add_image(im, f"L{2 + i * 30}")
    wb.create_sheet("Data").append(["a", "b", "c"])
    wb.save(OUT / "xlsx_gambar.xlsx")


# ---------------------------------------------------------------------- gambar

def build_images(a: dict) -> None:
    shutil.copy(a["photo_jpg"][0], OUT / "gambar_foto.jpg")
    shutil.copy(a["photo_png"][1], OUT / "gambar_foto.png")
    shutil.copy(a["screenshot"], OUT / "gambar_screenshot.png")
    shutil.copy(a["diagram"], OUT / "gambar_diagram.png")
    shutil.copy(a["logo"], OUT / "gambar_logo_transparan.png")
    Image.open(a["screenshot"]).save(OUT / "gambar_screenshot.jpg", quality=92)


# ---------------------------------------------------------------------- legacy

def build_legacy() -> None:
    from compressor import convert, deps
    if not deps.has("soffice"):
        print("  (lewati DOC/PPT/XLS: LibreOffice belum terpasang → brew install --cask libreoffice)")
        return
    for src, fmt in [("docx_foto.docx", "doc"), ("pptx_foto.pptx", "ppt"), ("xlsx_data.xlsx", "xls")]:
        stem = {"doc": "doc_foto", "ppt": "ppt_foto", "xls": "xls_data"}[fmt]
        out = convert.convert(OUT / src, OUT, fmt)
        out.replace(OUT / f"{stem}.{fmt}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Buat file uji di samples/generated/")
    ap.add_argument("--force", action="store_true", help="buat ulang walau sudah ada")
    args = ap.parse_args()
    if OUT.exists() and any(OUT.glob("*.pdf")) and not args.force:
        print(f"File uji sudah ada di {OUT} (pakai --force untuk membuat ulang).")
        return
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    print("Membuat file uji ...")
    a = build_assets()
    for label, fn in [("PDF", build_pdfs), ("DOCX", build_docx), ("PPTX", build_pptx),
                      ("XLSX", build_xlsx), ("gambar", build_images)]:
        print(f"  {label} ...")
        fn(a)
    print("  DOC/PPT/XLS (lewat LibreOffice) ...")
    try:
        build_legacy()
    except Exception as e:  # modul konversi belum ada / gagal → bukan masalah fatal
        print(f"  (lewati format lama: {e})")
    shutil.rmtree(ASSETS, ignore_errors=True)
    total = 0
    for p in sorted(OUT.iterdir()):
        if p.is_file():
            total += p.stat().st_size
            print(f"    {p.name:<32} {p.stat().st_size / 1e6:8.2f} MB")
    print(f"Selesai: {total / 1e6:.1f} MB di {OUT}")


if __name__ == "__main__":
    main()
