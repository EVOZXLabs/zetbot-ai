"""On-chain token watchlist — the scannable universe for DEX tokens.

Unlike a CEX (where ``fetch_markets()`` safely returns every listed
pair), a DEX has no equivalent: anyone can deploy a token and a pool
for it, so "scan everything" would mean scanning scams and rug-pulls
by default. On-chain scanning therefore works off an explicit,
user-curated watchlist instead.

File format (``data/onchain_watchlist.json``)::

    [
      {
        "symbol": "PEPE/USDC",
        "chain": "ethereum",
        "token_address": "0x6982508145454Ce325dDbE47a25d4ec3d2311933",
        "quote_address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
      },
      {
        "symbol": "JUP/USDC",
        "chain": "solana",
        "token_address": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
        "quote_address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
      }
    ]

Only entries whose ``chain`` is listed in ``config.onchain_chains``
(comma-separated) are actually scanned — this is a second, deliberate
opt-in layer on top of ``ONCHAIN_ENABLED`` and the watchlist file
itself, so enabling on-chain trading in general doesn't silently start
scanning every chain you've ever configured a wallet for.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

WATCHLIST_PATH = "data/onchain_watchlist.json"


@dataclass
class OnchainWatchlistEntry:
    symbol: str          # display symbol, e.g. "PEPE/USDC"
    chain: str            # "ethereum" | "bsc" | "polygon" | "solana"
    token_address: str    # contract address (EVM) or mint (Solana)
    quote_address: str = ""  # quote-side contract/mint, for swap routing


def load_onchain_watchlist(config: Any, path: str = WATCHLIST_PATH) -> list[OnchainWatchlistEntry]:
    """Load and filter the watchlist by ``config.onchain_chains``.

    Returns ``[]`` if the file doesn't exist, is malformed, or on-chain
    scanning isn't enabled — never raises, since a missing/optional
    watchlist should never break the (CEX) scanner it's layered on top of.
    """
    if not getattr(config, "onchain_enabled", False):
        return []

    allowed_chains = {
        c.strip().lower()
        for c in (getattr(config, "onchain_chains", "") or "").split(",")
        if c.strip()
    }
    if not allowed_chains:
        return []

    if not os.path.exists(path):
        return []

    try:
        with open(path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(raw, list):
        return []

    entries: list[OnchainWatchlistEntry] = []
    for item in raw:
        try:
            chain = str(item["chain"]).strip().lower()
            if chain not in allowed_chains:
                continue
            entries.append(OnchainWatchlistEntry(
                symbol=str(item["symbol"]).strip(),
                chain=chain,
                token_address=str(item["token_address"]).strip(),
                quote_address=str(item.get("quote_address", "")).strip(),
            ))
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed entries rather than aborting the whole scan

    return entries
