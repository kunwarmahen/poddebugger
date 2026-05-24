"""Reporter — write the final :class:`LogFinding`."""

from __future__ import annotations

from inquiro import Agent, AgentContext, PREAMBLE, evidence_block, list_block

from ..models import LogFinding


SYSTEM = PREAMBLE + """

ROLE: Reporter. Produce the final finding from the team's work.

Return JSON:
{ "summary": "one-sentence summary",
  "likely_cause": "the most likely cause, explained",
  "confidence": 0.0,
  "evidence": ["facts that support this"] }
"""


class Reporter(Agent):
    name = "Reporter"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        state = ac.state
        return (
            f"Classification: {state.classification}\n\n"
            f"Confirmed findings:\n"
            + list_block(
                state.confirmed_findings,
                lambda f: f"{f.id}: {f.statement} ({f.confidence:.0%})",
            )
            + f"\n\nOpen hypotheses:\n"
            + list_block(
                state.hypotheses,
                lambda h: f"{h.id}: {h.statement} [{h.status}]",
            )
            + f"\n\nEvidence:\n{evidence_block(state)}\n\n"
            "Write the final finding."
        )

    def apply(self, ac: AgentContext, response: dict) -> LogFinding:
        try:
            conf = float(response.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        return LogFinding(
            summary=str(response.get("summary") or "").strip(),
            classification=ac.state.classification,
            likely_cause=str(response.get("likely_cause") or "").strip(),
            evidence=[str(e) for e in (response.get("evidence") or [])],
            confidence=max(0.0, min(1.0, conf)),
        )
