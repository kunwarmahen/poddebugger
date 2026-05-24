"""``inquiro`` — a domain-agnostic multi-agent investigation framework.

A team of role-specialized LLM agents (Scout, Planner, Coordinator,
Analyst, Prober, Verifier, Auditor, Adjudicator, Reporter, plus any custom
agents you add) cooperate over a persistent, git-tracked
``InvestigationState`` to reach a confirmed diagnosis. Originally extracted
from PodDebugger (HLD §15); the framework is the machinery, applications
supply the domain (the providers, the probes, the prompts, the result type).

Public surface — what an application imports::

    from inquiro import (
        Agent, ActionAgent, HookAgent, AgentContext, LIFECYCLE_POINTS,
        InvestigationState, SanityCheck, Lead, Evidence, Hypothesis,
        Finding, RuledOut, DispatchRecord,
        Workspace, AgentLLMs, LLMSpec,
        LLMClient, LLMError,
        PREAMBLE, list_block, sanity_block, evidence_block,
        extract_json,
    )
"""

from __future__ import annotations

from .agent import (
    LIFECYCLE_POINTS,
    ActionAgent,
    Agent,
    AgentContext,
    HookAgent,
)
from .json_utils import extract_json
from .llm import LLMClient, LLMError
from .llms import AgentLLMs, LLMSpec
from .prompts import (
    PREAMBLE,
    evidence_block,
    list_block,
    sanity_block,
)
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

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # agents
    "Agent",
    "ActionAgent",
    "HookAgent",
    "AgentContext",
    "LIFECYCLE_POINTS",
    # state + entities
    "InvestigationState",
    "SanityCheck",
    "Lead",
    "Evidence",
    "Hypothesis",
    "Finding",
    "RuledOut",
    "DispatchRecord",
    # workspace / llm plumbing
    "Workspace",
    "AgentLLMs",
    "LLMSpec",
    "LLMClient",
    "LLMError",
    # prompt helpers
    "PREAMBLE",
    "list_block",
    "sanity_block",
    "evidence_block",
    # json
    "extract_json",
]
