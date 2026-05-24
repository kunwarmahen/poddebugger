"""Tiny investigation engine — Scout → Analyst → Reporter.

This is intentionally minimal (~50 lines): a real engine would have a
Coordinator loop, a Verifier, budgets, retries, audit chain, etc.
PodDebugger's ``scaffold/engine.py`` is the reference. The point of this
file is to show that **the inquiro primitives are enough** to drive a
multi-agent investigation in a non-pod domain.
"""

from __future__ import annotations

from inquiro import (
    AgentContext,
    AgentLLMs,
    InvestigationState,
    LLMClient,
    Workspace,
    extract_json,
)

from .agents import Analyst, Reporter, Scout
from .models import LogContext, LogFinding


class MiniEngine:
    """Runs one investigation: Scout → Analyst → Reporter."""

    def __init__(self, llm: LLMClient | AgentLLMs, workspace_base=None):
        self._llms = llm if isinstance(llm, AgentLLMs) else AgentLLMs.uniform(llm)
        self._workspace_base = workspace_base
        # The agent registry is just a dict.
        self._agents = {a.name: a for a in (Scout(), Analyst(), Reporter())}

    def investigate(self, ctx: LogContext) -> LogFinding:
        state = InvestigationState(target=ctx.path, platform="logs")
        workspace = Workspace.create(ctx.path, base=self._workspace_base)

        # Scout — classify + seed leads.
        self._run("Scout", state, ctx)
        workspace.commit(state, "scout: classify")

        # Analyst — propose one hypothesis. Promote it directly (no Verifier
        # in this tiny example — keep it tight).
        hyp = self._run("Analyst", state, ctx)
        if hyp is not None:
            state.promote(hyp.id)
        workspace.commit(state, "analyst: hypothesize + promote")

        # Reporter — write the final finding.
        state.phase = "reporting"
        finding: LogFinding = self._run("Reporter", state, ctx)
        workspace.commit(state, "reporter: final finding")
        return finding

    def _run(self, role: str, state: InvestigationState, ctx: LogContext):
        agent = self._agents[role]
        llm = self._llms.for_role(role)
        ac = AgentContext(
            provider=None,           # this app has no provider
            ref=ctx.path,            # the "target" identifier
            state=state,
            ctx=ctx,
            llm=llm,
        )
        raw = llm.complete(agent.system_prompt, agent.build_user_prompt(ac))
        data = extract_json(raw)
        return agent.apply(ac, data)
