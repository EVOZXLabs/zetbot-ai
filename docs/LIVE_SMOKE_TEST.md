# LIVE Smoke Test Runbook — Indodax (Modal Kecil)

Satu-satunya tujuan: membuktikan **write-ahead guard (BUG-1)** bekerja dengan
uang sungguhan — bot yang mati (kill) di jendela crash TP/SL tidak boleh
menjual ulang kuantitas yang sudah terkirim ke exchange saat restart.

Posisi yang di-test DIBUKA OTOMATIS oleh pipeline, bukan `/buy`.
Jalur auto-exit TP/SL hanya membaca `data/positions.json`; posisi manual
(`/buy`) masuk `data/live_positions.json` dan TIDAK di-TP/SL-kan otomatis.

Semua angka/fakta diverifikasi dari kode (ccxt 4.5.64, indodax, taker 0.2%).

---

## 0. Prasyarat

- Akun indodax terdanai (disarankan Rp200.000–500.000).
- Bot terpasang & watchdog berjalan (`scripts/watchdog.py --status`).
- Telegram terhubung (`.env` + `TELEGRAM_ENABLED=true`).

---

## 1. Minimum order / notional market BUY indodax

Minimum per order = **~Rp10.000** untuk semua pair (ccxt `limits.amount.min`
× harga pasar hari ini ≈ Rp9.980–10.000):

| Pair | min qty | notional min | catatan |
|---|---|---|---|
| **BTC/IDR** | 0.00000876 BTC | ~Rp9.991 | paling likuid, spread ketat — pilihan utama |
| ETH/IDR | 0.000299 ETH | ~Rp9.983 | likuid |
| SOL/IDR | 0.00759 SOL | ~Rp9.980 | likuid |
| GOAT/IDR | 41.8 GOAT | ~Rp10.000 | volatil, min base besar |
| USDT/IDR | 0.559 USDT | ~Rp10.000 | likuid tapi **nyaris tanpa volatilitas — jangan dipakai** |

Catatan:
- BUY di-kode sebagai `qty_base × price` (`market_buy_requires_price()→True`,
  `scripts/exchange_providers.py:419`) → spend IDR ≈ `position_size_usdt`.
- **Pair tes: BTC/IDR**, notional **Rp15.000–25.000** (nyaman di atas min).
- Untuk pemicu TP/SL butuh volatilitas → BTC/ETH/SOL/GOAT.

---

## 2. Urutan command Telegram

Prasyarat: `.env` sudah di-set (bagian 3) lalu **restart bot** — mode engine
baru "LIVE" setelah restart (`/golive` menolak bila engine masih PAPER).

1. `/livecheck` → pastikan "READY TO ARM" (balance, koneksi, permission).
2. `/golive` → muncul warning real-money, menunggu konfirmasi
   (**window 120 detik**, `CONFIRM_WINDOW_SEC`).
3. Balas persis **`CONFIRM LIVE`** — plain text tanpa slash, huruf besar,
   `strip().upper()` (scripts/telegram_commands.py:316). → "LIVE TRADING ARMED".
4. Verifikasi: `/golive` lagi → "already ARMED", atau `/status`.

### Memutus LIVE kembali ke paper

- **Stop posisi baru:** `/pause` (tulis `data/.paused`; posisi existing tetap
  di-TP/SL-kan). Resume: `/resume`.
- **Matikan LIVE penuh:** TIDAK ada command disarm. Stop graceful
  (`/shutdown` atau `touch data/.shutdown_requested`) → bot disarm sendiri →
  set `PAPER_MODE=true` di `.env` → start lagi.
- **PENTING:** restart apa pun selalu auto-disarm (main.py:1157 +
  `LiveExecutor.ENABLED` reset). Bot TIDAK pernah live tanpa
  `/golive`+`CONFIRM LIVE` lagi setelah restart.
- **Watchdog:** kill/stop sengaja akan di-restart bot ~20 detik.
  Sebelum tes: `touch data/.watchdog_paused`. Saat siap restart:
  `rm data/.watchdog_paused`.

---

## 3. Blok `.env` aman untuk smoke test

Tempel ke `.env` (isi `<>`), lalu restart bot.

```env
# ── ZetBot AI · LIVE smoke test (indodax) ──────────────────────────
# PENTING: restart apa pun otomatis DISARM. Setelah tiap restart,
# /golive lalu balas "CONFIRM LIVE" lagi.

# Mode / exchange
PAPER_MODE=false                     # WAJIB untuk live
EXCHANGE=indodax
QUOTE_CURRENCY=IDR
API_KEY=<isi API key indodax>
API_SECRET=<isi API secret indodax>
DATA_DIR=data
LOGS_DIR=logs

# Account / sizing
ACCOUNT_BALANCE=<saldo IDR sungguhan, mis. 300000>
MAX_POSITIONS=1                      # default 1 — satu posisi saja
MAX_POSITION_SIZE_PCT=0.05           # 5% equity per posisi (DEFAULT 0.6 = 60%!)
MAX_RISK_PER_TRADE_PCT=0.5           # 0.5% risiko per trade (default 1.0)
MAX_DAILY_TRADES=2                   # batas trade harian (default 20)
MAX_DAILY_LOSS_PCT=3.0               # halt bila -3% harian
STOP_FIXED_PCT=5.0

# Filter sinyal — hanya sinyal confident & likuid
MIN_PROBABILITY=70                   # default 50
MIN_VOLUME_24H=1000000               # default 100000
MIN_RR=1.5
MAX_RR=5.0

# Protection orders OFF → tes satu jalur exit pada satu waktu
AUTO_PROTECT=false                   # catatan: default TRUE jika tidak di-set

# Pipeline
AUTO_PIPELINE=true
PIPELINE_INTERVAL=300                # default; beri waktu pantau yang bersih

# Telegram
TELEGRAM_ENABLED=true
TELEGRAM_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat id>
```

Catatan sizing (diverifikasi dari kode):
- **`MAX_POSITION_SIZE_PCT` adalah kunci.** Pipeline melewatinya ke
  `RiskManager._max_new_position_value()` = `pct × equity`, hard-cap notional
  per posisi baru (risk_manager.py:694, 729-735). Dengan equity Rp300.000 →
  posisi maksimal ~Rp15.000 (di atas min order).
- `RISK_PER_TRADE`/`MM_*` default dipakai engine sizing; set apa adanya.
- Di LIVE, `wallet.balance` = balance exchange asli (`_LiveWalletAdapter`,
  service_container.py:329, cache ~10s), `ACCOUNT_BALANCE` hanya fallback.
- `MAX_DAILY_TRADES` dipakai `safety_limits.py:169` (di-thread via
  ServiceContainer:103).

---

## 4. Simulasi crash saat exit — timing yang PERSIS

Urutan kode saat TP1 trigger (LIVE), `scripts/execution_pipeline.py`:

1. `TP_TRIGGERED` — emit (baris 179).
2. **Write-ahead** `_write_ahead` persist state baru (tp1_hit, remaining
   berkurang) KE data/positions.json (baris 187-190).
3. `_sell()` — MARKET SELL ke indodax (baris 192; `create_order` di
   execution_provider.py:853).
4. `_settle_live_order` — poll `fetch_order` hingga **±3 s** (baris 938-954).
5. `EXIT_SUBMITTED` — emit (baris 198).
6. `POSITION_CLOSED` — emit (baris 244-245), posisi final tersimpan.

**Window yang benar untuk kill = setelah order SELL benar-benar terkirim ke
indodax, sebelum `save_position`/`POSITION_CLOSED`** — praktis: selama poll
±3 detik itu.

- Kill tepat setelah `TP_TRIGGERED` = **TERLALU CEPAT** → order belum tentu
  terkirim; write-ahead sudah tercatat padahal belum ada yang terjual (phantom).
- Kill setelah `POSITION_CLOSED` = **TERLALU LAMBAT** → state sudah konsisten,
  tidak menguji apa pun.
- **Kill ~1,5–2 s setelah `TP_TRIGGERED`** = di tengah poll, order sudah di
  exchange, state belum final. Inilah yang diuji.

### Helper otomatis (disediakan)

`python scripts/smoke_crash_kill.py` membaca `data/execution_events.jsonl`
(append, bukan console log — lebih andal), memverifikasi PID bot
(`data/zetbot.pid`), lalu SIGKILL tepat di jendela yang diinginkan.

Prosedur:
1. `touch data/.watchdog_paused` — jangan biarkan watchdog restart bot di
   tengah tes.
2. Jalankan helper: `python scripts/smoke_crash_kill.py --dry-run` (cek dulu)
   → lalu `python scripts/smoke_crash_kill.py --delay 1.5`.
3. Tunggu posisi pipeline terbuka → harga menyentuh TP1 (atau SL).
4. Helper akan SIGKILL bot ~1,5 s setelah `TP_TRIGGERED`.

Opsi: `--trigger SL_TRIGGERED` (uji jalur SL), `--scan-existing`
(arm terhadap event yang sudah ada di log), `--dry-run` (log saja, tidak kill).

**Verifikasi window benar-benar teruji:**
- Di indodax harus ADA order SELL yang terkirim, DAN
- `data/positions.json` sudah menampilkan `tp1_hit: true` / remaining
  berkurang / `status: STOPPED`.
- Dua-duanya ada = crash jatuh tepat di window (write-ahead persist lalu order
  masuk exchange, tapi state belum final).
- `tp1_hit` muncul TAPI tidak ada sell di indodax → kill terlalu awal
  (phantom, tes inkonklusif) → koreksi remaining lokal sesuai holding asli.

**Tes restart (bagian inti BUG-1):**
1. `rm data/.watchdog_paused` (atau start ulang bot secara manual).
2. `/golive` → balas `CONFIRM LIVE` (re-arm — restart selalu disarm).
3. Tunggu 1 siklus monitor (~60 s) + 1 siklus pipeline (~300 s).
4. **Pastikan TIDAK ada order SELL kedua** untuk level yang sudah terjual.
   Itulah bukti write-ahead guard bekerja dengan uang sungguhan.

Catatan keamanan: indodax market SELL butuh base qty yang ada — bila crash
terjadi setelah fill, qty sudah habis, jadi re-sell mustahil oversell (error
insufficient balance), bukan double-sell.

---

## 5. Verifikasi manual setelah smoke test

**(a) Order benar-benar FILLED di indodax**
- Web/app indodax → Riwayat Transaksi → cocokkan buy & sell (jumlah, harga,
  waktu) dengan `data/execution_events.jsonl` (`EXIT_SUBMITTED`,
  `POSITION_CLOSED`).
- Opsional read-only (membaca, tidak mengirim order):
  `python3 -c "from scripts.exchange_manager import ExchangeManager; ..."` —
  ganti dengan order id dari events: `provider.fetch_order('ORDER_ID','SYMBOL/IDR')`
  (indodax ccxt `has['fetchOrder']=True`).

**(b) Balance cocok**
- `paper_balance.json` TIDAK ditulis di mode LIVE. Sumber kebenaran = akun
  indodax + `data/live_positions.json`.
- Bandingkan IDR free di indodax vs `/wallet` (balance exchange asli, cache
  ~10s). Cek equity: `IDR free + Σ(remaining_qty × last)` posisi OPEN di
  `positions.json` ≈ saldo total indodax. Selisih taker 0.2% per trade wajar.

**(c) Tidak ada posisi hantu — cross-check 3 tampilan**
- `data/positions.json` (record mesin exit) vs `data/live_positions.json`
  (kebenaran exchange, di-refresh `_resync_live_positions` tiap siklus) vs
  balance indodax.
- OPEN di `positions.json` tapi balance base = 0 di indodax → hantu lokal
  (khas kill terlalu awal) → koreksi `positions.json`.
- Holding base di indodax tanpa record OPEN → tidak ter-track (bot tidak
  akan menutup otomatis).
- `/positions` memicu live sync on-demand — pakai untuk perbandingan.

Catatan `live_positions.json`: rekonstruksi entry price butuh `fetchMyTrades`
yang tidak diadvertikan ccxt-indodax — hanya memengaruhi display entry, bukan
jalur exit `positions.json`.

---

## Checklist ringkas

- [ ] `.env` sesuai bagian 3, restart bot
- [ ] `/livecheck` → READY TO ARM
- [ ] `/golive` → balas `CONFIRM LIVE`
- [ ] `touch data/.watchdog_paused`
- [ ] `python scripts/smoke_crash_kill.py --dry-run` → jalan normal
- [ ] `python scripts/smoke_crash_kill.py --delay 1.5`
- [ ] Posisi pipeline terbuka, TP1/SL tersentuh → bot di-SIGKILL
- [ ] Verifikasi window: sell ADA di indodax + `tp1_hit` tercatat
- [ ] `rm data/.watchdog_paused`, restart, re-arm (`/golive` + `CONFIRM LIVE`)
- [ ] Tidak ada SELL kedua untuk level yang sama
- [ ] Balance/equity cocok, tidak ada posisi hantu
