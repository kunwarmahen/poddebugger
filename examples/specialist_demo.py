"""Hello-World for on-the-fly specialist agents (Phase 15B — HLD §19.3).

Every other agent in PodDebugger has a *static* system prompt written by a
human. A **Specialist** is different: the Coordinator decides mid-run that
the team lacks a skill, names the specialty, and writes the new agent's
charter — and the engine composes the spawned agent's system prompt from
both. The LLM writes the prompt for the next LLM call.

The boundary that makes this safe: a Specialist is **advisory only**. It
is an ordinary `Agent` whose output lands as Evidence and Leads tagged
``dynamic:<slug>`` — it cannot probe, run actions, or touch the
remediation catalog. Spawns are budgeted (2 unique specialties per run)
and every composed prompt is persisted to ``specialists/<slug>.md`` in
the run workspace, captured by the per-iteration git commit.

Two modes:

  1. ``--demo`` — deterministic walk-through. No LLM, no container.
     Builds a Specialist exactly the way the engine does, shows the
     composed system prompt and the audit document, then feeds it a
     scripted response and shows how the output lands in the state.

  2. (LIVE) — the real flow against Podman + a local Ollama model:

         podman run -d --name pd-pg docker.io/library/postgres:16-alpine
         poddebugger analyze pd-pg --platform podman --specialists \\
             --llm-provider ollama --model qwen3.5:9b --verbose

     Watch for:
         [scaffold] coordinator: specialist PostgreSQL crash analysis
         [scaffold] specialist spawned: Specialist:postgresql-crash-analysis (1/2)
     and find the generated prompts under the run workspace's
     ``specialists/`` directory afterwards.

See [HLD.md §19.3](../HLD.md) and the README's "Spawn domain specialists
mid-run" section for the design.
"""

from __future__ import annotations

import sys

from poddebugger.scaffold.agents import make_specialist
from poddebugger.scaffold.agents.base import AgentContext
from poddebugger.scaffold.state import InvestigationState


def demo() -> None:
    print("=== 1. the Coordinator asks for an expert ===\n")
    specialty = "PostgreSQL crash analysis"
    charter = ("The container exits at boot before initdb completes. "
               "Judge whether the entrypoint's password requirement or a "
               "volume permission problem explains it, using the evidence.")
    print(f"  target (specialty): {specialty!r}")
    print(f"  instruction (charter, written by the Coordinator):\n    {charter}\n")

    print("=== 2. the engine composes the new agent ===\n")
    agent = make_specialist(specialty, charter=charter)
    print(f"  name: {agent.name}")
    print(f"  evidence tag: dynamic:{agent.slug}")
    print("\n  composed system prompt (excerpt):")
    for line in agent.system_prompt.splitlines():
        if "ROLE:" in line or "charter" in line.lower() or "ADVISORY" in line:
            print(f"    | {line.strip()}")

    print("\n=== 3. the audit document (persisted to specialists/<slug>.md) ===\n")
    for line in agent.prompt_document().splitlines()[:5]:
        print(f"  {line}")
    print("  ...")

    print("\n=== 4. a scripted answer lands in the investigation state ===\n")
    state = InvestigationState(target="pd-pg", platform="podman")
    ac = AgentContext(provider=None, ref=None, state=state, ctx=None, llm=None)
    agent.apply(ac, {
        "observations": [
            "entrypoint aborts in the password check, before initdb runs",
            "no POSTGRES_PASSWORD / POSTGRES_HOST_AUTH_METHOD in the spec env",
        ],
        "leads": ["re-run with POSTGRES_PASSWORD set via --context"],
        "assessment": "classic unconfigured-image failure; not a volume issue",
    })
    for e in state.evidence:
        print(f"  evidence [{e.source}] {e.summary}")
    for l in state.leads:
        print(f"  lead     [{l.source}] {l.description}")
    print(f"  dispatch [{state.dispatch_history[-1].role}] "
          f"{state.dispatch_history[-1].summary}")

    print("\nThe specialist informed the team — but only the catalog validator")
    print("and the approval gate decide what may actually run.")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        print(__doc__)
