# Fase 0 — Solid-kan Fondasi Multi-Exchange — Status: SELESAI

Ringkasan perubahan untuk setiap item checklist di `ZetBot-AI-Roadmap-Web3-DEX.md`.

---

## 1. Audit `scanner.py` — hilangkan hardcode `EXCHANGE_NAME = "binance"`

**Ditemukan 2 hardcode, bukan cuma 1:**

- `EXCHANGE_NAME = "binance"` di level modul, dipakai langsung oleh
  `MarketScanner`, `PairAnalyzer` (thread-local ccxt instance), dan
  `ScannerReport` (console/JSON/watchlist output).
- `fetch_markets()` juga hardcode `quote == "USDT"` — padahal
  `AppConfig.quote_currency` (env `QUOTE_CURRENCY`) sudah ada dan dipakai
  di `order_manager.py`, `execution_engine.py`, `live_position_sync.py`,
  tapi scanner sama sekali tidak membacanya. Ini yang membuat catatan di
  `.env.example` ("leaving this as USDT with Indodax will find zero
  markets") jadi benar-benar tidak terhindarkan sebelumnya — Indodax
  tidak akan pernah menghasilkan satu pair pun meski `QUOTE_CURRENCY=IDR`
  sudah diset dengan benar.

**Perbaikan** (`scripts/scanner.py`):
- `MarketScanner.__init__` sekarang resolve `self.exchange_name` dan
  `self.quote_currency` dari `AppConfig` (fallback ke `EXCHANGE_NAME`/
  `"USDT"` hanya kalau config tidak punya nilai).
- `fetch_markets()` filter pakai `self.quote_currency`, bukan `"USDT"`.
- `PairAnalyzer._get_exchange()` diubah dari selalu `ccxt.binance(...)`
  jadi resolve ccxt class lewat `scripts.exchange_providers` (sama
  seperti yang dipakai `ExchangeManager`) — di-cache per
  (thread, exchange_name), bukan cuma per thread.
- `PairAnalyzer.analyze()` menerima `exchange_name`, pesan error kini
  mencantumkan exchange (`"[gate] rate limited"`) supaya gampang
  membedakan kegagalan spesifik-exchange saat multi-exchange dipakai.
- `ScannerReport.to_json()` / `to_watchlist()` menulis exchange yang
  benar-benar dipakai, bukan konstanta.
- `EXCHANGE_NAME` tetap ada sebagai default/fallback (kompatibel ke
  belakang), tapi bukan lagi satu-satunya sumber kebenaran.

**Bug terkait yang ikut diperbaiki** (prasyarat, ditemukan saat audit):
- `bot/data.py` — `_get_exchange_map()` cuma memetakan
  `binance/bybit/tokocrypto/indodax`. OKX, Gate, Kucoin, MEXC — 4 dari 5
  exchange yang eksplisit disebut roadmap — akan gagal dengan
  `ValueError: Unsupported exchange` walau `scripts/exchange_providers.py`
  dan `AppConfig.SUPPORTED_EXCHANGES` sudah mendukungnya. Ini dipakai
  langsung oleh `bot/paper_engine.py`, jadi bukan cuma scanner yang kena.
- `scripts/exchange_test.py` dan `scripts/diagnostics.py` — resolve
  exchange class dengan `getattr(ccxt, config.exchange)` langsung, jadi
  salah melaporkan `tokocrypto` sebagai "unknown exchange" (Tokocrypto
  tidak punya kelas ccxt sendiri, ia numpang kelas `ccxt.binance`).
  Diperbaiki untuk pakai registry `scripts.exchange_providers` yang sama.
- `scripts/exchange_test.py` — bug kecil `any("SPOT" in str(...))` yang
  mengiterasi per-karakter string (selalu `False`) diperbaiki jadi
  pengecekan `in` biasa.

## 2. `/scan` dan `/pipeline` bisa jalan di OKX, Gate, Kucoin, MEXC, Indodax

Tercapai lewat perbaikan #1 di atas. Baik jalur `Pipeline._run_scanner`
(module-level) maupun `Pipeline._run_scanner_di` / `_ScannerAdapter`
(dependency-injection) sama-sama memanggil `scripts.scanner.main()`, yang
sekarang membaca `AppConfig` (env `EXCHANGE` + `QUOTE_CURRENCY`) secara
konsisten — tidak perlu perubahan di `pipeline.py` atau
`service_container.py`.

**Catatan operasional (bukan bug, tapi perlu diketahui):**
`MIN_VOLUME_24H` (default $50,000) didefinisikan dalam satuan quote
currency. Untuk Indodax (`QUOTE_CURRENCY=IDR`), nilai default ini akan
jauh terlalu kecil secara nominal Rupiah — bukan berarti salah, tapi
sesuaikan `scanner_min_volume` di `.env` ketika beralih ke exchange
dengan quote non-USD.

## 3. Tambah test untuk provider yang belum ada coverage-nya

- `tests/test_exchange_providers.py` sudah ada sebelumnya, tapi
  **daftar `SUPPORTED` basi** — tidak menyertakan `indodax` padahal
  `IndodaxProvider` sudah terdaftar di `_BUILTIN_PROVIDERS`. Provider ini
  nol coverage. Ditambahkan: test identitas Indodax, dan Indodax masuk ke
  semua test yang mengiterasi `SUPPORTED`.
- Ditambahkan test error-handling (`TestCredentialFailureHandling`) yang
  memverifikasi tiap provider — bukan cuma Binance — melempar
  `ExchangeAuthError` saat kredensial ada tapi API call gagal (bukan
  diam-diam mengembalikan saldo kosong). Ini bagian dari "validasi error
  handling tiap exchange" di checklist.
- Ditambahkan test precision helper (`amount_to_precision` /
  `price_to_precision`) fallback aman tanpa network.
- **`tests/test_scanner.py` (baru)** — sebelumnya `scripts/scanner.py`
  (± 900 baris, inti Fase 0) tidak punya test sama sekali. Menutup:
  - Resolusi `exchange_name`/`quote_currency` dari config (semua 8
    exchange, termasuk fallback & lowercasing).
  - `fetch_markets()` filter sesuai `quote_currency` yang dikonfigurasi
    — termasuk regression guard eksplisit untuk kasus Indodax/IDR yang
    tadinya selalu menghasilkan nol pair.
  - `PairAnalyzer._get_exchange()` resolve & cache per exchange lewat
    registry provider, termasuk alias Tokocrypto → kelas ccxt Binance.
  - Pesan error `analyze()` menyertakan nama exchange.
- `tests/test_data.py` — daftar exchange yang diuji `MarketData`
  diperluas dari 3 jadi 8 (match `AppConfig.SUPPORTED_EXCHANGES`), yang
  akan menangkap regresi seperti bug `_get_exchange_map()` di atas kalau
  terjadi lagi.

Semua test baru pakai mock (`unittest.mock`) — tidak butuh network/API
key, konsisten dengan pola `@pytest.mark.network` yang sudah ada di
`tests/conftest.py`.

> Catatan: sandbox tempat perubahan ini dibuat tidak punya akses
> jaringan untuk `pip install ccxt/pytest`, jadi test tidak dijalankan
> lewat `pytest` di sini. Logika intinya sudah diverifikasi manual
> (import + assertion langsung dengan `unittest.mock`, hasil PASS untuk
> semua skenario). Jalankan `pytest tests/test_scanner.py
> tests/test_exchange_providers.py tests/test_data.py -v` di environment
> dengan `requirements.txt` ter-install untuk konfirmasi akhir.

## 4. Validasi rate-limit & error handling tiap exchange

- Semua provider (`BaseProvider._get_exchange`) dan `PairAnalyzer`
  scanner sama-sama selalu set `enableRateLimit: True` — pembatasan
  rate-limit bawaan ccxt aktif untuk kedelapan exchange, tidak ada yang
  luput.
- `fetch_balance()` / `fetch_order()` sudah punya kontrak yang benar
  sejak awal (raise `ExchangeAuthError` saat kredensial ada tapi gagal),
  sekarang **dites eksplisit di kedelapan provider**, bukan cuma
  diasumsikan benar dari implementasi Binance saja.
- `scripts/exchange_test.py` (`/exchange_test` command) dan
  `scripts/diagnostics.py` — alat diagnostik yang justru dipakai untuk
  memvalidasi ini secara live — diperbaiki agar tidak salah melaporkan
  Tokocrypto sebagai exchange tak dikenal.
- Karakteristik API yang **berbeda per exchange** (limit kandel OHLCV,
  format simbol, dsb.) belum diuji end-to-end terhadap API live di sini
  (perlu network). Jalankan `/exchange_test` atau
  `pytest -m network tests/test_exchange_providers.py` per exchange
  secara manual, satu-satu dengan `EXCHANGE=<nama>` di `.env`, sebelum
  lanjut ke Fase 1.

---

## 5. (Tambahan) `/exchange` Telegram tidak benar-benar redirect scanner

Ditemukan saat user menguji `/exchange indodax` lewat Telegram: pesan
konfirmasi "Switched active exchange" muncul dan `/exchanges` menunjukkan
Indodax aktif, tapi scanner tetap diam-diam scan exchange lama.

**Akar masalah:** `ServiceContainer._ScannerAdapter` (dipakai `/pipeline`
dan auto-pipeline) memanggil `scripts.scanner.main()` tanpa argumen —
yang di dalamnya `MarketScanner()` selalu `load_config()` ulang dari
`.env`. Sementara `ExchangeManager` (yang diubah `/exchange` command)
cuma dipakai `OrderManager` untuk eksekusi order. Jadi switch exchange
via Telegram cuma nyambung ke separuh sistem — scanner cari pair di
exchange lama, order eksekusi diarahkan ke exchange baru. Untuk exchange
dengan quote currency beda (Indodax = IDR), scanner malah selalu
menghasilkan nol pair karena `QUOTE_CURRENCY` juga tidak ikut berubah.

**Perbaikan:**
- `scripts/scanner.py` — `main()` sekarang menerima `config` opsional,
  diteruskan ke `MarketScanner`.
- `scripts/service_container.py` — `_ScannerAdapter` dibekali
  `ExchangeManager`, dan membangun `_ScannerConfigView` (config view yang
  override `.exchange`/`.quote_currency` dari `ExchangeManager` yang
  sedang aktif, field lain tetap ikut `AppConfig` dari `.env`).
- `scripts/exchange_manager.py` — `ExchangeManager` sekarang punya state
  `quote_currency` yang bisa diubah runtime (`set_quote_currency()`),
  bukan cuma exchange name.
- `telegram/commands/exchange.py` — `/exchange <name> [quote]` bisa set
  quote currency sekalian dalam satu command (`/exchange indodax IDR`),
  dan otomatis memperingatkan kalau quote belum disesuaikan untuk
  exchange yang butuh quote non-USDT (saat ini: Indodax → IDR).
- `telegram/commands/exchanges.py` — menampilkan quote currency aktif.
- Test baru di `tests/test_service_container.py` (`TestScannerAdapter`)
  memverifikasi scanner benar-benar menerima exchange & quote currency
  yang aktif di `ExchangeManager`, termasuk setelah runtime switch.

**Catatan:** switch lewat Telegram ini murni runtime (in-memory) — kalau
bot di-restart, balik lagi ke `EXCHANGE`/`QUOTE_CURRENCY` di `.env`.
Ini disengaja (bukan bug) supaya restart selalu kembali ke konfigurasi
yang eksplisit tertulis di `.env`, bukan state tersembunyi dari sesi
sebelumnya.

---

## 6. (Tambahan) Akun baru/reset menampilkan $0.00 + "-100% all-time"

Ditemukan saat user pertama kali connect ke Indodax: `/status`/`/balance`
menampilkan Total Balance $0.00, Cash $0.00, padahal `.env` sudah set
`ACCOUNT_BALANCE=10000` (atau berapa pun). Ini muncul di sesi mana pun
yang baru (uptime baru ~1-2 menit, belum sempat scan pertama) — bukan
disebabkan oleh switch exchange.

**Akar masalah:** `scripts/accounting_reconcile.py` — fungsi `reconcile()`
yang jalan sekali di startup bot, kalau `paper_state.json` DAN
`paper_balance.json` sama-sama belum ada (akun benar-benar baru, atau
baru habis `reset_paper_state.py`), cuma `log.debug(...)` lalu skip —
tidak pernah menulis saldo awal ke disk. Sementara itu Telegram
(`_WalletAdapter`, `MetricsManager.account()`) membaca file itu
langsung; kalau belum ada, `cash`/`equity` default ke `0.0`. Parahnya,
persentase return-nya (`total_return_pct`) fallback ke asumsi baseline
$10,000 (`resolve_initial_balance`), jadi `(0 - 10000) / 10000 * 100 =
-100%` — kelihatan seperti rugi total padahal cuma belum ada data.

**Perbaikan:** `reconcile()` sekarang menginisialisasi
`paper_state.json` + `paper_balance.json` dengan `account_balance` yang
dikonfigurasi (`.env` → `ACCOUNT_BALANCE`) begitu terdeteksi kedua file
belum ada — bukan cuma skip. `main.py` diteruskan `config.account_balance`
saat memanggil `reconcile()`. Hasilnya: `/status`/`/balance`/`/wallet`
langsung menampilkan saldo yang benar sejak query pertama, `total_return_pct`
= 0% (bukan -100%), tanpa perlu menunggu siklus pipeline pertama selesai.

Test baru: `tests/test_accounting_fixes.py::TestStartupReconciliation::test_initializes_fresh_account_when_no_files_exist`

---

## File yang diubah

| File | Perubahan |
|---|---|
| `bot/data.py` | Tambah okx/gate/kucoin/mexc ke `_get_exchange_map()` |
| `scripts/scanner.py` | Hapus hardcode exchange & quote currency, threading exchange_name |
| `scripts/exchange_test.py` | Resolve exchange lewat registry provider, fix bug `has_spot` |
| `scripts/diagnostics.py` | Resolve exchange lewat registry provider |
| `README.md` | Update daftar Supported Exchanges & tabel env var |
| `SPECIFICATION.md` | §7 Supported Exchanges diperluas ke 8 exchange |
| `TODO_PRODUCTION.md` | Tambah section Multi-Exchange Support |
| `.env.example` | `MAX_POSITIONS` default 2 → 1 (samakan dgn spec §49) |
| `OPERATIONS.md` | Tabel `MAX_POSITIONS` default 3 → 1 |
| `scripts/exchange_manager.py` | Tambah state `quote_currency` runtime |
| `scripts/service_container.py` | `_ScannerAdapter` ikut `ExchangeManager` aktif |
| `telegram/commands/exchange.py` | `/exchange <name> [quote]` + warning quote mismatch |
| `telegram/commands/exchanges.py` | Tampilkan quote currency aktif |
| `tests/test_data.py` | Perluas cakupan ke 8 exchange |
| `tests/test_exchange_providers.py` | Tambah coverage Indodax + error-handling + precision helper |
| `tests/test_scanner.py` | **Baru** — cakupan multi-exchange scanner |
| `tests/test_service_container.py` | Tambah test `/exchange` ⇄ scanner sinkron |
| `scripts/accounting_reconcile.py` | Inisialisasi saldo akun baru, bukan skip diam-diam |
| `main.py` | Teruskan `account_balance` ke `reconcile()` |
| `tests/test_accounting_fixes.py` | Update + tambah test akun baru |
| `FASE0-SUMMARY.md` | **Baru** — dokumen ini |

## Yang sengaja TIDAK disentuh (di luar scope Fase 0)

- Konversi otomatis `MIN_VOLUME_24H` antar quote currency (USD ↔ IDR,
  dst.) — butuh sumber FX rate, lebih cocok masuk saat benar-benar
  dipakai live per exchange, bukan prasyarat "solid-kan fondasi".
- `coinbase | kraken | huobi | bitget | phemex` yang disebut di
  `.env.example` sebagai "diterima setup.sh tapi belum ada provider" —
  di luar 5 exchange yang eksplisit disebut roadmap Fase 0.
