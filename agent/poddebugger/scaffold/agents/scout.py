"""Scout — first on the scene (HLD §11.2)."""

from __future__ import annotations

from ..prompts import PREAMBLE, context_block
from .base import Agent, AgentContext

SYSTEM = PREAMBLE + """

ROLE: Scout. Given a snapshot of a failing workload (status, events, logs,
spec), classify the failure and seed the investigation.

Return JSON:
{
  "classification": "short failure category — CrashLoopBackOff | OOMKilled | ImagePullError | ProbeFailure | ConfigError | NetworkError | Unknown",
  "evidence": ["concise factual observations taken from the snapshot"],
  "leads": ["specific, short things worth investigating"]
}"""


class Scout(Agent):
    """Gathers the deterministic snapshot's facts and classifies the failure."""

    name = "Scout"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        return "Workload snapshot:\n\n" + context_block(ac.ctx)

    def apply(self, ac: AgentContext, response: dict):
        ac.state.classification = str(response.get("classification", "Unknown"))
        for fact in response.get("evidence", []) or []:
            ac.add_evidence(str(fact), source="scout")
        for lead in response.get("leads", []) or []:
            ac.add_lead(str(lead), source="scout")
        ac.record_dispatch("Scout", "classify", ac.state.classification)
        # The engine reads `None` as "the call failed" — return the parsed
        # response so a successful Scout is not mistaken for a dead one.
        return response
