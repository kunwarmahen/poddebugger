"""Multi-agent investigation scaffold (Phase 6 — see HLD.md §11).

Replaces the one-shot analyzer with an iterative, role-specialized engine that
scales effort to a failure's difficulty. Stage A ships the persistent state
model and the per-run git workspace; the agent loop lands in Stage B.
"""

from __future__ import annotations

from .agents import ActionAgent, Agent, AgentContext, HookAgent
from .engine import InvestigationEngine, load_agents_from_env
from .llms import AgentLLMs
from .state import (
    DispatchRecord,
    Evidence,
    Finding,
    Hypothesis,
    InvestigationState,
    Lead,
    RuledOut,
    SanityCheck,
)
from .workspace import Workspace

__all__ = [
    "InvestigationState",
    "SanityCheck",
    "Lead",
    "Evidence",
    "Hypothesis",
    "Finding",
    "RuledOut",
    "DispatchRecord",
    "Workspace",
    "InvestigationEngine",
    "AgentLLMs",
    "Agent",
    "ActionAgent",
    "HookAgent",
    "AgentContext",
    "load_agents_from_env",
]
