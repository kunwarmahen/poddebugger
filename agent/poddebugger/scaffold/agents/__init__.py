"""Agent base classes + the nine built-in scaffold agents (HLD §11.2 / §14).

This package is the framework's public extension surface. The nine built-ins
(Scout, Planner, Coordinator, Analyst, Prober, Verifier, Auditor, Adjudicator,
Reporter) subclass ``Agent`` here. Users add their own by subclassing
``ActionAgent`` (Coordinator-dispatched) or ``HookAgent`` (lifecycle).
"""

from __future__ import annotations

from .adjudicator import Adjudicator
from .analyst import Analyst
from .auditor import Auditor
from .base import (
    LIFECYCLE_POINTS,
    ActionAgent,
    Agent,
    AgentContext,
    HookAgent,
)
from .coder import Coder
from .coordinator import Coordinator
from .librarian import Librarian
from .planner import Planner
from .prober import Prober
from .remediator import Remediator
from .reporter import Reporter
from .scout import Scout
from .specialist import Specialist, make_specialist, specialty_slug
from .verifier import Verifier


def built_in_agents() -> list[Agent]:
    """A fresh list of the nine investigative scaffold agents — the default team.

    The Remediator (HLD §12.3) and the Librarian (HLD §13) are **not**
    included here. Each is opt-in via an engine constructor flag —
    ``remediation_enabled=True`` (``analyze --fix``) and
    ``research_enabled=True`` (``analyze --research``) — so the default
    investigation never mutates or hits the network by accident.
    """
    return [
        Scout(), Planner(), Coordinator(),
        Analyst(), Prober(), Verifier(),
        Auditor(), Adjudicator(), Reporter(),
    ]


__all__ = [
    # framework primitives
    "Agent",
    "ActionAgent",
    "HookAgent",
    "AgentContext",
    "LIFECYCLE_POINTS",
    # built-in roles
    "Scout",
    "Planner",
    "Coordinator",
    "Analyst",
    "Prober",
    "Verifier",
    "Auditor",
    "Adjudicator",
    "Reporter",
    "Remediator",
    "Librarian",
    "Coder",
    "Specialist",
    "make_specialist",
    "specialty_slug",
    "built_in_agents",
]
