"""Coordinator — picks the next action each iteration (HLD §11.2, §14.4).

The action menu is **dynamic**: built from every registered ActionAgent and
passed via ``ac.extras["actions"]`` at dispatch time. Adding a new
ActionAgent automatically makes it available to the Coordinator with no
prompt edits — the Stage 9D extension point.
"""

from __future__ import annotations

from ..prompts import PREAMBLE, list_block, sanity_block
from .base import Agent, AgentContext

SYSTEM = PREAMBLE + """

ROLE: Coordinator. Read the investigation state and choose the single next
action. Pick exactly one of the actions listed in the user prompt below, or
"done" to stop.

- "done": stop. Prefer this — choose it as soon as the confirmed findings
  explain the failure and satisfy the sanity checks. Do not keep probing or
  analyzing once the failure is already explained.

Ordering: a hypothesis must exist before it can be probed or verified. If
there are no hypotheses yet, choose "analyze". Do not probe to "look
around" — probe only to settle a specific open hypothesis.

Return JSON:
{ "action": "<action name from the menu>|done",
  "target": "id of the lead or hypothesis to act on, or empty string",
  "instruction": "what the dispatched agent should focus on",
  "reason": "why this action" }"""


def _render_menu(actions) -> str:
    if not actions:
        return '- "done": stop (no action agents are registered)'
    lines = [f'- "{name}": {desc}' for name, desc in actions]
    lines.append('- "done": stop. Use when the failure is already explained.')
    return "\n".join(lines)


class Coordinator(Agent):
    """Decides the next move each iteration — picks from the dynamic menu."""

    name = "Coordinator"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        state = ac.state
        iterations_left = int(ac.extras.get("iterations_left", 0))
        actions = ac.extras.get("actions", [])  # list[(name, description)]
        open_leads = [l for l in state.leads if l.status == "open"]
        return (
            f"Available actions:\n{_render_menu(actions)}\n\n"
            f"Iteration {state.iteration} (≈{iterations_left} left).\n"
            f"Classification: {state.classification}\n"
            f"Strategy: {state.strategy}\n\n"
            f"Sanity checks:\n{sanity_block(state)}\n\n"
            f"Open leads:\n"
            + list_block(open_leads, lambda l: f"{l.id}: {l.description}") + "\n\n"
            f"Hypotheses under review:\n"
            + list_block(state.hypotheses,
                         lambda h: f"{h.id}: {h.statement} [{h.status}]") + "\n\n"
            f"Confirmed findings:\n"
            + list_block(state.confirmed_findings,
                         lambda f: f"{f.id}: {f.statement} ({f.confidence:.0%})") + "\n\n"
            f"Ruled out:\n"
            + list_block(state.ruled_out, lambda r: f"{r.id}: {r.statement}") + "\n\n"
            "Choose the next action."
        )

    def apply(self, ac: AgentContext, response: dict) -> tuple[str, str, str]:
        action = str(response.get("action", "done")).lower()
        # Accept any registered action name + "done"; coerce unknowns to done.
        allowed = {name for name, _ in ac.extras.get("actions", [])}
        allowed.add("done")
        if action not in allowed:
            action = "done"
        target = str(response.get("target", ""))
        instruction = str(response.get("instruction") or response.get("reason") or "")
        ac.record_dispatch("Coordinator", action, str(response.get("reason", "")))
        return action, target, instruction
