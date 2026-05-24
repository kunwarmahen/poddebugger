"""Analyst — turn evidence into one root-cause hypothesis."""

from __future__ import annotations

from inquiro import (
    Agent,
    AgentContext,
    PREAMBLE,
    evidence_block,
    list_block,
)


SYSTEM = PREAMBLE + """

ROLE: Analyst. From the Scout's classification + the evidence, propose ONE
candidate root-cause hypothesis.

Return JSON:
{ "hypothesis": "the likely cause, one sentence",
  "evidence": ["ids of supporting evidence"] }
"""


class Analyst(Agent):
    name = "Analyst"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        state = ac.state
        return (
            f"Classification: {state.classification}\n\n"
            f"Open leads:\n"
            + list_block(state.leads, lambda l: f"{l.id}: {l.description}")
            + f"\n\nEvidence:\n{evidence_block(state)}\n\n"
            "Propose one root-cause hypothesis."
        )

    def apply(self, ac: AgentContext, response: dict):
        statement = str(response.get("hypothesis") or "").strip()
        if not statement:
            return None
        return ac.add_hypothesis(
            statement=statement,
            evidence_ids=[str(x) for x in (response.get("evidence") or [])],
        )
