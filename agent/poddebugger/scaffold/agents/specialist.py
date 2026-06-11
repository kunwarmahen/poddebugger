"""Specialist — a domain expert spawned mid-run (Phase 15B — HLD §19.3).

Unlike every other agent, a Specialist's system prompt is composed at
runtime: the Coordinator names the specialty and writes the charter, and
both are baked into the prompt — the LLM writing the prompt for the next
LLM call. The composed prompt is persisted to the run workspace
(``specialists/<slug>.md``) so the run stays replayable.

A Specialist is advisory only: an ordinary :class:`Agent` whose output is
Evidence and Leads tagged ``dynamic:<slug>``. It holds no new
capabilities — no probes, no actions, no catalog access.
"""

from __future__ import annotations

import re

from ..prompts import PREAMBLE, context_block, evidence_block, list_block
from .base import Agent, AgentContext

_TEMPLATE = PREAMBLE + """

ROLE: {title} — a domain specialist the Coordinator consulted mid-run.

Your charter for this investigation, written by the Coordinator:
{charter}

You are ADVISORY ONLY. Read the snapshot and the investigation state
through your specialty's lens; your value is interpretation the
generalist team lacks. You cannot run commands, probe, or change
anything. Ground every statement in the material shown — do not invent
facts about the system.

Return JSON:
{{ "observations": ["specialist readings of the evidence — concrete and factual"],
  "leads": ["specific next things worth checking, if any"],
  "assessment": "your one-paragraph specialist judgment" }}"""

_MAX_EVIDENCE_SHOWN = 15


def specialty_slug(specialty: str) -> str:
    """``"PostgreSQL crash analysis"`` -> ``"postgresql-crash-analysis"``."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", specialty).strip("-").lower()
    return slug[:60]


class Specialist(Agent):
    """A per-spawn agent: same machinery as the built-ins, dynamic prompt."""

    # Placeholders satisfy the base-class contract; __init__ overrides both
    # on the instance with the spawned specialty's values.
    name = "Specialist"
    system_prompt = "(composed per instance)"

    def __init__(self, specialty: str, charter: str = ""):
        super().__init__()
        specialty = " ".join(str(specialty).split())
        if not specialty:
            raise ValueError("a Specialist needs a non-empty specialty")
        slug = specialty_slug(specialty)
        if not slug:
            raise ValueError(f"specialty {specialty!r} yields an empty slug")
        self.specialty = specialty
        self.slug = slug
        self.charter = " ".join(str(charter or "").split())
        self.name = f"Specialist:{slug}"
        self.system_prompt = _TEMPLATE.format(
            title=specialty,
            charter=self.charter or f"Advise the team as a {specialty} expert.",
        )

    def build_user_prompt(self, ac: AgentContext) -> str:
        state = ac.state
        return (
            "Workload snapshot:\n\n" + context_block(ac.ctx) + "\n\n"
            f"Evidence so far (most recent {_MAX_EVIDENCE_SHOWN}):\n"
            + evidence_block_tail(state) + "\n\n"
            "Hypotheses under review:\n"
            + list_block(state.hypotheses,
                         lambda h: f"{h.id}: {h.statement} [{h.status}]") + "\n\n"
            "Confirmed findings:\n"
            + list_block(state.confirmed_findings,
                         lambda f: f"{f.id}: {f.statement} ({f.confidence:.0%})")
            + "\n\n"
            f"The Coordinator's question for you: {ac.instruction or '(general read)'}"
        )

    def apply(self, ac: AgentContext, response: dict):
        source = f"dynamic:{self.slug}"
        for obs in response.get("observations") or []:
            ac.add_evidence(str(obs), source=source)
        for lead in response.get("leads") or []:
            ac.add_lead(str(lead), source=source)
        assessment = str(response.get("assessment") or "").strip()
        if assessment:
            ac.add_evidence(f"specialist assessment ({self.specialty})",
                            detail=assessment, source=source)
        ac.record_dispatch(self.name, "consult", assessment[:80])
        return response

    def prompt_document(self) -> str:
        """The audit document persisted to ``specialists/<slug>.md``."""
        return (
            f"# Specialist: {self.specialty}\n\n"
            f"- slug: `{self.slug}`\n"
            f"- charter (Coordinator-written): {self.charter or '(default)'}\n\n"
            "## Composed system prompt\n\n```\n"
            + self.system_prompt + "\n```\n"
        )


def evidence_block_tail(state, limit: int = _MAX_EVIDENCE_SHOWN) -> str:
    """The most recent evidence entries — full history would blow the prompt."""
    return list_block(state.evidence[-limit:],
                      lambda e: f"{e.id}: {e.summary} ({e.source})")


def make_specialist(specialty: str, charter: str = "") -> Specialist:
    """Factory the engine uses — one Specialist per unique specialty slug."""
    return Specialist(specialty, charter)
