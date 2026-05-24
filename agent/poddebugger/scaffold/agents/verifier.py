"""Verifier — adversarially judges one hypothesis (HLD §11.2)."""

from __future__ import annotations

from ..prompts import PREAMBLE, context_block, evidence_block, sanity_block
from ..state import Hypothesis
from .base import Agent, AgentContext

SYSTEM = PREAMBLE + """

ROLE: Verifier. Adversarially judge ONE hypothesis against the evidence and the
sanity checks.
- VERIFIED: the evidence clearly supports it and no sanity check is violated.
- REFUTED: the evidence contradicts it.
- INCONCLUSIVE: more evidence is needed — name a probe that would settle it.

Return JSON:
{ "verdict": "VERIFIED|REFUTED|INCONCLUSIVE",
  "confidence": 0.0,
  "note": "the reasoning behind the verdict",
  "suggested_probe": "a probe name to settle an INCONCLUSIVE verdict, or empty string" }"""


class Verifier(Agent):
    """Judges a single hypothesis; the engine wraps grounded re-verify on top."""

    name = "Verifier"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        hyp: Hypothesis = ac.subject  # type: ignore[assignment]
        return (
            f"Hypothesis to judge:\n{hyp.id}: {hyp.statement}\n"
            f"Proposed test: {hyp.test}\n\n"
            f"Sanity checks:\n{sanity_block(ac.state)}\n\n"
            f"Evidence:\n{evidence_block(ac.state)}\n\n"
            f"Raw workload snapshot:\n\n{context_block(ac.ctx)}\n\n"
            "Issue your verdict."
        )

    def apply(self, ac: AgentContext, response: dict) -> dict:
        """Return the parsed verdict for the engine to act on (promotion /
        refutation / grounded re-verify are engine-level control flow)."""
        return {
            "verdict": str(response.get("verdict", "INCONCLUSIVE")).upper(),
            "confidence": response.get("confidence", 0.0),
            "note": str(response.get("note", "")),
            "suggested_probe": str(response.get("suggested_probe", "")).strip(),
        }
