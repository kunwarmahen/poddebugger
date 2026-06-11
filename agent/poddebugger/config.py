"""Environment-driven configuration (see HLD.md section 7).

Values come from environment variables. A ``.env`` file (searched for upward
from the working directory) supplies defaults for any variable not already set.

Each investigation agent (Scout, Planner, ... — HLD §11) uses the default LLM
unless a per-agent override is set:

    PODDEBUGGER_<ROLE>_LLM_PROVIDER   e.g. PODDEBUGGER_VERIFIER_LLM_PROVIDER
    PODDEBUGGER_<ROLE>_LLM_MODEL
    PODDEBUGGER_<ROLE>_LLM_BASE_URL
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .dotenv import load_dotenv
# LLMSpec is framework code — the canonical definition lives there; we
# re-export it here so the existing import path (``from poddebugger.config
# import LLMSpec``) keeps working.
from .framework.llms import LLMSpec  # noqa: F401

# The nine investigation-scaffold agents (HLD §11). Used to build the
# PODDEBUGGER_<ROLE>_LLM_* override env-var names.
AGENT_ROLES = (
    "scout", "planner", "coordinator", "analyst", "prober",
    "verifier", "auditor", "adjudicator", "reporter",
)


@dataclass
class Config:
    platform: str = "podman"
    llm_provider: str = "anthropic"
    llm_model: str = ""          # empty -> provider default
    llm_base_url: str = ""       # empty -> provider default
    log_lines: int = 200
    remediation_mode: str = "SuggestOnly"
    # Phase 8 — web research. Off by default (HLD §13.3): the Librarian
    # only joins the registry when ``analyze --research`` is in effect.
    # The backend defaults to "noop" so even an opt-in run is air-gap safe
    # unless a real backend is named explicitly.
    search_backend: str = "noop"
    # Phase 11 — interactive approvals. ``session`` (default) prompts and
    # remembers in-memory; ``persistent`` also offers [P]ersist to save a
    # rule; ``off`` ignores the rules file entirely. (HLD §16.5)
    approvals_mode: str = "session"
    # Phase 15A — cross-run experience memory. Off by default: recall and
    # recording only happen with ``analyze --learn`` or PODDEBUGGER_LEARN=1.
    learn: bool = False
    env_file: str = ""           # path of the .env that was loaded, if any

    @property
    def default_llm(self) -> LLMSpec:
        """The LLM every agent uses unless it has a per-agent override."""
        return LLMSpec(self.llm_provider, self.llm_model, self.llm_base_url)

    @classmethod
    def from_env(cls) -> "Config":
        loaded = load_dotenv()
        return cls(
            env_file=str(loaded) if loaded else "",
            platform=os.environ.get("PODDEBUGGER_PLATFORM", "podman"),
            llm_provider=os.environ.get("PODDEBUGGER_LLM_PROVIDER", "anthropic"),
            llm_model=os.environ.get("PODDEBUGGER_LLM_MODEL", ""),
            llm_base_url=os.environ.get("PODDEBUGGER_LLM_BASE_URL", ""),
            log_lines=int(os.environ.get("PODDEBUGGER_LOG_LINES", "200")),
            remediation_mode=os.environ.get("PODDEBUGGER_REMEDIATION_MODE", "SuggestOnly"),
            search_backend=os.environ.get("PODDEBUGGER_SEARCH_BACKEND", "noop"),
            approvals_mode=os.environ.get("PODDEBUGGER_APPROVALS_MODE", "session"),
            learn=os.environ.get("PODDEBUGGER_LEARN", "") == "1",
        )


_OVERRIDE_RE = re.compile(
    r"^PODDEBUGGER_([A-Z0-9][A-Z0-9_]*)_LLM_(PROVIDER|MODEL|BASE_URL)$"
)


def agent_llm_overrides(default: LLMSpec) -> dict[str, LLMSpec]:
    """Read ``PODDEBUGGER_<ROLE>_LLM_*`` env vars into a role -> LLMSpec map.

    Picks up overrides for **any** role name, not just the nine built-ins —
    so a custom :class:`Agent` registered via ``extra_agents=`` /
    ``PODDEBUGGER_EXTRA_AGENTS`` can be pointed at its own model with
    ``PODDEBUGGER_<NAME>_LLM_*``. The agent's ``name`` attribute is the
    routing key.

    Only roles with at least one override variable set appear in the result.
    A role that overrides just the *provider* does not inherit the default's
    model/base_url (those belong to a different provider).
    """
    overrides: dict[str, LLMSpec] = {}
    seen: set[str] = set()

    def _record(role: str) -> None:
        prefix = f"PODDEBUGGER_{role.upper()}_LLM_"
        provider = os.environ.get(prefix + "PROVIDER")
        model = os.environ.get(prefix + "MODEL")
        base_url = os.environ.get(prefix + "BASE_URL")
        if not (provider or model or base_url):
            return
        if provider and provider != default.provider:
            # Switching provider — don't carry over the old provider's model.
            overrides[role] = LLMSpec(provider, model or "", base_url or "")
        else:
            overrides[role] = LLMSpec(
                provider or default.provider,
                model or default.model,
                base_url or default.base_url,
            )

    # 1. Built-in roles first — preserves describe()'s historical ordering.
    for role in AGENT_ROLES:
        _record(role)
        seen.add(role)

    # 2. Any custom agent name with PODDEBUGGER_<NAME>_LLM_* env vars set.
    #    Scan keys instead of constructing prefixes so we discover names we
    #    don't know about (custom agents register their names at engine
    #    construction time, after Config has already been built).
    for key in sorted(os.environ):
        m = _OVERRIDE_RE.match(key)
        if not m:
            continue
        role = m.group(1).lower()
        if role in seen:
            continue
        seen.add(role)
        _record(role)

    return overrides
