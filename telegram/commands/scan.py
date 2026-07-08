from telegram.base_command import BaseCommand, CommandMeta


class ScanCommand(BaseCommand):
    meta = CommandMeta(
        name="scan",
        aliases=["scanner"],
        description="Run market scanner only",
        usage="/scan",
        permission="admin",
    )

    def execute(self, ctx, args: str) -> str:
        if ctx.services is not None:
            ctx.services.scanner.run()
        else:
            from scripts import scanner  # noqa: PLC0415
            scanner.main()

        data = ctx.read_json("scanner_results.json")
        total = data.get("total_pairs", 0)
        results_list = data.get("results", data.get("sorted", []))
        top_count = min(5, len(results_list) if isinstance(results_list, list) else 0)
        top_pairs = []
        if isinstance(results_list, list):
            for i, s in enumerate(results_list[:top_count], 1):
                sym = s.get("symbol", "?")
                score = s.get("overall_score", 0)
                top_pairs.append(f"  `{i}. {sym}` score={score:.1f}")

        lines = [
            f"\U0001f50d *Scan Complete*",
            f"Pairs scanned: `{total}`",
        ]
        if top_pairs:
            lines.append("")
            lines.append("Top pairs:")
            lines.extend(top_pairs)

        return "\n".join(lines)
