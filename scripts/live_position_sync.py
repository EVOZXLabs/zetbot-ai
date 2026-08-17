"""Live spot position reconstruction — the exchange is the source of
truth, positions.json-style caches are a convenience layer on top.

Spot exchanges don't track "positions" the way futures/margin do —
there's no server-side entry price or position record, only balances
and trade history. This module:

    - treats ``fetch_balance()`` as truth for QUANTITY currently held
    - reconstructs an approximate ENTRY PRICE from the account's own
      fills (``provider.fetch_my_trades()``), not from any local cache —
      every provider exposes it generically; Indodax implements it via
      the signed ``GET /api/v2/myTrades`` endpoint (the legacy /tapi
      tradeHistory method was decommissioned 2026-04-07)

Entry-price reconstruction is BEST-EFFORT, not accounting-grade: it
walks fills newest-first, nets sells against the buys they closed out,
and averages the buys that cover the currently-held quantity. If the
fetched trade history doesn't go back far enough to fully account for
the held quantity, entry_price comes back as ``None`` rather than a
guess — a caller (e.g. Native SL/TP OCO, later) MUST treat that as
"don't know the entry price", never default it to the current price.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from scripts.exchange_manager import ExchangeManager
from scripts.exchange_providers import ExchangeAuthError

LIVE_POSITIONS_PATH = "data/live_positions.json"
DUST_THRESHOLD = 1e-8


def parse_exclude_symbols(raw: Optional[str]) -> set[str]:
    """Parse ``EXCLUDE_SYMBOLS`` (comma-separated base currencies, e.g.
    ``"RFC,JELLYJELLY"``) into a normalized set of uppercase base symbols.

    Accepts either the bare base currency (``"RFC"``) or a full pair
    (``"RFC/IDR"``) — only the base is kept, since the exclusion is
    about the *coin*, not one specific quote pairing of it.
    """
    if not raw:
        return set()
    out: set[str] = set()
    for chunk in raw.split(","):
        sym = chunk.strip().upper()
        if not sym:
            continue
        out.add(sym.split("/")[0])
    return out


class MissingEntryPriceError(Exception):
    """Raised by ``require_entry_price()`` when a position has no known
    entry price. Protective orders (SL/TP / OCO) MUST NOT be submitted
    without a real entry price — there is nothing to size stop/target
    levels off of, and guessing risks placing them at the wrong distance
    from a price the bot doesn't actually know.
    """


def require_entry_price(position: dict[str, Any]) -> float:
    """Guard used before submitting any protective order (SL/TP / OCO).

    Raises ``MissingEntryPriceError`` if ``position["entry_price"]`` is
    ``None`` (i.e. ``LivePositionSync`` couldn't fully reconstruct it
    from trade history) — callers must stop and surface this to the
    operator rather than substituting the current price or a guess.
    """
    entry = position.get("entry_price")
    if entry is None:
        raise MissingEntryPriceError(
            f"Cannot create protection orders for {position.get('symbol', '?')}: "
            "entry price unknown. Sync trade history first "
            "(try /positions, or check fetch_my_trades coverage on the "
            "exchange for this symbol)."
        )
    return entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LivePositionSync:
    """Rebuilds live spot positions directly from the exchange."""

    def __init__(
        self,
        exchange: ExchangeManager,
        quote_currency: str = "USDT",
        exclude_symbols: Optional[set[str] | list[str] | str] = None,
    ) -> None:
        self._exchange = exchange
        self._quote = (quote_currency or "USDT").upper()
        if isinstance(exclude_symbols, str):
            self._exclude = parse_exclude_symbols(exclude_symbols)
        elif exclude_symbols:
            self._exclude = {s.split("/")[0].strip().upper() for s in exclude_symbols if s}
        else:
            self._exclude = set()

    @staticmethod
    def _extract(balance: dict[str, Any], currency: str, field: str) -> Optional[float]:
        bucket = balance.get(field)
        if isinstance(bucket, dict) and currency in bucket:
            try:
                return float(bucket[currency])
            except (TypeError, ValueError):
                return None
        per_currency = balance.get(currency)
        if isinstance(per_currency, dict) and field in per_currency:
            try:
                return float(per_currency[field])
            except (TypeError, ValueError):
                return None
        return None

    def _reconstruct_entry_price(
        self, provider: Any, symbol: str, held_qty: float,
    ) -> Optional[float]:
        try:
            trades = provider.fetch_my_trades(symbol)
        except Exception:
            return None
        if not trades:
            return None

        remaining = held_qty
        cost = 0.0
        qty_accum = 0.0
        for t in sorted(trades, key=lambda x: x.get("timestamp", 0) or 0, reverse=True):
            side = (t.get("side") or "").lower()
            amt = float(t.get("qty", t.get("amount", 0)) or 0)
            price = float(t.get("price", 0) or 0)
            if amt <= 0 or price <= 0:
                continue
            if side == "sell":
                # A later sell means an earlier buy of this size doesn't
                # belong to what we hold NOW — approximate offset, not
                # exact FIFO/LIFO lot accounting.
                remaining += amt
                continue
            if side == "buy":
                take = min(amt, remaining)
                if take <= 0:
                    continue
                fee = t.get("fee", 0)
                if isinstance(fee, dict):
                    fee = float(fee.get("cost", 0) or 0)
                else:
                    fee = float(fee or 0)
                cost += take * price + fee * (take / amt if amt else 0.0)
                qty_accum += take
                remaining -= take
                if remaining <= DUST_THRESHOLD:
                    break

        if qty_accum <= 0 or remaining > DUST_THRESHOLD:
            # Couldn't fully account for the held quantity within the
            # fetched history window — refuse to guess.
            return None
        return cost / qty_accum

    def sync_positions(self, symbols: list[str]) -> list[dict[str, Any]]:
        """Return the CURRENT live position for each symbol (skips any
        with no meaningful — i.e. above-dust — balance).

        Symbols whose base currency is in ``exclude_symbols`` are skipped
        entirely — the bot will not surface a position for them, will not
        reconstruct an entry price for them, and will not auto-manage or
        auto-sell them. This is a hard "hands off" list, so it applies
        here regardless of which caller (auto-discovery, /positions,
        TP/SL reconciliation, /buy, /sell) invoked the sync.
        """
        provider = self._exchange.get_provider()
        balance = provider.fetch_balance()  # raises ExchangeAuthError w/ creds set
        if not balance:
            raise ExchangeAuthError("Position sync: balance fetch returned nothing.")

        results: list[dict[str, Any]] = []
        for symbol in symbols:
            base = symbol.split("/")[0]
            if base.upper() in self._exclude:
                continue
            free = self._extract(balance, base, "free")
            total = self._extract(balance, base, "total")
            qty = total if total is not None else free
            if qty is None or qty <= DUST_THRESHOLD:
                continue

            entry_price = self._reconstruct_entry_price(provider, symbol, qty)
            if entry_price is None:
                # The exchange cannot reconstruct an entry for this
                # holding (no trade history, or bought before the history
                # window), but a previously-known entry must not be lost —
                # PnL/management baseline stays stable.
                cached = load_live_positions().get(symbol, {})
                entry_price = cached.get("entry_price")

            current_price = None
            try:
                ticker = self._exchange.get_ticker(symbol)
                current_price = ticker.get("last") or ticker.get("ask")
            except Exception:
                pass

            pnl_pct = None
            if entry_price and current_price:
                pnl_pct = (current_price - entry_price) / entry_price * 100.0

            results.append({
                "symbol": symbol,
                "quantity": qty,
                "entry_price": entry_price,
                "current_price": current_price,
                "pnl_pct": pnl_pct,
                "exchange": provider.name,
                "source": "live_exchange_sync",
                "synced_at": _now(),
            })
        return results

    def sync_all_positions(self) -> list[dict[str, Any]]:
        """Discover every non-dust holding directly from the account
        balance (not just symbols the bot has traded before), and
        reconstruct a position for each.

        This is what makes the exchange the actual source of truth —
        it will surface a position even if it came from a manual trade
        on the exchange itself, not only from bot-driven /buy orders.

        Candidates are filtered against ``load_markets()`` first, so
        wallet dust / non-tradeable balances (staking tokens, referral
        rewards, etc.) that don't correspond to an actual ``<asset>/quote``
        market are skipped instead of producing noisy "Unknown" entries.
        """
        provider = self._exchange.get_provider()
        balance = provider.fetch_balance()
        if not balance:
            raise ExchangeAuthError("Position sync: balance fetch returned nothing.")

        # A cached entry whose quote no longer matches the account's
        # current quote currency (e.g. a leftover BTC/USDT record from
        # before the account was reconfigured to IDR) can never appear
        # in ``candidates`` below again, so ``merge_live_positions``
        # would otherwise keep it forever — it's never in the checked
        # set, so it's never popped. Purge it here instead, once, on
        # every sync. This only edits the on-disk cache; it never
        # touches the exchange or places any order.
        purge_mismatched_quote_positions(self._quote)

        free = balance.get("free") if isinstance(balance.get("free"), dict) else {}
        total = balance.get("total") if isinstance(balance.get("total"), dict) else {}
        currencies = set(free.keys()) | set(total.keys())
        currencies.discard(self._quote)
        currencies = {c for c in currencies if c.upper() not in self._exclude}

        markets = provider.load_markets()
        candidates = [f"{c}/{self._quote}" for c in sorted(currencies)]
        if markets:
            # Only keep candidates that are an actual tradeable market —
            # skip e.g. LDBTC (staking token) or referral-reward dust
            # that isn't a real <asset>/<quote> pair on this exchange.
            candidates = [s for s in candidates if s in markets]
        # If load_markets() itself failed (returned {}), fall back to
        # trying every candidate as-is — sync_positions() already
        # tolerates a failed ticker/trades lookup per-symbol.

        return self.sync_positions(candidates)


def purge_mismatched_quote_positions(quote_currency: str) -> list[str]:
    """Drop cached ``live_positions.json`` entries whose symbol's quote
    currency does not match ``quote_currency`` (e.g. a stale ``BTC/USDT``
    record left over from before the account was reconfigured to IDR).

    Such entries can never be re-synced under the current config (they
    are not in ``sync_all_positions``'s candidate list), so without this
    they sit in the cache forever, permanently skipped by the
    ``ExecutionPipeline`` TP/SL currency guard on every cycle. Best-effort,
    cache-only — never touches the exchange. Returns the removed symbols.
    """
    quote = (quote_currency or "").upper()
    if not quote:
        return []
    current = load_live_positions()
    stale = [
        sym for sym in current
        if "/" in sym and sym.split("/")[1].upper() != quote
    ]
    if not stale:
        return []
    for sym in stale:
        current.pop(sym, None)
    save_live_positions(current)
    return stale


def load_live_positions() -> dict[str, Any]:
    try:
        with open(LIVE_POSITIONS_PATH) as f:
            return dict(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def snapshot_levels_for_symbol(symbol: str) -> dict[str, float]:
    """Best-effort SL/TP levels for ``symbol`` from the write-once entry
    snapshot store (``data/entry_snapshots.json``), keyed by the symbol
    alone so ANY symbol can be healed — never symbol-specific code.

    Returns only keys whose value is a positive price
    (``stop_loss``/``tp1``/``tp2``/``tp3``).  Empty dict when there is no
    snapshot (e.g. a position that predates snapshotting).
    """
    try:
        from scripts.paper_state_lock import load_entry_snapshots  # noqa: PLC0415
        out: dict[str, float] = {}
        for snap in load_entry_snapshots().values():
            if not isinstance(snap, dict) or snap.get("symbol") != symbol:
                continue
            for key in ("stop_loss", "tp1", "tp2", "tp3"):
                val = snap.get(key)
                try:
                    fval = float(val or 0)
                except (TypeError, ValueError):
                    continue
                if fval > 0:
                    out[key] = fval
            if out:
                break
        return out
    except Exception:
        return {}


def bot_managed_live_positions(path: Optional[str] = None) -> list[dict[str, Any]]:
    """Live position records whose entry price is known (bot-managed).

    A position reconstructed purely from the exchange balance — legacy /
    manual / dust holdings with no matching fill history — comes back with
    ``entry_price=None`` (see ``LivePositionSync._reconstruct_entry_price``)
    and is NOT considered bot-managed. Those must never count toward
    ``MAX_POSITIONS``-style gates, or a few IDR dust balances would
    permanently block every new BUY.

    Handles both cache shapes: ``{"SYMBOL": {...}, ...}`` (what the sync
    actually writes) and ``{"positions": [...]}`` / a bare list (legacy).
    ``path`` defaults to ``LIVE_POSITIONS_PATH`` and is resolved at call
    time so a test can redirect it (e.g.
    ``SafeGuard.set_live_positions_path``) without re-importing.
    """
    try:
        with open(path or LIVE_POSITIONS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        records = data.get("positions")
        if not isinstance(records, list):
            records = []
            for key, rec in data.items():
                if not isinstance(rec, dict):
                    continue
                if not rec.get("symbol"):
                    # Legacy record missing the symbol key — fall back to
                    # the cache key so it is never silently dropped.
                    rec = dict(rec)
                    rec["symbol"] = key
                records.append(rec)
    elif isinstance(data, list):
        records = data
    else:
        return []
    return [
        p for p in records
        if isinstance(p, dict) and p.get("entry_price") is not None
    ]


def count_live_open_positions() -> int:
    """Number of bot-managed live positions (see ``bot_managed_live_positions``)."""
    return len(bot_managed_live_positions())


def save_live_positions(positions_by_symbol: dict[str, Any]) -> None:
    try:
        from scripts.paper_state_lock import atomic_write_json as _awj
        _awj(LIVE_POSITIONS_PATH, positions_by_symbol, indent=2)
    except Exception:
        pass  # best-effort cache write; the exchange remains the truth


def merge_live_positions(
    new_positions: list[dict[str, Any]], synced_symbols: list[str],
) -> dict[str, Any]:
    """Merge freshly-synced positions into the on-disk cache.

    ``synced_symbols`` is every symbol that was just CHECKED (even ones
    that came back with zero/dust balance) — any of those missing from
    ``new_positions`` is removed from the cache (position closed / fully
    sold), instead of lingering there stale forever.
    """
    current = load_live_positions()
    # Snapshot management levels BEFORE the stale entries are popped so a
    # freshly-synced record can carry them over (a sync only knows
    # price/quantity, not adopted stop/TP levels or a previously known
    # entry price). The sync's own reconstruction can legitimately return
    # ``None`` for the entry (e.g. Indodax has no fetchMyTrades) — the
    # cached entry must never be overwritten with ``None`` on those
    # exchanges, or PnL and stop/target sizing lose their baseline.
    extras = {
        sym: {
            k: rec.get(k) for k in ("entry_price", "stop_loss", "tp1", "tp2", "tp3")
            if rec.get(k) is not None
        }
        for sym, rec in current.items() if isinstance(rec, dict)
    }
    for sym in synced_symbols:
        current.pop(sym, None)
    for pos in new_positions:
        for key, val in extras.get(pos["symbol"], {}).items():
            if val is not None and pos.get(key) is None:
                pos[key] = val
        # Generic fallback: when the previous cache has no SL/TP for this
        # symbol (e.g. position stamped before snapshotting existed), the
        # write-once entry snapshot store may still hold the plan levels.
        # Never symbol-specific — heals any symbol uniformly.
        if not extras.get(pos["symbol"]):
            for key, val in snapshot_levels_for_symbol(pos["symbol"]).items():
                if val > 0 and pos.get(key) is None:
                    pos[key] = val
        current[pos["symbol"]] = pos
    save_live_positions(current)
    return current
