"""Hello-World custom agent — drop your own ActionAgent into the team.

To extend the investigation harness, subclass ``ActionAgent`` (or
``HookAgent`` for a lifecycle hook) and hand it to the engine via
``extra_agents=`` — that's it. See [AGENT_HARNESS.md](../AGENT_HARNESS.md)
for the full extension guide.

This file ships a tiny ``MetricsAgent`` that picks a metric to fetch (CPU /
memory / request_rate), records the result as Evidence, and shows its
``apply()`` shape. In a real deployment you'd call your monitoring backend
(Prometheus, Cloudwatch, Datadog) instead of returning the canned samples.

There are TWO ways to run this example:

1. **--demo (deterministic, no LLM needed)** — directly invokes
   ``MetricsAgent.apply()`` against a scripted response so you can see
   exactly what evidence it records. Recommended for a first run::

       python ../examples/custom_agent.py --demo

2. **Against a real container (full investigation loop)** — runs the whole
   multi-agent scaffold over a Podman target. The Coordinator picks one
   action per iteration; whether it picks ``metrics`` depends on how
   ambiguous the failure is. For a simple crash like the one below, the
   Analyst usually explains it without ever needing more data, so the
   Coordinator stops at ``done`` without calling ``metrics`` — that's the
   harness scaling effort to the failure's difficulty, not a bug::

       cd agent
       pip install -e '.[anthropic]'      # or '.[openai]', or use Ollama
       podman run --name pd-demo docker.io/library/alpine:latest \\
           sh -c 'echo "ERROR: high CPU" >&2; exit 1'
       python ../examples/custom_agent.py pd-demo

   Either way, ``verbose=True`` prints an ``action agents available: ...``
   line at startup so you can confirm the new agent is wired into the
   Coordinator's menu — even on a run where it doesn't get picked. The
   workspace's ``state.md`` records every dispatch.

Per-agent LLM routing is automatic: set ``PODDEBUGGER_METRICS_LLM_*`` env
vars to give this agent its own provider / model — the agent's ``name``
attribute is the routing key.
"""

from __future__ import annotations

import sys

from poddebugger.config import Config
from poddebugger.providers import get_provider
from poddebugger.scaffold import ActionAgent, AgentContext, InvestigationEngine
from poddebugger.scaffold.llms import AgentLLMs


# --- the custom agent ------------------------------------------------------


class MetricsAgent(ActionAgent):
    """Fetches a metric the model deems relevant and records it as Evidence.

    A real implementation would call Prometheus / Cloudwatch / your
    monitoring system here.
    """

    name = "metrics"
    action_name = "metrics"
    description = (
        "Fetch a recent metric (CPU / memory / request rate) for the workload — "
        "useful when a finding hinges on observed resource pressure."
    )
    system_prompt = (
        "You are the Metrics agent. Pick exactly ONE metric to fetch that "
        "would help confirm or refute the open hypothesis.\n\n"
        'Return JSON: {"metric": "<one of: cpu | memory | request_rate>", '
        '"rationale": "..."}'
    )

    def build_user_prompt(self, ac: AgentContext) -> str:
        return (
            f"Workload: {ac.state.target}\n"
            f"Open hypotheses: {len(ac.state.hypotheses)}\n"
            f"Goal: {ac.instruction}\n\n"
            "Pick a metric to fetch."
        )

    def apply(self, ac: AgentContext, response: dict):
        metric = str(response.get("metric", "")).strip().lower()
        if metric not in {"cpu", "memory", "request_rate"}:
            ac.state.notes.append(f"metrics agent picked unknown metric {metric!r}")
            return
        # Stand-in: in a real deployment this would call your monitoring backend.
        sample = {
            "cpu": "92% (p99 1m) — sustained high",
            "memory": "RSS 480Mi / limit 512Mi — near the ceiling",
            "request_rate": "1.2 rps — within nominal range",
        }[metric]
        ac.add_evidence(
            f"metric {metric}: {sample}",
            detail=str(response.get("rationale", "")),
            source=f"metrics:{metric}",
        )
        ac.record_dispatch("metrics", metric, response.get("rationale", "")[:80])


# --- entry points ----------------------------------------------------------


def run_full_investigation(target: str) -> int:
    """Path 2 — full multi-agent investigation against a real container.

    Whether the Coordinator dispatches ``metrics`` depends on the failure;
    the new "action agents available" log line confirms registration.
    """
    cfg = Config.from_env()
    provider = get_provider(cfg.platform)
    provider.preflight()
    ref = provider.resolve(target)

    engine = InvestigationEngine(
        provider, AgentLLMs.from_config(cfg),
        verbose=True,
        extra_agents=[MetricsAgent()],     # <-- the only line you need to add
    )
    diagnosis = engine.investigate(ref)
    print("\n=== diagnosis ===")
    print(diagnosis.summary)
    print(diagnosis.root_cause)
    print(f"workspace: {engine.workspace.path}")
    return 0


def run_demo() -> int:
    """Path 1 — deterministic, no LLM, no container needed.

    Builds the AgentContext by hand, hands MetricsAgent a scripted response,
    and prints the Evidence it adds. Use this to verify your custom agent's
    `apply()` shape before wiring it into a live investigation.
    """
    from inquiro import InvestigationState, Workspace
    from poddebugger.framework.agent import AgentContext as FrameworkAC
    from poddebugger.framework.llm import LLMClient

    class _NullLLM(LLMClient):
        name = "null"
        model_id = "n/a"
        def complete(self, system, user): return "{}"

    agent = MetricsAgent()
    state = InvestigationState(target="demo-container", platform="podman")
    state.add_hypothesis(
        "the workload is CPU-throttled at peak",
        test="check sustained CPU usage during the failure window",
    )
    ac = FrameworkAC(
        provider=None,
        ref="demo-container",
        state=state,
        ctx=None,
        llm=_NullLLM(),
        instruction="the Analyst suspects CPU pressure",
    )

    print("--- MetricsAgent.system_prompt ---")
    print(agent.system_prompt)
    print()
    print("--- MetricsAgent.build_user_prompt(ac) ---")
    print(agent.build_user_prompt(ac))
    print()

    # Pretend the model picked "cpu". In a live run, the LLM would emit this.
    scripted_response = {
        "metric": "cpu",
        "rationale": "open hypothesis is CPU pressure — fetch the CPU metric",
    }
    print("--- scripted LLM response ---")
    print(scripted_response)
    print()

    agent.apply(ac, scripted_response)

    print("=== state after MetricsAgent.apply ===")
    print(f"evidence ({len(state.evidence)} entries):")
    for e in state.evidence:
        print(f"  [{e.id}] {e.summary}")
        if e.detail:
            print(f"        detail: {e.detail}")
        print(f"        source: {e.source}")
    print(f"\ndispatch history ({len(state.dispatch_history)} entries):")
    for d in state.dispatch_history:
        print(f"  [{d.role}] {d.action} — {d.summary}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        _usage()
        return 2
    if argv[0] == "--demo":
        return run_demo()
    return run_full_investigation(argv[0])


def _usage() -> None:
    print(__doc__.splitlines()[0], file=sys.stderr)
    print(file=sys.stderr)
    print("usage:", file=sys.stderr)
    print("  python custom_agent.py --demo               # deterministic, no LLM",
          file=sys.stderr)
    print("  python custom_agent.py <pod-or-container>   # full investigation",
          file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
