"""Adjudicator — rules on a critique against one Finding (HLD §11.2)."""

from __future__ import annotations

from ..prompts import PREAMBLE, context_block, evidence_block, sanity_block
from ..state import Finding
from .base import Agent, AgentContext

SYSTEM = PREAMBLE + """

ROLE: Adjudicator. A critique has been filed against ONE confirmed finding.
Judge it independently against the evidence — do not defer to either side.
- "uphold": the critique is valid; the finding is not adequately supported and
  must be demoted.
- "dismiss": the critique is unfounded; the finding stands.

Return JSON:
{ "ruling": "uphold|dismiss", "reason": "the basis for your ruling" }"""


class Adjudicator(Agent):
    """The judge — independently arbitrates a critique against one Finding."""

    name = "Adjudicator"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        finding, concern = ac.subject  # type: ignore[misc]
        finding: Finding
        return (
            f"Finding under challenge:\n{finding.id}: {finding.statement} "
            f"({finding.confidence:.0%})\n\n"
            f"Critique to judge:\n{concern}\n\n"
            f"Sanity checks:\n{sanity_block(ac.state)}\n\n"
            f"Evidence:\n{evidence_block(ac.state)}\n\n"
            f"Raw workload snapshot:\n\n{context_block(ac.ctx)}\n\n"
            "Rule on the critique."
        )

    def apply(self, ac: AgentContext, response: dict) -> dict:
        """Return ``{"ruling": ..., "reason": ...}``; demotion is engine-level."""
        return {
            "ruling": str(response.get("ruling", "dismiss")).lower(),
            "reason": str(response.get("reason", "")),
        }
