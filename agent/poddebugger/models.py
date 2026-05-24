"""Platform-agnostic data models shared by the brain and providers.

These types are the contract between the platform providers (which know how to
talk to Podman / Kubernetes) and the analyzer (which knows nothing about either).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WorkloadRef:
    """Points at a single workload to analyze.

    For Podman, ``name`` is a container or pod name. For Kubernetes, ``name`` is
    a pod name and ``namespace`` is required.
    """

    name: str
    namespace: Optional[str] = None
    container: Optional[str] = None  # specific container within a pod
    platform: str = "podman"

    def __str__(self) -> str:
        if self.namespace:
            return f"{self.namespace}/{self.name}"
        return self.name


@dataclass
class Event:
    """A normalized lifecycle event (podman event / k8s Event)."""

    timestamp: str
    type: str       # e.g. "container", "Warning"
    reason: str     # e.g. "died", "OOMKilled", "BackOff"
    message: str = ""

    def line(self) -> str:
        return f"[{self.timestamp}] {self.type}/{self.reason}: {self.message}".rstrip()


@dataclass
class Workload:
    """Normalized status of a workload, regardless of platform."""

    ref: WorkloadRef
    kind: str = "container"          # container | pod
    status: str = "unknown"          # running | exited | created | ...
    running: bool = False            # is a live process present (exec-able)?
    image: str = ""
    restart_count: int = 0
    exit_code: Optional[int] = None
    oom_killed: bool = False
    health_status: str = ""          # healthy | unhealthy | starting | ""
    started_at: str = ""
    finished_at: str = ""
    error: str = ""

    def summary_lines(self) -> list[str]:
        out = [
            f"kind:          {self.kind}",
            f"status:        {self.status}",
            f"image:         {self.image}",
            f"restart_count: {self.restart_count}",
        ]
        if self.exit_code is not None:
            out.append(f"exit_code:     {self.exit_code}")
        if self.oom_killed:
            out.append("oom_killed:    true")
        if self.health_status:
            out.append(f"health:        {self.health_status}")
        if self.started_at:
            out.append(f"started_at:    {self.started_at}")
        if self.finished_at:
            out.append(f"finished_at:   {self.finished_at}")
        if self.error:
            out.append(f"error:         {self.error}")
        return out


@dataclass
class DiagnosticContext:
    """Everything collected about a workload, fed to the LLM."""

    workload: Workload
    events: list[Event] = field(default_factory=list)
    logs: str = ""
    spec_excerpt: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)  # collector warnings
    # Phase 3 deep-inspection probe output: label -> command output.
    deep_inspection: dict = field(default_factory=dict)


@dataclass
class Fix:
    action: str
    rationale: str = ""
    risk: str = "unknown"  # low | medium | high


@dataclass
class Diagnosis:
    summary: str
    root_cause: str
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    suggested_fixes: list[Fix] = field(default_factory=list)
    needs_deep_inspection: bool = False
    # Phase 7B — set by the Remediator agent on `analyze --fix`. Contains the
    # validated catalog plan plus the model's rationale / expected_effect /
    # confidence, or ``{"action": "none", "reason": "..."}``. None when the
    # Remediator did not run.
    proposed_remediation: Optional[dict] = None
