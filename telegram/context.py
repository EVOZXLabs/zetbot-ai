from dataclasses import dataclass, field
from typing import Any, Optional
import time


@dataclass
class CommandContext:
    """Provides access to all bot components. Created per-request.

    When a ServiceContainer is available (via ``services``), all component
    properties delegate to it.  Otherwise, the old lazy-loading mechanism
    is used for backward compatibility.
    """

    config: Any = None
    logger: Any = None
    health_monitor: Any = None
    shutdown_event: Any = None
    pid_file: Any = None
    exchange: Any = None
    start_time: float = field(default_factory=time.time)
    chat_id: str = ""
    message_id: int = 0
    update_id: int = 0
    raw_text: str = ""
    is_admin: bool = False
    test_mode: bool = False

    # Service Container (DI)
    services: Any = None

    # Lazily-loaded components (backward compat, used when services is None)
    _scanner: Any = None
    _risk_manager: Any = None
    _position_manager: Any = None
    _paper_wallet: Any = None

    @property
    def scanner(self) -> Any:
        if self.services is not None:
            return self.services.scanner
        if self._scanner is None:
            from scripts import scanner  # noqa: PLC0415
            self._scanner = scanner
        return self._scanner

    @property
    def risk_manager(self) -> Any:
        if self.services is not None:
            return self.services.risk
        if self._risk_manager is None:
            from scripts import risk_manager  # noqa: PLC0415
            self._risk_manager = risk_manager
        return self._risk_manager

    @property
    def position_manager(self) -> Any:
        if self.services is not None:
            return self.services.position
        if self._position_manager is None:
            from scripts import position_manager  # noqa: PLC0415
            self._position_manager = position_manager
        return self._position_manager

    @property
    def paper_wallet(self) -> Any:
        if self.services is not None:
            return self.services.wallet
        if self._paper_wallet is None:
            from scripts import paper_trading_engine  # noqa: PLC0415
            self._paper_wallet = paper_trading_engine
        return self._paper_wallet

    def read_json(self, path: str) -> dict:
        import json  # noqa: PLC0415
        import os  # noqa: PLC0415
        try:
            with open(os.path.join("data", path)) as f:
                return dict(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return {}

    def runtime_formatted(self) -> str:
        rt = time.time() - self.start_time
        h, rem = divmod(int(rt), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
