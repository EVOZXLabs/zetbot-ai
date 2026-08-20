# Panduan Instalasi 5 Menit — ZetBot AI di HP Android (Termux)

> Panduan ini khusus untuk pemula: **tanpa laptop, tanpa tahu Python**.
> Semua dilakukan lewat HP Android + aplikasi Termux, dengan menyalin perintah
> di bawah satu per satu.

**Yang kamu butuhkan:**

- HP Android 7 ke atas
- WiFi / kuota internet yang stabil
- Sisa ruang penyimpanan ± 2 GB
- ± 10–25 menit (instalasi pertama butuh waktu karena Android "memasak"
  beberapa pustaka Python dari kode sumber — tunggu saja, jangan ditutup)
- Untuk cara tercepat: aplikasi **curl** (biasanya sudah ada di Termux;
  kalau belum, ketik `pkg install -y curl`)

**Jaminan keamanan:** bawaan bot adalah **Paper Trading** (`PAPER_MODE=true`)
— semua transaksi **simulasi**. Tidak ada uang asli yang terpakai selama kamu
belum mengubah pengaturan itu secara sengaja.

---

## 0. Cara tercepat — 1 perintah (semua otomatis)

Salin **satu baris** ini ke Termux dan Enter:

```bash
curl -fsSL https://raw.githubusercontent.com/EVOZXLabs/zetbot-ai/main/quickstart.sh | bash
```

Yang terjadi otomatis (**tanpa perlu ketik apa pun lagi**):

1. Unduh `quickstart.sh` dari repo resmi → clone repo → jalankan `install.sh`
   (paket sistem, Python, dependency, virtualenv, `.env` — semua sendiri).
2. `.env` dikonfigurasi otomatis ke **Indodax/IDR** (PAPER_MODE=true).
   Tidak ada pertanyaan interaktif — sepenuhnya non-interactive.
3. Bot langsung jalan di tmux (latar belakang) — **PAPER MODE** (uang simulasi, aman).
4. Untuk melihat log bot: `tmux attach -t zetbot-bot`
5. Untuk keluar dari tmux tanpa mematikan bot: **Ctrl+B** lalu **D**

> **Installer sepenuhnya non-interaktif.** Pemasangan paket sistem
> (`pkg update/upgrade/install`) dijalankan dengan opsi dpkg
> `--force-confold` dan `DEBIAN_FRONTEND=noninteractive`, sehingga tidak akan
> pernah meminta keputusan conffile (`Y/I/N/O/D/Z`). Konfigurasi yang sudah
> ada di perangkatmu **tidak akan ditimpa**.

> **Paket yang diinstall otomatis:** git, python, clang, rust, openssl,
> libffi, python-cryptography, tur-repo, python-numpy, python-pandas,
> **tmux, termux-api, cmake** — semua dari satu command.

> **Kenapa aman?** Perintah itu hanya mengunduh file `quickstart.sh` dari
> GitHub resmi repo ini dan menjalankannya dengan bash. Isi script-nya
> transparan dan bisa kamu baca sendiri di
> `https://github.com/EVOZXLabs/zetbot-ai/blob/main/quickstart.sh` — script
> itu hanya melakukan `git clone` + `bash install.sh` (persis jalur manual di
> bawah ini). **Script ini tidak pernah meminta API key dan tidak pernah
> mengaktifkan live trading.**

> **Mau kontrol penuh / tidak suka cara curl|bash?** Ikuti langkah manual
> mulai dari bagian 2 di bawah — hasil akhirnya sama saja.

> **Mau pakai Binance (USDT) alih-alih Indodax (IDR)?** Tambahkan env var:
> `QUICKSTART_EXCHANGE=2` sebelum pipe:
> `QUICKSTART_EXCHANGE=2 curl -fsSL ... | bash`

---

## 1. Install Termux

1. Buka Chrome di HP, lalu unduh **Termux dari F-Droid** (JANGAN dari Play
   Store — versi Play Store sudah lama dan bermasalah):
   `https://f-droid.org/packages/com.termux/`
2. Ketuk file APK yang terunduh → **Install** (izinkan "install dari sumber
   tidak dikenal" jika diminta).
3. Buka **Termux**. Kamu akan melihat layar hitam dengan teks — itu terminal.
   Ketik perintah langsung di situ.
4. (Opsional) Izinkan akses penyimpanan supaya file bisa dilihat dari
   aplikasi lain:

   ```bash
   termux-setup-storage
   ```

---

## 2. Clone repository (unduh kode bot)

Salin tempel perintah ini satu per satu ke Termux:

```bash
pkg update
pkg install -y git
git clone https://github.com/EVOZXLabs/zetbot-ai.git
cd zetbot-ai
```

> `pkg update` butuh internet. Jika perintah selesai tanpa error, lanjut.

---

## 3. Jalankan install.sh (semua ter-install otomatis)

```bash
bash install.sh
```

Installer melakukan semuanya sendiri — **tanpa perlu input manual**:

1. Deteksi Termux
2. `pkg update` + `pkg upgrade` otomatis
3. Install `git`, `python`, `clang`, `rust`, `openssl`, `libffi` + `tur-repo`
   (termasuk `python-numpy` dan `python-pandas` sebagai paket siap pakai)
4. Install `tmux`, `termux-api`, `cmake` (tmux untuk supervisor bot, cmake
   untuk build native extensions)
5. Buat virtualenv (`.venv/`)
6. Install semua dependency dari `requirements.txt`
7. Buat file `.env` dari `.env.example`
8. Buat folder `data/`, `logs/`, `backups/`
9. Buat pintasan optional Termux:Widget (satu tap di home screen)
10. Self-check dengan status PASS/FAIL

**Contoh akhir output:**

```
[10/11] Running self-check
  PASS  Dependencies importable (ccxt, requests, dotenv, colorama, cryptography, cffi)
  PASS  .env present
  PASS  Runtime folders present

[11/11] Summary
  Installer summary: 14 passed  0 failed  0 warnings  (15 checks)

  INSTALLATION: PASS

  Next steps:
    zetbot start      → start the bot
    zetbot status     → show bot status
    zetbot logs       → follow bot logs
    zetbot stop       → stop the bot
    bash update.sh    → update the bot
    bash uninstall.sh → remove the bot (config/data preserved)
    nano .env         → edit exchange / Telegram credentials
```

Yang penting: di bagian paling akhir tertulis **`INSTALLATION: PASS`**.

> Jika tiba-tiba `FAIL` di tengah jalan, cukup jalankan `bash install.sh`
> lagi — instalasi aman diulang dan tidak menghapus apa pun yang sudah jadi.

---

## 4. Edit .env (atur exchange)

> **Kalau kamu pakai cara tercepat (bagian 0), langkah ini otomatis** —
> `EXCHANGE`, `QUOTE_CURRENCY`, serta `ACCOUNT_BALANCE` sudah diatur ke
> Indodax/IDR. Langkah manual di bawah tetap berlaku kalau mau mengubah
> dengan tangan atau memakai exchange lain yang sudah didukung.

`install.sh` sudah membuat file `.env` (format aman, tidak akan di-commit).
Sekarang kita ubah sedikit pengaturannya:

```bash
nano .env
```

Ubah 3 nilai ini (di layar, gerakkan dengan tombol panah):

| Baris | Ubah menjadi (Indonesia) | Ubah menjadi (global) |
|---|---|---|
| `EXCHANGE=binance` | `EXCHANGE=indodax` | `EXCHANGE=binance` |
| `QUOTE_CURRENCY=USDT` | `QUOTE_CURRENCY=IDR` | `QUOTE_CURRENCY=USDT` |
| `ACCOUNT_BALANCE=10000` | `ACCOUNT_BALANCE=1000000` | `ACCOUNT_BALANCE=10000` |

Catatan:

- `PAPER_MODE=true` — **biarkan**. Ini mode uang simulasi.
- `API_KEY=` dan `API_SECRET=` — **biarkan kosong**. Hanya diisi jika nanti
  mau trading sungguhan (lihat `INSTALL.md`).
- `TELEGRAM_ENABLED=false` — biarkan. (Opsional: bisa diaktifkan nanti
  supaya dapat notifikasi di Telegram.)

Cara menyimpan di `nano`: tekan **Ctrl+X** → ketik **Y** → tekan **Enter**.
(Keyboard Termux punya baris tombol bantuan di bawah layar termasuk tombol
`CTRL`, jadi tekan `CTRL` lalu `X`.)

---

## 5. Jalankan bot

**Kalau pakai quickstart (bagian 0):** bot sudah jalan otomatis di tmux.

```bash
tmux attach -t zetbot-bot     # lihat log bot
# keluar dari tmux: Ctrl+B lalu D (bot tetap jalan)
```

**Kalau install manual (bagian 2-3):**

```bash
bash run.sh
```

Di Termux, `run.sh` otomatis menjalankan bot + watchdog di tmux (latar
belakang). Untuk melihat log:

```bash
tmux attach -t zetbot-bot     # lihat log bot
tmux attach -t zetbot-watchdog # lihat log watchdog
```

**Mode satu tap (opsional, Termux:Widget):** kalau sudah terpasang,
`install.sh` membuat pintasan `~/.shortcuts/zetbot-start.sh`. Install aplikasi
**Termux:Widget** dari F-Droid (`https://f-droid.org/packages/com.termux.termuxwidget/`),
lalu tambahkan widget **"ZetBot Start"** di home screen — tap sekali untuk
memulai bot tanpa membuka Termux dulu.

---

## 6. Cara stop bot

```bash
bash run.sh --stop
```

Untuk cek status:

```bash
bash run.sh --status
```

---

## 7. Cara update bot (ambil versi terbaru)

```bash
bash update.sh
```

Proses update:

1. Membackup `.env` dan `data/` otomatis (tidak akan tertimpa)
2. Menarik kode terbaru dari GitHub
3. Mengupdate dependency
4. Menampilkan pesan "Update complete!"

Lalu mulai lagi:

```bash
bash run.sh
```

---

## 8. Cara melihat log

File log disimpan di folder `logs/`:

```bash
ls logs/                        # daftar file log
tail -n 100 logs/bot-console.log   # 100 baris terakhir output bot
tail -f logs/bot-console.log      # pantau langsung (tekan Ctrl+C untuk berhenti memantau)
```

- Log harian pipeline: `logs/YYYY-MM-DD.log` (contoh: `logs/2026-08-10.log`)
- Log watchdog (pemantau restart): `logs/watchdog.log`
- Mode latar belakang: `tmux attach -t zetbot-bot` (keluar: **Ctrl+B** lalu **D**)

---

## 9. Cara uninstall (hapus bot)

```bash
bash uninstall.sh
```

Yang dilakukan:

- Menghentikan bot + watchdog yang sedang jalan
- **`.env` dan folder `data/` dipindahkan ke folder backup**
  (`.uninstall-backup-<tanggal>/`) — tidak dihapus
- Menghapus virtualenv, log, dan cache
- **Kode sumber + folder `.git` tetap ada**

Mau pasang lagi? Cukup `bash install.sh`. Mau pulihkan konfigurasi lama?

```bash
cp .uninstall-backup-*/ .env   # ikuti petunjuk yang muncul di layar
```

---

## Troubleshooting cepat

| Masalah | Solusi |
|---|---|
| `pkg: command not found` | Termux harus dari **F-Droid**, bukan Play Store |
| `git: command not found` | Jalankan `pkg install -y git` |
| Install terhenti / error `end of file on stdin at conffile prompt` | Sudah diperbaiki: installer sekarang non-interaktif (`--force-confold`). Jalankan ulang `bash install.sh` |
| `Failed building wheel for pycares` | Sudah diperbaiki: `cmake` sekarang diinstall otomatis oleh `install.sh`. Jalankan ulang `bash install.sh` |
| `Failed building wheel for zlib-ng` | Versi `ccxt` paling baru butuh `zlib-ng` yang tidak punya versi siap pakai di Android. `requirements.txt` sudah mengunci ccxt ke versi yang terbukti jalan. Jalankan `bash update.sh` lalu `bash install.sh` lagi |
| `Failed building wheel for cmake` / gagal install numpy atau pandas | Jalankan `pkg install -y tur-repo && pkg install -y python-numpy python-pandas` lalu ulangi `bash install.sh` |
| `Unable to locate package python-pandas` | Jalankan `pkg install -y tur-repo` dulu (pandas cuma tersedia lewat repo komunitas TUR), baru ulangi `bash install.sh` |
| Bot berhenti tiba-tiba tanpa error | Android mematikan Termux — whitelist baterai Termux (lihat `OPERATIONS.md`) |
| Exchange menolak koneksi (rate limit) | Tunggu beberapa menit lalu `bash run.sh` lagi |

## Keamanan singkat

- Bawaan **Paper Trading** — tidak ada uang asli yang terpakai.
- Jika suatu saat membuat API key untuk trading sungguhan: **jangan pernah
  mengaktifkan izin Withdrawal**. Lihat bagian "API Key Security" di
  `README.md`.
- `.env` otomatis diabaikan oleh git — kredensialmu tidak akan ter-upload.

Dokumen lain: `INSTALL.md` (instalasi manual), `OPERATIONS.md` (operasional
lengkap, watchdog, auto-start setelah reboot).
