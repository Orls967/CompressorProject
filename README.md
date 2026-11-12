# Compressor

Aplikasi lokal di macOS untuk mengecilkan ukuran dokumen sekecil mungkin, dengan hasil yang tetap bisa dibuka normal dan terbaca jelas. Semua proses berjalan 100% di komputer ini; tidak ada file yang dikirim ke internet.

Format yang didukung: **PDF, DOCX, PPTX, XLSX, DOC, PPT, XLS, JPG, PNG**.

## Menjalankan

```bash
./run.sh
```

Perintah itu menyiapkan lingkungan Python (sekali saja), lalu membuka UI di `http://127.0.0.1:8765`. Jatuhkan file ke halaman itu, pilih mode dan preset, lalu unduh hasilnya atau buka folder `output/`.

Perintah lain:

| Perintah | Fungsi |
|---|---|
| `./run.sh cli <folder atau file>` | Kompres batch lewat terminal |
| `./run.sh check` | Cek dependency dan cara memasang yang kurang |
| `./run.sh samples` | Buat file uji di `samples/generated/` |
| `./run.sh benchmark` | Bandingkan semua preset, hasil di `benchmark/results.md` |

Contoh CLI:

```bash
./run.sh cli ~/Documents/laporan
```

```bash
./run.sh cli ~/Documents/laporan --preset maximum --out ~/Desktop/hasil --recursive
```

```bash
./run.sh cli proposal.docx --mode pdf --dpi 120 --quality 60
```

Opsi CLI: `--mode same|pdf`, `--preset lossless|balanced|maximum`, `--dpi`, `--quality`, `--grayscale`, `--out`, `--recursive`, `--jobs`, `--verbose`.

## Dependency

Butuh Homebrew dan Python 3.11 (dibuat otomatis lewat `uv`).

```bash
brew install uv ghostscript mozjpeg pngquant oxipng
```

```bash
brew install --cask libreoffice
```

| Tool | Dipakai untuk | Kalau tidak ada |
|---|---|---|
| Ghostscript | subset font dan buang duplikat di PDF | PDF tetap dikompres, hasilnya sedikit lebih besar |
| mozjpeg | encoder JPEG | memakai encoder Pillow (±5–10% lebih besar) |
| pngquant, oxipng | PNG lossy dan lossless | memakai Pillow |
| LibreOffice | DOC/PPT/XLS dan mode Convert ke PDF | fitur itu tidak tersedia, dengan pesan cara memasangnya |

Aplikasi tidak pernah gagal diam-diam: tool yang kurang ditampilkan di UI dan di `./run.sh check`, lengkap dengan perintah `brew`-nya.

## Mode dan preset

- **Format sama**: PDF→PDF, DOCX→DOCX, PPTX→PPTX, XLSX→XLSX, JPG→JPG, PNG→PNG. Format lama DOC/PPT/XLS keluar sebagai DOCX/PPTX/XLSX.
- **Convert ke PDF**: dokumen Office dikonversi ke PDF oleh LibreOffice, lalu PDF-nya dikompres.

| Preset | DPI foto | DPI gambar berisi teks | JPEG | Keterangan |
|---|---:|---:|---:|---|
| Lossless | tetap | tetap | tetap | Piksel tidak berubah, hanya optimasi struktur |
| Balanced (default) | 150 | 220 | q75 | Cocok untuk hampir semua keperluan |
| Maximum | 100 | 150 | q50 | Sekecil mungkin; teks dan vektor tidak dirasterisasi; ada lintasan zopfli tambahan (lebih lama) |

DPI dihitung dari ukuran tampil tiap gambar di dalam dokumen, bukan dari ukuran file gambarnya. DPI, kualitas JPEG, dan grayscale bisa diubah manual di "Pengaturan custom" atau lewat opsi CLI.

## Aturan yang selalu dijaga

- **File asli tidak pernah ditimpa.** Output bernama `<nama>_compressed.<ext>`; kalau sudah ada, diberi nomor (`-2`, `-3`).
- **Tidak lebih kecil = file asli dipakai**, dan itu dilaporkan di tabel hasil.
- **Ukuran sebelum → sesudah dan persentase hemat** selalu ditampilkan.
- **Setiap output divalidasi** sebelum dipakai:
  - PDF: bisa dibuka, jumlah halaman sama, minimal 99,5% karakter teks masih bisa diekstrak, dan render beberapa halaman dibandingkan dengan aslinya.
  - DOCX/PPTX/XLSX: arsip utuh, semua relasi valid, jumlah slide/sheet sama, dan seluruh XML isi dokumen identik byte-per-byte dengan aslinya.
  - JPG/PNG: bisa dibuka dan tampilannya dibandingkan dengan aslinya.

## Cara kerja

**PDF.** Beberapa strategi dijalankan dan yang terkecil serta lolos validasi dipakai:

1. `pikepdf`: tiap gambar dikompres ulang langsung di dalam PDF sesuai ukuran tampilnya; teks, vektor, font, tautan, dan form tidak disentuh. Ditambah object stream, deflate ulang semua stream, buang metadata, thumbnail, dan objek yang tidak terpakai. Di preset Lossless dan Maximum, stream teks dan font dipoles lagi dengan zopfli.
2. `pikepdf+gs`: hasil langkah 1 dilewatkan ke Ghostscript untuk subset font dan membuang duplikat.
3. `gs-downsample`: cadangan kalau dua strategi di atas belum banyak mengecilkan file.

**DOCX/PPTX/XLSX.** File dibuka sebagai ZIP. Gambar di folder media di-resize sesuai ukuran tampilnya di XML (termasuk crop dan grup), media yang tidak terpakai, duplikat, dan thumbnail dibuang, lalu arsip di-zip ulang dengan deflate zopfli (kompatibel penuh dengan ZIP biasa, tapi lebih kecil daripada deflate level 9; untuk spreadsheet besar butuh sekitar 1 detik per MB XML).

**Foto vs gambar berisi teks.** Tiap gambar diklasifikasi dari porsi area rata dan ketajaman tepinya:

- Foto tanpa transparansi → JPEG (mozjpeg).
- Screenshot, diagram, dan gambar berisi teks → tetap PNG, dikuantisasi dengan pngquant, supaya tepi huruf tidak blur. Di dalam PDF bentuknya Flate + palet.
- Halaman teks hasil scan → JPEG, tapi tidak pernah di bawah 150 DPI.
- PNG dengan transparansi → tetap PNG.

**Convert ke PDF.** LibreOffice mengekspor dengan gambar lossless (JPEG asli diteruskan apa adanya), sehingga gambar hanya dikompres satu kali oleh pipeline PDF.

## Batasan

- PDF bertanda tangan digital: tanda tangannya tidak lagi valid setelah dikompres (ada peringatan).
- Metadata XMP PDF dibuang, jadi klaim PDF/A tidak dipertahankan.
- PDF yang butuh password untuk dibuka tidak bisa diproses.
- Gambar EMF/WMF/SVG, video, audio, dan font tertanam di dokumen Office tidak diubah.
- Di mode Convert, dokumen yang isinya hanya teks bisa menghasilkan PDF yang lebih besar dari file aslinya. PDF tetap disimpan dan diberi tanda.
- Linearize ("fast web view") mati secara default karena menambah ukuran; nyalakan dengan `--linearize`.
- Gambar JPG/PNG tidak dikonversi ke PDF di mode Convert; tetap dikompres dalam format yang sama.

## Struktur proyek

```
run.sh                  satu perintah untuk semua
compressor/
  deps.py               deteksi tool + petunjuk brew
  presets.py            Lossless / Balanced / Maximum / custom
  images.py             JPEG/PNG, klasifikasi foto vs grafis
  pdf.py                strategi kompresi PDF
  office.py             DOCX / PPTX / XLSX
  convert.py            LibreOffice headless
  validate.py           validasi output
  deflate.py            deflate zopfli + penulis ZIP
  pipeline.py           orkestrasi per file, penamaan, fallback
  cli.py                batch lewat terminal
  server.py, static/    UI web lokal
tools/make_samples.py   pembuat file uji
tools/benchmark.py      benchmark semua preset
benchmark/results.md    hasil benchmark terakhir
```

`output/`, `samples/real/`, dan `samples/generated/` tidak ikut di-commit. Taruh file aslimu di `samples/real/` kalau ingin ikut dibenchmark.
