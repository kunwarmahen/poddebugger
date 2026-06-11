"""Hello-World for the cross-run experience memory (Phase 15A — HLD §19.2).

PodDebugger normally investigates every failure from a blank slate. With
``--learn``, two things change:

  * **Record** — after a ``--fix --confirm`` run reaches a verified
    outcome, a redacted *experience record* is persisted: the failure
    signature (classification, exit code, OOM flag, image, keywords),
    what was tried, and whether it worked.
  * **Recall** — on later runs, right after the Scout classifies, the
    most similar past records land as Evidence (``experience:<id>``),
    so the Planner and the Remediator start from "we've seen this
    before — restart did NOT help, set-env did".

The safety framing: learning changes what the LLM is *told*, never what
it is *allowed* to do. Recalled records are prompt context only; the
catalog validator and the approval gate remain the capability boundary.
Secret-looking values are masked and identifiers scrubbed BEFORE a
record reaches disk.

Two modes:

  1. ``--demo`` — deterministic walk-through. No LLM, no container.
     Saves two incidents into a temp store, shows what recall finds for
     a new failure's signature, and shows what redaction did to the
     stored record. Always runs.

  2. (LIVE) — the real flow against Podman + a local Ollama model:

         # first incident: the team figures it out the hard way, and
         # the verified outcome is remembered
         poddebugger analyze my-app --fix --confirm --learn \\
             --context APP_TOKEN=s3cret --max-risk medium \\
             --llm-provider ollama --model qwen3.5:9b --yes

         # next time the same failure signature shows up, recall fires:
         poddebugger analyze my-app-2 --fix --confirm --learn ...
         # → evidence "past incident (fix worked): ..." appears, and the
         #   Remediator is steered away from fixes that already failed

         poddebugger experience list      # what has been remembered
         poddebugger experience clear     # forget everything

See [HLD.md §19](../HLD.md) and the README's "Learn from past incidents"
section for the design.
"""

from __future__ import annotations

import sys
import tempfile

from poddebugger.experience import (
    ExperienceStore,
    make_record,
    signature_from_context,
)
from poddebugger.models import DiagnosticContext, Workload, WorkloadRef


def demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = ExperienceStore(tmp)

        print("=== 1. two past incidents get recorded ===\n")
        oom = make_record(
            {"platform": "podman", "classification": "OOMKilled",
             "image": "registry.local/app:2.1", "exit_code": 137,
             "oom_killed": True, "keywords": ["memory", "limit", "killed"]},
            summary="app OOM-killed under load",
            root_cause="memory limit 64Mi is too small for the working set",
            attempts=[
                {"action": "restart", "params": {}, "outcome": "still-failing"},
                {"action": "set-resources",
                 "params": {"memory_limit": "256Mi"}, "outcome": "recovered"},
            ],
            outcome="recovered",
        )
        config = make_record(
            {"platform": "podman", "classification": "ConfigError",
             "image": "mysql:8", "exit_code": 1, "oom_killed": False,
             "keywords": ["mysql_root_password", "required"]},
            summary="mysql exits at boot at 10.0.0.5",   # IP gets scrubbed
            root_cause="MYSQL_ROOT_PASSWORD not set",
            attempts=[{"action": "set-env",
                       "params": {"container": "db",
                                  "env": {"MYSQL_ROOT_PASSWORD": "hunter2"}},
                       "outcome": "recovered"}],
            outcome="recovered",
        )
        for rec in (oom, config):
            store.save(rec)
            print(f"  saved {rec.id}: [{rec.classification}] {rec.root_cause}")

        print("\n=== 2. redaction happened before anything hit disk ===\n")
        saved = next(r for r in store.load_all() if r.classification == "ConfigError")
        print(f"  summary on disk:  {saved.summary!r}   (IP scrubbed)")
        print(f"  env value on disk: "
              f"{saved.attempts[0]['params']['env']['MYSQL_ROOT_PASSWORD']!r}"
              "   (secret masked)")

        print("\n=== 3. a NEW failure shows up — what does recall find? ===\n")
        ref = WorkloadRef(name="app-7f9d4-x2k1c", platform="podman")
        workload = Workload(ref=ref, status="exited", running=False,
                            image="registry.local/app:2.2", exit_code=137,
                            oom_killed=True, error="OOM killed")
        ctx = DiagnosticContext(workload=workload,
                                logs="fatal: out of memory; killed\n")
        signature = signature_from_context(ctx, "OOMKilled")
        print(f"  signature: classification={signature['classification']} "
              f"exit_code={signature['exit_code']} "
              f"oom={signature['oom_killed']} keywords={signature['keywords']}")

        matches = store.find_similar(signature)
        print(f"\n  recalled {len(matches)} of {len(store.load_all())} records "
              "(the mysql incident scored below the bar):\n")
        for rec, score in matches:
            print(f"  [score {score}] {rec.recall_summary()}")
            for line in rec.recall_detail().splitlines():
                print(f"      {line}")

        print("\nIn a real run this lands as Evidence (source=experience:<id>)")
        print("right after the Scout classifies — the team is told 'restart")
        print("did NOT work last time; set-resources did'.")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        print(__doc__)
