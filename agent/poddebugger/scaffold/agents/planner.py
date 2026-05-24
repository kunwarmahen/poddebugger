"""Planner — sets the strategy and the sanity checks (HLD §11.2)."""

from __future__ import annotations

from ..prompts import PREAMBLE, evidence_block, list_block
from .base import Agent, AgentContext

SYSTEM = PREAMBLE + """

ROLE: Planner. From the classification, evidence and leads, set an
investigation strategy and the sanity checks the final explanation MUST
satisfy.

Return JSON:
{
  "strategy": "a short paragraph: what to investigate, in what order",
  "sanity_checks": ["invariants the explanation must satisfy, e.g. 'the exit code is consistent with the stated cause'"]
}"""


class Planner(Agent):
    """Lays out the line of inquiry and the rules the final answer must obey."""

    name = "Planner"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        return (
            f"Classification: {ac.state.classification}\n\n"
            f"Evidence:\n{evidence_block(ac.state)}\n\n"
            f"Leads:\n" + list_block(ac.state.leads, lambda l: f"{l.id}: {l.description}")
        )

    def apply(self, ac: AgentContext, response: dict):
        ac.state.strategy = str(response.get("strategy", ""))
        for sc in response.get("sanity_checks", []) or []:
            ac.state.add_sanity_check(str(sc))
        ac.record_dispatch("Planner", "plan",
                           f"{len(ac.state.sanity_checks)} sanity checks")
