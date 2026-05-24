"""Librarian — the web-research agent (Phase 8 — HLD §13).

Formulates ONE generalized search query from the current investigation
state. The engine then runs the query through :func:`search.redact_query`,
issues it via the configured :class:`SearchBackend`, and folds the hits
back into the state as Evidence tagged ``source=web:<domain>`` — a
**lead**, not authority. The Verifier still has to ground any hypothesis
on cluster facts (HLD §13.2).

Like the Remediator, this agent is **opt-in** — it joins the registry only
when ``InvestigationEngine(research_enabled=True)`` (via ``analyze
--research``). Coordinator-dispatchable via ``action: research``.

Output JSON:

    { "query": "CrashLoopBackOff OOMKilled Java 17 OutOfMemoryError",
      "rationale": "look up the OOM-killed Java pattern" }

    { "query": "", "reason": "no novel signature to look up" }
"""

from __future__ import annotations

from ..prompts import PREAMBLE, evidence_block, list_block, sanity_block
from .base import ActionAgent, AgentContext


SYSTEM = PREAMBLE + """

ROLE: Librarian. Frame ONE generalized web search that might surface a known
issue, vendor doc, or post-mortem matching the failure under investigation.

Rules — these are absolute:
- ONE query. Keep it under 12 words.
- Use *signatures*: error class names, status/reason codes, image+version,
  exit codes, library identifiers. NEVER pod names, namespaces, IPs, or
  raw log lines — the engine redacts these out before any HTTP call, and
  including them just wastes tokens.
- If the failure is too generic to look up productively (e.g. "container
  exited with code 0"), return {"query": "", "reason": "..."}.

Return EXACTLY one JSON object:

  { "query": "<the search query>",
    "rationale": "what you hope to find" }

  { "query": "",
    "reason": "why a search wouldn't help here" }"""


class Librarian(ActionAgent):
    """Picks a *generalized* search query from the current state."""

    name = "Librarian"
    action_name = "research"
    description = (
        "Have the Librarian search the web for a known issue / vendor doc / "
        "post-mortem matching this failure. Use when the failure has a clear "
        "signature (error class, vendor product, version) that hasn't been "
        "explained by existing evidence."
    )
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        state = ac.state
        return (
            f"Classification: {state.classification or '(unknown)'}\n\n"
            f"Sanity checks:\n{sanity_block(state)}\n\n"
            f"Confirmed findings:\n"
            + list_block(state.confirmed_findings,
                         lambda f: f"{f.id}: {f.statement}")
            + "\n\n"
            f"Open hypotheses:\n"
            + list_block(state.hypotheses,
                         lambda h: f"{h.id}: {h.statement} [{h.status}]")
            + "\n\n"
            f"Evidence collected so far:\n{evidence_block(state)}\n\n"
            f"Focus: {ac.instruction or '(use your judgement)'}\n\n"
            "Choose ONE generalized query — or return an empty query if "
            "nothing here is worth looking up."
        )

    def apply(self, ac: AgentContext, response: dict) -> dict:
        """Return the proposed query — the engine validates + searches."""
        query = str(response.get("query") or "").strip()
        return {
            "query": query,
            "rationale": str(response.get("rationale") or "").strip(),
            "reason": str(response.get("reason") or "").strip(),
        }
