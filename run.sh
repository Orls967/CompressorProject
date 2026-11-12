#!/bin/bash
# Satu perintah untuk menjalankan Compressor.
#   ./run.sh                 → buka UI web di http://127.0.0.1:8765
#   ./run.sh cli <folder>    → kompres satu folder lewat terminal
#   ./run.sh check           → cek dependency
#   ./run.sh samples         → buat file uji di samples/generated/
#   ./run.sh benchmark       → bandingkan semua preset
set -e
cd "$(dirname "$0")"

PY=.venv/bin/python
STAMP=.venv/.requirements.stamp

setup_venv() {
  if [ ! -x "$PY" ]; then
    echo "→ Menyiapkan lingkungan Python 3.11 di .venv ..."
    if command -v uv >/dev/null 2>&1; then
      uv venv --python 3.11 .venv
    elif command -v python3.11 >/dev/null 2>&1; then
      python3.11 -m venv .venv
    else
      echo "✗ Python 3.11 tidak ditemukan."
      echo "  Pasang dengan:  brew install uv     (atau: brew install python@3.11)"
      exit 1
    fi
  fi
  if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
    echo "→ Memasang paket Python ..."
    if command -v uv >/dev/null 2>&1; then
      uv pip install --quiet --python "$PY" -r requirements.txt
    else
      "$PY" -m pip install --quiet -r requirements.txt
    fi
    touch "$STAMP"
  fi
}

setup_venv

case "${1:-ui}" in
  ui)        shift || true; exec "$PY" -m compressor.server "$@" ;;
  cli)       shift; exec "$PY" -m compressor.cli "$@" ;;
  check)     exec "$PY" -m compressor.deps ;;
  samples)   shift; exec "$PY" tools/make_samples.py "$@" ;;
  benchmark) shift; exec "$PY" tools/benchmark.py "$@" ;;
  -h|--help|help)
    sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//' ;;
  *)
    echo "Perintah tidak dikenal: $1"; sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
