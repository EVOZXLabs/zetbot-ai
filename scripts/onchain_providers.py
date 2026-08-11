"""On-chain (Web3/DApp) trading providers — EVM chains + Solana.

Mirrors the design of ``scripts/exchange_providers.py`` (CEX providers)
but talks to blockchains and DEXs instead of a CEX REST API. Kept as a
separate module because the underlying mechanics are fundamentally
different (wallets + gas + swaps vs. API-key + order book), even though
the *shape* of what a command needs (balance, price, "place a trade") is
the same.

Safety model
------------
Executing a swap moves real, irreversible on-chain funds — there is no
customer support to call and no chargeback. Every swap-executing method
therefore requires ``config.onchain_live_confirmed`` to be explicitly
``True`` (set via ``ONCHAIN_LIVE_CONFIRMED=true`` in ``.env``), on top of
credentials being present. This mirrors the existing ``/golive`` +
"CONFIRM LIVE" gate used for centralized-exchange live trading — the bot
should never be able to spend on-chain funds just because a key happened
to be configured.

Private keys are read once from config (itself populated from
environment variables) and are never logged, returned in any dict, or
included in any Telegram/WhatsApp-facing string.

Dependencies (see requirements.txt): ``web3``, ``eth-account`` for EVM;
``solders`` for Solana signing. Both chains' price/quote data come from
public, keyless APIs (DexScreener, GeckoTerminal, Jupiter) — no paid API
key is required to get started, though production use should consider a
dedicated RPC provider (Alchemy/Infura/QuickNode/Helius) instead of
public rate-limited RPC endpoints.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import requests

DEXSCREENER_API = "https://api.dexscreener.com/latest/dex"
GECKOTERMINAL_API = "https://api.geckoterminal.com/api/v2"
JUPITER_PRICE_API = "https://price.jup.ag/v6/price"
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"

# Uniswap-V2-compatible router ABI subset (also works for PancakeSwap,
# QuickSwap, SushiSwap, and most V2 forks — the interface is identical).
_ROUTER_V2_ABI = [
    {
        "name": "swapExactTokensForTokens",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
    {
        "name": "swapExactETHForTokens",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
    {
        "name": "swapExactTokensForETH",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
]

_ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

# Well-known router + wrapped-native addresses per EVM chain. Extend as
# needed — these are the standard V2 router deployments.
EVM_CHAIN_DEFAULTS: dict[str, dict[str, str]] = {
    "ethereum": {
        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",  # Uniswap V2
        "wrapped_native": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # WETH
        "dexscreener_chain": "ethereum",
        "geckoterminal_network": "eth",
    },
    "bsc": {
        "router": "0x10ED43C718714eb63d5aA57B78B54704E256024E",  # PancakeSwap V2
        "wrapped_native": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",  # WBNB
        "dexscreener_chain": "bsc",
        "geckoterminal_network": "bsc",
    },
    "polygon": {
        "router": "0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff",  # QuickSwap
        "wrapped_native": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",  # WMATIC
        "dexscreener_chain": "polygon",
        "geckoterminal_network": "polygon_pos",
    },
}


class OnchainAuthError(Exception):
    """Wallet/credentials are configured but a chain call failed.

    Same contract as ``ExchangeAuthError`` in exchange_providers.py:
    callers must treat this as fatal and must NOT interpret it as a
    zero balance.
    """


class LiveNotConfirmedError(Exception):
    """Raised when a swap is attempted without the explicit live-trading
    confirmation gate (``ONCHAIN_LIVE_CONFIRMED=true``) being set."""


def _require_live_confirmed(config: Any) -> None:
    if not getattr(config, "onchain_live_confirmed", False):
        raise LiveNotConfirmedError(
            "On-chain live trading is not confirmed. Set "
            "ONCHAIN_LIVE_CONFIRMED=true in .env only after you have "
            "tested on a testnet and understand swaps are irreversible."
        )


# ======================================================================
#  EVM provider (Ethereum / BSC / Polygon / any Uniswap-V2-compatible DEX)
# ======================================================================


class EVMProvider:
    """DEX trading + wallet reads for a single EVM chain."""

    def __init__(
        self,
        chain: str,
        rpc_url: str,
        wallet_address: str = "",
        private_key: str = "",
        slippage_bps: int = 50,
        router_address: Optional[str] = None,
        wrapped_native: Optional[str] = None,
    ) -> None:
        self._chain = chain
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._private_key = private_key  # never logged, never returned
        self._slippage_bps = slippage_bps

        defaults = EVM_CHAIN_DEFAULTS.get(chain, {})
        self._router_address = router_address or defaults.get("router", "")
        self._wrapped_native = wrapped_native or defaults.get("wrapped_native", "")
        self._dexscreener_chain = defaults.get("dexscreener_chain", chain)
        self._geckoterminal_network = defaults.get("geckoterminal_network", chain)

        self._w3: Any = None  # lazy — avoid importing web3 unless used
        self._decimals_cache: dict[str, int] = {}

    @property
    def name(self) -> str:
        return f"evm:{self._chain}"

    # ------------------------------------------------------------------
    #  Connection
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        if self._w3 is None:
            from web3 import Web3  # noqa: PLC0415

            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url, request_kwargs={"timeout": 15}))
        return self._w3

    def health_check(self) -> bool:
        try:
            return bool(self._client().is_connected())
        except Exception:
            return False

    def get_token_decimals(self, token_address: str) -> int:
        """ERC-20 ``decimals()``, cached — needed to convert a human
        amount (or USD notional) into the token's smallest unit before
        building a swap transaction. Never guess this value; a wrong
        decimals count means a swap for the wrong amount, off by a
        power of ten.
        """
        key = token_address.lower()
        if key in self._decimals_cache:
            return self._decimals_cache[key]
        w3 = self._client()
        token = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=_ERC20_ABI)
        decimals = int(token.functions.decimals().call())
        self._decimals_cache[key] = decimals
        return decimals

    # ------------------------------------------------------------------
    #  Balances
    # ------------------------------------------------------------------

    def fetch_balance(self) -> dict[str, Any]:
        """Native + tracked ERC-20 balances for the configured wallet.

        Returns ``{}`` if no wallet address is configured (read-only /
        scanning use). Raises ``OnchainAuthError`` if a wallet address is
        configured but the RPC call fails — never silently returns an
        empty balance in that case.
        """
        if not self._wallet_address:
            return {}
        try:
            w3 = self._client()
            checksum = w3.to_checksum_address(self._wallet_address)
            native_wei = w3.eth.get_balance(checksum)
            native = float(w3.from_wei(native_wei, "ether"))
            return {"native": native, "chain": self._chain, "wallet": self._wallet_address}
        except Exception as exc:
            raise OnchainAuthError(f"EVM ({self._chain}) balance fetch failed: {exc}") from exc

    def fetch_token_balance(self, token_address: str) -> float:
        """Balance of a single ERC-20 token, in human units (decimals-adjusted)."""
        if not self._wallet_address:
            return 0.0
        try:
            w3 = self._client()
            token = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=_ERC20_ABI)
            raw = token.functions.balanceOf(w3.to_checksum_address(self._wallet_address)).call()
            decimals = token.functions.decimals().call()
            return raw / (10 ** decimals)
        except Exception as exc:
            raise OnchainAuthError(f"EVM ({self._chain}) token balance fetch failed: {exc}") from exc

    # ------------------------------------------------------------------
    #  Market data (public, keyless APIs — no RPC needed)
    # ------------------------------------------------------------------

    def get_ticker(self, token_address: str) -> dict[str, Any]:
        """Current USD price + 24h stats for a token, via DexScreener."""
        resp = requests.get(f"{DEXSCREENER_API}/tokens/{token_address}", timeout=10)
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        chain_pairs = [p for p in pairs if p.get("chainId") == self._dexscreener_chain]
        pool = (chain_pairs or pairs or [None])[0]
        if not pool:
            return {}
        return {
            "symbol": pool.get("baseToken", {}).get("symbol", "?"),
            "price_usd": float(pool.get("priceUsd") or 0.0),
            "price_change_24h": pool.get("priceChange", {}).get("h24"),
            "volume_24h_usd": pool.get("volume", {}).get("h24"),
            "liquidity_usd": pool.get("liquidity", {}).get("usd"),
            "pair_address": pool.get("pairAddress"),
            "dex": pool.get("dexId"),
        }

    def fetch_ohlcv(self, token_address: str, timeframe: str = "1h", limit: int = 200) -> list[list[float]]:
        """OHLCV candles via GeckoTerminal, using the token's most liquid pool."""
        ticker = self.get_ticker(token_address)
        pool_address = ticker.get("pair_address")
        if not pool_address:
            return []
        gt_timeframe = {"1m": "minute", "5m": "minute", "15m": "minute",
                         "1h": "hour", "4h": "hour", "1d": "day"}.get(timeframe, "hour")
        url = (
            f"{GECKOTERMINAL_API}/networks/{self._geckoterminal_network}"
            f"/pools/{pool_address}/ohlcv/{gt_timeframe}"
        )
        resp = requests.get(url, params={"limit": min(limit, 1000)}, timeout=10)
        resp.raise_for_status()
        rows = resp.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        # GeckoTerminal returns [timestamp, open, high, low, close, volume]
        return [[r[0] * 1000, r[1], r[2], r[3], r[4], r[5]] for r in rows]

    # ------------------------------------------------------------------
    #  Swap execution
    # ------------------------------------------------------------------

    def swap(
        self,
        config: Any,
        token_in: str,
        token_out: str,
        amount_in_wei: int,
        min_amount_out_wei: int = 0,
        deadline_seconds: int = 300,
        native_in: bool = False,
    ) -> dict[str, Any]:
        """Execute a swap via the chain's default V2-compatible router.

        ``token_in``/``token_out`` are contract addresses; use the chain's
        wrapped-native address (or set ``native_in=True``) to swap from
        the native coin. Amounts are in the token's smallest unit (wei).

        Requires ``ONCHAIN_LIVE_CONFIRMED=true`` — see module docstring.
        """
        _require_live_confirmed(config)
        if not self._wallet_address or not self._private_key:
            raise OnchainAuthError(f"EVM ({self._chain}): wallet address/private key not configured")
        if not self._router_address:
            raise OnchainAuthError(f"EVM ({self._chain}): no router address configured for this chain")

        w3 = self._client()
        account = w3.to_checksum_address(self._wallet_address)
        router = w3.eth.contract(address=w3.to_checksum_address(self._router_address), abi=_ROUTER_V2_ABI)
        deadline = int(time.time()) + deadline_seconds

        if not native_in:
            self._ensure_allowance(token_in, amount_in_wei)

        path = [w3.to_checksum_address(token_in), w3.to_checksum_address(token_out)]
        common = dict(
            amountOutMin=min_amount_out_wei,
            path=path,
            to=account,
            deadline=deadline,
        )

        if native_in:
            fn = router.functions.swapExactETHForTokens(
                common["amountOutMin"], common["path"], common["to"], common["deadline"],
            )
            value = amount_in_wei
        else:
            fn = router.functions.swapExactTokensForTokens(
                amount_in_wei, common["amountOutMin"], common["path"], common["to"], common["deadline"],
            )
            value = 0

        tx = fn.build_transaction({
            "from": account,
            "value": value,
            "nonce": w3.eth.get_transaction_count(account),
            "gas": 300_000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=self._private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        return {
            "tx_hash": tx_hash.hex(),
            "status": "success" if receipt.status == 1 else "failed",
            "gas_used": receipt.gasUsed,
            "block_number": receipt.blockNumber,
        }

    def swap_exact_in(
        self,
        config: Any,
        token_in: str,
        token_out: str,
        amount_in_human: float,
        native_in: bool = False,
    ) -> dict[str, Any]:
        """Convenience wrapper over ``swap()``: takes a human-readable
        amount (e.g. 25.0 USDC) instead of raw wei, fetches the correct
        decimals itself, and derives ``min_amount_out`` from the current
        quoted price and ``config.onchain_slippage_bps`` rather than
        leaving slippage unprotected (0 minimum).
        """
        decimals_in = 18 if native_in else self.get_token_decimals(token_in)
        amount_in_wei = int(round(amount_in_human * (10 ** decimals_in)))

        # Slippage-protect using current DexScreener quotes as the
        # expected rate. If a quote is unavailable, fall back to
        # amountOutMin=0 (no protection) rather than blocking the swap —
        # but this is exactly why testnet verification matters before
        # relying on this in production.
        min_amount_out_wei = 0
        try:
            price_in_address = self._wrapped_native if native_in else token_in
            price_in_usd = float(self.get_ticker(price_in_address).get("price_usd") or 0)
            price_out_usd = float(self.get_ticker(token_out).get("price_usd") or 0)
            if price_in_usd > 0 and price_out_usd > 0:
                usd_value_in = amount_in_human * price_in_usd
                expected_out_human = usd_value_in / price_out_usd
                decimals_out = self.get_token_decimals(token_out)
                slippage = self._slippage_bps / 10_000
                min_amount_out_wei = int(round(
                    expected_out_human * (1 - slippage) * (10 ** decimals_out)
                ))
        except Exception:
            pass  # keep min_amount_out_wei = 0 — see docstring

        return self.swap(
            config, token_in, token_out, amount_in_wei,
            min_amount_out_wei=min_amount_out_wei, native_in=native_in,
        )

    def _ensure_allowance(self, token_address: str, amount_wei: int) -> None:
        """Approve the router to spend ``token_address`` if the current
        allowance is insufficient (one-time per token, standard ERC-20 flow)."""
        w3 = self._client()
        account = w3.to_checksum_address(self._wallet_address)
        token = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=_ERC20_ABI)
        current = token.functions.allowance(account, w3.to_checksum_address(self._router_address)).call()
        if current >= amount_wei:
            return
        tx = token.functions.approve(
            w3.to_checksum_address(self._router_address), amount_wei,
        ).build_transaction({
            "from": account,
            "nonce": w3.eth.get_transaction_count(account),
            "gas": 100_000,
            "gasPrice": w3.eth.gas_price,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=self._private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)


# ======================================================================
#  Solana provider (via Jupiter aggregator — the standard approach for
#  Solana swaps: Jupiter finds the route and returns a ready-to-sign
#  transaction, rather than hand-encoding AMM program instructions)
# ======================================================================


class SolanaProvider:
    """DEX trading + wallet reads for Solana, routed through Jupiter."""

    def __init__(
        self,
        rpc_url: str,
        wallet_address: str = "",
        private_key: str = "",
        slippage_bps: int = 50,
    ) -> None:
        self._rpc_url = rpc_url
        self._wallet_address = wallet_address
        self._private_key = private_key  # base58 secret key, never logged
        self._slippage_bps = slippage_bps

    @property
    def name(self) -> str:
        return "solana"

    # ------------------------------------------------------------------
    #  Balances (raw JSON-RPC — no extra SDK required for reads)
    # ------------------------------------------------------------------

    def fetch_balance(self) -> dict[str, Any]:
        if not self._wallet_address:
            return {}
        try:
            resp = requests.post(self._rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "getBalance",
                "params": [self._wallet_address],
            }, timeout=10)
            resp.raise_for_status()
            lamports = resp.json()["result"]["value"]
            return {"native": lamports / 1e9, "chain": "solana", "wallet": self._wallet_address}
        except Exception as exc:
            raise OnchainAuthError(f"Solana balance fetch failed: {exc}") from exc

    def fetch_token_balance(self, mint_address: str) -> float:
        if not self._wallet_address:
            return 0.0
        try:
            resp = requests.post(self._rpc_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                "params": [
                    self._wallet_address,
                    {"mint": mint_address},
                    {"encoding": "jsonParsed"},
                ],
            }, timeout=10)
            resp.raise_for_status()
            accounts = resp.json()["result"]["value"]
            if not accounts:
                return 0.0
            info = accounts[0]["account"]["data"]["parsed"]["info"]
            return float(info["tokenAmount"]["uiAmount"] or 0.0)
        except Exception as exc:
            raise OnchainAuthError(f"Solana token balance fetch failed: {exc}") from exc

    def get_token_decimals(self, mint_address: str) -> int:
        """SPL token decimals, via ``getTokenSupply`` — cached per call
        site (not memoized across calls; mints rarely change decimals
        but this keeps the provider stateless/simple)."""
        if mint_address == "So11111111111111111111111111111111111111112":
            return 9  # wrapped SOL — fixed, matches native SOL's lamport precision
        resp = requests.post(self._rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenSupply",
            "params": [mint_address],
        }, timeout=10)
        resp.raise_for_status()
        return int(resp.json()["result"]["value"]["decimals"])

    # ------------------------------------------------------------------
    #  Market data
    # ------------------------------------------------------------------

    def get_ticker(self, mint_address: str) -> dict[str, Any]:
        resp = requests.get(JUPITER_PRICE_API, params={"ids": mint_address}, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", {}).get(mint_address)
        if not data:
            return {}
        return {"symbol": data.get("mintSymbol", "?"), "price_usd": float(data.get("price", 0.0))}

    def fetch_ohlcv(self, mint_address: str, timeframe: str = "1h", limit: int = 200) -> list[list[float]]:
        """OHLCV via GeckoTerminal's Solana pools (Jupiter itself has no OHLCV endpoint)."""
        resp = requests.get(
            f"{GECKOTERMINAL_API}/networks/solana/tokens/{mint_address}/pools", timeout=10,
        )
        resp.raise_for_status()
        pools = resp.json().get("data", [])
        if not pools:
            return []
        pool_address = pools[0]["attributes"]["address"]
        gt_timeframe = {"1m": "minute", "5m": "minute", "15m": "minute",
                         "1h": "hour", "4h": "hour", "1d": "day"}.get(timeframe, "hour")
        url = f"{GECKOTERMINAL_API}/networks/solana/pools/{pool_address}/ohlcv/{gt_timeframe}"
        resp = requests.get(url, params={"limit": min(limit, 1000)}, timeout=10)
        resp.raise_for_status()
        rows = resp.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
        return [[r[0] * 1000, r[1], r[2], r[3], r[4], r[5]] for r in rows]

    # ------------------------------------------------------------------
    #  Swap execution (Jupiter quote → swap → sign locally → send)
    # ------------------------------------------------------------------

    def swap(
        self,
        config: Any,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
    ) -> dict[str, Any]:
        """Execute a swap through Jupiter's aggregator.

        Requires ``ONCHAIN_LIVE_CONFIRMED=true`` — see module docstring.
        The private key never leaves this process: Jupiter returns an
        unsigned transaction, which is signed locally with ``solders``
        before being submitted to the RPC.
        """
        _require_live_confirmed(config)
        if not self._wallet_address or not self._private_key:
            raise OnchainAuthError("Solana: wallet address/private key not configured")

        quote_resp = requests.get(JUPITER_QUOTE_API, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount_lamports,
            "slippageBps": self._slippage_bps,
        }, timeout=15)
        quote_resp.raise_for_status()
        quote = quote_resp.json()

        swap_resp = requests.post(JUPITER_SWAP_API, json={
            "quoteResponse": quote,
            "userPublicKey": self._wallet_address,
            "wrapAndUnwrapSol": True,
        }, timeout=15)
        swap_resp.raise_for_status()
        swap_tx_b64 = swap_resp.json()["swapTransaction"]

        from base64 import b64decode

        from solders.keypair import Keypair  # noqa: PLC0415
        from solders.transaction import VersionedTransaction  # noqa: PLC0415

        keypair = Keypair.from_base58_string(self._private_key)
        raw_tx = VersionedTransaction.from_bytes(b64decode(swap_tx_b64))
        signed_tx = VersionedTransaction(raw_tx.message, [keypair])

        send_resp = requests.post(self._rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [bytes(signed_tx).hex(), {"encoding": "hex"}],
        }, timeout=15)
        send_resp.raise_for_status()
        result = send_resp.json()
        if "error" in result:
            raise OnchainAuthError(f"Solana swap send failed: {result['error']}")

        return {"tx_signature": result.get("result"), "status": "submitted"}

    def swap_exact_in(
        self,
        config: Any,
        input_mint: str,
        output_mint: str,
        amount_in_human: float,
    ) -> dict[str, Any]:
        """Convenience wrapper over ``swap()``: takes a human-readable
        amount instead of raw lamports/smallest-units. Unlike the EVM
        version, slippage protection doesn't need to be computed
        manually here — Jupiter's quote already applies
        ``self._slippage_bps`` server-side.
        """
        decimals_in = self.get_token_decimals(input_mint)
        amount_lamports = int(round(amount_in_human * (10 ** decimals_in)))
        return self.swap(config, input_mint, output_mint, amount_lamports)


# ======================================================================
#  Factory
# ======================================================================


def get_onchain_provider(chain: str, config: Any) -> Any:
    """Build the right provider for ``chain`` from AppConfig fields."""
    chain = chain.lower()
    if chain == "solana":
        return SolanaProvider(
            rpc_url=config.solana_rpc_url,
            wallet_address=config.solana_wallet_address,
            private_key=config.solana_private_key,
            slippage_bps=config.onchain_slippage_bps,
        )
    if chain in EVM_CHAIN_DEFAULTS or config.evm_rpc_url:
        return EVMProvider(
            chain=chain,
            rpc_url=config.evm_rpc_url,
            wallet_address=config.evm_wallet_address,
            private_key=config.evm_private_key,
            slippage_bps=config.onchain_slippage_bps,
        )
    raise ValueError(f"Unsupported or unconfigured on-chain network: {chain}")
