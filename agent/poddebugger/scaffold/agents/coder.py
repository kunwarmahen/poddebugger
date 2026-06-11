"""Coder — writes a short script the sandbox runs (Stage 13D — HLD §18.6).

The Coder proposes ONE script (bash or python); the **engine** validates
it, asks the approval gate (which shows the human the full script body),
and executes it in a sandbox *sibling* container — never inside the
target, never with the target's filesystem. Output lands as Evidence
tagged ``coder:<purpose>:<hash8>``.

Like the Remediator and Librarian, this agent is opt-in — it joins the
registry only when ``InvestigationEngine(coder_enabled=True)`` (via
``analyze --coder``). Coordinator-dispatchable via ``action: code``.

Output JSON::

    { "language": "bash" | "python",
      "script": "<the script>",
      "purpose": "probe" | "fix" | "build",
      "rationale": "what this should establish" }

    { "script": "", "reason": "no script would help here" }
"""

from __future__ import annotations

from ..prompts import PREAMBLE, evidence_block, list_block
from .base import ActionAgent, AgentContext

SYSTEM = PREAMBLE + """

ROLE: Coder. Write ONE short script to advance the investigation. It runs
in a SANDBOX SIDECAR container that shares the target's network (so you can
reach its ports and resolve what it resolves) but NOT its filesystem or
process table. Available in the sandbox: sh/bash, python3, curl, wget, jq,
nslookup/dig, nc.

Rules — these are absolute:
- ONE script, under 40 lines. It must be self-contained and non-interactive.
- Prefer "probe" scripts that gather facts (hit an endpoint, resolve a
  name, test a port, parse a response). Use purpose "fix" only when a
  network-reachable mutation is genuinely the right move.
- The script CANNOT touch the target's files or processes — do not try.
- Never exfiltrate data: no posting logs/env/credentials to external hosts.
- A human reviews the full script before it runs; write it readably.
- If no script would add information, return {"script": "", "reason": "..."}.

Return EXACTLY one JSON object:

  { "language": "bash" | "python",
    "script": "<the script>",
    "purpose": "probe" | "fix" | "build",
    "rationale": "what this should establish" }

  { "script": "",
    "reason": "why a script wouldn't help" }"""


class Coder(ActionAgent):
    """Writes one sandboxed script; the engine gates and runs it."""

    name = "Coder"
    action_name = "code"
    description = (
        "Have the Coder write a short bash/python script that runs in a "
        "sandbox sharing the target's NETWORK only (probe an endpoint, "
        "resolve DNS, test a port, query a service). High risk — every "
        "script needs human approval. Use when the whitelisted probes "
        "can't answer the question."
    )
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        state = ac.state
        return (
            f"Classification: {state.classification or '(unknown)'}\n\n"
            f"Open hypotheses:\n"
            + list_block(state.hypotheses,
                         lambda h: f"{h.id}: {h.statement} [{h.status}]")
            + "\n\n"
            f"Confirmed findings:\n"
            + list_block(state.confirmed_findings,
                         lambda f: f"{f.id}: {f.statement}")
            + "\n\n"
            f"Evidence so far:\n{evidence_block(state)}\n\n"
            f"The Coordinator's question: {ac.instruction or '(use your judgement)'}\n\n"
            "Write ONE script that answers it — or return an empty script "
            "if none would help."
        )

    def apply(self, ac: AgentContext, response: dict) -> dict:
        """Return the proposal — the engine validates, gates, and runs it."""
        return {
            "language": str(response.get("language") or "").strip().lower(),
            "script": str(response.get("script") or ""),
            "purpose": str(response.get("purpose") or "probe").strip().lower(),
            "rationale": str(response.get("rationale") or "").strip(),
            "reason": str(response.get("reason") or "").strip(),
        }
