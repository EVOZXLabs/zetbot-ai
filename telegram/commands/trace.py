from telegram.base_command import BaseCommand, CommandMeta
from telegram.ui import header, SEPARATOR, build_message
from scripts.decision_trace import DecisionTrace


_DECISION_LABELS = {
    "ACCEPTED": "✅ ACCEPTED",
    "REJECTED": "❌ REJECTED",
    "WAIT":     "⏳ WAIT",
    "SKIPPED":  "⏭️ SKIPPED",
}


class TraceCommand(BaseCommand):
    meta = CommandMeta(
        name="trace",
        aliases=["dt", "decision-trace"],
        description="Show decision trace for the last pipeline run",
        usage="/trace",
        permission="user",
    )

    def execute(self, ctx, args: str) -> str:
        trace = DecisionTrace.load()
        if not trace.entries:
            return build_message(
                header(),
                f"📋 *DECISION TRACE*\n{SEPARATOR}",
                "No trace data. Run the pipeline first.",
            )

        lines: list[str] = []
        for i, entry in enumerate(trace.entries):
            label = _DECISION_LABELS.get(entry.decision, entry.decision)
            icon = "📡" if entry.stage == "Scanner" else \
                   "🧠" if entry.stage == "Decision" else \
                   "⚠️" if entry.stage == "Risk" else \
                   "💼" if entry.stage == "Trade" else \
                   "📌" if entry.stage == "Position" else \
                   "💰"
            lines.append(
                f"{icon} *{entry.stage}*\n"
                f"  {label}\n"
                f"  _{entry.reason}_"
            )

            if entry.scores:
                parts = []
                for k, v in entry.scores.items():
                    if isinstance(v, float):
                        parts.append(f"{k}={v:+.2f}" if abs(v) < 1000 else f"{k}={v:.2f}")
                    else:
                        parts.append(f"{k}={v}")
                if parts:
                    lines.append(f"  `{'  '.join(parts)}`")

            if i < len(trace.entries) - 1:
                lines.append("  ↓")

        return build_message(
            header(),
            f"📋 *DECISION TRACE*\n{SEPARATOR}",
            f"Top Candidate: `{trace.top_candidate}`\n"
            f"Run: {trace.timestamp or 'N/A'}",
            *lines,
        )
