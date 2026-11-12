"""UI web lokal (FastAPI). Hanya mendengarkan di 127.0.0.1 — tidak bisa diakses
dari jaringan, dan tidak ada data yang dikirim ke internet.

    ./run.sh                    # buka http://127.0.0.1:8765
    ./run.sh ui --port 9000 --no-browser
"""
from __future__ import annotations

import argparse
import atexit
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __version__, deps
from .pipeline import MODES, SUPPORTED, human, process_file
from .presets import PRESET_LABELS, PRESETS, make_settings

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
STATIC = Path(__file__).resolve().parent / "static"
UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="compressor_upload_"))
atexit.register(shutil.rmtree, UPLOAD_DIR, True)


@dataclass
class Job:
    id: str
    name: str
    size: int
    mode: str
    preset: str
    status: str = "queued"          # queued | running | done
    progress: float = 0.0
    stage: str = "menunggu giliran"
    result: dict | None = None
    created: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "size": self.size, "mode": self.mode,
                "preset": self.preset, "status": self.status, "progress": round(self.progress, 3),
                "stage": self.stage, "result": self.result}


JOBS: dict[str, Job] = {}
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="compress")

app = FastAPI(title="Compressor", version=__version__, docs_url=None, redoc_url=None)


def _run(job: Job, src: Path, mode: str, settings) -> None:
    job.status, job.stage = "running", "mulai"

    def progress(frac: float, msg: str) -> None:
        job.progress, job.stage = frac, msg

    try:
        res = process_file(src, mode, settings, OUTPUT_DIR, progress)
        job.result = res.to_dict()
    except Exception as e:  # process_file sudah menangkap error; ini jaring pengaman
        job.result = {"status": "error", "message": str(e), "name": job.name,
                      "size_before": job.size, "size_after": job.size, "saved_pct": 0, "notes": []}
    finally:
        job.progress, job.status, job.stage = 1.0, "done", "selesai"
        shutil.rmtree(src.parent, ignore_errors=True)  # salinan unggahan tidak disimpan


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/info")
def info() -> dict:
    return {
        "version": __version__,
        "output_dir": str(OUTPUT_DIR),
        "modes": MODES,
        "presets": {k: {"label": PRESET_LABELS[k], "dpi": v.dpi, "quality": v.jpeg_quality,
                        "lossless": v.lossless} for k, v in PRESETS.items()},
        "extensions": sorted(SUPPORTED),
        "deps": deps.status(),
    }


@app.post("/api/compress")
async def compress(
    files: list[UploadFile] = File(...),
    mode: str = Form("same"),
    preset: str = Form("balanced"),
    dpi: int | None = Form(None),
    quality: int | None = Form(None),
    grayscale: bool = Form(False),
) -> dict:
    if mode not in MODES:
        raise HTTPException(400, f"Mode tidak dikenal: {mode}")
    try:
        settings = make_settings(preset, dpi=dpi, quality=quality, grayscale=grayscale or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    created = []
    for up in files:
        name = Path(up.filename or "file").name
        job_id = uuid.uuid4().hex[:12]
        folder = UPLOAD_DIR / job_id
        folder.mkdir(parents=True)
        dest = folder / name
        with dest.open("wb") as fh:
            shutil.copyfileobj(up.file, fh, 1024 * 1024)
        job = Job(job_id, name, dest.stat().st_size, mode, settings.describe())
        with LOCK:
            JOBS[job_id] = job
        POOL.submit(_run, job, dest, mode, settings)
        created.append(job.to_dict())
    return {"jobs": created}


@app.get("/api/jobs")
def jobs() -> dict:
    with LOCK:
        items = sorted(JOBS.values(), key=lambda j: j.created)
    done = [j.result for j in items if j.status == "done" and j.result and j.result.get("status") != "error"]
    before = sum(r["size_before"] for r in done)
    after = sum(r["size_after"] for r in done)
    return {
        "jobs": [j.to_dict() for j in items],
        "active": sum(j.status != "done" for j in items),
        "total": {"before": before, "after": after, "before_h": human(before), "after_h": human(after),
                  "saved_h": human(before - after),
                  "saved_pct": round(100 * (before - after) / before, 1) if before else 0.0},
    }


def _output_of(job_id: str) -> Path:
    job = JOBS.get(job_id)
    if not job or not job.result or not job.result.get("output"):
        raise HTTPException(404, "Hasil tidak ditemukan")
    path = Path(job.result["output"]).resolve()
    if OUTPUT_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "File hasil sudah dipindahkan atau dihapus")
    return path


@app.get("/api/download/{job_id}")
def download(job_id: str) -> FileResponse:
    path = _output_of(job_id)
    return FileResponse(path, filename=path.name)


@app.post("/api/reveal/{job_id}")
def reveal(job_id: str) -> dict:
    subprocess.run(["open", "-R", str(_output_of(job_id))], check=False)
    return {"ok": True}


@app.post("/api/open-folder")
def open_folder() -> dict:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["open", str(OUTPUT_DIR)], check=False)
    return {"ok": True}


@app.post("/api/clear")
def clear() -> dict:
    """Bersihkan daftar di layar (file di folder output TIDAK dihapus)."""
    with LOCK:
        for jid in [j.id for j in JOBS.values() if j.status == "done"]:
            del JOBS[jid]
    return {"ok": True}


@app.exception_handler(Exception)
async def on_error(_request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc) or type(exc).__name__}, status_code=500)


def _free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"Tidak ada port kosong di sekitar {preferred}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="./run.sh", description="UI web lokal Compressor")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--no-browser", action="store_true", help="jangan buka browser otomatis")
    args = ap.parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    print(f"Compressor {__version__} — {url}")
    print(f"Folder output: {OUTPUT_DIR}")
    miss = deps.missing()
    if miss:
        print("\nDependency yang belum terpasang (aplikasi tetap jalan dengan fitur terbatas):")
        for t in miss:
            print(f"  ✗ {t.name}: {t.required_for}\n      {t.install}")
    print("\nTekan Ctrl+C untuk berhenti.")
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
