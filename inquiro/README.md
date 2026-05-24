# inquiro

A domain-agnostic, multi-agent **investigation framework**. A team of
role-specialized LLM agents — Scout, Planner, Coordinator, Analyst,
Prober, Verifier, Auditor, Adjudicator, Reporter, plus any custom agents
you add — cooperate over a persistent, git-tracked `InvestigationState`
to reach a confirmed diagnosis.

The framework provides the **machinery**; applications supply the
**domain** (providers, probes, prompts, result type).
[PodDebugger](../) is the reference application;
[`examples/log_investigator/`](../examples/log_investigator/) is a
second-domain example that uses `inquiro` for a non-pod problem.

## Install

```bash
pip install -e .
```

inquiro itself has **zero runtime dependencies** — no LLM client SDK, no
HTTP library. Concrete LLM clients live in the applications that use it.

## Quick start

```python
from inquiro import (
    Agent, ActionAgent, AgentContext, AgentLLMs, LLMClient,
    InvestigationState, Workspace,
    PREAMBLE, evidence_block, sanity_block,
)

class MyAgent(ActionAgent):
    name = "MyAgent"
    action_name = "do-something"
    description = "Inspects the thing."
    system_prompt = PREAMBLE + "\n\nROLE: ..."

    def build_user_prompt(self, ac: AgentContext) -> str:
        return f"Evidence so far:\n{evidence_block(ac.state)}"

    def apply(self, ac: AgentContext, response: dict):
        ac.add_evidence(response["finding"], source="MyAgent")
```

To build a full application on inquiro, you also need an engine — the
investigation loop that dispatches actions, retries failed LLM calls,
manages budgets, and orchestrates the audit chain. PodDebugger's
`scaffold/engine.py` is the current reference; extracting a generic
`BaseInvestigationEngine` into inquiro is a planned follow-up (see
HLD §15.7).

## Public API

| Symbol | Purpose |
|---|---|
| `Agent`, `ActionAgent`, `HookAgent` | Subclass to add agents to the team. |
| `AgentContext` | The per-call object every agent receives. Domain-agnostic — `provider`/`ref`/`ctx` are `Any`. |
| `LIFECYCLE_POINTS` | Where `HookAgent`s may run. |
| `InvestigationState` + entity types | The state model; claim lifecycle Lead → Hypothesis → Finding (or RuledOut). |
| `Workspace` | Per-run directory + git history; resumable. |
| `AgentLLMs`, `LLMSpec` | Per-role LLM resolver with caching. |
| `LLMClient`, `LLMError` | Client ABC. Concrete clients live in applications. |
| `PREAMBLE`, `list_block`, `sanity_block`, `evidence_block` | Prompt helpers. |
| `extract_json` | Recover a JSON object from an LLM response (tolerates code fences). |

See [FRAMEWORK.md](../FRAMEWORK.md) for the full boundary between framework
and application.

## Configuration

| Variable | Purpose | Default |
|---|---|---|
| `INQUIRO_RUNS_DIR` | Where `Workspace.create()` lands run directories when the caller doesn't pass `base=` explicitly. | `~/.cache/inquiro/runs/` |

That's the entire env surface inquiro owns. Applications layered on top
(like PodDebugger) bring their own.

## Tests

```bash
python -m unittest discover tests
```

The inquiro test suite runs **without any application on the path** —
that's the boundary validation.
