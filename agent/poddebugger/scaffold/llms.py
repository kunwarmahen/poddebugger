"""PodDebugger-specific binding for the framework's per-agent LLM resolver.

Wraps :class:`poddebugger.framework.llms.AgentLLMs` with PodDebugger's
``get_llm`` factory and the :class:`poddebugger.config.Config`-driven
``from_config`` builder. The framework class is exposed unchanged, so
existing callers that did ``from poddebugger.scaffold.llms import AgentLLMs``
keep working — but now ``AgentLLMs`` is a thin subclass that knows how to
turn PodDebugger env vars into per-agent LLM specs.
"""

from __future__ import annotations

from ..config import Config, LLMSpec, agent_llm_overrides
from ..framework.llms import AgentLLMs as _BaseAgentLLMs
from ..framework.llm import LLMClient
from ..llm import get_llm


class AgentLLMs(_BaseAgentLLMs):
    """Per-agent LLM resolver wired to PodDebugger's get_llm factory."""

    def __init__(
        self,
        default: LLMSpec,
        overrides: dict[str, LLMSpec] | None = None,
        fixed: LLMClient | None = None,
    ):
        super().__init__(default, overrides, fixed,
                         builder=lambda p, m, b: get_llm(p, m, b))

    @classmethod
    def from_config(cls, cfg: Config) -> "AgentLLMs":
        """Build from a Config — default LLM plus PODDEBUGGER_<ROLE>_LLM_* overrides."""
        return cls(cfg.default_llm, agent_llm_overrides(cfg.default_llm))


__all__ = ["AgentLLMs"]
