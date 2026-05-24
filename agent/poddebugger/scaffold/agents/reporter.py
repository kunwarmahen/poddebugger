"""Reporter — writes the final Diagnosis (HLD §11.2)."""

from __future__ import annotations

from ..prompts import PREAMBLE, evidence_block, list_block, sanity_block
from .base import Agent, AgentContext

SYSTEM = PREAMBLE + """

ROLE: Reporter. Produce the final root-cause diagnosis from the confirmed
findings and the evidence. If no finding was confirmed, give the best
supported explanation and lower the confidence accordingly.

Return JSON:
{ "summary": "one-sentence plain-language summary",
  "root_cause": "the most likely root cause, explained",
  "confidence": 0.0,
  "evidence": ["facts that support this"],
  "suggested_fixes": [ { "action": "what to do", "rationale": "why", "risk": "low|medium|high" } ],
  "needs_deep_inspection": false }"""


class Reporter(Agent):
    """Writes the final diagnosis. The engine wraps the deterministic fallback."""

    name = "Reporter"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        state = ac.state
        return (
            f"Classification: {state.classification}\n\n"
            f"Confirmed findings:\n"
            + list_block(state.confirmed_findings,
                         lambda f: f"{f.id}: {f.statement} ({f.confidence:.0%})") + "\n\n"
            f"Hypotheses still open:\n"
            + list_block(state.hypotheses,
                         lambda h: f"{h.id}: {h.statement} [{h.status}]") + "\n\n"
            f"Ruled out:\n"
            + list_block(state.ruled_out, lambda r: f"{r.id}: {r.statement}") + "\n\n"
            f"Sanity checks:\n{sanity_block(state)}\n\n"
            f"Evidence:\n{evidence_block(state)}\n\n"
            "Produce the final diagnosis."
        )

    def apply(self, ac: AgentContext, response: dict) -> dict:
        """Return the raw diagnosis dict; the engine maps it to Diagnosis."""
        return response
