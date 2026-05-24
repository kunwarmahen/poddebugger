"""Domain model — log file → finding.

These types stay in the *application*. The framework knows nothing about
log files; it just carries our :class:`LogContext` through ``AgentContext.ctx``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LogContext:
    """Everything an agent needs to see about the log under investigation."""

    path: str
    lines: list[str] = field(default_factory=list)

    def tail(self, n: int = 40) -> str:
        return "\n".join(self.lines[-n:])

    def errors(self) -> list[str]:
        return [
            ln for ln in self.lines
            if any(tag in ln.upper() for tag in ("ERROR", "FATAL", "EXCEPTION"))
        ]


@dataclass
class LogFinding:
    """The result the Reporter agent returns — this app's `Diagnosis` equivalent."""

    summary: str
    classification: str = ""
    likely_cause: str = ""
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0
