"""Shared prompt helpers.

The framework provides the cross-domain pieces: the system-prompt preamble,
a generic list renderer, and renderers for the parts of
:class:`InvestigationState` every domain has (sanity checks, evidence).
Domain-specific context renderers stay in each application's prompts module.
"""

from __future__ import annotations

from .state import InvestigationState

#: Shared preamble — first line(s) of every agent's system prompt.
PREAMBLE = (
    "You are one specialized agent inside a multi-agent investigation system. "
    "You see only the context for your own task. Reason only from the facts "
    "given — never invent logs, events, or results. Respond with ONLY a "
    "JSON object: no prose, no fences."
)


def list_block(items, fmt, empty: str = "(none)") -> str:
    """Render ``items`` line-by-line via ``fmt``, or ``empty`` if there are none."""
    return "\n".join(fmt(i) for i in items) if items else empty


def evidence_block(state: InvestigationState) -> str:
    """Render the state's evidence collection for an agent's user prompt."""
    if not state.evidence:
        return "(no evidence yet)"
    return "\n".join(
        f"{e.id}: {e.summary}" + (f"\n     {e.detail}" if e.detail else "")
        for e in state.evidence
    )


def sanity_block(state: InvestigationState) -> str:
    """Render the state's sanity checks for an agent's user prompt."""
    return list_block(state.sanity_checks,
                      lambda s: f"{s.id}: {s.description}", "(none)")
