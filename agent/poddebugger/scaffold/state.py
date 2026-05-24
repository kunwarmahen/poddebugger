"""Compat re-export — the canonical home is :mod:`poddebugger.framework.state`.

The InvestigationState model is framework code (see FRAMEWORK.md). Phase 10B
moved the implementation under :mod:`poddebugger.framework`; this module
keeps the old import path working for existing callers.
"""

from __future__ import annotations

from ..framework.state import (  # noqa: F401 — re-exports
    DispatchRecord,
    Evidence,
    Finding,
    Hypothesis,
    InvestigationState,
    Lead,
    RuledOut,
    SanityCheck,
)

__all__ = [
    "InvestigationState",
    "SanityCheck",
    "Lead",
    "Evidence",
    "Hypothesis",
    "Finding",
    "RuledOut",
    "DispatchRecord",
]
