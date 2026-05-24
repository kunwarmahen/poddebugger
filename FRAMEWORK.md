# Framework / Application boundary

Classifies every symbol in this repository as either **framework** (the
domain-agnostic machinery now packaged as
[`inquiro`](inquiro/)) or **application** (PodDebugger-specific code that
stays under `poddebugger.*`).

**Why this exists.** PodDebugger's investigation machinery — the agent
base classes, the investigation state, the engine loop, per-agent LLM
routing, the workspace, the oversight pattern — is not specific to pods.
The same pattern works for a log-event investigator, a CI-failure
triager, a slow-query diagnoser. The machinery lives in `inquiro/` so
other applications can reuse it; this doc is the contract.

**Rule of thumb.** If removing the symbol would break only the "diagnose a
pod" use case, it's *application*. If removing it would break ANY
multi-agent investigation (including hypothetical non-pod ones), it's
*framework*.

---

## Inventory

### Framework — lives in `inquiro/`

| File | What it provides | Why framework |
|---|---|---|
| `scaffold/state.py` | `InvestigationState`, `SanityCheck`, `Lead`, `Evidence`, `Hypothesis`, `Finding`, `RuledOut`, `DispatchRecord`, lifecycle (`promote`/`refute`/`demote`), id sequencer, JSON round-trip | The state model + claim lifecycle is the framework's spine. None of these entities mention pods. |
| `scaffold/workspace.py` | `Workspace.create()`, per-iteration git commits, replay/audit/resume, graceful degradation without git | Run-level artifact store. Domain-blind — it commits whatever JSON the state serializes to. |
| `scaffold/agents/base.py` | `Agent`, `ActionAgent`, `HookAgent`, `AgentContext`, `LIFECYCLE_POINTS` | The agent base classes are the framework's public extension surface (HLD §14). Already designed to be domain-agnostic. |
| `scaffold/llms.py` | `AgentLLMs` (per-agent provider/model/base_url routing), `AgentLLMs.uniform`/`from_config`/`describe` | Per-role LLM dispatch is framework. Reads PodDebugger config today, but the *mechanism* is generic — see "split this file" below. |
| `scaffold/prompts.py` | `PREAMBLE`, `list_block`, `sanity_block`, `evidence_block`, `context_block` | `PREAMBLE`, `list_block`, `sanity_block`, `evidence_block` operate on `InvestigationState` — framework. `context_block` reaches into `DiagnosticContext` — app-specific (see "split this file" below). |
| `analyzer._extract_json` | Strips code fences, recovers the first JSON object from an LLM response | Generic JSON-from-LLM parser. The `_to_diagnosis` helper next to it is app-specific. |
| `llm/base.py` (`LLMClient` ABC + `LLMError`) | Abstract LLM client + the exception every concrete client raises | The contract every framework agent calls through. No pod awareness. |
| `llm/anthropic_client.py`, `llm/openai_client.py`, plus the Ollama / llama.cpp variants | Concrete `LLMClient` implementations | Talking to Anthropic/OpenAI/Ollama isn't pod-specific. |
| `llm/__init__.py` | The `get_client(provider, model, base_url)` factory | Provider-agnostic factory. |

### Application — stays under `poddebugger.*`

| File | What it provides | Why application |
|---|---|---|
| `models.py` | `WorkloadRef`, `Workload`, `Event`, `DiagnosticContext`, `Fix`, `Diagnosis` | Pod/container domain model + the final RCA output type. |
| `providers/base.py` + `providers/podman.py` + `providers/kubernetes.py` | `ContainerPlatform` ABC + Podman / Kubernetes implementations | "Talk to a container runtime" is pod-specific. |
| `collector.py` | `collect(provider, ref) -> DiagnosticContext` | Gathers status/events/logs/spec for a container or pod. |
| `deepinspect.py` | Curated in-container probe shell-out (`ps`, `ss`, `df`, …) | The probe commands themselves are SRE-domain. |
| `remediation.py` | The action catalog (restart, scale, set-resources, adjust-probe, rollback), validators, `verify_recovery`, save/load for undo | Pod-runtime mutations. |
| `analyzer.py` (`_to_diagnosis`, `analyze`) | Maps an LLM response onto `Diagnosis`; legacy one-shot analyzer (pre-scaffold) | The mapping is app-specific (it targets PodDebugger's `Diagnosis`). |
| `prompts.py` (top-level) | `SYSTEM_PROMPT`, `build_user_prompt` for the one-shot analyzer | App-specific SRE prompt. |
| `cli.py`, `config.py`, `dotenv.py`, `__main__.py` | Argparse, env-driven config, env loader, CLI entry point | App entry points + config. |
| `scaffold/probes.py` | `PROBE_MENU`, `run_probe` — the SRE probe registry | Pod-specific. |
| `scaffold/search.py` | `SearchBackend` ABC, `NoopBackend`, `DuckDuckGoBackend`, `redact_query` | **Mostly framework.** The ABC, factory, and noop are domain-agnostic; `DuckDuckGoBackend` is too. `redact_query` is tuned for pod identifiers (k8s pod-suffix regex), so it stays app-side until a third application shows what a domain-neutral redactor looks like. |
| `scaffold/engine.py` | `InvestigationEngine` | **Mixed.** The loop, registry, dispatch, retry/degrade, budgets, dynamic-menu Coordinator, and audit chain are framework; the methods that touch `DiagnosticContext`, `Diagnosis`, `Fix`, `WorkloadRef`, `ContainerPlatform`, `remediation`, and `collect` are app. A future refactor could extract a `BaseInvestigationEngine` so PodDebugger subclasses (or composes) it. |
| `scaffold/agents/{scout,planner,coordinator,analyst,prober,verifier,auditor,adjudicator,reporter,remediator,librarian}.py` | The eleven concrete SRE agents | The role *structure* is framework (subclassing `Agent` / `ActionAgent` is the framework contract); the prompts and `apply` bodies are app. They stay under `poddebugger.scaffold.agents/`. |

### Files split along the boundary

These files mixed framework and app symbols; each was split so the
framework half lives in `inquiro/` while the app half stays under
`poddebugger.*`:

| File | Framework half | Application half |
|---|---|---|
| `scaffold/prompts.py` | `PREAMBLE`, `list_block`, `sanity_block`, `evidence_block` → `inquiro.prompts` | `context_block` (reads `DiagnosticContext`) → `poddebugger.scaffold.prompts` |
| `scaffold/llms.py` | `AgentLLMs` class + `uniform()` + `describe()` + `for_role()` (the dispatch mechanism) → `inquiro.llms` | `AgentLLMs.from_config(cfg: poddebugger.Config)` factory (reads PodDebugger env vars) → `poddebugger.scaffold.llms` (a thin app-side helper that calls into the framework) |
| `scaffold/engine.py` | The base engine loop (registry, dispatch, retry/degrade, budgets, audit chain) → `inquiro.engine.BaseInvestigationEngine` | Subclass methods that touch `DiagnosticContext`, `collect()`, `_to_diagnosis`, `_fallback_diagnosis`, `propose_remediation` → `poddebugger.scaffold.engine.InvestigationEngine(BaseInvestigationEngine)` |
| `analyzer.py` | `_extract_json` → `inquiro.json_utils` | `_to_diagnosis`, `analyze` → stay |

---

## The framework's public API

The `inquiro` package's public surface:

```python
# Agents
from inquiro import Agent, ActionAgent, HookAgent, AgentContext, LIFECYCLE_POINTS

# State
from inquiro import (
    InvestigationState, SanityCheck, Lead, Evidence,
    Hypothesis, Finding, RuledOut, DispatchRecord,
)

# Workspace + LLM plumbing
from inquiro import Workspace, AgentLLMs
from inquiro.llm import LLMClient, LLMError, get_client

# Engine
from inquiro import BaseInvestigationEngine

# Prompt helpers
from inquiro.prompts import (
    PREAMBLE, list_block, sanity_block, evidence_block,
)

# JSON extraction from LLM responses
from inquiro.json_utils import extract_json
```

What `inquiro` does NOT export:
- Anything pod-shaped: `Workload`, `WorkloadRef`, `DiagnosticContext`,
  `Diagnosis`, `Fix`, `ContainerPlatform`, the remediation catalog, the
  probe registry, the SRE agent prompts, the collector.
- Search backends (the abstraction is framework-shaped but the redactor
  is tuned for the SRE case; a domain-supplied redactor callback could
  let the rest move into `inquiro`).
- The CLI / config / dotenv loader.

---

## What an application supplies (HLD §15.2)

To build an investigation app on `inquiro`, you provide:

1. **A context type** for what your agents need to see (e.g. PodDebugger's
   `DiagnosticContext`). Pass it through `AgentContext.ctx`.
2. **A collector** that builds that context from your domain (PodDebugger's
   `collect()` reads pods; a log-event investigator would read a log file).
3. **A result type** (PodDebugger's `Diagnosis`) and a `Reporter` agent
   whose `apply` returns it. The framework engine returns whatever the
   Reporter's `apply` returns.
4. **One or more `ActionAgent` subclasses** with domain prompts (PodDebugger
   has the Scout/Planner/Coordinator/Analyst/Prober/Verifier/Reporter team).
5. **Optional `HookAgent` subclasses** for things that always run at a
   lifecycle point (Auditor/Adjudicator).
6. **Optional engine subclass** if you want to add domain side effects like
   probes, remediation, or web research.

---

## Boundary validation strategy

1. The full PodDebugger test suite stays green through any framework
   change — that's the contract.
2. `agent/tests/test_framework_boundary.py` imports the framework
   symbols from `inquiro` directly and asserts they ARE the same
   objects re-exported from `poddebugger.scaffold` (so `is` identity
   holds).
3. The CI script runs the framework's own test suite separately too —
   it must pass without any PodDebugger code on the Python path. That's
   what makes `inquiro` genuinely standalone, not just well-factored.
