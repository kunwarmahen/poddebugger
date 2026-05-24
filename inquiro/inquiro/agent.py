"""Base classes for investigation agents.

The domain-agnostic core of inquiro. The ``AgentContext`` fields
``provider``, ``ref``, and ``ctx`` are typed as ``Any`` so the framework
doesn't depend on any application's domain model — applications fill them
with whatever shapes they need.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMClient
from .state import Evidence, Hypothesis, InvestigationState, Lead


#: Hook points at which a :class:`HookAgent` may run.
LIFECYCLE_POINTS: frozenset[str] = frozenset({
    "pre_loop",         # after Scout/Planner, before the Coordinator loop
    "post_loop",        # after the Coordinator loop, before Reporter
    "before_reporter",  # final pass right before the Reporter writes the verdict
})


# --- the per-call context object -------------------------------------------


@dataclass
class AgentContext:
    """Everything one agent call needs — the clean interface agents see.

    Built fresh by the engine for every agent dispatch. ``provider``, ``ref``,
    and ``ctx`` are intentionally typed as ``Any`` so the framework is
    domain-agnostic: in PodDebugger they are ``ContainerPlatform`` /
    ``WorkloadRef`` / ``DiagnosticContext``; another app might pass a log
    handle, a CI build id, and a parsed event stream.
    """

    provider: Any
    ref: Any
    state: InvestigationState
    ctx: Any
    llm: LLMClient

    #: A role-specific subject the engine passes in for specialized agents —
    #: e.g. the Hypothesis under review for the Verifier.
    subject: object | None = None

    #: The Coordinator's instruction to a dispatched agent. Empty otherwise.
    instruction: str = ""

    #: Free-form per-call extras (e.g. ``iterations_left`` for the Coordinator).
    extras: dict = field(default_factory=dict)

    # --- thin convenience wrappers (delegate to InvestigationState) --------

    def add_evidence(self, summary: str, detail: str = "", source: str = "") -> Evidence:
        """Record a new piece of evidence in the state."""
        return self.state.add_evidence(summary, detail, source)

    def add_lead(self, description: str, source: str = "") -> Lead:
        """Seed a new lead — something worth investigating."""
        return self.state.add_lead(description, source)

    def add_hypothesis(
        self,
        statement: str,
        test: str = "",
        evidence_ids: list[str] | None = None,
    ) -> Hypothesis:
        """Propose a new hypothesis for the Verifier to judge."""
        return self.state.add_hypothesis(statement, test, evidence_ids)

    def record_dispatch(self, role: str, action: str, summary: str = "") -> None:
        """Append to the audit log of agent calls."""
        self.state.record_dispatch(role, action, summary)


# --- the base agent --------------------------------------------------------


class Agent(abc.ABC):
    """Base class for all investigation agents.

    Subclasses must set the two class attributes below and implement the two
    abstract methods. Instantiating a subclass that omits any of these
    raises :class:`TypeError` so misconfiguration fails loudly at startup
    rather than mid-investigation.
    """

    #: The role name. Used in logs, dispatch records, and **LLM routing**:
    #: env vars keyed on ``name`` override this agent's LLM.
    name: str = ""

    #: The agent's static system prompt — same shape each call (cacheable on
    #: providers that support prompt caching).
    system_prompt: str = ""

    def __init__(self) -> None:
        cls = type(self)
        if not cls.name:
            raise TypeError(
                f"{cls.__name__} must set a non-empty class attribute 'name'"
            )
        if not cls.system_prompt:
            raise TypeError(
                f"{cls.__name__} must set a non-empty class attribute 'system_prompt'"
            )

    # --- the contract every agent implements -------------------------------

    @abc.abstractmethod
    def build_user_prompt(self, ac: AgentContext) -> str:
        """Render this role's slice of the state into a user prompt."""

    @abc.abstractmethod
    def apply(self, ac: AgentContext, response: dict):
        """Apply the parsed JSON response to the InvestigationState.

        The return type is role-specific (e.g. the Analyst returns a list of
        new hypotheses, the Coordinator returns an action choice). A return
        of ``None`` is fine for agents that only mutate the state.
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"


# --- ActionAgent: dispatched by the Coordinator ----------------------------


class ActionAgent(Agent):
    """An :class:`Agent` the Coordinator can dispatch by name.

    Adds two class attributes the framework's dynamic menu uses:

    - :attr:`action_name` — the value the Coordinator returns in
      ``{"action": "..."}`` to call this agent.
    - :attr:`description` — a one-line blurb included in the Coordinator's
      prompt menu so it knows when to pick this action.
    """

    #: Coordinator picks this agent by returning this string.
    action_name: str = ""

    #: One-line description shown in the Coordinator's menu.
    description: str = ""

    def __init__(self) -> None:
        super().__init__()
        cls = type(self)
        if not cls.action_name:
            raise TypeError(
                f"{cls.__name__} must set a non-empty class attribute 'action_name'"
            )
        if not cls.description:
            raise TypeError(
                f"{cls.__name__} must set a non-empty class attribute 'description'"
            )


# --- HookAgent: runs at a lifecycle point ----------------------------------


class HookAgent(Agent):
    """An :class:`Agent` run at a fixed lifecycle point, not via dispatch.

    Use for agents that should always run at a specific moment — e.g. a
    custom auditor that sweeps the state before the Reporter writes the
    verdict.
    """

    #: When this agent runs. Must be one of :data:`LIFECYCLE_POINTS`.
    lifecycle: str = ""

    def __init__(self) -> None:
        super().__init__()
        cls = type(self)
        if cls.lifecycle not in LIFECYCLE_POINTS:
            raise TypeError(
                f"{cls.__name__}: lifecycle must be one of "
                f"{sorted(LIFECYCLE_POINTS)}, got {cls.lifecycle!r}"
            )
