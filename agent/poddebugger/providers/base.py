"""The platform provider interface.

The analyzer ("brain") depends only on this abstraction, never on Podman or
Kubernetes directly. Phase 1 needs the read-only methods; ``inject_debug`` /
``exec`` / ``remediate`` are declared here but land in later phases.
"""

from __future__ import annotations

import abc

from ..models import Event, Workload, WorkloadRef


class ProviderError(RuntimeError):
    """Raised when a provider cannot talk to its runtime."""


class ContainerPlatform(abc.ABC):
    """Observe (and later, act on) workloads on one container runtime."""

    name: str = "base"

    # --- read-only (Phase 1) -------------------------------------------------

    @abc.abstractmethod
    def preflight(self) -> None:
        """Verify the runtime is reachable; raise ProviderError otherwise."""

    @abc.abstractmethod
    def resolve(self, target: str, namespace: str | None = None) -> WorkloadRef:
        """Turn a user-supplied target string into a concrete WorkloadRef."""

    @abc.abstractmethod
    def get_workload(self, ref: WorkloadRef) -> Workload:
        """Return normalized status for the workload."""

    @abc.abstractmethod
    def get_events(self, ref: WorkloadRef) -> list[Event]:
        """Return recent lifecycle events for the workload."""

    @abc.abstractmethod
    def get_logs(self, ref: WorkloadRef, tail: int = 200) -> str:
        """Return the last ``tail`` lines of logs."""

    @abc.abstractmethod
    def get_spec(self, ref: WorkloadRef) -> dict:
        """Return a redacted spec excerpt (image, cmd, resources, probes)."""

    def get_stats(self, ref: WorkloadRef) -> dict:
        """Return live resource stats (CPU/memory). Empty dict if unsupported."""
        return {}

    # --- mutating (Phase 3+) -------------------------------------------------

    def inject_debug(self, ref: WorkloadRef, image: str):  # pragma: no cover
        raise NotImplementedError("deep inspection is a Phase 3 feature")

    def exec(self, ref: WorkloadRef, command: list[str]) -> str:  # pragma: no cover
        raise NotImplementedError("deep inspection is a Phase 3 feature")

    def remediate(self, ref: WorkloadRef, action: str):  # pragma: no cover
        raise NotImplementedError("remediation is a Phase 5 feature")
