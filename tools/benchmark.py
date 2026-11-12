#!/usr/bin/env python3
"""Benchmark: bandingkan semua preset pada semua file uji.

    ./run.sh benchmark                 # samples/generated + samples/real
    ./run.sh benchmark --visual        # + cek kemiripan visual dokumen Office (lebih lama)
    ./run.sh benchmark path/ke/folder  # folder lain

Hasil: benchmark/results.md (tabel) dan benchmark/results.json (data mentah).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from compressor import convert, deps, validate  # noqa: E402
from compressor.pipeline import (IMAGE_EXT, LEGACY_EXT, OFFICE_EXT, PDF_EXT, SUPPORTED,  # noqa: E402
                                 human, process_file)
from compressor.presets import PRESETS  # noqa: E402

WORK = ROOT / "benchmark" / "work"
PRESET_ORDER = ["lossless", "balanced", "maximum"]


def media_formats(path: Path) -> str:
    """Format gambar di dalam dokumen hasil (bukti screenshot tetap PNG)."""
    ext = path.suffix.lower()
    c: Counter = Counter()
    try:
        if ext in OFFICE_EXT:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if "/media/" in n:
                        c[n.rsplit(".", 1)[-1].upper().replace("JPG", "JPEG")] += 1
        elif ext == ".pdf":
            import pikepdf
            with pikepdf.open(path) as pdf:
                seen = set()
                for obj in pdf.objects:
                    if isinstance(obj, pikepdf.Stream) and obj.get("/Subtype") == "/Image" \
                            and obj.objgen not in seen:
                        seen.add(obj.objgen)
                        f = obj.get("/Filter")
                        f = str(f[-1] if isinstance(f, pikepdf.Array) else f)
                        if obj.get("/ImageMask"):
                            continue
                        c[{"/DCTDecode": "JPEG", "/FlateDecode": "Flate"}.get(f, f.strip("/"))] += 1
    except Exception:
        return "?"
    return ", ".join(f"{n} {k}" for k, n in sorted(c.items())) or "—"


def office_visual(orig: Path, out: Path) -> float | None:
    """Render dokumen asli & hasil ke PDF lewat LibreOffice, lalu bandingkan halamannya."""
    if not deps.has("soffice"):
        return None
    try:
        with tempfile.TemporaryDirectory() as t:
            a = convert.convert(orig, Path(t) / "a", "pdf")
            b = convert.convert(out, Path(t) / "b", "pdf")
            v = validate.validate_pdf(a, b)
            return v.ssim
    except Exception:
        return None


def gs_baseline(src: Path, setting: str) -> int | None:
    """Pembanding: Ghostscript dengan preset bawaan (/ebook, /screen)."""
    gs = deps.path("gs")
    if not gs:
        return None
    with tempfile.TemporaryDirectory() as t:
        out = Path(t) / "o.pdf"
        p = subprocess.run([gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
                            f"-dPDFSETTINGS=/{setting}", f"-sOutputFile={out}", str(src)],
                           capture_output=True, timeout=900)
        return out.stat().st_size if p.returncode == 0 and out.exists() else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Benchmark semua preset")
    ap.add_argument("folders", nargs="*", help="folder berisi file uji")
    ap.add_argument("--visual", action="store_true",
                    help="ukur kemiripan visual dokumen Office lewat render LibreOffice")
    ap.add_argument("--no-convert", action="store_true", help="lewati mode Convert ke PDF")
    args = ap.parse_args()

    folders = [Path(f) for f in args.folders] or [ROOT / "samples" / "generated", ROOT / "samples" / "real"]
    files = sorted(f for d in folders if d.is_dir() for f in d.iterdir()
                   if f.is_file() and f.suffix.lower() in SUPPORTED and not f.name.startswith("."))
    if not files:
        print("Tidak ada file uji. Jalankan dulu: ./run.sh samples")
        sys.exit(1)
    if WORK.exists():
        shutil.rmtree(WORK)
    print(deps.report(), "\n")

    rows = []
    t_all = time.time()
    for f in files:
        ext = f.suffix.lower()
        modes = ["same"]
        if ext in OFFICE_EXT | LEGACY_EXT and not args.no_convert:
            modes.append("pdf")
        for mode in modes:
            for preset in PRESET_ORDER:
                out_dir = WORK / f"{mode}_{preset}"
                r = process_file(f, mode, PRESETS[preset], out_dir)
                row = r.to_dict()
                row.update(file=f.name, ext=ext, mode=mode, preset_key=preset, media="", visual=None)
                if r.output and r.status in ("ok", "larger", "kept_original"):
                    out = Path(r.output)
                    row["media"] = media_formats(out)
                    if args.visual and mode == "same" and out.suffix.lower() in OFFICE_EXT \
                            and r.status == "ok" and preset != "lossless":
                        ref = f
                        row["visual"] = office_visual(ref, out)
                rows.append(row)
                v = (row.get("validation") or {}).get("summary", "")
                print(f"{f.name:<28} {mode:<5} {preset:<9} {human(r.size_before):>9} → {human(r.size_after):>9} "
                      f"{r.saved_pct:6.1f}%  {r.seconds:5.1f}s  {r.status:<13} {v}", flush=True)

    baselines = {}
    for f in files:
        if f.suffix.lower() == ".pdf":
            baselines[f.name] = {s: gs_baseline(f, s) for s in ("ebook", "screen")}

    write_report(rows, baselines, files, time.time() - t_all, args)


def write_report(rows, baselines, files, elapsed, args) -> None:
    out_dir = ROOT / "benchmark"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))

    def cell(r) -> str:
        if r["status"] == "error":
            return "GAGAL"
        if r["status"] == "kept_original":
            return f"{human(r['size_after'])} (asli)"
        return f"{human(r['size_after'])} (−{r['saved_pct']:.1f}%)" if r["saved_pct"] >= 0 \
            else f"{human(r['size_after'])} (+{-r['saved_pct']:.1f}%)"

    by = {(r["file"], r["mode"], r["preset_key"]): r for r in rows}
    md = ["# Hasil benchmark Compressor", "",
          f"Dibuat: {time.strftime('%Y-%m-%d %H:%M')} · {len(files)} file · total waktu {elapsed:.0f} detik", "",
          "Preset: **Lossless** (struktur saja) · **Balanced** (150 DPI, JPEG q75) · "
          "**Maximum** (100 DPI, JPEG q50). Persentase = penghematan dibanding file asli.", ""]

    for mode, title in (("same", "Mode format sama"), ("pdf", "Mode Convert ke PDF")):
        names = [f.name for f in files if (f.name, mode, "balanced") in by]
        if not names:
            continue
        md += [f"## {title}", "", "| File | Asli | Lossless | Balanced | Maximum | Validasi (Balanced) |",
               "|---|---:|---:|---:|---:|---|"]
        tot = {p: [0, 0] for p in PRESET_ORDER}
        for n in names:
            rs = [by[(n, mode, p)] for p in PRESET_ORDER]
            for p, r in zip(PRESET_ORDER, rs):
                if r["status"] != "error":
                    tot[p][0] += r["size_before"]
                    tot[p][1] += r["size_after"]
            val = (rs[1].get("validation") or {}).get("summary", rs[1]["message"])
            md.append(f"| {n} | {human(rs[0]['size_before'])} | {cell(rs[0])} | {cell(rs[1])} | {cell(rs[2])} | {val} |")
        total_cells = " | ".join(
            f"**{human(a)}** (−{100 * (b - a) / b:.1f}%)" if b else "—" for b, a in (tot[p] for p in PRESET_ORDER))
        md.append(f"| **Total** | **{human(tot['balanced'][0])}** | {total_cells} | |")
        md.append("")

    md += ["## Aturan foto vs screenshot", "",
           "Format gambar di dalam dokumen hasil (preset Balanced). Foto menjadi JPEG; "
           "screenshot/diagram/gambar berisi teks tetap PNG (di PDF: Flate + palet), jadi teksnya tidak blur.", "",
           "| File | Gambar di hasil | Rincian |", "|---|---|---|"]
    for f in files:
        r = by.get((f.name, "same", "balanced"))
        if r and r["media"] and r["media"] not in ("—", "?") and f.suffix.lower() not in IMAGE_EXT:
            md.append(f"| {f.name} | {r['media']} | {r['detail']} |")
    md.append("")

    if any(r.get("visual") is not None for r in rows):
        md += ["## Kemiripan visual dokumen Office", "",
               "Dokumen asli dan hasil dirender ke PDF lewat LibreOffice, lalu halamannya dibandingkan "
               "(1,000 = identik; ambang gagal 0,72).", "", "| File | Balanced | Maximum |", "|---|---:|---:|"]
        for f in files:
            b, m = by.get((f.name, "same", "balanced")), by.get((f.name, "same", "maximum"))
            if b and (b.get("visual") is not None or (m and m.get("visual") is not None)):
                fmt = lambda r: f"{r['visual']:.3f}" if r and r.get("visual") is not None else "—"  # noqa: E731
                md.append(f"| {f.name} | {fmt(b)} | {fmt(m)} |")
        md.append("")

    if baselines:
        md += ["## Pembanding: Ghostscript dengan preset bawaan", "",
               "`/ebook` (150 DPI) dan `/screen` (72 DPI) adalah cara yang umum dipakai. "
               "Perhatikan bahwa preset bawaan itu menjadikan screenshot sebagai JPEG.", "",
               "| File | Asli | gs /ebook | Compressor Balanced | gs /screen | Compressor Maximum |",
               "|---|---:|---:|---:|---:|---:|"]
        for name, b in baselines.items():
            src = by[(name, "same", "balanced")]["size_before"]
            g = lambda v: human(v) if v else "gagal"  # noqa: E731
            md.append(f"| {name} | {human(src)} | {g(b['ebook'])} | "
                      f"{human(by[(name, 'same', 'balanced')]['size_after'])} | {g(b['screen'])} | "
                      f"{human(by[(name, 'same', 'maximum')]['size_after'])} |")
        md.append("")

    md += ["## Waktu proses", "", "| File | Mode | Lossless | Balanced | Maximum |", "|---|---|---:|---:|---:|"]
    for f in files:
        for mode in ("same", "pdf"):
            if (f.name, mode, "balanced") in by:
                md.append(f"| {f.name} | {mode} | " + " | ".join(
                    f"{by[(f.name, mode, p)]['seconds']:.1f} s" for p in PRESET_ORDER) + " |")
    md.append("")
    problems = [r for r in rows if r["status"] == "error"]
    md += ["## Catatan", ""]
    if problems:
        md += [f"- GAGAL: {r['file']} ({r['mode']}, {r['preset_key']}): {r['message']}" for r in problems]
    else:
        md.append("- Tidak ada file yang gagal diproses; semua output lolos validasi.")
    kept = [r for r in rows if r["status"] == "kept_original"]
    for r in kept:
        md.append(f"- {r['file']} ({r['mode']}, {r['preset_key']}): {r['message']}")
    larger = sorted({r["file"] for r in rows if r["status"] == "larger"})
    if larger:
        md.append(f"- Di mode Convert, PDF lebih besar dari file asli untuk: {', '.join(larger)} "
                  "(dokumen tanpa gambar besar; teks + font di PDF memang lebih besar daripada XML ter-zip).")
    md.append("")
    (out_dir / "results.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\nLaporan: {out_dir / 'results.md'}")


if __name__ == "__main__":
    main()
