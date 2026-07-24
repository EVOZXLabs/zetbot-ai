from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass
class DecisionTraceEntry:
    stage: str
    symbol: str
    decision: str
    reason: str
    scores: dict[str, Any] = field(default_factory=dict)


@dataclass
class DecisionTrace:
    timestamp: str = ""
    top_candidate: str = ""
    entries: list[DecisionTraceEntry] = field(default_factory=list)

    def add(self, stage: str, symbol: str, decision: str, reason: str,
            scores: dict[str, Any] | None = None) -> None:
        self.entries.append(DecisionTraceEntry(
            stage=stage,
            symbol=symbol,
            decision=decision,
            reason=reason,
            scores=scores or {},
        ))

    def save(self, path: str = "data/decision_trace.json") -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump({
                "timestamp": self.timestamp,
                "top_candidate": self.top_candidate,
                "entries": [asdict(e) for e in self.entries],
            }, f, indent=2)

    @staticmethod
    def load(path: str = "data/decision_trace.json") -> DecisionTrace:
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return DecisionTrace()
        return DecisionTrace(
            timestamp=data.get("timestamp", ""),
            top_candidate=data.get("top_candidate", ""),
            entries=[DecisionTraceEntry(**e) for e in data.get("entries", [])],
        )
