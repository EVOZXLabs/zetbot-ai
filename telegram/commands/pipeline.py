from telegram.base_command import BaseCommand, CommandMeta


class PipelineCommand(BaseCommand):
    meta = CommandMeta(
        name="pipeline",
        aliases=["run"],
        description="Run the full analysis pipeline",
        usage="/pipeline",
        permission="admin",
    )

    def execute(self, ctx, args: str) -> str:
        # This runs synchronously — the caller (CommandCenter) will send
        # the result. The original sent an intermediate "running..." message.
        from scripts.logger import PipelineLogger  # noqa: PLC0415
        from scripts.pipeline import Pipeline  # noqa: PLC0415

        logger = PipelineLogger(ctx.config)
        pipeline = Pipeline(ctx.config, logger)
        results = pipeline.run()

        total = sum(r.duration for r in results)
        lines = ["\U0001f4ca *Pipeline Report*"]
        for r in results:
            icon = "\u2705" if r.success else "\u274c"
            detail = f"  {r.detail}" if r.detail else ""
            lines.append(f"{icon} `{r.name:>10s}` {r.duration:.1f}s{detail}")
        lines.append(f"Total: `{total:.1f}s`")

        failed = [r for r in results if not r.success]
        if failed:
            lines.append("")
            lines.append(f"\u26a0\ufe0f *{len(failed)} stage(s) failed*")
            for f in failed:
                lines.append(f"`{f.name}`: {f.error}")

        return "\n".join(lines)
