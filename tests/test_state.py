"""
Unit tests for MarketStateDetector and StateManager.

Covers: input validation, trending markets, sideways markets,
volatility scenarios, edge cases, MarketData integration,
persistent state save/load, corruption recovery, engine integration.
"""

import json
import math
import os
from datetime import datetime, timezone

import pandas as pd
import pytest

from bot.state import (
    STATE_VERSION,
    MarketStateDetector,
    SIDEWAYS,
    StateManager,
    TRENDING,
)


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _trending_df(n: int = 80) -> pd.DataFrame:
    """Strong uptrend → ADX should be well above 25."""
    highs = [100.0 + i * 2.0 for i in range(n)]
    lows  = [90.0 + i * 1.8 for i in range(n)]
    close = [95.0 + i * 2.0 for i in range(n)]
    return pd.DataFrame({"high": highs, "low": lows, "close": close})


def _sideways_df(n: int = 80) -> pd.DataFrame:
    """Tight range, no direction → ADX near 0."""
    base = 100.0
    return pd.DataFrame({
        "high": [base + 1.0] * n,
        "low":  [base - 1.0] * n,
        "close":[base] * n,
    })


def _compressed_volatility_df(n: int = 80) -> pd.DataFrame:
    """Wide then narrow range → volatility compression."""
    highs = []
    lows = []
    close = []
    for i in range(n):
        if i < 40:
            highs.append(120.0 + i * 0.5)
            lows.append(80.0 - i * 0.5)
            close.append(100.0)
        else:
            highs.append(101.0)
            lows.append(99.0)
            close.append(100.0)
    return pd.DataFrame({"high": highs, "low": lows, "close": close})


# ---------------------------------------------------------------------------
#  Input validation
# ---------------------------------------------------------------------------

class TestStateValidation:

    def test_empty_df_raises(self) -> None:
        detector = MarketStateDetector()
        with pytest.raises(ValueError, match="empty"):
            detector.detect(pd.DataFrame())

    def test_missing_high_raises(self) -> None:
        df = pd.DataFrame({"close": [1.0], "low": [1.0]})
        detector = MarketStateDetector()
        with pytest.raises(ValueError, match="high"):
            detector.detect(df)

    def test_missing_low_raises(self) -> None:
        df = pd.DataFrame({"close": [1.0], "high": [1.0]})
        detector = MarketStateDetector()
        with pytest.raises(ValueError, match="low"):
            detector.detect(df)

    def test_missing_close_raises(self) -> None:
        df = pd.DataFrame({"high": [1.0], "low": [1.0]})
        detector = MarketStateDetector()
        with pytest.raises(ValueError, match="close"):
            detector.detect(df)


# ---------------------------------------------------------------------------
#  Trending market detection
# ---------------------------------------------------------------------------

class TestTrendingDetection:

    def test_strong_uptrend_returns_trending(self) -> None:
        df = _trending_df(80)
        detector = MarketStateDetector(adx_threshold=25)
        result = detector.detect(df)
        assert result == TRENDING, f"Expected TRENDING, got {result}"

    def test_strong_downtrend_returns_trending(self) -> None:
        n = 80
        highs = [200.0 - i * 2.0 for i in range(n)]
        lows  = [190.0 - i * 2.0 for i in range(n)]
        close = [195.0 - i * 2.0 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        detector = MarketStateDetector(adx_threshold=25)
        result = detector.detect(df)
        assert result == TRENDING, f"Expected TRENDING, got {result}"

    def test_low_adx_threshold_catches_weak_trend(self) -> None:
        """With a low threshold, even weak trends are TRENDING."""
        n = 80
        highs = [100.0 + i * 0.5 for i in range(n)]
        lows  = [90.0 + i * 0.4 for i in range(n)]
        close = [95.0 + i * 0.5 for i in range(n)]
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        detector = MarketStateDetector(adx_threshold=10)
        result = detector.detect(df)
        assert result == TRENDING

    def test_high_adx_threshold_rejects_random_walk(self) -> None:
        """With a high threshold, random walk is SIDEWAYS."""
        import random
        random.seed(42)
        n = 80
        base = 100.0
        highs = []
        lows = []
        close = [base]
        for i in range(1, n):
            prev = close[-1]
            change = random.uniform(-1.0, 1.0)
            cur = prev + change
            close.append(cur)
            highs.append(max(prev, cur) + random.uniform(0, 0.5))
            lows.append(min(prev, cur) - random.uniform(0, 0.5))
        highs.insert(0, base + 1.0)
        lows.insert(0, base - 1.0)
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        detector = MarketStateDetector(adx_threshold=50)
        result = detector.detect(df)
        assert result == SIDEWAYS


# ---------------------------------------------------------------------------
#  Sideways market detection
# ---------------------------------------------------------------------------

class TestSidewaysDetection:

    def test_flat_market_returns_sideways(self) -> None:
        df = _sideways_df(80)
        detector = MarketStateDetector()
        result = detector.detect(df)
        assert result == SIDEWAYS

    def test_compressed_volatility_returns_sideways(self) -> None:
        df = _compressed_volatility_df(80)
        detector = MarketStateDetector(atr_multiplier=0.5)
        result = detector.detect(df)
        assert result == SIDEWAYS

    def test_price_compression_returns_sideways(self) -> None:
        """Narrow recent range relative to long range."""
        n = 80
        highs = []
        lows = []
        close = []
        for i in range(n):
            if i < 60:
                highs.append(120.0)
                lows.append(80.0)
                close.append(100.0)
            else:
                highs.append(101.0)
                lows.append(99.0)
                close.append(100.0)
        df = pd.DataFrame({"high": highs, "low": lows, "close": close})
        detector = MarketStateDetector(
            adx_threshold=50,
            compression_lookback=20,
            compression_ratio=0.3,
        )
        result = detector.detect(df)
        assert result == SIDEWAYS

    def test_small_dataset_falls_back_to_sideways(self) -> None:
        """Very small dataset (but valid OHLC) → SIDEWAYS."""
        n = 30
        base = 100.0
        df = pd.DataFrame({
            "high": [base + 0.5] * n,
            "low":  [base - 0.5] * n,
            "close":[base] * n,
        })
        detector = MarketStateDetector()
        result = detector.detect(df)
        assert result == SIDEWAYS


# ---------------------------------------------------------------------------
#  MarketData integration
# ---------------------------------------------------------------------------

@pytest.mark.network
class TestMarketDataStateIntegration:
    """Verify market_state() works after fetch_ohlcv."""

    def test_market_state_after_fetch(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=100)
        result = md.market_state(df)
        assert result in (TRENDING, SIDEWAYS)

    def test_market_state_is_string(self) -> None:
        from bot.data import MarketData
        md = MarketData(exchange_name="binance")
        df = md.fetch_ohlcv(symbol="BTC/USDT", timeframe="1h", limit=100)
        result = md.market_state(df)
        assert isinstance(result, str)
        assert result in (TRENDING, SIDEWAYS)


# ---------------------------------------------------------------------------
#  StateManager — persistent state
# ---------------------------------------------------------------------------

class TestStateManagerSaveLoad:
    """Save and load state to/from JSON."""

    def test_save_state(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        state = {"state_version": 1, "balance": 10_000.0, "trades": []}
        mgr.save(state)
        assert os.path.isfile(path)

    def test_load_state(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        original = {
            "state_version": 1,
            "balance": 8_500.0,
            "trades": [],
            "position": None,
        }
        mgr.save(original)
        loaded = mgr.load()
        assert loaded is not None
        assert loaded["balance"] == 8_500.0
        assert loaded["trades"] == []

    def test_load_returns_none_when_missing(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "nonexistent.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        assert mgr.load() is None

    def test_state_exists(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        assert not mgr.state_exists()
        mgr.save({"state_version": 1, "balance": 1000.0, "trades": []})
        assert mgr.state_exists()

    def test_clear_state(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        mgr.save({"state_version": 1, "balance": 1000.0, "trades": []})
        assert mgr.state_exists()
        mgr.clear()
        assert not mgr.state_exists()


class TestStateManagerVersion:
    """Version validation."""

    def test_missing_version_rejected(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        mgr.save({"state_version": 1, "balance": 1000.0, "trades": []})
        # Manually write a state with no version
        with open(path, "w") as f:
            json.dump({"balance": 1000.0, "trades": []}, f)
        result = mgr.load()
        assert result is None

    def test_version_is_int(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        with open(path, "w") as f:
            json.dump({"state_version": "one", "balance": 1000.0, "trades": []}, f)
        result = mgr.load()
        assert result is None


class TestStateManagerCorruption:
    """Corrupted file recovery."""

    def test_corrupted_json_returns_none(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        with open(path, "w") as f:
            f.write("not valid json")
        result = mgr.load()
        assert result is None

    def test_corrupted_file_backed_up(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=True)
        with open(path, "w") as f:
            f.write("{invalid")
        result = mgr.load()
        assert result is None
        # Backup should exist
        assert os.path.isfile(path + ".corrupted")

    def test_corrupted_not_dict_rejected(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        with open(path, "w") as f:
            json.dump([1, 2, 3], f)
        result = mgr.load()
        assert result is None

    def test_missing_balance_rejected(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        with open(path, "w") as f:
            json.dump({"state_version": 1}, f)
        result = mgr.load()
        assert result is None

    def test_missing_trades_rejected(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        with open(path, "w") as f:
            json.dump({"state_version": 1, "balance": 1000.0}, f)
        result = mgr.load()
        assert result is None


class TestStateManagerAtomicWrite:
    """Atomic write safety."""

    def test_partial_write_does_not_corrupt(self, tmp_path: pytest.TempPathFactory) -> None:
        path = str(tmp_path / "state.json")
        mgr = StateManager(state_path=path, backup_corrupted=False)
        mgr.save({"state_version": 1, "balance": 5_000.0, "trades": []})
        # Simulate a partial write by creating a tmp file manually
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write("partial")
        # os.replace is atomic, but if it fails, original should remain
        loaded = mgr.load()
        assert loaded is not None
        assert loaded["balance"] == 5_000.0


class TestStateManagerWithEngine:
    """Integration with PaperTradingEngine."""

    def test_save_after_run_once(self, tmp_path: pytest.TempPathFactory) -> None:
        from bot.paper_engine import PaperTradingEngine
        engine = PaperTradingEngine(initial_balance=10_000.0)
        path = str(tmp_path / "engine_state.json")
        engine._state_manager = StateManager(state_path=path, backup_corrupted=False)
        engine._auto_save = True

        # Run with a flat DataFrame (no signal → HOLD)
        n = 250
        df = pd.DataFrame({
            "high": [51_000.0] * n,
            "low":  [49_000.0] * n,
            "close":[50_000.0] * n,
        })
        engine.run_once(df=df)
        assert os.path.isfile(path)

    def test_restore_balance(self, tmp_path: pytest.TempPathFactory) -> None:
        from bot.paper_engine import PaperTradingEngine
        path = str(tmp_path / "restore.json")

        # Save state with modified balance
        mgr = StateManager(state_path=path, backup_corrupted=False)
        mgr.save({
            "state_version": 1,
            "balance": 7_500.0,
            "trades": [],
            "position": None,
            "paper": {
                "balance": 7_500.0,
                "position": None,
                "trades": [],
            },
        })
        # Load into engine
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine._state_manager = mgr
        restored = engine.restore_state()
        assert restored
        assert engine.current_balance() == 7_500.0

    def test_restore_position(self, tmp_path: pytest.TempPathFactory) -> None:
        from datetime import datetime
        from bot.paper_engine import PaperTradingEngine
        path = str(tmp_path / "position.json")

        # Create a saved position
        entry_time = datetime.now(timezone.utc)
        pos = {
            "entry_time": entry_time.isoformat(),
            "entry_price": 50_000.0,
            "quantity": 0.02,
            "balance_before": 10_000.0,
            "position_size_percent": 10.0,
            "stop_loss_price": 49_250.0,
            "take_profit_price": 51_250.0,
            "status": "OPEN",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
        }
        mgr = StateManager(state_path=path, backup_corrupted=False)
        mgr.save({
            "state_version": 1,
            "balance": 10_000.0,
            "position": pos,
            "trades": [],
            "paper": {
                "balance": 10_000.0,
                "position": pos,
                "trades": [],
            },
        })
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine._state_manager = mgr
        restored = engine.restore_state()
        assert restored
        pos_after = engine.current_position()
        assert pos_after is not None
        assert pos_after["entry_price"] == 50_000.0
        assert pos_after["stop_loss_price"] == 49_250.0
        assert pos_after["status"] == "OPEN"

    def test_restore_trade_history(self, tmp_path: pytest.TempPathFactory) -> None:
        from bot.paper_engine import PaperTradingEngine
        path = str(tmp_path / "trades.json")

        trades = [{
            "entry_price": 50_000.0,
            "exit_price": 51_000.0,
            "net_pnl": 20.0,
            "exit_reason": "Take Profit",
        }]
        mgr = StateManager(state_path=path, backup_corrupted=False)
        mgr.save({
            "state_version": 1,
            "balance": 10_020.0,
            "position": None,
            "trades": trades,
            "statistics": {"total_trades": 1, "total_profit": 20.0},
            "paper": {
                "balance": 10_020.0,
                "position": None,
                "trades": trades,
            },
        })
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine._state_manager = mgr
        restored = engine.restore_state()
        assert restored
        assert len(engine.trade_history()) == 1

    def test_restore_then_cycle_does_not_duplicate_buy(self, tmp_path: pytest.TempPathFactory) -> None:
        """When a position exists in saved state, the engine should NOT
        open another BUY — it should MONITOR the existing position."""
        from bot.paper_engine import PaperTradingEngine
        path = str(tmp_path / "nodupe.json")

        entry_time = datetime.now(timezone.utc)
        pos = {
            "entry_time": entry_time.isoformat(),
            "entry_price": 50_000.0,
            "quantity": 0.02,
            "balance_before": 10_000.0,
            "position_size_percent": 10.0,
            "stop_loss_price": 49_250.0,
            "take_profit_price": 51_250.0,
            "status": "OPEN",
            "symbol": "BTC/USDT",
            "timeframe": "1h",
        }
        mgr = StateManager(state_path=path, backup_corrupted=False)
        mgr.save({
            "state_version": 1,
            "balance": 10_000.0,
            "position": pos,
            "trades": [],
            "paper": {
                "balance": 10_000.0,
                "position": pos,
                "trades": [],
            },
        })
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine._state_manager = mgr
        engine.restore_state()
        assert engine.current_position() is not None

        # Run a cycle with flat data (stays within TP/SL range)
        n = 250
        df = pd.DataFrame({
            "high": [50_100.0] * n,
            "low":  [49_900.0] * n,
            "close":[50_000.0] * n,
        })

        result = engine.run_once(df=df)
        # Should not open a second position — trade should be None
        assert result["trade"] is None
        # The original position should still exist (not closed by this cycle)
        assert engine.current_position() is not None
        assert engine.current_position()["entry_price"] == 50_000.0

    def test_no_state_starts_fresh(self, tmp_path: pytest.TempPathFactory) -> None:
        from bot.paper_engine import PaperTradingEngine
        path = str(tmp_path / "nonexistent.json")
        engine = PaperTradingEngine(initial_balance=10_000.0)
        engine._state_manager = StateManager(state_path=path, backup_corrupted=False)
        restored = engine.restore_state()
        assert not restored
        assert engine.current_balance() == 10_000.0
        assert engine.current_position() is None
