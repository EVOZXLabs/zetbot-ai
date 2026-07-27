# Setup: On-chain (Web3) Trading + WhatsApp Control

This covers the two new features added to ZetBot AI:

1. Trading on EVM chains (Ethereum/BSC/Polygon) and Solana via DEX swaps
2. Controlling the bot over WhatsApp (via Twilio), alongside Telegram

Both are **off by default** and require explicit configuration below.

---

## 1. On-chain / Web3 trading

### What's implemented
- `scripts/onchain_providers.py` — `EVMProvider` (any Uniswap-V2-style
  DEX: Ethereum/Uniswap, BSC/PancakeSwap, Polygon/QuickSwap) and
  `SolanaProvider` (via the Jupiter aggregator)
- Real, read-only wallet balance + price/OHLCV lookups (no credentials
  needed for read-only use — price data comes from free public APIs:
  DexScreener, GeckoTerminal, Jupiter)
- Swap execution (`.swap(...)`) for both chains

### What's NOT done yet (needs your input before going live)
- **Not wired into the scanner/strategy/risk pipeline.** Right now
  `onchain_providers.py` is a standalone module you can call directly
  (e.g. from a Python shell or a new command) — it does not yet feed
  into `/scan`, `/pipeline`, or the automatic strategy engine the way
  centralized-exchange trading does. Connecting it fully means
  deciding: which tokens/pairs to scan, how position sizing/risk rules
  translate to gas-fee-bearing trades, and whether on-chain trades
  share the same paper-wallet ledger or a separate one.
- **Never tested against a live chain** in this environment (no network
  access here). The code follows standard, well-documented patterns
  (Uniswap V2 router ABI; Jupiter quote→swap→sign→send), but you should
  verify on a **testnet** (Sepolia, BSC testnet, Solana devnet) with a
  throwaway wallet before touching real funds.

### Configuration (`.env`)
```
ONCHAIN_ENABLED=true
ONCHAIN_CHAINS=bsc,solana          # any of: ethereum, bsc, polygon, solana
ONCHAIN_SLIPPAGE_BPS=50            # 0.50%
ONCHAIN_LIVE_CONFIRMED=false       # MUST be explicitly "true" before any swap executes

# EVM (pick RPC per chain you use — a dedicated provider like Alchemy/
# Infura/QuickNode is strongly recommended over public RPCs for anything
# beyond casual testing)
EVM_RPC_URL=https://bsc-dataseed.binance.org
EVM_WALLET_ADDRESS=0xYourWalletAddress
EVM_PRIVATE_KEY=your_private_key_here   # keep this out of git, ever

# Solana
SOLANA_RPC_URL=https://api.mainnet-beta.solana.com   # consider Helius/QuickNode for production
SOLANA_WALLET_ADDRESS=YourSolanaPublicKey
SOLANA_PRIVATE_KEY=your_base58_secret_key_here
```

### Security — read this before funding a wallet
- `EVM_PRIVATE_KEY` / `SOLANA_PRIVATE_KEY` give **full control** of
  whatever funds are in that wallet. Use a **dedicated trading wallet**,
  never your main wallet, and only fund it with what you're willing to
  put at risk.
- `ONCHAIN_LIVE_CONFIRMED` is a hard gate — even with keys configured,
  no swap executes until you set this to `true`. Treat it the same way
  you'd treat the existing `/golive` + "CONFIRM LIVE" flow for
  centralized-exchange trading.
- On-chain swaps are **irreversible**. There is no exchange support line
  to call if a swap goes through at a bad price — slippage protection
  (`ONCHAIN_SLIPPAGE_BPS`) is your main defense, plus checking liquidity
  before trading illiquid tokens.

---

## 2. WhatsApp control (via Twilio)

### What's implemented
- `whatsapp/whatsapp_commands.py` — a webhook server that receives
  WhatsApp messages via Twilio and dispatches them through the **same**
  command system Telegram uses. Every existing command (`/status`,
  `/balance`, `/wallet`, `/positions`, `/health`, etc.) works over
  WhatsApp with no duplicated logic.
- An allow-list (`WHATSAPP_ALLOWED_NUMBERS`) — required, not optional.
  Twilio's webhook URL is reachable by anyone who finds it, so without
  an allow-list anyone could message your bot.
- `WhatsAppCommandCenter.send(text)` for outbound/proactive messages
  (e.g. you can call this to push trade alerts), but it is **not yet
  wired into the automatic trade-notification path** (`notify_buy`,
  `notify_close`, etc. in `bot/notifier.py`) — right now those still go
  to Telegram only. Wiring WhatsApp in as a second notification target
  is a small follow-up if you want it.

### Twilio setup
1. Create a Twilio account: https://www.twilio.com
2. For **testing**: activate the WhatsApp Sandbox (Twilio Console →
   Messaging → Try it out → Send a WhatsApp message). You'll get a
   sandbox number and a join code you send once from your phone.
3. For **production**: apply for a WhatsApp Business Sender under your
   own Twilio number — this requires business verification with Meta
   and takes a few days to be approved.
4. Twilio needs a **public HTTPS URL** to reach your webhook. For local
   testing, use `ngrok http 8088` and put the resulting `https://...`
   URL + `/whatsapp/webhook` into Twilio's "WHEN A MESSAGE COMES IN"
   field. For a real deployment, put the bot behind a reverse proxy
   (nginx/Caddy) with a real TLS certificate.
5. Note WhatsApp's **24-hour session window**: you can reply freely
   within 24 hours of the user's last message, but *bot-initiated*
   messages outside that window (e.g. a 3am trade alert with no recent
   user message) require a pre-approved message template in Twilio —
   plain text won't send. Worth knowing before relying on WhatsApp for
   overnight alerts.

### Configuration (`.env`)
```
WHATSAPP_ENABLED=true
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886      # Twilio sandbox or your approved sender
WHATSAPP_ALLOWED_NUMBERS=whatsapp:+628123456789 # comma-separated, your phone(s) only
WHATSAPP_WEBHOOK_HOST=0.0.0.0
WHATSAPP_WEBHOOK_PORT=8088
```

### Running it
It starts automatically alongside the bot's main process (`main.py`)
once `WHATSAPP_ENABLED=true` and the Twilio fields above are set — no
separate process needed. For standalone testing:
```
TEST_MODE=true python -m whatsapp.whatsapp_commands
```

### Formatting note
WhatsApp doesn't support single-backtick inline code the way Telegram
does — bars like `` `████░░` `` render as plain text over WhatsApp
(the backticks are stripped, the bar itself still shows). Bold (`*text*`)
and italic (`_text_`) look identical on both platforms. Triple-backtick
blocks (used in `/balance`'s "Full breakdown" table) are preserved.

---

## Suggested next steps
Given you asked for both at once, a reasonable order to actually wire
things up end-to-end:
1. Get WhatsApp working read-only first (`/status`, `/balance`, etc.) —
   lowest risk, immediately useful, no funds at stake.
2. Test on-chain balance reads (`fetch_balance()`) on a funded testnet
   wallet — still no funds at stake, verifies RPC/wallet config.
3. Test a small swap on testnet with `ONCHAIN_LIVE_CONFIRMED=true`.
4. Only then consider mainnet, starting with a small amount.
