"""Regression tests: ``EXCLUDE_SYMBOLS`` must make the bot fully hands-off
a coin — no live position surfaced, no entry-price reconstruction, no
auto-manage, no auto-sell.

Root cause: spot exchanges (e.g. Indodax) don't distinguish a manual
trade from a bot trade — everything is just "wallet balance", and
``fetch_my_trades()`` returns the whole account's fill history
regardless of who placed the order. ``LivePositionSync.sync_all_positions``
is explicitly designed to surface a position for ANY non-dust balance
(see its docstring: "it will surface a position even if it came from a
manual trade"), so without an exclude mechanism the bot could reconstruct
an entry price for a manually-bought coin and start managing/selling it.

Covers:
  * ``parse_exclude_symbols`` normalizes comma-separated input (bare
    base symbols and full pairs, case-insensitive, whitespace-tolerant)
  * ``sync_positions`` skips excluded symbols entirely (no entry price,
    no ticker lookup, not present in results)
  * ``sync_all_positions`` never surfaces an excluded symbol even though
    it has a non-dust wallet balance
  * excluding a symbol with no exclude list configured is a no-op
    (everything behaves exactly as before)
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# scripts.exchange_providers imports ccxt at module load time; stub it so
# these tests still run where ccxt isn't installed. IMPORTANT: only stub
# when the real package is truly unavailable — installing a bare module
# while ccxt is importable poisons ``sys.modules`` for every test module
# imported later (ccxt gets shadowed by the empty stub, so exchange calls
# like ``getattr(ccxt, "indodax")`` return None and balance/price lookups
# fail with 0 balance).
try:
    import ccxt  # noqa: F401
except ImportError:  # pragma: no cover - depends on environment
    sys.modules["ccxt"] = types.ModuleType("ccxt")

from scripts.live_position_sync import (  # noqa: E402
    LivePositionSync,
    parse_exclude_symbols,
)


class _FakeProvider:
    name = "indodax"

    def __init__(self, balances: dict, trades: dict, markets: dict | None = None):
        self._balances = balances
        self._trades = trades
        self._markets = markets or {}

    def fetch_balance(self):
        return {"free": dict(self._balances), "total": dict(self._balances)}

    def fetch_my_trades(self, symbol, *, since=None, until=None):
        return self._trades.get(symbol, [])

    def _get_exchange(self):
        trades = self._trades

        class _Ex:
            def fetch_my_trades(self, symbol, limit=200):
                return trades.get(symbol, [])

        return _Ex()

    def load_markets(self):
        return self._markets


class _FakeExchangeManager:
    def __init__(self, provider: _FakeProvider, last_price: float = 3.0):
        self._provider = provider
        self._last_price = last_price

    def get_provider(self):
        return self._provider

    def get_ticker(self, symbol):
        return {"last": self._last_price}


class TestParseExcludeSymbols:
    def test_empty_and_none(self) -> None:
        assert parse_exclude_symbols("") == set()
        assert parse_exclude_symbols(None) == set()

    def test_basic_csv(self) -> None:
        assert parse_exclude_symbols("RFC,JELLYJELLY") == {"RFC", "JELLYJELLY"}

    def test_whitespace_case_and_pairs(self) -> None:
        assert parse_exclude_symbols(" rfc , VRA/IDR ,, PLPA ") == {
            "RFC", "VRA", "PLPA",
        }


class TestSyncPositionsRespectsExclude:
    def _make_syncer(self, exclude=None):
        provider = _FakeProvider(
            balances={"RFC": 100.0, "BTC": 0.01},
            trades={
                "RFC/IDR": [
                    {"side": "buy", "amount": 100.0, "price": 10.0, "timestamp": 1},
                ],
                "BTC/IDR": [
                    {"side": "buy", "amount": 0.01, "price": 900_000_000.0, "timestamp": 1},
                ],
            },
            markets={"RFC/IDR": {}, "BTC/IDR": {}},
        )
        exchange = _FakeExchangeManager(provider)
        return LivePositionSync(exchange, quote_currency="IDR", exclude_symbols=exclude)

    def test_no_exclude_surfaces_everything(self) -> None:
        syncer = self._make_syncer(exclude=None)
        results = syncer.sync_positions(["RFC/IDR", "BTC/IDR"])
        symbols = {p["symbol"] for p in results}
        assert symbols == {"RFC/IDR", "BTC/IDR"}
        rfc = next(p for p in results if p["symbol"] == "RFC/IDR")
        assert rfc["entry_price"] == 10.0

    def test_exclude_string_hides_symbol(self) -> None:
        syncer = self._make_syncer(exclude="RFC,JELLYJELLY")
        results = syncer.sync_positions(["RFC/IDR", "BTC/IDR"])
        symbols = {p["symbol"] for p in results}
        assert "RFC/IDR" not in symbols
        assert "BTC/IDR" in symbols

    def test_exclude_accepts_set_and_list(self) -> None:
        for exclude in ({"RFC"}, ["RFC"], ["rfc"]):
            syncer = self._make_syncer(exclude=exclude)
            results = syncer.sync_positions(["RFC/IDR", "BTC/IDR"])
            assert "RFC/IDR" not in {p["symbol"] for p in results}

    def test_sync_all_positions_skips_excluded_balance(self) -> None:
        syncer = self._make_syncer(exclude="RFC")
        results = syncer.sync_all_positions()
        symbols = {p["symbol"] for p in results}
        assert "RFC/IDR" not in symbols
        assert "BTC/IDR" in symbols
