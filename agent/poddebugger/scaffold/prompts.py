"""SRE-specific prompt helpers + re-exports of the framework helpers.

The cross-domain pieces (``PREAMBLE``, ``list_block``, ``sanity_block``,
``evidence_block``) live in :mod:`poddebugger.framework.prompts` and are
re-exported here for backward compatibility. ``context_block`` is
PodDebugger-specific — it renders a ``DiagnosticContext`` (Workload + events
+ spec + logs + deep-inspection probes) and stays in the app.
"""

from __future__ import annotations

import json

from ..framework.prompts import (  # noqa: F401 — re-exports
    PREAMBLE,
    evidence_block,
    list_block,
    sanity_block,
)
from ..models import DiagnosticContext

__all__ = ["PREAMBLE", "list_block", "sanity_block", "evidence_block", "context_block"]


def context_block(ctx: DiagnosticContext) -> str:
    """Render the raw workload snapshot — status / events / spec / logs.

    PodDebugger-specific (depends on the ``DiagnosticContext`` model).
    """
    w = ctx.workload
    parts = ["## Status\n" + "\n".join(w.summary_lines())]
    if ctx.events:
        parts.append("## Events\n" + "\n".join(e.line() for e in ctx.events[-30:]))
    parts.append("## Spec\n" + json.dumps(ctx.spec_excerpt, indent=2, default=str))
    parts.append("## Logs\n```\n" + (ctx.logs or "(no logs)") + "\n```")
    if ctx.deep_inspection:
        di = "\n".join(f"[{k}]\n{v}" for k, v in ctx.deep_inspection.items())
        parts.append("## Deep inspection\n" + di)
    return "\n\n".join(parts)
