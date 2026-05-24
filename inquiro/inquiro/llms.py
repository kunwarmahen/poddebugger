"""Per-agent LLM resolution.

Resolves a role name to an :class:`LLMClient`. Clients with an identical
(provider, model, base_url) are built once and shared. The framework class
takes a *builder* callable rather than building clients itself, so any
application can wire its own client construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .llm import LLMClient


@dataclass(frozen=True)
class LLMSpec:
    """A resolved provider / model / endpoint triple for one LLM client."""

    provider: str
    model: str = ""        # empty -> provider default
    base_url: str = ""     # empty -> provider default


ClientBuilder = Callable[[str, str, str], LLMClient]
"""(provider, model, base_url) -> LLMClient — what the framework needs to
construct a client for a spec."""


class AgentLLMs:
    """Resolves an LLM client per agent role."""

    def __init__(
        self,
        default: LLMSpec,
        overrides: dict[str, LLMSpec] | None = None,
        fixed: LLMClient | None = None,
        builder: ClientBuilder | None = None,
    ):
        self._default = default
        self._overrides = {r.lower(): s for r, s in (overrides or {}).items()}
        self._fixed = fixed                       # all roles share this client
        self._cache: dict[tuple, LLMClient] = {}  # spec -> built client
        self._builder = builder

    @classmethod
    def uniform(cls, client: LLMClient) -> "AgentLLMs":
        """Every role shares one pre-built client (tests / single-model use)."""
        return cls(LLMSpec(provider=""), fixed=client)

    def spec_for(self, role: str) -> LLMSpec:
        """The :class:`LLMSpec` a role resolves to (its override, else default)."""
        return self._overrides.get(role.lower(), self._default)

    def for_role(self, role: str) -> LLMClient:
        """The LLM client for one agent role — built lazily, shared by spec."""
        if self._fixed is not None:
            return self._fixed
        if self._builder is None:
            raise RuntimeError(
                "AgentLLMs needs a `builder` to construct clients when no "
                "fixed client is provided"
            )
        spec = self.spec_for(role)
        key = (spec.provider, spec.model, spec.base_url)
        if key not in self._cache:
            self._cache[key] = self._builder(spec.provider, spec.model, spec.base_url)
        return self._cache[key]

    def describe(self) -> str:
        """A short human-readable summary for the CLI footer."""
        if self._fixed is not None:
            return f"{self._fixed.name}:{self._fixed.model_id}"
        d = self._default
        line = f"{d.provider}:{d.model or 'default'}"
        if self._overrides:
            ov = ", ".join(
                f"{role}={s.provider}:{s.model or 'default'}"
                for role, s in sorted(self._overrides.items())
            )
            line += f"  (per-agent: {ov})"
        return line
