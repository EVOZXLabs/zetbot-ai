"""Batch 1 production-hardening regression tests.

Covers:
  1.1  Balance protection — ExecutionPipeline.execute_plan rejects BUY when
       estimated cost > available balance (pipeline-level pre-flight).
  1.2  Global per-symbol trading lock — concurrent BUY is rejected when the
       symbol lock is held by an ongoing SELL.
  1.3  MAX_OPEN_POSITIONS enforced by SafeGuard.can_open_new_position() reading
       from .env MAX_POSITIONS and positions.json.
  1.4  Atomic writes — PaperBalance.save() and PaperExecutionProvider._save_positions()
       use atomic_write_json (temp-file + os.replace), never plain open(w).
  1.5  Exchange API retry — exchange_call_with_retry retries on transient errors
       with exponential backoff and does NOT retry on permanent errors.
  1.6  .gitignore — .env is excluded; .env.example contains no real credentials.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
#  Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sandbox(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    os.makedirs("data", exist_ok=True)


def _write_json(path: str, data: object) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _write_positions(positions: list[dict]) -> None:
    _write_json("data/positions.json", {"positions": positions})


# ===========================================================================
#  1.1 — Balance protection (pipeline pre-flight)
# ===========================================================================


class TestBalancePreFlight:
    """ExecutionPipeline.execute_plan must reject BUY when balance is too low."""

    def _make_pipeline(self, balance: float = 100.0):
        from scripts.execution_pipeline import ExecutionPipeline
        provider = MagicMock()
        provider.get_balance.return_value = balance
        filled = MagicMock()
        filled.status = "FILLED"
        provider.execute_buy.return_value = filled
        return ExecutionPipeline(provider, quote_currency="USDT"), provider

    def _plan(self, qty: float = 1.0, price: float = 50.0) -> dict:
        return {
            "symbol": "BTC/USDT",
            "entry_price": price,
            "quantity": qty,
            "position_size_usdt": qty * price,
            "stop_loss": price * 0.95,
            "tp1": price * 1.05,
        }

    def test_rejects_when_cost_exceeds_balance(self) -> None:
        pipeline, provider = self._make_pipeline(balance=40.0)
        result = pipeline.execute_plan(self._plan(qty=1.0, price=50.0))
        assert result is not None
        assert result.status == "REJECTED"
        assert "Insufficient balance" in (result.error or "")
        provider.execute_buy.assert_not_called()

    def test_allows_when_balance_sufficient(self) -> None:
        pipeline, provider = self._make_pipeline(balance=10_000.0)
        result = pipeline.execute_plan(self._plan(qty=1.0, price=50.0))
        assert result.status == "FILLED"
        provider.execute_buy.assert_called_once()

    def test_allows_when_balance_none(self) -> None:
        """If provider can't return balance (None), pass through to provider."""
        pipeline, provider = self._make_pipeline()
        provider.get_balance.return_value = None
        result = pipeline.execute_plan(self._plan(qty=1.0, price=50.0))
        assert result.status == "FILLED"

    def test_rejection_message_includes_quote_currency(self) -> None:
        pipeline, _ = self._make_pipeline(balance=30.0)
        result = pipeline.execute_plan(self._plan(qty=1.0, price=50.0))
        assert result.status == "REJECTED"
        assert "USDT" in (result.error or "")

    def test_zero_balance_always_rejected(self) -> None:
        pipeline, provider = self._make_pipeline(balance=0.0)
        result = pipeline.execute_plan(self._plan(qty=0.001, price=50.0))
        assert result.status == "REJECTED"
        provider.execute_buy.assert_not_called()

    def test_exact_balance_allowed(self) -> None:
        """Balance exactly equal to cost (accounting for fee buffer) is allowed."""
        pipeline, provider = self._make_pipeline(balance=50.10)
        result = pipeline.execute_plan(self._plan(qty=1.0, price=50.0))
        # 50.0 * 1.0015 = 50.075 < 50.10 → allowed
        assert result.status == "FILLED"


# ===========================================================================
#  1.2 — Per-symbol trading lock
# ===========================================================================


class TestPerSymbolTradingLock:
    """BUY is rejected when the symbol's lock is held by an ongoing SELL."""

    def _make_pipeline(self, balance: float = 10_000.0):
        from scripts.execution_pipeline import ExecutionPipeline
        provider = MagicMock()
        provider.get_balance.return_value = balance
        filled = MagicMock()
        filled.status = "FILLED"
        provider.execute_buy.return_value = filled
        return ExecutionPipeline(provider, quote_currency="USDT"), provider

    def _plan(self, symbol: str = "ETH/USDT", price: float = 100.0) -> dict:
        return {
            "symbol": symbol,
            "entry_price": price,
            "quantity": 1.0,
            "position_size_usdt": price,
            "stop_loss": price * 0.95,
            "tp1": price * 1.05,
        }

    def test_buy_rejected_when_symbol_lock_held(self) -> None:
        from scripts.exit_gate import lock_for
        import threading

        pipeline, provider = self._make_pipeline()
        sym = "ETH/USDT_LOCK_TEST"

        sym_lock = lock_for(sym)
        result_holder: list = []

        def _hold_lock_and_buy() -> None:
            # Hold the lock from another thread so the RLock is NOT reentrant
            # for the thread that calls execute_plan (which runs in this thread).
            sym_lock.acquire()
            ready.set()       # signal: lock is now held
            proceed.wait()    # wait for the BUY to be attempted
            sym_lock.release()

        ready = threading.Event()
        proceed = threading.Event()

        holder = threading.Thread(target=_hold_lock_and_buy, daemon=True)
        holder.start()
        ready.wait(timeout=2.0)   # wait until the other thread holds the lock

        try:
            result = pipeline.execute_plan(self._plan(sym))
            result_holder.append(result)
        finally:
            proceed.set()
            holder.join(timeout=2.0)

        assert result_holder, "execute_plan must return a result"
        result = result_holder[0]
        assert result is not None
        assert result.status == "REJECTED"
        assert "locked" in (result.error or "").lower()
        provider.execute_buy.assert_not_called()

    def test_buy_proceeds_when_lock_free(self) -> None:
        pipeline, provider = self._make_pipeline()
        result = pipeline.execute_plan(self._plan("SOL/USDT"))
        assert result.status == "FILLED"
        provider.execute_buy.assert_called_once()

    def test_lock_released_after_successful_buy(self) -> None:
        from scripts.exit_gate import lock_for

        pipeline, _ = self._make_pipeline()
        sym = "BNB/USDT"
        pipeline.execute_plan(self._plan(sym))

        sym_lock = lock_for(sym)
        acquired = sym_lock.acquire(blocking=False)
        assert acquired, "Lock must be released after execute_plan returns"
        sym_lock.release()

    def test_lock_released_even_if_provider_raises(self) -> None:
        from scripts.exit_gate import lock_for
        from scripts.execution_pipeline import ExecutionPipeline

        provider = MagicMock()
        provider.get_balance.return_value = 99_999.0
        provider.execute_buy.side_effect = RuntimeError("provider boom")

        pipeline = ExecutionPipeline(provider, quote_currency="USDT")
        sym = "BOOM/USDT"

        with pytest.raises(RuntimeError, match="provider boom"):
            pipeline.execute_plan(self._plan(sym, price=10.0))

        sym_lock = lock_for(sym)
        acquired = sym_lock.acquire(blocking=False)
        assert acquired, "Lock must be released even after provider exception"
        sym_lock.release()


# ===========================================================================
#  1.3 — MAX_OPEN_POSITIONS enforced by SafeGuard
# ===========================================================================


class TestSafeGuardMaxOpenPositions:

    def test_blocks_when_at_limit(self) -> None:
        from scripts.safety_limits import SafeGuard

        _write_positions([{"symbol": "BTC/USDT", "status": "OPEN"}])
        sg = SafeGuard(max_open_positions=1)
        sg.set_positions_path("data/positions.json")
        ok, reason = sg.can_open_new_position()
        assert not ok
        assert "Max open positions" in reason
        assert "1/1" in reason

    def test_allows_when_below_limit(self) -> None:
        from scripts.safety_limits import SafeGuard

        _write_positions([])
        sg = SafeGuard(max_open_positions=2)
        sg.set_positions_path("data/positions.json")
        ok, _ = sg.can_open_new_position()
        assert ok

    def test_counts_only_open_statuses(self) -> None:
        from scripts.safety_limits import SafeGuard

        _write_positions([
            {"symbol": "BTC/USDT", "status": "CLOSED"},
            {"symbol": "ETH/USDT", "status": "STOPPED"},
        ])
        sg = SafeGuard(max_open_positions=1)
        sg.set_positions_path("data/positions.json")
        ok, _ = sg.can_open_new_position()
        assert ok

    def test_reads_limit_from_env(self, monkeypatch: Any) -> None:
        from scripts.safety_limits import SafeGuard

        monkeypatch.setenv("MAX_POSITIONS", "3")
        _write_positions([
            {"symbol": "A/USDT", "status": "OPEN"},
            {"symbol": "B/USDT", "status": "OPEN"},
        ])
        sg = SafeGuard()
        sg.set_positions_path("data/positions.json")
        ok, _ = sg.can_open_new_position()
        assert ok  # 2 < 3

    def test_blocks_via_env_at_limit(self, monkeypatch: Any) -> None:
        from scripts.safety_limits import SafeGuard

        monkeypatch.setenv("MAX_POSITIONS", "2")
        _write_positions([
            {"symbol": "A/USDT", "status": "OPEN"},
            {"symbol": "B/USDT", "status": "OPEN"},
        ])
        sg = SafeGuard()
        sg.set_positions_path("data/positions.json")
        ok, reason = sg.can_open_new_position()
        assert not ok
        assert "2/2" in reason

    def test_reason_mentions_env_var(self) -> None:
        from scripts.safety_limits import SafeGuard

        _write_positions([{"symbol": "BTC/USDT", "status": "OPEN"}])
        sg = SafeGuard(max_open_positions=1)
        sg.set_positions_path("data/positions.json")
        _, reason = sg.can_open_new_position()
        assert "MAX_POSITIONS" in reason

    def test_graceful_when_positions_file_missing(self) -> None:
        from scripts.safety_limits import SafeGuard

        sg = SafeGuard(max_open_positions=1)
        sg.set_positions_path("data/positions_nonexistent.json")
        ok, _ = sg.can_open_new_position()
        assert ok  # 0 open < 1 limit

    def test_partial_statuses_count_as_open(self) -> None:
        from scripts.safety_limits import SafeGuard

        _write_positions([{"symbol": "BTC/USDT", "status": "PARTIAL"}])
        sg = SafeGuard(max_open_positions=1)
        sg.set_positions_path("data/positions.json")
        ok, _ = sg.can_open_new_position()
        assert not ok


# ===========================================================================
#  1.4 — Atomic writes in execution_provider
# ===========================================================================


class TestAtomicWritesExecutionProvider:

    def test_paper_balance_save_calls_atomic_write(self) -> None:
        from scripts.execution_provider import PaperBalance
        from scripts import paper_state_lock

        written: list[str] = []
        orig = paper_state_lock.atomic_write_json

        def _spy(path: str, *a: Any, **kw: Any) -> None:
            written.append(path)
            return orig(path, *a, **kw)

        with patch.object(paper_state_lock, "atomic_write_json", side_effect=_spy):
            bal = PaperBalance()
            bal.balance = 9_500.0
            bal.save()

        assert any("paper_balance" in p for p in written), (
            f"paper_balance.json not written atomically; paths={written}"
        )

    def test_paper_balance_save_no_direct_open_write(self) -> None:
        """PaperBalance.save() must not use open(..., 'w') for paper_balance."""
        import builtins
        from scripts.execution_provider import PaperBalance

        orig = builtins.open
        plain_writes: list[str] = []

        def _watch(file: Any, mode: str = "r", *a: Any, **kw: Any):
            if "w" in str(mode) and "paper_balance" in str(file):
                plain_writes.append(str(file))
            return orig(file, mode, *a, **kw)

        with patch("builtins.open", side_effect=_watch):
            bal = PaperBalance()
            bal.balance = 9_500.0
            bal.save()

        assert not plain_writes, (
            f"PaperBalance.save() still uses plain open(w): {plain_writes}"
        )

    def test_save_positions_calls_atomic_write(self) -> None:
        from scripts.execution_provider import PaperExecutionProvider
        from scripts import paper_state_lock

        written: list[str] = []
        orig = paper_state_lock.atomic_write_json

        def _spy(path: str, *a: Any, **kw: Any) -> None:
            written.append(path)
            return orig(path, *a, **kw)

        with patch.object(paper_state_lock, "atomic_write_json", side_effect=_spy):
            provider = PaperExecutionProvider()
            provider._save_positions()

        assert any("paper_state" in p for p in written), (
            f"paper_state.json not written atomically; paths={written}"
        )

    def test_save_positions_no_direct_open_write(self) -> None:
        """_save_positions must not use open(..., 'w') for paper_state."""
        import builtins
        from scripts.execution_provider import PaperExecutionProvider

        orig = builtins.open
        plain_writes: list[str] = []

        def _watch(file: Any, mode: str = "r", *a: Any, **kw: Any):
            if "w" in str(mode) and "paper_state" in str(file):
                plain_writes.append(str(file))
            return orig(file, mode, *a, **kw)

        with patch("builtins.open", side_effect=_watch):
            provider = PaperExecutionProvider()
            provider._save_positions()

        assert not plain_writes, (
            f"_save_positions still uses plain open(w): {plain_writes}"
        )


# ===========================================================================
#  1.5 — Exchange API retry with backoff
# ===========================================================================


class TestExchangeRetry:

    def test_retries_on_network_error(self) -> None:
        import ccxt
        from scripts.exchange_providers import exchange_call_with_retry

        calls: list[int] = []

        def _fn() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise ccxt.NetworkError("connection reset")
            return "ok"

        with patch("scripts.exchange_providers.time.sleep"):
            result = exchange_call_with_retry(_fn, retries=3)
        assert result == "ok"
        assert len(calls) == 3

    def test_retries_on_request_timeout(self) -> None:
        import ccxt
        from scripts.exchange_providers import exchange_call_with_retry

        calls: list[int] = []

        def _fn() -> str:
            calls.append(1)
            if len(calls) < 2:
                raise ccxt.RequestTimeout("timed out")
            return "done"

        with patch("scripts.exchange_providers.time.sleep"):
            result = exchange_call_with_retry(_fn, retries=3)
        assert result == "done"
        assert len(calls) == 2

    def test_does_not_retry_auth_error(self) -> None:
        import ccxt
        from scripts.exchange_providers import exchange_call_with_retry

        calls: list[int] = []

        def _fn() -> str:
            calls.append(1)
            raise ccxt.AuthenticationError("bad key")

        with patch("scripts.exchange_providers.time.sleep"):
            with pytest.raises(ccxt.AuthenticationError):
                exchange_call_with_retry(_fn, retries=3)
        assert len(calls) == 1, "AuthenticationError must not be retried"

    def test_raises_after_all_retries_exhausted(self) -> None:
        import ccxt
        from scripts.exchange_providers import exchange_call_with_retry

        with patch("scripts.exchange_providers.time.sleep"):
            with pytest.raises(ccxt.NetworkError):
                exchange_call_with_retry(
                    lambda: (_ for _ in ()).throw(ccxt.NetworkError("fail")),
                    retries=3,
                )

    def test_record_failure_called_once_per_failed_attempt(self) -> None:
        import ccxt
        from scripts.exchange_providers import exchange_call_with_retry

        failures: list[int] = []

        with patch("scripts.exchange_providers.time.sleep"):
            with pytest.raises(ccxt.NetworkError):
                exchange_call_with_retry(
                    lambda: (_ for _ in ()).throw(ccxt.NetworkError("fail")),
                    retries=3,
                    record_failure=lambda: failures.append(1),
                )
        # Exactly one callback per executed failed attempt — never a
        # redundant extra call on the final attempt (BUG-1 regression).
        assert len(failures) == 3, "record_failure() count must equal retries"

    def test_record_failure_count_equals_failed_attempts_on_success(self) -> None:
        """When fn() eventually succeeds, record_failure() must be called
        exactly once for each failed attempt (not retries, not retries+1)."""
        import ccxt
        from scripts.exchange_providers import exchange_call_with_retry

        calls: list[int] = []
        failures: list[int] = []

        def _fn() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise ccxt.NetworkError("connection reset")
            return "ok"

        with patch("scripts.exchange_providers.time.sleep"):
            result = exchange_call_with_retry(
                _fn, retries=5,
                record_failure=lambda: failures.append(1),
            )
        assert result == "ok"
        assert len(calls) == 3, "3 attempts executed"
        assert len(failures) == 2, (
            "record_failure() must count failed attempts only — "
            f"got {len(failures)}"
        )

    def test_success_on_first_try_no_sleep(self) -> None:
        from scripts.exchange_providers import exchange_call_with_retry

        with patch("scripts.exchange_providers.time.sleep") as mock_sleep:
            result = exchange_call_with_retry(lambda: 42, retries=3)
        assert result == 42
        mock_sleep.assert_not_called()

    def test_backoff_delay_increases(self) -> None:
        import ccxt
        from scripts.exchange_providers import exchange_call_with_retry

        sleep_times: list[float] = []

        with patch("scripts.exchange_providers.time.sleep",
                   side_effect=lambda d: sleep_times.append(d)):
            with patch("scripts.exchange_providers.random.uniform", return_value=0.0):
                with pytest.raises(ccxt.NetworkError):
                    exchange_call_with_retry(
                        lambda: (_ for _ in ()).throw(ccxt.NetworkError("fail")),
                        retries=3,
                    )

        assert len(sleep_times) == 2
        assert sleep_times[1] > sleep_times[0], "Backoff must increase between retries"

    def test_base_provider_get_ticker_uses_retry(self) -> None:
        """BaseProvider.get_ticker() must use the retry wrapper."""
        import ccxt
        from scripts.exchange_providers import BaseProvider

        class _TestProvider(BaseProvider):
            CCXT_NAME = "binance"

        provider = _TestProvider()
        call_count = [0]

        mock_ex = MagicMock()
        def _fetch_ticker(sym: str) -> dict:
            call_count[0] += 1
            if call_count[0] < 3:
                raise ccxt.NetworkError("transient")
            return {"last": 50_000.0}

        mock_ex.fetch_ticker.side_effect = _fetch_ticker
        provider._instance = mock_ex

        with patch("scripts.exchange_providers.time.sleep"):
            result = provider.get_ticker("BTC/USDT")

        assert result.get("last") == 50_000.0
        assert call_count[0] == 3


# ===========================================================================
#  1.6 — Credential safety
# ===========================================================================


class TestCredentialSafety:

    def test_gitignore_excludes_env_file(self) -> None:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        gitignore = os.path.join(root, ".gitignore")
        assert os.path.exists(gitignore), ".gitignore must exist"
        with open(gitignore) as f:
            content = f.read()
        assert ".env" in content, ".gitignore must exclude .env"

    def test_env_example_has_no_real_credentials(self) -> None:
        import re
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        env_example = os.path.join(root, ".env.example")
        if not os.path.exists(env_example):
            pytest.skip(".env.example not found")
        with open(env_example) as f:
            lines = f.readlines()
        # Check line-by-line so there's no cross-line matching ambiguity.
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for key in ("API_KEY", "API_SECRET", "TELEGRAM_TOKEN"):
                if stripped.startswith(f"{key}="):
                    value = stripped[len(f"{key}="):].strip()
                    assert not value, (
                        f".env.example has a real value for {key}: {value!r}"
                    )

    def test_env_not_tracked_by_git(self) -> None:
        import subprocess
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        result = subprocess.run(
            ["git", "ls-files", ".env"],
            capture_output=True, text=True, cwd=root,
        )
        assert result.stdout.strip() == "", (
            ".env is tracked by git — credentials may be committed!"
        )

    def test_readme_mentions_withdrawal_permission(self) -> None:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        readme = os.path.join(root, "README.md")
        with open(readme) as f:
            content = f.read()
        assert "Withdrawal" in content and "Never" in content, (
            "README must document that Withdrawal permission must be disabled"
        )
