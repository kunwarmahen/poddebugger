"""Auditor — whole-state review before the verdict (HLD §11.2)."""

from __future__ import annotations

from ..prompts import PREAMBLE, evidence_block, list_block, sanity_block
from .base import Agent, AgentContext

SYSTEM = PREAMBLE + """

ROLE: Auditor. Review the WHOLE investigation for problems a single agent would
miss:
- Coherence: do the confirmed findings agree, or do any contradict each other?
- Over-confidence: is any finding accepted on thin or circumstantial evidence?
- Sanity: does any finding violate a sanity check?
- Dead ends: is the strategy still pursuing a refuted path?
File one critique per genuine problem. Target a finding by its id (e.g. F2),
or "strategy" for a process-level concern. If the investigation looks sound,
return an empty critiques list — do not invent problems.

Return JSON:
{ "assessment": "your overall judgement",
  "critiques": [ { "target": "<finding id or 'strategy'>",
                   "concern": "the specific problem" } ] }"""


class Auditor(Agent):
    """Internal-affairs sweep — files critiques the Adjudicator arbitrates."""

    name = "Auditor"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        state = ac.state
        return (
            f"Classification: {state.classification}\n"
            f"Strategy: {state.strategy}\n\n"
            f"Sanity checks:\n{sanity_block(state)}\n\n"
            f"Confirmed findings:\n"
            + list_block(state.confirmed_findings,
                         lambda f: f"{f.id}: {f.statement} ({f.confidence:.0%})") + "\n\n"
            f"Ruled out:\n"
            + list_block(state.ruled_out, lambda r: f"{r.id}: {r.statement}") + "\n\n"
            f"Evidence:\n{evidence_block(state)}\n\n"
            "Audit the investigation."
        )

    def apply(self, ac: AgentContext, response: dict) -> dict:
        """Return the audit result (assessment + critiques); the engine routes
        finding-critiques to the Adjudicator and records strategy-critiques."""
        ac.record_dispatch("Auditor", "audit", str(response.get("assessment", ""))[:80])
        return {
            "assessment": str(response.get("assessment", "")),
            "critiques": list(response.get("critiques", []) or []),
        }
