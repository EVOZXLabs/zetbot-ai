"""Tests for ACCOUNT_BALANCE consistency between config and paper accounting.

Verifies that when ACCOUNT_BALANCE=180000 and paper state is empty,
the initial balance comes from config, not from a hardcoded 10000.0.
"""

import json
import os
import tempfile

import pytest


class TestInitialBalanceFromConfig:

    def test_module_level_constant_reads_env(self) -> None:
        """INITIAL_BALANCE should reflect ACCOUNT_BALANCE env var."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ACCOUNT_BALANCE", "180000")
            import importlib
            import scripts.paper_trading_engine as pte
            importlib.reload(pte)
            assert pte.INITIAL_BALANCE == 180000.0
            importlib.reload(pte)

    def test_module_level_constant_fallback(self) -> None:
        """INITIAL_BALANCE falls back to 10000 when env is not set."""
        with pytest.MonkeyPatch.context() as mp:
            mp.delenv("ACCOUNT_BALANCE", raising=False)
            import importlib
            import scripts.paper_trading_engine as pte
            importlib.reload(pte)
            assert pte.INITIAL_BALANCE == 10000.0
            importlib.reload(pte)

    def test_engine_uses_account_balance_from_env(self) -> None:
        """PaperTradingEngine wallet uses env ACCOUNT_BALANCE when no state file."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ACCOUNT_BALANCE", "180000")
            import importlib
            import scripts.paper_trading_engine as pte
            importlib.reload(pte)
            # Point state to a non-existent path so _load_state() returns
            # early. NOTE: must be set AFTER reload — reload() re-executes
            # the module body and would otherwise reset STATE_PATH to the
            # real data/paper_state.json, leaking live bot state into the
            # test.
            mp.setattr("scripts.paper_trading_engine.STATE_PATH", "/tmp/nonexistent_state.json")
            engine = pte.PaperTradingEngine()
            assert engine.wallet.initial == 180000.0
            assert engine.wallet.balance == 180000.0
            importlib.reload(pte)

    def test_engine_accepts_explicit_initial_balance(self) -> None:
        """Explicit initial_balance passed to PaperTradingEngine is used."""
        from scripts.paper_trading_engine import PaperTradingEngine

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("scripts.paper_trading_engine.STATE_PATH", "/tmp/nonexistent_state.json")
            engine = PaperTradingEngine(initial_balance=250000.0)
            assert engine.wallet.initial == 250000.0
            assert engine.wallet.balance == 250000.0

    def test_engine_passed_overrides_env(self) -> None:
        """Explicit initial_balance takes precedence over env."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ACCOUNT_BALANCE", "50000")
            import importlib
            import scripts.paper_trading_engine as pte
            importlib.reload(pte)
            mp.setattr("scripts.paper_trading_engine.STATE_PATH", "/tmp/nonexistent_state.json")
            engine = pte.PaperTradingEngine(initial_balance=99999.0)
            assert engine.wallet.initial == 99999.0
            importlib.reload(pte)

    def test_balance_json_writes_correct_initial(self) -> None:
        """PaperExport.balance_json writes the correct initial_balance."""
        from scripts.paper_trading_engine import PaperExport

        metrics = {
            "initial_balance": 180000.0,
            "final_balance": 180000.0,
            "final_equity": 180000.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_pct": 0.0,
            "total_return_pct": 0.0,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "paper_balance.json")
            PaperExport.balance_json(metrics, [], path)
            with open(path) as f:
                data = json.load(f)
            assert data["initial_balance"] == 180000.0
            assert data["final_balance"] == 180000.0

    def test_no_hardcoded_10000_fallback_in_balance_json(self) -> None:
        """balance_json fallback uses INITIAL_BALANCE (from env), not 10000."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ACCOUNT_BALANCE", "180000")
            import importlib
            import scripts.paper_trading_engine as pte
            importlib.reload(pte)

            metrics = {
                "final_balance": 180000.0,
                "final_equity": 180000.0,
            }
            with tempfile.TemporaryDirectory() as tmpdir:
                path = os.path.join(tmpdir, "paper_balance.json")
                pte.PaperExport.balance_json(metrics, [], path)
                with open(path) as f:
                    data = json.load(f)
                assert data.get("initial_balance", 0) != 10000.0
            importlib.reload(pte)

    def test_execution_provider_uses_account_balance(self) -> None:
        """PaperBalance in execution_provider reads env ACCOUNT_BALANCE."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ACCOUNT_BALANCE", "180000")
            import importlib
            from scripts import execution_provider
            importlib.reload(execution_provider)
            assert execution_provider.PAPER_INITIAL_BALANCE == 180000.0
            bal = execution_provider.PaperBalance()
            assert bal.initial == 180000.0
            assert bal.balance == 180000.0
            importlib.reload(execution_provider)

    def test_balance_resolver_uses_account_balance(self) -> None:
        """balance_resolver fallback reads env ACCOUNT_BALANCE."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ACCOUNT_BALANCE", "180000")
            import importlib
            from scripts import balance_resolver
            importlib.reload(balance_resolver)
            result = balance_resolver.resolve_initial_balance(
                paper_balance={},
                paper_state={},
            )
            assert result == 180000.0
            result = balance_resolver.resolve_initial_balance(
                paper_balance={"initial_balance": 50000.0},
                paper_state={},
            )
            assert result == 50000.0
            importlib.reload(balance_resolver)

    def test_paper_state_without_initial_uses_env(self) -> None:
        """When state file exists but has no initial_balance, fallback uses env."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ACCOUNT_BALANCE", "180000")
            import importlib
            import scripts.paper_trading_engine as pte
            importlib.reload(pte)

            with tempfile.TemporaryDirectory() as tmpdir:
                state_path = os.path.join(tmpdir, "paper_state.json")
                with open(state_path, "w") as f:
                    json.dump({"balance": 5000.0}, f)
                mp.setattr("scripts.paper_trading_engine.STATE_PATH", state_path)
                engine = pte.PaperTradingEngine(initial_balance=180000.0)
                assert engine.wallet.balance == 5000.0  # restored from state
                assert engine.wallet.initial == 180000.0  # from param, since state has no initial_balance
            importlib.reload(pte)