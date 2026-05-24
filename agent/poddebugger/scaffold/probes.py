"""Probe registry — the whitelisted read-only actions the Prober may pick from.

The model never receives a raw ``exec``; it chooses a probe *by name* from this
menu and the engine runs it (same safety principle as Phase 5 remediation).
Every probe is built on existing provider read methods.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import deepinspect
from ..models import WorkloadRef
from ..providers.base import ContainerPlatform, ProviderError


@dataclass(frozen=True)
class Probe:
    name: str
    description: str


# The menu shown to the Prober agent.
PROBE_MENU: list[Probe] = [
    Probe("logs_more",
          "Fetch a larger tail (1000 lines) of the workload's logs."),
    Probe("deep_inspect",
          "Run read-only probes inside the running container — processes, "
          "listening ports, memory, cgroup limits, mounts. Needs a live process."),
    Probe("recheck_status",
          "Re-read the workload's current status, restart count and exit code."),
]

_PROBE_NAMES = {p.name for p in PROBE_MENU}


def menu_text() -> str:
    """Render the probe menu for inclusion in the Prober prompt."""
    return "\n".join(f"- {p.name}: {p.description}" for p in PROBE_MENU)


def run_probe(name: str, provider: ContainerPlatform, ref: WorkloadRef,
              *, gate=None) -> str:
    """Execute a whitelisted probe and return its output as text.

    Raises ProviderError for an unknown probe or a probe that cannot run.

    Phase 11: ``logs_more`` and ``recheck_status`` are plain provider reads
    (no exec) so they're not gated. ``deep_inspect`` runs code inside the
    container — the gate is threaded into :func:`deepinspect.run`.
    """
    if name not in _PROBE_NAMES:
        raise ProviderError(f"unknown probe {name!r} (choose from: {sorted(_PROBE_NAMES)})")

    if name == "logs_more":
        return provider.get_logs(ref, tail=1000) or "(no logs)"

    if name == "recheck_status":
        return "\n".join(provider.get_workload(ref).summary_lines())

    if name == "deep_inspect":
        result = deepinspect.run(provider, ref, gate=gate)
        return "\n\n".join(f"[{label}]\n{out}" for label, out in result.items())

    raise ProviderError(f"probe {name!r} is not implemented")  # unreachable
