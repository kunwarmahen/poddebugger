"""Prompt construction for the analyzer.

The system prompt is static (good for prompt caching). The user prompt is the
rendered DiagnosticContext — the LLM only ever sees collected facts.
"""

from __future__ import annotations

import json

from .models import DiagnosticContext

SYSTEM_PROMPT = """\
You are PodDebugger, an expert site-reliability engineer that diagnoses failing
containers and pods (Podman, Kubernetes, OpenShift).

You are given a structured snapshot of one workload: its status, lifecycle
events, recent logs, and spec. Diagnose the most likely root cause.

Rules:
- Reason only from the evidence provided. Do not invent logs or events.
- If the evidence is insufficient for a confident call, say so and set
  needs_deep_inspection to true.
- A "Deep inspection" section, when present, holds live in-container state
  (processes, ports, disk, memory, limits). Treat it as authoritative and set
  needs_deep_inspection to false when it is present.
- Prefer concrete, actionable fixes over generic advice.
- Common patterns: OOMKilled / memory-limit issues, CrashLoopBackOff from a bad
  command or missing config, failed health checks, image pull errors,
  permission/UID errors, missing env vars or mounts, port conflicts.

Respond with ONLY a JSON object (no markdown, no prose) of this shape:
{
  "summary": "one-sentence plain-language summary",
  "root_cause": "the most likely root cause, explained",
  "confidence": 0.0-1.0,
  "evidence": ["which facts from the snapshot support this"],
  "suggested_fixes": [
    {"action": "what to do", "rationale": "why", "risk": "low|medium|high"}
  ],
  "needs_deep_inspection": true|false
}
"""


def build_user_prompt(ctx: DiagnosticContext) -> str:
    w = ctx.workload
    parts: list[str] = []

    parts.append(f"# Workload: {w.ref}")
    parts.append("\n## Status\n" + "\n".join(w.summary_lines()))

    if ctx.events:
        lines = "\n".join(e.line() for e in ctx.events[-40:])
        parts.append(f"\n## Events ({len(ctx.events)})\n{lines}")
    else:
        parts.append("\n## Events\n(none collected)")

    parts.append("\n## Spec excerpt\n" + json.dumps(ctx.spec_excerpt, indent=2, default=str))

    logs = ctx.logs or "(no logs)"
    parts.append(f"\n## Logs (tail)\n```\n{logs}\n```")

    if ctx.deep_inspection:
        blocks = [
            f"### {label}\n```\n{output}\n```"
            for label, output in ctx.deep_inspection.items()
        ]
        parts.append(
            "\n## Deep inspection (live in-container state)\n" + "\n".join(blocks)
        )

    if ctx.notes:
        parts.append("\n## Collector notes\n" + "\n".join(f"- {n}" for n in ctx.notes))

    parts.append("\nDiagnose the root cause. Respond with the JSON object only.")
    return "\n".join(parts)
