import time

from telegram.base_command import BaseCommand, CommandMeta
from scripts.position_status import is_open


class ScanCommand(BaseCommand):
    meta = CommandMeta(
        name="scan",
        aliases=["scanner"],
        description="Run market scanner only",
        usage="/scan",
        permission="admin",
    )

    def execute(self, ctx, args: str) -> str:
        t0 = time.time()

        if ctx.services is not None:
            ctx.services.scanner.run()
        else:
            from scripts import scanner  # noqa: PLC0415
            scanner.main(config=ctx.config)

        elapsed = time.time() - t0

        # Read results from canonical JSON files
        scan_data = ctx.read_json("scanner_results.json")
        risk_data = ctx.read_json("risk_results.json")
        plan_data = ctx.read_json("trade_plan.json")
        pos_data = ctx.read_json("positions.json")

        total_pairs = scan_data.get("total_pairs", 0)
        results_list = scan_data.get("results", scan_data.get("sorted", []))
        candidate_count = len(results_list) if isinstance(results_list, list) else 0

        approvals = []
        if isinstance(risk_data, list):
            approvals = [r for r in risk_data if r.get("approval") == "APPROVED"]
        elif isinstance(risk_data, dict):
            approvals = [
                r for r in risk_data.get("results", [])
                if r.get("approval") == "APPROVED"
            ]

        plans = []
        if isinstance(plan_data, list):
            plans = [p for p in plan_data if p.get("status") == "READY"]
        elif isinstance(plan_data, dict):
            plans = [
                p for p in plan_data.get("plans", [])
                if p.get("status") == "READY"
            ]

        pos_list = pos_data.get("positions", [])
        open_positions = [p for p in pos_list if is_open(p.get("status"))]

        # Top 5 candidates
        results_list_sorted = results_list
        top_n = results_list_sorted[:5] if isinstance(results_list_sorted, list) else []

        lines = [
            f"\U0001f50d *Scan Complete*",
            f"Elapsed: `{elapsed:.1f}s`",
            f"Candidates: `{candidate_count}` / `{total_pairs}` pairs",
            f"Approved: `{len(approvals)}`",
            f"Ready Plans: `{len(plans)}`",
            f"Open Positions: `{len(open_positions)}`",
        ]

        if top_n:
            lines.append("")
            lines.append("Top candidates:")
            for i, s in enumerate(top_n, 1):
                sym = s.get("symbol", "?")
                score = s.get("overall_score", 0.0)
                trend = s.get("trend_alignment", "?")
                lines.append(f"  `{i}. {sym}` score={score:.1f} trend={trend}")

        return "\n".join(lines)
