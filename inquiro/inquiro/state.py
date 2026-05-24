"""InvestigationState — the persistent, structured record of one investigation.

Every agent call assembles the context it needs from this object; the engine
serializes it to the run workspace after each iteration. It is plain data —
no LLM logic lives here.

Claim lifecycle (the framework's spine):

    Lead -> Hypothesis -> [Verifier] -> Finding   (or -> RuledOut)
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

# --- entities --------------------------------------------------------------


@dataclass
class SanityCheck:
    """An invariant the final root-cause explanation must satisfy."""

    id: str
    description: str
    status: str = "unchecked"  # unchecked | pass | fail


@dataclass
class Lead:
    """Something worth investigating — a thread the engine may pull on."""

    id: str
    description: str
    source: str = ""           # logs | events | spec | auditor | ...
    status: str = "open"       # open | pursued | dropped


@dataclass
class Evidence:
    """A collected fact — a probe result, a log excerpt, an observation."""

    id: str
    summary: str
    detail: str = ""
    source: str = ""           # prober:<probe> | analyst | scout | ...


@dataclass
class Hypothesis:
    """A candidate root cause under review."""

    id: str
    statement: str
    test: str = ""             # how to confirm or refute it
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    status: str = "proposed"   # proposed | verified | refuted | inconclusive
    verdict_note: str = ""


@dataclass
class Finding:
    """A confirmed root-cause finding (a promoted Hypothesis)."""

    id: str
    statement: str
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    from_hypothesis: str = ""


@dataclass
class RuledOut:
    """A refuted hypothesis — a recorded dead end, so it is not revisited."""

    id: str
    statement: str
    reason: str = ""
    from_hypothesis: str = ""


@dataclass
class DispatchRecord:
    """One entry in the audit log of agent calls."""

    iteration: int
    role: str
    action: str
    summary: str = ""


# --- state -----------------------------------------------------------------


@dataclass
class InvestigationState:
    """Everything known about one investigation, persisted between iterations."""

    target: str
    # Free-form domain tag — what kind of thing is being investigated.
    # PodDebugger uses "podman" or "kubernetes"; another app might use
    # "logs", "ci-job", etc. Plain string so the framework stays neutral.
    platform: str = ""
    classification: str = ""           # failure category (set by the Scout)
    strategy: str = ""                 # current plan (set/revised by the Planner)
    phase: str = "scouting"            # scouting|investigating|auditing|reporting|done
    iteration: int = 0

    sanity_checks: list[SanityCheck] = field(default_factory=list)
    leads: list[Lead] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    confirmed_findings: list[Finding] = field(default_factory=list)
    ruled_out: list[RuledOut] = field(default_factory=list)
    dispatch_history: list[DispatchRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # Monotonic per-prefix id counters — persisted so ids stay unique on resume.
    seq: dict = field(default_factory=dict)

    # --- id minting ---------------------------------------------------------

    def _mint(self, prefix: str) -> str:
        self.seq[prefix] = self.seq.get(prefix, 0) + 1
        return f"{prefix}{self.seq[prefix]}"

    # --- mutation helpers ---------------------------------------------------

    def add_sanity_check(self, description: str) -> SanityCheck:
        sc = SanityCheck(id=self._mint("S"), description=description)
        self.sanity_checks.append(sc)
        return sc

    def add_lead(self, description: str, source: str = "") -> Lead:
        lead = Lead(id=self._mint("L"), description=description, source=source)
        self.leads.append(lead)
        return lead

    def add_evidence(self, summary: str, detail: str = "", source: str = "") -> Evidence:
        ev = Evidence(id=self._mint("E"), summary=summary, detail=detail, source=source)
        self.evidence.append(ev)
        return ev

    def add_hypothesis(self, statement: str, test: str = "",
                       evidence_ids: list[str] | None = None) -> Hypothesis:
        hyp = Hypothesis(
            id=self._mint("H"), statement=statement, test=test,
            evidence_ids=list(evidence_ids or []),
        )
        self.hypotheses.append(hyp)
        return hyp

    def record_dispatch(self, role: str, action: str, summary: str = "") -> None:
        self.dispatch_history.append(
            DispatchRecord(iteration=self.iteration, role=role, action=action,
                           summary=summary)
        )

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis | None:
        return next((h for h in self.hypotheses if h.id == hypothesis_id), None)

    # --- lifecycle transitions ---------------------------------------------

    def promote(self, hypothesis_id: str) -> Finding:
        """Verified: move a Hypothesis to a confirmed Finding."""
        hyp = self.get_hypothesis(hypothesis_id)
        if hyp is None:
            raise KeyError(f"no hypothesis {hypothesis_id!r}")
        self.hypotheses.remove(hyp)
        finding = Finding(
            id=self._mint("F"), statement=hyp.statement,
            evidence_ids=list(hyp.evidence_ids), confidence=hyp.confidence,
            from_hypothesis=hyp.id,
        )
        self.confirmed_findings.append(finding)
        return finding

    def refute(self, hypothesis_id: str, reason: str) -> RuledOut:
        """Refuted: move a Hypothesis to the ruled-out list (a dead end)."""
        hyp = self.get_hypothesis(hypothesis_id)
        if hyp is None:
            raise KeyError(f"no hypothesis {hypothesis_id!r}")
        self.hypotheses.remove(hyp)
        dead = RuledOut(id=self._mint("R"), statement=hyp.statement,
                        reason=reason, from_hypothesis=hyp.id)
        self.ruled_out.append(dead)
        return dead

    def demote(self, finding_id: str, reason: str) -> RuledOut:
        """Adjudicated against: move a confirmed Finding back to ruled-out."""
        finding = next((f for f in self.confirmed_findings if f.id == finding_id), None)
        if finding is None:
            raise KeyError(f"no finding {finding_id!r}")
        self.confirmed_findings.remove(finding)
        dead = RuledOut(id=self._mint("R"), statement=finding.statement,
                        reason=reason, from_hypothesis=finding.from_hypothesis)
        self.ruled_out.append(dead)
        return dead

    # --- serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "InvestigationState":
        st = cls(target=d["target"], platform=d.get("platform", ""))
        st.classification = d.get("classification", "")
        st.strategy = d.get("strategy", "")
        st.phase = d.get("phase", "scouting")
        st.iteration = d.get("iteration", 0)
        st.notes = list(d.get("notes", []))
        st.seq = dict(d.get("seq", {}))
        st.sanity_checks = [SanityCheck(**x) for x in d.get("sanity_checks", [])]
        st.leads = [Lead(**x) for x in d.get("leads", [])]
        st.evidence = [Evidence(**x) for x in d.get("evidence", [])]
        st.hypotheses = [Hypothesis(**x) for x in d.get("hypotheses", [])]
        st.confirmed_findings = [Finding(**x) for x in d.get("confirmed_findings", [])]
        st.ruled_out = [RuledOut(**x) for x in d.get("ruled_out", [])]
        st.dispatch_history = [DispatchRecord(**x) for x in d.get("dispatch_history", [])]
        return st

    def render(self) -> str:
        """A human-legible Markdown view — committed alongside state.json so the
        per-iteration git history reads cleanly."""
        lines = [
            f"# Investigation — {self.target}",
            "",
            f"- platform: {self.platform}",
            f"- iteration: {self.iteration}",
            f"- phase: {self.phase}",
            f"- classification: {self.classification or '(unset)'}",
            "",
            f"## Strategy\n\n{self.strategy or '(none yet)'}",
        ]

        def section(title, items, fmt):
            lines.append(f"\n## {title} ({len(items)})")
            if items:
                lines.extend(f"- {fmt(i)}" for i in items)
            else:
                lines.append("- (none)")

        section("Sanity checks", self.sanity_checks,
                lambda s: f"[{s.id}] {s.description} — {s.status}")
        section("Leads", self.leads,
                lambda l: f"[{l.id}] {l.description} ({l.source}) — {l.status}")
        section("Hypotheses", self.hypotheses,
                lambda h: f"[{h.id}] {h.statement} — {h.status} ({h.confidence:.0%})")
        section("Confirmed findings", self.confirmed_findings,
                lambda f: f"[{f.id}] {f.statement} ({f.confidence:.0%})")
        section("Ruled out", self.ruled_out,
                lambda r: f"[{r.id}] {r.statement} — {r.reason}")
        section("Evidence", self.evidence,
                lambda e: f"[{e.id}] {e.summary} ({e.source})")
        return "\n".join(lines) + "\n"
