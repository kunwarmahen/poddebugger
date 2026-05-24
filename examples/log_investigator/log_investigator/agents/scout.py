"""Scout — classify the log failure and seed an initial lead.

Subclasses :class:`inquiro.Agent`. Builds its user prompt from the
application's ``LogContext`` (passed via ``ac.ctx``). The framework knows
nothing about log files — it just shuttles the context through.
"""

from __future__ import annotations

from inquiro import Agent, AgentContext, PREAMBLE

from ..models import LogContext


SYSTEM = PREAMBLE + """

ROLE: Scout. Read the error lines from a log file and classify what kind of
failure this looks like (e.g. "OutOfMemory", "ConnectionRefused",
"NullPointerException"). Seed exactly ONE lead worth investigating.

Return JSON:
{ "classification": "...",
  "lead": "what to look into",
  "evidence": ["short factual quotes from the log"] }
"""


class Scout(Agent):
    name = "Scout"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        ctx: LogContext = ac.ctx
        errors = ctx.errors()
        return (
            f"Log file: {ctx.path}\n"
            f"Total lines collected: {len(ctx.lines)}\n"
            f"Error lines ({len(errors)}):\n"
            + ("\n".join(f"- {ln}" for ln in errors[-15:]) or "(none)")
            + f"\n\nTail of the log:\n{ctx.tail(20)}\n\n"
            "Classify and seed one lead."
        )

    def apply(self, ac: AgentContext, response: dict) -> None:
        ac.state.classification = str(response.get("classification") or "Unknown")
        for ev in response.get("evidence", []) or []:
            ac.add_evidence(str(ev), source="scout")
        if response.get("lead"):
            ac.add_lead(str(response["lead"]), source="scout")
