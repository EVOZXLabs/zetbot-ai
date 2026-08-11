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

**Jaminan keamanan:** bawaan bot adalah **Paper Trading** (`PAPER_MODE=true`)
— semua transaksi **simulasi**. Tidak ada uang asli yang terpakai selama kamu
belum mengubah pengaturan itu secara sengaja.

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
3. Install `git`, `python`, `clang`, `rust`, `openssl`, `libffi`
4. Buat virtualenv (`.venv/`)
5. Install semua dependency dari `requirements.txt`
6. Buat file `.env` dari `.env.example`
7. Buat folder `data/`, `logs/`, `backups/`
8. Self-check dengan status PASS/FAIL

**Contoh akhir output:**

```
[8/9] Running self-check
  PASS  Dependencies importable (ccxt, requests, dotenv, colorama)
  PASS  .env present
  PASS  Runtime folders present
  PASS  Health check passed

[9/9] Summary
  Installer summary: 12 passed  0 failed  0 warnings  (12 checks)

  INSTALLATION: PASS

  Next steps:
    bash run.sh       → start the bot
    bash update.sh    → update the bot
    bash uninstall.sh → remove the bot (config/data preserved)
    nano .env         → edit exchange / Telegram credentials
```

Yang penting: di bagian paling akhir tertulis **`INSTALLATION: PASS`**.

> Jika tiba-tiba `FAIL` di tengah jalan, cukup jalankan `bash install.sh`
> lagi — instalasi aman diulang dan tidak menghapus apa pun yang sudah jadi.

---

## 4. Edit .env (atur exchange)

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

## 5. Jalankan run.sh (mulai bot)

```bash
bash run.sh
```

Bot langsung jalan dan log-nya tampil di layar:

**Contoh output:**

```
...
[2026-08-10 14:02:01] INFO  Pipeline scheduler started (interval=300s)
[2026-08-10 14:02:02] INFO  Scanner: 50 pairs analyzed, 5 signals
[2026-08-10 14:02:03] INFO  Decision: 2 candidates (paper mode)
```

> Selama masih di layar ini, bot sedang berjalan. **Jangan tutup Termux**
> kalau masih mau bot jalan (mode sederhana). Untuk menjalankan bot tetap
> hidup di latar belakang, lihat catatan di bawah.

**Mode sederhana (bawaan):** bot jalan di layar. Cocok untuk mulai belajar.

**Mode latar belakang (opsional, agar bot tetap jalan walau Termux
diminimalkan):**

```bash
pkg install -y tmux termux-api
```

1. Install aplikasi **Termux:API** dari F-Droid (`https://f-droid.org/packages/com.termux.api/`)
   dan buka sekali.
2. Buka **Pengaturan Android → Aplikasi → Termux → Baterai → Tanpa
   batasan** (agar Android tidak mematikan bot diam-diam — lihat
   `OPERATIONS.md` bagian "Menjalankan di Termux").
3. Jalankan lagi `bash run.sh` → sekarang bot + watchdog berjalan di tmux
   (latar belakang), dan bisa dilihat dengan `tmux attach -t zetbot-bot`
   (keluar dari tmux: tekan **Ctrl+B** lalu **D**).

---

## 6. Cara stop bot

**Mode sederhana (di layar):** tekan **Ctrl+C** (tombol `CTRL` di baris
bantuan keyboard, lalu huruf `C`).

**Mode latar belakang (tmux):**

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
| Instalasi lama (10–25 menit) | Normal — jangan tutup Termux, pastikan internet stabil |
| `INSTALLATION: FAIL` | Jalankan ulang `bash install.sh` (aman diulang) |
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
