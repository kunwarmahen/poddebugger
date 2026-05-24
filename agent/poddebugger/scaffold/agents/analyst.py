"""Analyst — proposes candidate root-cause hypotheses (HLD §11.2)."""

from __future__ import annotations

from ..prompts import PREAMBLE, context_block, evidence_block, sanity_block
from ..state import Hypothesis
from .base import ActionAgent, AgentContext

SYSTEM = PREAMBLE + """

ROLE: Analyst. Reason over the collected evidence to form candidate root-cause
hypotheses. Reference supporting evidence by id (e.g. E1).

Return JSON:
{
  "hypotheses": [
    { "statement": "a candidate root cause",
      "test": "how it could be confirmed or refuted",
      "evidence": ["ids of supporting evidence"] }
  ],
  "new_evidence": ["new observations derived purely from existing evidence"]
}"""


class Analyst(ActionAgent):
    """The theorist — proposes hypotheses from existing evidence (no probing)."""

    name = "Analyst"
    action_name = "analyze"
    description = (
        "Have the Analyst form or refine hypotheses from existing evidence. "
        "Use when there are open leads or unexplained evidence."
    )
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        return (
            f"Focus: {ac.instruction}\n\n"
            f"Sanity checks:\n{sanity_block(ac.state)}\n\n"
            f"Evidence:\n{evidence_block(ac.state)}\n\n"
            f"Raw workload snapshot for reference:\n\n{context_block(ac.ctx)}\n\n"
            "Form candidate root-cause hypotheses."
        )

    def apply(self, ac: AgentContext, response: dict) -> list[Hypothesis]:
        for ev in response.get("new_evidence", []) or []:
            ac.add_evidence(str(ev), source="analyst")
        new: list[Hypothesis] = []
        for h in response.get("hypotheses", []) or []:
            if not isinstance(h, dict) or not h.get("statement"):
                continue
            new.append(ac.add_hypothesis(
                statement=str(h.get("statement", "")),
                test=str(h.get("test", "")),
                evidence_ids=[str(x) for x in (h.get("evidence") or [])],
            ))
        ac.record_dispatch("Analyst", "hypothesize", f"{len(new)} hypotheses")
        return new
