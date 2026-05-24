"""Deep inspection (Phase 3) — run read-only diagnostic probes inside a
running target container and collect the output.

Only used when the user explicitly opts in (``--deep``). Every probe is a
read-only shell command — nothing in the target is modified. Deep inspection
needs a *live process*, so it applies to running-but-misbehaving workloads;
a crash-looped/exited container has nothing to exec into.

Phase 11: the whole probe bundle is gated by a single Phase 11 ApprovalGate
call (``kind="probe"``, ``action="deep_inspect"``). A user pre-approves
deep inspection as one unit; the 9 curated commands then run together. A
gate denial returns an empty dict + a one-line note so the caller knows
why the section is missing.
"""

from __future__ import annotations

from .models import WorkloadRef
from .providers.base import ContainerPlatform, ProviderError

# (label, shell command). Commands are busybox-compatible and chain fallbacks
# so they degrade gracefully on minimal images.
PROBES: list[tuple[str, str]] = [
    ("processes",
     "ps -ef 2>/dev/null || ps aux 2>/dev/null || ls /proc 2>/dev/null | grep -E '^[0-9]+$'"),
    ("listening_ports",
     "netstat -tlnp 2>/dev/null || ss -tlnp 2>/dev/null || echo '(netstat/ss unavailable)'"),
    ("disk_usage", "df -h 2>/dev/null"),
    ("memory", "free -m 2>/dev/null || head -5 /proc/meminfo 2>/dev/null"),
    ("cgroup_memory_limit",
     "cat /sys/fs/cgroup/memory.max 2>/dev/null "
     "|| cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null"),
    ("pid1_cmdline", "tr '\\0' ' ' < /proc/1/cmdline 2>/dev/null"),
    ("pid1_limits", "cat /proc/1/limits 2>/dev/null"),
    ("dns_config", "cat /etc/resolv.conf 2>/dev/null"),
    ("mounts", "head -20 /proc/mounts 2>/dev/null"),
]

_MARKER = "poddebugger-shell-ok"
_MAX_OUTPUT = 4000  # chars per probe — keep the second-pass prompt bounded


def run(
    provider: ContainerPlatform,
    ref: WorkloadRef,
    *,
    gate=None,
) -> dict[str, str]:
    """Execute the probe set inside the target; return ``label -> output``.

    Raises :class:`ProviderError` if the target has no usable shell (e.g. a
    distroless image) — the caller should fall back to plain analysis.

    Phase 11: when ``gate`` is supplied, ask it once for the whole
    ``deep_inspect`` bundle before doing anything. A denial returns
    ``{"deep_inspect": "(refused by approval gate)"}`` rather than raising —
    keeps the rest of the investigation alive.
    """
    if gate is not None:
        from .approvals import ActionDescriptor, is_allowed
        descriptor = ActionDescriptor(
            kind="probe",
            action="deep_inspect",
            target=ref,
            risk="low",
            summary=(f"runs {len(PROBES)} read-only commands in the container "
                     "(ps, netstat/ss, df, free, /proc, /etc/resolv.conf, …)"),
        )
        decision = gate.request(descriptor)
        if not is_allowed(decision):
            return {"deep_inspect": f"(refused by approval gate: {decision.value})"}

    check = provider.exec(ref, ["sh", "-c", f"echo {_MARKER}"])
    if _MARKER not in check:
        raise ProviderError(
            "target container has no usable shell — deep inspection needs a "
            "shell in the image (toolkit-image injection is a later phase)"
        )

    results: dict[str, str] = {}
    for label, cmd in PROBES:
        try:
            out = provider.exec(ref, ["sh", "-c", cmd]).strip()
        except ProviderError as exc:
            out = f"(probe failed: {exc})"
        if len(out) > _MAX_OUTPUT:
            out = out[:_MAX_OUTPUT] + "\n…(truncated)"
        results[label] = out or "(no output)"
    return results
