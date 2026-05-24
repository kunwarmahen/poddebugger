"""Prober — picks a read-only probe to run on the live workload (HLD §11.2)."""

from __future__ import annotations

from ..probes import menu_text
from ..prompts import PREAMBLE, evidence_block
from .base import ActionAgent, AgentContext

SYSTEM = PREAMBLE + """

ROLE: Prober. Choose ONE read-only probe to run on the live workload. Pick a
probe name from the menu only.

Return JSON:
{ "probe": "<probe name from the menu>", "reason": "what you expect to learn" }"""


class Prober(ActionAgent):
    """The field agent — picks a whitelisted probe to gather a fresh fact."""

    name = "Prober"
    action_name = "probe"
    description = (
        "Have the Prober gather a new fact from the live workload. Use ONLY "
        "to test an existing hypothesis that needs a fact you do not have."
    )
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        return (
            f"Goal: {ac.instruction}\n\n"
            f"Classification: {ac.state.classification}\n"
            f"Evidence so far:\n{evidence_block(ac.state)}\n\n"
            f"Probe menu:\n{menu_text()}\n\n"
            "Choose one probe."
        )

    def apply(self, ac: AgentContext, response: dict) -> str:
        """Return the chosen probe name; the engine runs it (probes are
        whitelisted — never an LLM-emitted command)."""
        return str(response.get("probe", "")).strip()
