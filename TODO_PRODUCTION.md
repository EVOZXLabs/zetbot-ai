# Production Readiness Checklist

## Stability

- [x] Paper Trading Engine running
- [x] Accounting reconciliation implemented
- [x] Position state recovery implemented
- [x] Telegram Command Center integrated
- [x] Health Monitor implemented
- [x] Pipeline Scheduler verified
- [x] Critical live-mode bugs audited and fixed
- [ ] 24h Paper Trading soak test
- [ ] Long-term restart recovery verification

## Performance

- [x] Memory usage monitoring
- [x] CPU usage monitoring
- [x] Thread monitoring
- [ ] Extended memory leak observation

## Deployment

- [x] Production setup health check
- [x] Environment validation
- [ ] VPS deployment
- [ ] systemd service
- [ ] Automatic restart
- [ ] Log rotation

## Trading Validation

- [x] Paper trading validation
- [x] Risk management validation
- [x] Position sizing validation
- [x] Exit reason verification
- [x] Critical live-mode bug audit completed
- [x] SL/TP protection placement verified for all order states
- [x] Fee deduction verified for all sell orders
- [x] Position exposure counting verified (PARTIAL/TRAILING/BREAKEVEN)
- [x] Daily loss limit calibrated to live balance
- [x] Pipeline safety guard preserves existing position monitoring
- [x] Admin permissions enforced by chat_id matching
- [x] Notification retries non-blocking
- [ ] Live dry-run validation
- [ ] Small-capital live validation

## Blockchain / Web3 Expansion

- [ ] DEX integration
- [ ] On-chain data analysis
- [ ] Wallet portfolio management
- [ ] Smart contract interaction

## Multi-Exchange Support (Fase 0 — Web3/DEX Roadmap)

- [x] Scanner hardcode to Binance/USDT removed — follows `EXCHANGE` and
      `QUOTE_CURRENCY` from `.env`
- [x] `MarketData` exchange map covers all 8 supported exchanges
      (previously missing OKX, Gate, Kucoin, MEXC)
- [x] Exchange diagnostics (`--test-exchange`, `setup.sh` health check)
      resolve exchanges through the provider registry (previously
      misreported Tokocrypto as unsupported)
- [x] Test coverage added for previously-uncovered providers (Indodax)
      and for the scanner's exchange/quote resolution
- [x] Credential-failure (`ExchangeAuthError`) behavior verified across
      all 8 providers, not just Binance
- [ ] Live `/scan` + `/pipeline` run validated against each real
      exchange (OKX, Gate, Kucoin, MEXC, Indodax) with a live API key
- [ ] `MIN_VOLUME_24H` / `scanner_min_volume` reviewed per quote
      currency (default is USD-denominated; needs adjustment for
      IDR on Indodax)
