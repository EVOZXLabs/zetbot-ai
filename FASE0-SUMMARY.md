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

## File yang diubah

| File | Perubahan |
|---|---|
| `bot/data.py` | Tambah okx/gate/kucoin/mexc ke `_get_exchange_map()` |
| `scripts/scanner.py` | Hapus hardcode exchange & quote currency, threading exchange_name |
| `scripts/exchange_test.py` | Resolve exchange lewat registry provider, fix bug `has_spot` |
| `scripts/diagnostics.py` | Resolve exchange lewat registry provider |
| `README.md` | Update daftar Supported Exchanges & tabel env var |
| `tests/test_data.py` | Perluas cakupan ke 8 exchange |
| `tests/test_exchange_providers.py` | Tambah coverage Indodax + error-handling + precision helper |
| `tests/test_scanner.py` | **Baru** — cakupan multi-exchange scanner |
| `FASE0-SUMMARY.md` | **Baru** — dokumen ini |

## Yang sengaja TIDAK disentuh (di luar scope Fase 0)

- Konversi otomatis `MIN_VOLUME_24H` antar quote currency (USD ↔ IDR,
  dst.) — butuh sumber FX rate, lebih cocok masuk saat benar-benar
  dipakai live per exchange, bukan prasyarat "solid-kan fondasi".
- `coinbase | kraken | huobi | bitget | phemex` yang disebut di
  `.env.example` sebagai "diterima setup.sh tapi belum ada provider" —
  di luar 5 exchange yang eksplisit disebut roadmap Fase 0.
