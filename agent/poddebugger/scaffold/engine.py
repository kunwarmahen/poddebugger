"""Investigation engine — the core agent loop (HLD §11, Stage 9B).

Orchestrates a registry of agents — built-ins from
:mod:`poddebugger.scaffold.agents` plus any user-supplied extras — over a
shared :class:`InvestigationState`. Each agent is a fresh-context
``complete()`` call returning JSON; the engine builds the per-call
``AgentContext``, runs the call (with the per-role LLM and the retry/degrade
layer), and lets the agent's ``apply()`` mutate the state.

The output is a plain :class:`~poddebugger.models.Diagnosis` — the existing
contract — so the CLI, watcher, and operator are unchanged.

Resilience: no single agent can derail the run. A failed call is retried,
then degraded — the engine carries on. The Reporter has a deterministic
fallback if it cannot speak.
"""

from __future__ import annotations

import importlib
import os
import sys
from typing import Iterable

from .. import remediation
from ..analyzer import _extract_json, _to_diagnosis
from ..collector import collect
from ..llm.base import LLMClient, LLMError
from ..models import Diagnosis, Fix, WorkloadRef
from ..providers.base import ContainerPlatform, ProviderError
from .agents import (
    ActionAgent,
    Agent,
    AgentContext,
    Librarian,
    Remediator,
    built_in_agents,
    make_specialist,
    specialty_slug,
)
from .llms import AgentLLMs
from .probes import PROBE_MENU, run_probe
from .search import NoopBackend, SearchBackend, SearchError, get_backend, redact_query
from .state import Finding, Hypothesis, InvestigationState
from .workspace import Workspace

_PROBE_NAMES = {p.name for p in PROBE_MENU}

DEFAULT_MAX_ITERATIONS = 10
DEFAULT_MAX_LLM_CALLS = 48
_PROBE_OUTPUT_CAP = 6000  # chars of probe output recorded as evidence
# Once a finding explains the failure, allow only this many more iterations.
_EXTRA_ITERS_AFTER_FINDING = 2


def _ref_to_dict(ref: WorkloadRef) -> dict:
    """Serialize a WorkloadRef for the undo-save payload."""
    return {
        "name": ref.name,
        "namespace": ref.namespace,
        "container": ref.container,
        "platform": ref.platform,
    }


def _clamp(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def load_agents_from_env(var: str = "PODDEBUGGER_EXTRA_AGENTS") -> list[Agent]:
    """Import and instantiate the agents listed in a comma-separated env var.

    Each entry is a dotted Python path (``mypkg.module.MyAgent``). A path that
    fails to import or instantiate is skipped with a warning on stderr — one
    misconfigured plugin must not block the rest of the run.
    """
    raw = os.environ.get(var, "").strip()
    if not raw:
        return []
    loaded: list[Agent] = []
    for path in (p.strip() for p in raw.split(",") if p.strip()):
        module_name, _, attr = path.rpartition(".")
        if not module_name or not attr:
            print(f"[scaffold] skipping bad agent path {path!r}", file=sys.stderr)
            continue
        try:
            mod = importlib.import_module(module_name)
            cls = getattr(mod, attr)
            instance = cls()
        except Exception as exc:  # noqa: BLE001 — third-party agents may raise anything
            print(f"[scaffold] could not load agent {path!r}: {exc}", file=sys.stderr)
            continue
        if not isinstance(instance, Agent):
            print(
                f"[scaffold] {path!r} is not an Agent subclass — skipping",
                file=sys.stderr,
            )
            continue
        loaded.append(instance)
    return loaded


class InvestigationEngine:
    """Runs one investigation: collect -> scout -> plan -> loop -> audit -> report."""

    def __init__(
        self,
        provider: ContainerPlatform,
        llm: LLMClient | AgentLLMs,
        *,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
        log_lines: int = 200,
        workspace_base=None,
        verbose: bool = False,
        extra_agents: Iterable[Agent] | None = None,
        remediation_enabled: bool = False,
        research_enabled: bool = False,
        search_backend: SearchBackend | None = None,
        gate=None,
        context: dict | None = None,
        max_remediation_attempts: int = 3,
        learning_enabled: bool = False,
        experience_store=None,
        specialists_enabled: bool = False,
        max_specialists: int = 2,
        prompt_pack: dict | None = None,
    ):
        self.provider = provider
        # Accept either a single client (all roles) or a per-role resolver.
        self.llms = llm if isinstance(llm, AgentLLMs) else AgentLLMs.uniform(llm)
        self.max_iterations = max_iterations
        self.max_llm_calls = max_llm_calls
        self.log_lines = log_lines
        self.workspace_base = workspace_base
        self.verbose = verbose
        self.remediation_enabled = remediation_enabled
        self.research_enabled = research_enabled
        # Phase 13C — values the user supplied that the system can't infer
        # (db password, correct image name, …). Threaded into the Remediator.
        self.context: dict = dict(context or {})
        # Phase 13A — cap on how many fixes the loop will try before giving up.
        self.max_remediation_attempts = max_remediation_attempts
        # Phase 15A — cross-run experience memory. Recalled records are prompt
        # context only; the catalog + gate stay the capability boundary.
        # Imported lazily: experience pulls in scaffold.search, and this module
        # is imported during the scaffold package's own initialization.
        self.learning_enabled = learning_enabled
        self._exp_store = experience_store
        if learning_enabled and self._exp_store is None:
            from ..experience import ExperienceStore

            self._exp_store = ExperienceStore()
        # Phase 15B — on-the-fly specialist agents. A Specialist is advisory
        # only (ordinary Agent, no probes/actions), so enabling this adds no
        # new capabilities — just dynamically composed prompts, budgeted per
        # run and persisted to the workspace for audit.
        self.specialists_enabled = specialists_enabled
        self.max_specialists = max_specialists
        self._specialists: dict[str, Agent] = {}  # slug -> spawned instance
        # The Librarian formulates queries; this backend runs them. Default
        # noop keeps the engine air-gap safe (HLD §13.3).
        self.search_backend: SearchBackend = (
            search_backend if search_backend is not None else NoopBackend()
        )
        # Phase 11: the approval gate for any side-effecting probe (and any
        # mutation, if a future engine path needs it). Defaults to None —
        # `_do_probe` then runs probes without a gate, preserving legacy
        # test behavior. The CLI always supplies one.
        self.gate = gate

        # Agent registry: built-ins first, then `extra_agents`, then env-loaded.
        # Later registrations win on name conflict — callers can replace
        # a built-in by registering another agent with the same name.
        self._agents: dict[str, Agent] = {}
        self._action_agents: dict[str, ActionAgent] = {}
        for agent in built_in_agents():
            self._register(agent)
        # The Remediator is opt-in (Phase 7B). It is never reachable from the
        # Coordinator loop — the engine invokes it explicitly via
        # ``propose_remediation`` after the verdict.
        if remediation_enabled:
            self._register(Remediator())
        # The Librarian is also opt-in (Phase 8). When enabled, it IS
        # Coordinator-dispatchable as the ``research`` action.
        if research_enabled:
            self._register(Librarian())
        for agent in (extra_agents or ()):
            self._register(agent)
        for agent in load_agents_from_env():
            self._register(agent)
        # Phase 15C — prompt pack: per-role system-prompt replacements
        # (already validated by promptpack.load_pack). Instance-attribute
        # override, so other engines / fresh agents keep the built-ins.
        # Roles not registered in THIS engine (e.g. Remediator without
        # --fix) are simply not consulted.
        for role, text in (prompt_pack or {}).items():
            if role in self._agents:
                self._agents[role].system_prompt = text

        # populated during a run
        self.state: InvestigationState | None = None
        self.workspace: Workspace | None = None
        self._calls = 0
        self._ref: WorkloadRef | None = None
        self._probes_run: set[str] = set()
        self._queries_run: set[str] = set()
        # Phase 15A — the failure signature captured at investigate() time,
        # BEFORE any fix mutates the workload (a post-recovery collect would
        # describe a healthy container).
        self._signature: dict | None = None

    # --- registry -----------------------------------------------------------

    def _register(self, agent: Agent) -> None:
        """Add an agent to the registry. Later registrations win on name."""
        self._agents[agent.name] = agent
        if isinstance(agent, ActionAgent):
            self._action_agents[agent.action_name] = agent

    def _action_menu(self) -> list[tuple[str, str]]:
        """The Coordinator's dynamic action menu — every registered
        ActionAgent contributes one ``(action_name, description)`` entry."""
        menu = [(a.action_name, a.description) for a in self._action_agents.values()]
        if self.specialists_enabled:
            menu.append((
                "specialist",
                "consult a domain specialist (advisory only): put the "
                'specialty in "target" (e.g. "PostgreSQL crash analysis") '
                'and write its charter/question in "instruction"',
            ))
        return menu

    # --- helpers ------------------------------------------------------------

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[scaffold] {message}", file=sys.stderr)

    def _budget_ok(self) -> bool:
        return self._calls < self.max_llm_calls

    def _call(self, role: str, system: str, user: str, attempts: int = 2) -> dict:
        """One agent turn: complete() + JSON parse, retrying transient failures.

        Uses the LLM configured for ``role`` (per-agent override or default).
        Raises LLMError only if every attempt fails (caught by ``_try_call``).
        """
        llm = self.llms.for_role(role)
        last: LLMError | None = None
        for i in range(attempts):
            nudge = "" if i == 0 else "\n\nReturn ONLY a valid JSON object — no prose."
            try:
                raw = llm.complete(system, user + nudge)
            except LLMError as exc:
                last = exc
                self._calls += 1
                continue
            self._calls += 1
            try:
                return _extract_json(raw)
            except LLMError as exc:
                last = exc
        raise last  # type: ignore[misc]

    def _try_call(self, system: str, user: str, label: str) -> dict | None:
        """``_call`` that degrades to None instead of raising."""
        try:
            return self._call(label, system, user)
        except LLMError as exc:
            if self.state is not None:
                self.state.notes.append(f"{label} agent failed: {exc}")
            self._log(f"{label}: FAILED — {exc}")
            return None

    def _commit(self, message: str) -> None:
        if self.workspace and self.state:
            self.workspace.commit(self.state, message)

    # --- generic agent dispatch --------------------------------------------

    def _make_context(self, ctx, *, subject=None, instruction="", extras=None) -> AgentContext:
        return AgentContext(
            provider=self.provider, ref=self._ref, state=self.state, ctx=ctx,
            llm=self.llms.for_role("default"),  # overridden below; here only for typing
            subject=subject, instruction=instruction, extras=extras or {},
        )

    def _run(self, agent_name: str, ctx, *, subject=None, instruction="", extras=None):
        """Dispatch one agent. Returns the agent's apply() result or None
        if the call ultimately failed (the agent never gets to mutate state)."""
        agent = self._agents.get(agent_name)
        if agent is None:
            raise KeyError(f"no agent registered with name {agent_name!r}")
        ac = AgentContext(
            provider=self.provider, ref=self._ref, state=self.state, ctx=ctx,
            llm=self.llms.for_role(agent.name),
            subject=subject, instruction=instruction, extras=extras or {},
        )
        user = agent.build_user_prompt(ac)
        data = self._try_call(agent.system_prompt, user, agent.name)
        if data is None:
            return None
        return agent.apply(ac, data)

    # --- specialized control flow ------------------------------------------

    def _verify(self, ctx, hyp: Hypothesis, allow_probe: bool = True) -> None:
        """Per-hypothesis verification, with one grounded re-verify on probe."""
        result = self._run("Verifier", ctx, subject=hyp)
        if result is None:
            hyp.status = "inconclusive"
            return
        verdict = result["verdict"]
        hyp.confidence = _clamp(result["confidence"])
        hyp.verdict_note = result["note"]

        if verdict == "VERIFIED":
            hyp.status = "verified"
            self.state.promote(hyp.id)
            self.state.record_dispatch("Verifier", "VERIFIED", hyp.id)
            self._log(f"verifier: {hyp.id} VERIFIED")
            return
        if verdict == "REFUTED":
            hyp.status = "refuted"
            self.state.refute(hyp.id, hyp.verdict_note)
            self.state.record_dispatch("Verifier", "REFUTED", hyp.id)
            self._log(f"verifier: {hyp.id} REFUTED")
            return

        hyp.status = "inconclusive"
        self.state.record_dispatch("Verifier", "INCONCLUSIVE", hyp.id)
        self._log(f"verifier: {hyp.id} INCONCLUSIVE")

        probe = result["suggested_probe"]
        if allow_probe and probe in _PROBE_NAMES and self._budget_ok():
            self._do_probe(probe, f"settle {hyp.id}")
            self._verify(ctx, hyp, allow_probe=False)

    def _do_probe(self, probe_name: str, goal: str) -> None:
        """Run a whitelisted probe — dedup, capture as Evidence, log."""
        if probe_name in self._probes_run:
            self.state.record_dispatch("Prober", probe_name, "skipped — already run")
            self._log(f"prober: {probe_name} already run, skipped")
            return
        self._probes_run.add(probe_name)
        try:
            output = run_probe(probe_name, self.provider, self._ref, gate=self.gate)
        except ProviderError as exc:
            self.state.add_evidence(f"probe '{probe_name}' could not run",
                                    detail=str(exc), source=f"prober:{probe_name}")
            self.state.record_dispatch("Prober", probe_name, f"failed: {exc}")
            self._log(f"prober: {probe_name} failed — {exc}")
            return
        self.state.add_evidence(f"probe '{probe_name}' result — {goal}",
                                detail=output[:_PROBE_OUTPUT_CAP],
                                source=f"prober:{probe_name}")
        self.state.record_dispatch("Prober", probe_name, goal)
        self._log(f"prober: ran {probe_name}")

    def _dispatch_action(self, action: str, ctx, target: str, instruction: str) -> None:
        """Route a Coordinator-chosen action to its ActionAgent and any
        engine-level follow-up (per-hypothesis verify, probe execution)."""
        if action == "analyze":
            new_hyps = self._run("Analyst", ctx, instruction=instruction) or []
            self._log(f"analyst: {len(new_hyps)} new hypotheses")
            for hyp in new_hyps:
                if self._budget_ok():
                    self._verify(ctx, hyp)
            lead = next((l for l in self.state.leads if l.id == target), None)
            if lead:
                lead.status = "pursued"
            return

        if action == "probe":
            probe_name = self._run("Prober", ctx, instruction=instruction)
            if not probe_name:
                return
            if probe_name not in _PROBE_NAMES:
                self.state.notes.append(f"Prober chose an unknown probe: {probe_name!r}")
                self.state.record_dispatch("Prober", "invalid", probe_name)
                return
            self._do_probe(probe_name, instruction or "gather evidence")
            return

        if action == "research" and "Librarian" in self._agents:
            self._do_research(ctx, instruction)
            return

        if action == "specialist" and self.specialists_enabled:
            self._do_specialist(ctx, target, instruction)
            return

        # Any other registered ActionAgent: dispatch generically. Its apply()
        # is responsible for recording evidence / dispatch records itself.
        if action in self._action_agents:
            self._run(self._action_agents[action].name, ctx, instruction=instruction)

    def _do_research(self, ctx, instruction: str) -> None:
        """Run the Librarian, redact its query, hit the backend, record hits.

        Modeled on _do_probe (HLD §13.1): the LLM picks WHAT to search for;
        the engine does the actual network call so the safety boundary —
        :func:`search.redact_query` — applies in exactly one place.
        """
        proposal = self._run("Librarian", ctx, instruction=instruction)
        if not proposal or not isinstance(proposal, dict):
            return
        query = (proposal.get("query") or "").strip()
        if not query:
            reason = proposal.get("reason") or "Librarian declined to search"
            self.state.record_dispatch("Librarian", "skip", reason[:80])
            self._log(f"librarian: skipped — {reason}")
            return
        redacted = redact_query(query)
        if not redacted:
            self.state.record_dispatch("Librarian", "skip",
                                       "redacted query was empty")
            return
        # Skip duplicate queries — same as the probe dedup.
        if redacted in self._queries_run:
            self.state.record_dispatch("Librarian", redacted,
                                       "skipped — already searched")
            self._log(f"librarian: {redacted!r} already run, skipped")
            return
        self._queries_run.add(redacted)

        if isinstance(self.search_backend, NoopBackend):
            self.state.notes.append(
                "research requested but PODDEBUGGER_SEARCH_BACKEND is unset — "
                "configure a backend (e.g. duckduckgo) to enable web search"
            )
            self.state.record_dispatch("Librarian", redacted, "noop backend")
            self._log("librarian: noop backend, no results")
            return

        try:
            hits = self.search_backend.search(redacted, max_results=5)
        except SearchError as exc:
            self.state.add_evidence(
                f"web search for {redacted!r} could not run",
                detail=str(exc), source="librarian:error",
            )
            self.state.record_dispatch("Librarian", redacted, f"failed: {exc}")
            self._log(f"librarian: search failed — {exc}")
            return

        if not hits:
            self.state.record_dispatch("Librarian", redacted, "no results")
            self._log(f"librarian: {redacted!r} -> 0 results")
            return

        for hit in hits:
            summary = f"{hit.title}".strip() or hit.url
            detail = (hit.snippet + ("\n" + hit.url if hit.url else "")).strip()
            self.state.add_evidence(
                summary, detail=detail[:_PROBE_OUTPUT_CAP],
                source=f"web:{hit.domain()}",
            )
        self.state.record_dispatch(
            "Librarian", redacted, f"{len(hits)} hits — {hits[0].domain()}",
        )
        self._log(f"librarian: {redacted!r} -> {len(hits)} hits")

    def _do_specialist(self, ctx, specialty: str, instruction: str) -> None:
        """Spawn (or re-consult) a domain specialist (Phase 15B — HLD §19.3).

        The Coordinator names the specialty (``target``) and writes the
        charter (``instruction``); both are composed into the new agent's
        system prompt. Budgeted per run; re-consulting an existing
        specialist is free. The composed prompt is persisted to the run
        workspace so the spawn is auditable and the run replayable.
        """
        specialty = (specialty or "").strip()
        if not specialty:
            self.state.record_dispatch("Specialist", "skip",
                                       "no specialty named in target")
            self._log("specialist: skipped — no specialty in target")
            return
        slug = specialty_slug(specialty)
        agent = self._specialists.get(slug)
        if agent is None:
            if len(self._specialists) >= self.max_specialists:
                self.state.notes.append(
                    f"specialist budget ({self.max_specialists}) exhausted — "
                    f"not spawning {specialty!r}"
                )
                self.state.record_dispatch("Specialist", "skip",
                                           f"budget exhausted: {specialty[:60]}")
                self._log(f"specialist: budget exhausted, not spawning {slug}")
                return
            try:
                agent = make_specialist(specialty, charter=instruction)
            except ValueError as exc:
                self.state.record_dispatch("Specialist", "skip", str(exc)[:80])
                self._log(f"specialist: {exc}")
                return
            self._specialists[slug] = agent
            self._register(agent)
            self._persist_specialist_prompt(agent)
            self._log(f"specialist spawned: {agent.name} "
                      f"({len(self._specialists)}/{self.max_specialists})")
        else:
            self._log(f"specialist: re-consulting {agent.name}")
        self._run(agent.name, ctx, instruction=instruction)

    def _persist_specialist_prompt(self, agent) -> None:
        """Write the composed prompt into the workspace; the next iteration
        commit (`git add -A`) captures it. Best-effort."""
        if self.workspace is None:
            return
        try:
            sub = self.workspace.path / "specialists"
            sub.mkdir(parents=True, exist_ok=True)
            (sub / f"{agent.slug}.md").write_text(agent.prompt_document())
        except OSError as exc:
            self.state.notes.append(f"could not persist specialist prompt: {exc}")

    def _run_audit_chain(self, ctx) -> None:
        """Auditor sweep, then route each finding-critique to the Adjudicator."""
        state = self.state
        if not state.confirmed_findings:
            return
        state.phase = "auditing"
        result = self._run("Auditor", ctx)
        if result is None:
            return
        for crit in result.get("critiques", []) or []:
            if not isinstance(crit, dict):
                continue
            concern = str(crit.get("concern", "")).strip()
            if not concern:
                continue
            target = str(crit.get("target", "")).strip()
            finding = next((f for f in state.confirmed_findings if f.id == target), None)
            if finding is None:
                state.notes.append(f"audit concern ({target or 'strategy'}): {concern}")
                state.record_dispatch("Auditor", "critique", concern[:80])
                self._log(f"auditor: critique on {target or 'strategy'}")
                continue
            ruling = self._run("Adjudicator", ctx, subject=(finding, concern))
            if ruling is None:
                continue
            if ruling["ruling"] == "uphold":
                state.demote(finding.id, f"adjudicated: {ruling['reason']}")
                state.record_dispatch("Adjudicator", "upheld — demoted", finding.id)
                self._log(f"adjudicator: {finding.id} demoted")
            else:
                state.record_dispatch("Adjudicator", "dismissed critique", finding.id)
                self._log(f"adjudicator: {finding.id} critique dismissed")

    def _fallback_diagnosis(self, state: InvestigationState) -> Diagnosis:
        """Deterministic Diagnosis built from the state — used when the
        Reporter agent fails, so the investigation still yields a result."""
        evidence = [e.summary for e in state.evidence]
        if state.confirmed_findings:
            top = max(state.confirmed_findings, key=lambda f: f.confidence)
            return Diagnosis(
                summary=top.statement, root_cause=top.statement,
                confidence=top.confidence, evidence=evidence,
                suggested_fixes=[], needs_deep_inspection=False,
            )
        if state.hypotheses:
            top = max(state.hypotheses, key=lambda h: h.confidence)
            return Diagnosis(
                summary=top.statement, root_cause=top.statement,
                confidence=round(top.confidence * 0.5, 2), evidence=evidence,
                suggested_fixes=[], needs_deep_inspection=True,
            )
        return Diagnosis(
            summary="investigation did not reach a conclusion",
            root_cause=f"{state.classification or 'failure'} — the agents could "
                       "not confirm a root cause; review the evidence directly",
            confidence=0.0, evidence=evidence,
            suggested_fixes=[Fix(action="inspect the workload manually",
                                 rationale="automated investigation was inconclusive",
                                 risk="low")],
            needs_deep_inspection=True,
        )

    # --- remediation proposal (Phase 7B) ------------------------------------

    def propose_remediation(self, diagnosis: Diagnosis,
                            attempts: list | None = None) -> dict | None:
        """Ask the Remediator to fill a typed catalog form for this diagnosis.

        Returns a dict the CLI / operator stores on
        :attr:`Diagnosis.proposed_remediation`. Shape:

        * Successful proposal with a validated plan::

              {"action": "<name>", "params": {...}, "risk": "low|medium|high",
               "rationale": "...", "expected_effect": "...", "confidence": 0.0,
               "plan": <Plan-as-dict>, "reversal": {...},
               "validated": True}

        * ``"action": "none"`` — no catalog action fits (also returned if the
          model proposes one but it fails catalog validation; ``validation_error``
          is set in that case so the CLI can show it to a human). May carry
          ``needs_context`` (a list of missing-value requests — Phase 13C).

        ``attempts`` is the list of prior fixes that did not recover the
        workload (Phase 13A), shown to the agent so it tries something
        different.

        Returns ``None`` if the Remediator was not registered (``--fix`` not
        in effect) or its call failed entirely.
        """
        if not self.remediation_enabled or "Remediator" not in self._agents:
            return None
        if self.state is None or self._ref is None:
            return None
        ctx = collect(self.provider, self._ref, log_lines=self.log_lines)
        extras = {
            "diagnosis": {
                "summary": diagnosis.summary,
                "root_cause": diagnosis.root_cause,
                "confidence": diagnosis.confidence,
            },
            "platform": self._ref.platform,
            "context": self.context,
            "attempts": attempts or [],
        }
        result = self._run("Remediator", ctx, extras=extras)
        if not result:
            return None
        action = result.get("action", "none")
        if action == "none":
            self.state.record_dispatch("Remediator", "none", result.get("reason", ""))
            self._commit("remediator: no catalog action fits")
            return result

        # The catalog is the safety boundary — validate before letting any
        # downstream code treat the proposal as executable.
        try:
            cleaned = remediation.parse_params(action, result.get("params") or {})
            plan = remediation.make_plan(self.provider, self._ref, action, cleaned)
        except remediation.RemediationError as exc:
            self.state.notes.append(
                f"remediator: proposal failed catalog validation: {exc}"
            )
            self.state.record_dispatch("Remediator", "rejected", str(exc)[:80])
            self._commit(f"remediator: proposal rejected ({exc})")
            return {
                "action": "none",
                "reason": "model proposal failed catalog validation",
                "validation_error": str(exc),
                "rejected_proposal": result,
            }

        self.state.record_dispatch(
            "Remediator", action, f"risk={plan.risk}"
        )
        self._commit(f"remediator: proposed {action} ({plan.risk} risk)")
        from dataclasses import asdict

        return {
            "action": action,
            "params": dict(cleaned),
            "risk": plan.risk,
            "rationale": result.get("rationale", ""),
            "expected_effect": result.get("expected_effect", ""),
            "confidence": result.get("confidence", 0.0),
            "plan": asdict(plan),
            "reversal": plan.reversal or None,
            "validated": True,
        }

    # --- adaptive remediation loop (Phase 13A) ------------------------------

    def apply_remediation(self, proposal: dict, *, max_risk: str = "low",
                          verify_wait: int = remediation.DEFAULT_VERIFY_WAIT) -> dict:
        """Execute one validated proposal under the gate + risk tier, then
        verify recovery. Mirrors what the CLI used to do inline, but lives in
        the engine so the loop can reuse it.

        Returns a dict with ``executed`` / ``skipped`` / ``verification`` /
        ``reversal`` / ``saved_to`` keys.
        """
        risk = proposal.get("risk", "medium")
        ranking = {"low": 0, "medium": 1, "high": 2}
        if ranking.get(risk, 99) > ranking.get(max_risk, 0):
            return {"executed": False, "skipped": True,
                    "action": proposal["action"],
                    "reason": f"risk={risk} exceeds max-risk={max_risk}"}
        try:
            params = remediation.parse_params(
                proposal["action"], proposal.get("params") or {})
            plan = remediation.make_plan(self.provider, self._ref,
                                         proposal["action"], params)
        except (remediation.RemediationError, ProviderError) as exc:
            return {"executed": False, "skipped": True,
                    "action": proposal["action"], "reason": str(exc)}

        baseline = (remediation.capture_baseline(self.provider, self._ref)
                    if verify_wait > 0 else {})
        result = remediation.execute(self.provider, self._ref, plan, gate=self.gate)
        verification = None
        if result.executed and verify_wait > 0:
            verification = remediation.verify_recovery(
                self.provider, self._ref, baseline, plan.action, verify_wait)
        saved_path = None
        if result.executed:
            saved_path = str(remediation.save_for_undo(self._ref, {
                "action": result.action, "executed": True,
                "result": result.result, "plan": result.plan,
                "reversal": result.reversal,
                "target": _ref_to_dict(self._ref),
                "verification": verification,
            }))
            self.state.record_dispatch(
                "Remediator", f"applied {result.action}",
                (verification or {}).get("outcome", "applied"))
            self._commit(f"remediation applied: {result.action}")
        return {
            "executed": result.executed,
            "skipped": False,
            "action": result.action,
            "result": result.result,
            "plan": result.plan,
            "reversal": result.reversal,
            "verification": verification,
            "saved_to": saved_path,
        }

    def remediate(self, diagnosis: Diagnosis, *, apply: bool = False,
                  max_risk: str = "low",
                  verify_wait: int = remediation.DEFAULT_VERIFY_WAIT) -> dict:
        """Drive the remediation loop, then (Phase 15A) record the verified
        outcome to the experience store when learning is enabled."""
        result = self._remediate_loop(diagnosis, apply=apply,
                                      max_risk=max_risk, verify_wait=verify_wait)
        if apply and self.learning_enabled and self._exp_store is not None:
            self._record_experience(diagnosis, result)
        return result

    def _remediate_loop(self, diagnosis: Diagnosis, *, apply: bool = False,
                        max_risk: str = "low",
                        verify_wait: int = remediation.DEFAULT_VERIFY_WAIT) -> dict:
        """Drive the full propose → apply → verify → replan loop (Phase 13A).

        With ``apply=False`` this proposes a single fix and stops (the
        ``analyze --fix`` preview behavior). With ``apply=True`` it keeps
        trying — up to ``max_remediation_attempts`` — until the workload
        recovers, the agent gives up (``action=none``), the gate refuses, or
        a needed context value is missing.

        Returns::

            {"outcome": "resolved" | "unresolved" | "proposed" | "refused"
                        | "needs_context" | "no_action",
             "attempts": [ {proposal, applied, verification}, ... ],
             "final_proposal": <last proposal dict>,
             "needs_context": [...]   # when outcome == needs_context
            }
        """
        attempts: list[dict] = []
        last_proposal: dict | None = None

        for attempt_no in range(1, self.max_remediation_attempts + 1):
            # Show the Remediator what already failed so it varies its choice.
            failed = [{
                "action": a["proposal"].get("action"),
                "params": a["proposal"].get("params"),
                "outcome": ((a.get("applied") or {}).get("verification") or {}).get(
                    "outcome", "applied"),
                "reason": ((a.get("applied") or {}).get("verification") or {}).get(
                    "reason", "") or (a.get("applied") or {}).get("reason", ""),
            } for a in attempts]

            proposal = self.propose_remediation(diagnosis, attempts=failed)
            last_proposal = proposal
            if proposal is None:
                return {"outcome": "no_action", "attempts": attempts,
                        "final_proposal": None}

            action = proposal.get("action", "none")
            if action == "none":
                nc = proposal.get("needs_context")
                if nc:
                    return {"outcome": "needs_context", "attempts": attempts,
                            "final_proposal": proposal, "needs_context": nc}
                # The agent honestly has no (further) idea.
                outcome = "unresolved" if attempts else "no_action"
                return {"outcome": outcome, "attempts": attempts,
                        "final_proposal": proposal}

            if not proposal.get("validated"):
                # validation failed in propose_remediation
                return {"outcome": "no_action", "attempts": attempts,
                        "final_proposal": proposal}

            if not apply:
                return {"outcome": "proposed", "attempts": attempts,
                        "final_proposal": proposal}

            applied = self.apply_remediation(
                proposal, max_risk=max_risk, verify_wait=verify_wait)
            attempts.append({"proposal": proposal, "applied": applied})

            if applied.get("skipped"):
                # Risk tier or validation stopped us before execution — the
                # loop can't proceed.
                return {"outcome": "blocked", "attempts": attempts,
                        "final_proposal": proposal}

            if not applied.get("executed"):
                # The gate refused, or the action couldn't run. A gate refusal
                # is the user's explicit "no" — stop, don't keep trying. An
                # execution error is a dead fix — let the team try another.
                if "approval gate" in str(applied.get("result", "")):
                    return {"outcome": "refused", "attempts": attempts,
                            "final_proposal": proposal}
                self.state.add_evidence(
                    f"remediation '{proposal.get('action')}' failed to execute",
                    detail=str(applied.get("result", "")),
                    source=f"remediator:{proposal.get('action')}:failed",
                )
                if attempt_no < self.max_remediation_attempts:
                    self._replan_after_failed_fix()
                    diagnosis = self._refresh_diagnosis() or diagnosis
                continue

            verification = applied.get("verification") or {}
            outcome = verification.get("outcome")
            if outcome == "recovered":
                return {"outcome": "resolved", "attempts": attempts,
                        "final_proposal": proposal}

            # still-failing / unknown / skipped → record as evidence and
            # let the whole team reconsider before the next attempt.
            self._record_failed_fix(proposal, verification)
            if attempt_no < self.max_remediation_attempts:
                self._replan_after_failed_fix()
                # Re-report so the next proposal reflects the new picture.
                diagnosis = self._refresh_diagnosis() or diagnosis

        return {"outcome": "unresolved", "attempts": attempts,
                "final_proposal": last_proposal}

    # --- cross-run experience memory (Phase 15A) -----------------------------

    def _recall_experience(self, ctx) -> None:
        """Land the most similar past incidents as Evidence. Best-effort —
        a broken store degrades to a note, never fails the run."""
        from .. import experience

        try:
            self._signature = experience.signature_from_context(
                ctx, self.state.classification)
            matches = self._exp_store.find_similar(self._signature)
        except Exception as exc:  # noqa: BLE001 — the store is non-critical
            self.state.notes.append(f"experience recall failed: {exc}")
            self._log(f"historian: recall failed — {exc}")
            return
        for rec, _score in matches:
            self.state.add_evidence(rec.recall_summary(),
                                    detail=rec.recall_detail(),
                                    source=f"experience:{rec.id}")
        self.state.record_dispatch("Historian", "recall",
                                   f"{len(matches)} similar past incidents")
        self._log(f"historian: recalled {len(matches)} past incidents")
        if matches:
            self._commit(f"historian: recalled {len(matches)} past incidents")

    def _record_experience(self, diagnosis: Diagnosis, result: dict) -> None:
        """Persist a verified remediation outcome. Only `resolved` and
        `unresolved` carry information worth remembering."""
        from .. import experience

        outcome = result.get("outcome")
        if outcome not in ("resolved", "unresolved"):
            return
        try:
            sig = self._signature
            if sig is None:
                # Standalone remediate() call — collect now. Less faithful
                # (a resolved workload reads healthy) but better than nothing.
                ctx = collect(self.provider, self._ref, log_lines=self.log_lines)
                sig = experience.signature_from_context(
                    ctx, self.state.classification if self.state else "")
            attempts = [{
                "action": a["proposal"].get("action"),
                "params": a["proposal"].get("params"),
                "outcome": ((a.get("applied") or {}).get("verification") or {}
                            ).get("outcome", "applied"),
            } for a in result.get("attempts") or []]
            rec = experience.make_record(
                sig, summary=diagnosis.summary, root_cause=diagnosis.root_cause,
                attempts=attempts,
                outcome="recovered" if outcome == "resolved" else "unresolved")
            saved = self._exp_store.save(rec)
        except Exception as exc:  # noqa: BLE001 — the store is non-critical
            if self.state is not None:
                self.state.notes.append(f"experience record failed: {exc}")
            self._log(f"historian: record failed — {exc}")
            return
        if saved:
            if self.state is not None:
                self.state.record_dispatch("Historian", "record", rec.id)
            self._log(f"historian: recorded {rec.id} ({rec.outcome}) -> {saved}")

    def _record_failed_fix(self, proposal: dict, verification: dict) -> None:
        outcome = verification.get("outcome", "unknown")
        reason = verification.get("reason", "")
        self.state.add_evidence(
            f"remediation '{proposal.get('action')}' did not recover the "
            f"workload ({outcome})",
            detail=reason,
            source=f"remediator:{proposal.get('action')}:{outcome}",
        )
        self._log(f"remediation '{proposal.get('action')}' → {outcome}; replanning")

    def _replan_after_failed_fix(self) -> None:
        """Run a short continuation of the Coordinator loop so the team can
        revise hypotheses given the new evidence, then re-audit."""
        if self.state is None or self._ref is None:
            return
        ctx = collect(self.provider, self._ref, log_lines=self.log_lines)
        self.state.phase = "investigating"
        extra_rounds = 2
        for _ in range(extra_rounds):
            if not self._budget_ok():
                break
            self.state.iteration += 1
            coord = self._run("Coordinator", ctx, extras={
                "iterations_left": extra_rounds,
                "actions": self._action_menu(),
            })
            action, target, instruction = coord if coord else ("done", "", "")
            if action == "done":
                break
            if action == "probe" and not self.state.hypotheses \
                    and not self.state.confirmed_findings:
                action = "analyze"
            self._dispatch_action(action, ctx, target, instruction)
            self._commit(f"replan iteration {self.state.iteration}: {action}")
        if self._budget_ok():
            self._run_audit_chain(ctx)

    def _refresh_diagnosis(self) -> Diagnosis | None:
        """Re-run the Reporter to get a fresh diagnosis after replanning."""
        if self.state is None or self._ref is None:
            return None
        ctx = collect(self.provider, self._ref, log_lines=self.log_lines)
        self.state.phase = "reporting"
        data = self._run("Reporter", ctx)
        if data is None:
            return None
        return _to_diagnosis(data)

    # --- public entry point -------------------------------------------------

    def investigate(self, ref: WorkloadRef) -> Diagnosis:
        """Run the full investigation and return a Diagnosis."""
        self._ref = ref
        self._calls = 0
        self._probes_run = set()
        self._queries_run = set()
        self._signature = None
        self._specialists = {}
        ctx = collect(self.provider, ref, log_lines=self.log_lines)

        state = InvestigationState(target=str(ref), platform=ref.platform)
        self.state = state
        self.workspace = Workspace.create(str(ref), base=self.workspace_base)
        self._log(f"workspace: {self.workspace.path}")
        # Surface the action menu so users adding a custom ActionAgent can see
        # at a glance that it's wired in — even when the Coordinator never
        # chooses it for this particular failure.
        menu = self._action_menu()
        if menu:
            names = ", ".join(name for name, _ in menu)
            self._log(f"action agents available: {names}")

        # Scout (pre-loop, with a fallback if the agent itself fails).
        state.phase = "scouting"
        if self._run("Scout", ctx) is None:
            state.classification = "Unknown"
            state.add_lead("investigate the failure from logs, events and status",
                           source="fallback")
        else:
            self._log(f"scout: {state.classification}, "
                      f"{len(state.leads)} leads, {len(state.evidence)} evidence")
        self._commit("scout: classify and seed leads")

        # Historian (Phase 15A, opt-in): now that the Scout has classified,
        # the signature is known — recall similar past incidents so the
        # Planner and the loop start from prior experience.
        if self.learning_enabled and self._exp_store is not None:
            self._recall_experience(ctx)

        # Planner.
        if self._run("Planner", ctx) is not None:
            self._log(f"planner: {len(state.sanity_checks)} sanity checks")
        self._commit("planner: strategy and sanity checks")

        # The investigation loop.
        state.phase = "investigating"
        findings_at: int | None = None
        while state.iteration < self.max_iterations and self._budget_ok():
            state.iteration += 1
            coord_result = self._run(
                "Coordinator", ctx,
                extras={
                    "iterations_left": self.max_iterations - state.iteration,
                    "actions": self._action_menu(),
                },
            )
            action, target, instruction = (
                coord_result if coord_result else ("done", "", "")
            )
            self._log(f"coordinator: {action} {target}".strip())

            if action == "done":
                break

            # Structural guard: a probe tests a hypothesis. With none yet,
            # analyze first — robust even when a small model misorders steps.
            if action == "probe" and not state.hypotheses and not state.confirmed_findings:
                self._log("override: probe -> analyze (no hypothesis to test yet)")
                action = "analyze"

            self._dispatch_action(action, ctx, target, instruction)

            self._commit(f"iteration {state.iteration}: {action}")

            # Early stop: once findings explain the failure, don't let a weak
            # Coordinator keep probing indefinitely.
            if state.confirmed_findings and findings_at is None:
                findings_at = state.iteration
            if (findings_at is not None
                    and state.iteration - findings_at >= _EXTRA_ITERS_AFTER_FINDING):
                self._log("early stop: confirmed findings explain the failure")
                break

        # Oversight: whole-state review before the verdict (HLD §11.2).
        if self._budget_ok():
            self._run_audit_chain(ctx)
            self._commit("auditor: cross-result review")

        # Reporter (with deterministic fallback).
        state.phase = "reporting"
        data = self._run("Reporter", ctx)
        diagnosis = _to_diagnosis(data) if data is not None else self._fallback_diagnosis(state)
        state.phase = "done"
        self._commit(f"reporter: final diagnosis ({self._calls} LLM calls)")
        self._log(f"done: {self._calls} LLM calls, {state.iteration} iterations")
        return diagnosis
