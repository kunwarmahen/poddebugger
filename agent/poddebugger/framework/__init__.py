"""Compat shim — re-exports the ``inquiro`` framework.

Phase 10C moved the framework primitives out into the ``inquiro`` package
(``../../inquiro/``). This module re-exports them so existing callers that
import from ``poddebugger.framework.*`` keep working unchanged. New code
should import from ``inquiro`` directly.
"""

from __future__ import annotations

from inquiro import (  # noqa: F401 — re-exports
    LIFECYCLE_POINTS,
    ActionAgent,
    Agent,
    AgentContext,
    AgentLLMs,
    DispatchRecord,
    Evidence,
    Finding,
    Hypothesis,
    HookAgent,
    InvestigationState,
    LLMClient,
    LLMError,
    LLMSpec,
    Lead,
    PREAMBLE,
    RuledOut,
    SanityCheck,
    Workspace,
    evidence_block,
    extract_json,
    list_block,
    sanity_block,
)

__all__ = [
    "Agent",
    "ActionAgent",
    "HookAgent",
    "AgentContext",
    "LIFECYCLE_POINTS",
    "InvestigationState",
    "SanityCheck",
    "Lead",
    "Evidence",
    "Hypothesis",
    "Finding",
    "RuledOut",
    "DispatchRecord",
    "Workspace",
    "AgentLLMs",
    "LLMSpec",
    "LLMClient",
    "LLMError",
    "PREAMBLE",
    "list_block",
    "sanity_block",
    "evidence_block",
    "extract_json",
]
