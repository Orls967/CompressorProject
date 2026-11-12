"""CLI batch: kompres satu folder (atau beberapa file) sekaligus.

    ./run.sh cli ~/Documents/laporan
    ./run.sh cli ~/Documents/laporan --preset maximum --out ~/Desktop/hasil
    ./run.sh cli berkas.docx --mode pdf
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import deps
from .pipeline import LEGACY_EXT, MODES, OFFICE_EXT, SUFFIX, SUPPORTED, Result, human, process_file
from .presets import PRESETS, make_settings

_ICON = {"ok": "✓", "kept_original": "=", "larger": "!", "error": "✗"}


def collect(paths: list[str], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            for f in sorted(it):
                # lewati hasil kompresi sebelumnya dan file sementara Office (~$xxx)
                if (f.is_file() and f.suffix.lower() in SUPPORTED and not f.name.startswith((".", "~$"))
                        and SUFFIX not in f.stem):
                    files.append(f)
        elif p.is_file():
            files.append(p)
        else:
            print(f"✗ tidak ditemukan: {raw}", file=sys.stderr)
    seen, unique = set(), []
    for f in files:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def _row(r: Result) -> str:
    icon = _ICON.get(r.status, "?")
    name = r.name if len(r.name) <= 34 else r.name[:31] + "…"
    if r.status == "error":
        return f"{icon} {name:<34} {human(r.size_before):>9}   GAGAL: {r.message}"
    pct = f"{r.saved_pct:5.1f}%" if r.status == "ok" else ("  0.0%" if r.status == "kept_original" else f"{r.saved_pct:5.1f}%")
    line = f"{icon} {name:<34} {human(r.size_before):>9} → {human(r.size_after):>9}  {pct}"
    if r.status == "kept_original":
        line += f"   {r.message}"
    elif r.status == "larger":
        line += "   PDF lebih besar dari file asli (tetap disimpan)"
    else:
        bits = [b for b in (r.strategy, (r.validation or {}).get("summary", "")) if b]
        line += "   " + " · ".join(bits)
    return line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="./run.sh cli",
        description="Kompres dokumen secara batch (PDF, DOCX, PPTX, XLSX, DOC, PPT, XLS, JPG, PNG). "
                    "100% lokal. File asli tidak pernah ditimpa.")
    ap.add_argument("paths", nargs="+", help="folder atau file yang akan dikompres")
    ap.add_argument("-m", "--mode", choices=list(MODES), default="same",
                    help="same = format sama (default); pdf = konversi dokumen Office ke PDF lalu kompres")
    ap.add_argument("-p", "--preset", choices=list(PRESETS), default="balanced",
                    help="lossless | balanced (default) | maximum")
    ap.add_argument("--dpi", type=int, help="DPI custom untuk gambar (menimpa preset)")
    ap.add_argument("--quality", type=int, help="kualitas JPEG custom 5–95 (menimpa preset)")
    ap.add_argument("--grayscale", action="store_true", help="ubah ke hitam-putih (abu-abu)")
    ap.add_argument("--linearize", action="store_true", help="PDF 'fast web view' (menambah beberapa KB)")
    ap.add_argument("-o", "--out", help=f"folder output (default: di samping file asli, akhiran {SUFFIX})")
    ap.add_argument("-r", "--recursive", action="store_true", help="telusuri subfolder")
    ap.add_argument("-j", "--jobs", type=int, default=max(1, min(4, (os.cpu_count() or 2) // 2)),
                    help="jumlah file yang diproses bersamaan")
    ap.add_argument("-v", "--verbose", action="store_true", help="tampilkan catatan detail tiap file")
    args = ap.parse_args(argv)

    settings = make_settings(args.preset, dpi=args.dpi, quality=args.quality,
                             grayscale=args.grayscale or None, linearize=args.linearize or None)
    files = collect(args.paths, args.recursive)
    if not files:
        print("Tidak ada file yang didukung untuk diproses.")
        return 1

    # Beri tahu dependency yang kurang SEBELUM mulai, dengan cara install-nya.
    need = set()
    if not settings.lossless and any(f.suffix.lower() == ".pdf" for f in files) or args.mode == "pdf":
        need.add("gs")
    if any(f.suffix.lower() in LEGACY_EXT for f in files) or (
            args.mode == "pdf" and any(f.suffix.lower() in OFFICE_EXT | LEGACY_EXT for f in files)):
        need.add("soffice")
    for key in sorted(need):
        if not deps.has(key):
            t = deps.detect()[key]
            print(f"⚠ {t.name} belum terpasang ({t.required_for}).\n  Pasang dengan: {t.install}\n")

    out_dir = Path(args.out).expanduser() if args.out else None
    print(f"Mode: {MODES[args.mode]} · Preset: {settings.describe()} · {len(files)} file"
          + (f" · output: {out_dir}" if out_dir else f" · output: di samping file asli (*{SUFFIX})"))
    print()

    results: list[Result] = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(process_file, f, args.mode, settings, out_dir): f for f in files}
        for fut in as_completed(futures):
            r = fut.result()
            with lock:
                results.append(r)
                print(_row(r), flush=True)
                if args.verbose or r.status == "error":
                    for n in r.notes:
                        print(f"      · {n}")
                    if r.detail and args.verbose:
                        print(f"      · {r.detail}")

    done = [r for r in results if r.status != "error"]
    before = sum(r.size_before for r in done)
    after = sum(r.size_after for r in done)
    n_ok = sum(r.status == "ok" for r in results)
    n_keep = sum(r.status == "kept_original" for r in results)
    n_larger = sum(r.status == "larger" for r in results)
    n_err = sum(r.status == "error" for r in results)
    print()
    print("─" * 78)
    if before:
        print(f"Total: {human(before)} → {human(after)}  (hemat {human(before - after)}, "
              f"{100 * (before - after) / before:.1f}%)")
    summary = [f"{n_ok} dikompres"]
    if n_keep:
        summary.append(f"{n_keep} sudah optimal (file asli dipakai)")
    if n_larger:
        summary.append(f"{n_larger} PDF lebih besar dari aslinya")
    if n_err:
        summary.append(f"{n_err} gagal")
    print(" · ".join(summary))
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
