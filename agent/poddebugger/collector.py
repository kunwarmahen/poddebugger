"""Context collector — runs the provider's read methods and assembles a
DiagnosticContext for the analyzer. Knows nothing about any specific runtime.
"""

from __future__ import annotations

from .models import DiagnosticContext, WorkloadRef
from .providers.base import ContainerPlatform, ProviderError

# Rough char budget for collected logs (~3-4k tokens). Logs are kept tail-first
# since the most recent lines are where a crash shows up.
_MAX_LOG_CHARS = 12000


def collect(provider: ContainerPlatform, ref: WorkloadRef, log_lines: int = 200) -> DiagnosticContext:
    """Gather status, events, logs and spec into one context object.

    Individual collection steps degrade gracefully: a failure becomes a note
    rather than aborting the whole analysis.
    """
    workload = provider.get_workload(ref)
    ctx = DiagnosticContext(workload=workload)

    pod_note = getattr(provider, "_pod_note", None)
    if pod_note:
        ctx.notes.append(pod_note)

    try:
        ctx.events = provider.get_events(ref)
    except ProviderError as exc:
        ctx.notes.append(f"could not collect events: {exc}")

    try:
        ctx.logs = provider.get_logs(ref, tail=log_lines)
        if not ctx.logs:
            ctx.notes.append("no logs available for this workload")
        elif len(ctx.logs) > _MAX_LOG_CHARS:
            ctx.logs = "...(earlier log lines truncated)...\n" + ctx.logs[-_MAX_LOG_CHARS:]
            ctx.notes.append(f"logs truncated to the most recent ~{_MAX_LOG_CHARS} chars")
    except ProviderError as exc:
        ctx.notes.append(f"could not collect logs: {exc}")

    try:
        ctx.spec_excerpt = provider.get_spec(ref)
    except ProviderError as exc:
        ctx.notes.append(f"could not collect spec: {exc}")

    # Runtime stats (CPU/memory) — only meaningful for a live workload.
    if workload.running:
        try:
            stats = provider.get_stats(ref)
            if stats:
                ctx.spec_excerpt["runtime_stats"] = stats
        except ProviderError as exc:
            ctx.notes.append(f"could not collect runtime stats: {exc}")

    return ctx
