"""Compat re-export — the canonical home is :mod:`poddebugger.framework.agent`.

The Agent / ActionAgent / HookAgent base classes are framework code (HLD
§14). Phase 10B moved the implementation under
:mod:`poddebugger.framework`; this module keeps the old import path working
so the nine built-in scaffold agents — and any custom subclasses out in
the wild — keep importing from here.
"""

from __future__ import annotations

from ...framework.agent import (  # noqa: F401 — re-exports
    LIFECYCLE_POINTS,
    ActionAgent,
    Agent,
    AgentContext,
    HookAgent,
)

__all__ = [
    "Agent",
    "ActionAgent",
    "HookAgent",
    "AgentContext",
    "LIFECYCLE_POINTS",
]
