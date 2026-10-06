"""Mock expression editor (V2): a pass-through that labels its output (§12.1 `post.expression`).

The output is the input file unchanged (same content hash), marked `passthrough: true`, so the
`post_expression` method stays honestly unexecuted until a real editor exists.
"""

from __future__ import annotations

import shutil

from ce_contracts.common import RunContext
from ce_contracts.interfaces import ExpressionEditor
from ce_contracts.models import ExpressionEditRequest, VideoResult

from ce_plugins_mock._base import MockAdapter
from ce_plugins_mock._media import probe_duration

__all__ = ["MockExpression"]


class MockExpression(MockAdapter, ExpressionEditor):
    async def run_expression_edit(self, request: ExpressionEditRequest, ctx: RunContext) -> VideoResult:
        source = await ctx.read_artifact(request.video)
        out = self.workdir(ctx, "edit") / "passthrough.mp4"
        shutil.copyfile(source, out)
        ref = await self.write(
            ctx,
            out,
            "video",
            role="video",
            mime="video/mp4",
            passthrough=True,
            operations_ignored=len(request.operations),
        )
        width, height = int(request.labels.get("width", 2)), int(request.labels.get("height", 2))
        return VideoResult(
            video=ref,
            duration_s=await probe_duration(out),
            fps=float(request.labels.get("fps", 25)),
            width=width,
            height=height,
        )
