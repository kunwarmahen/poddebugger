"""Hello-World for offline prompt-pack optimization (Phase 15C — HLD §19.4).

The idea: agent system prompts become *data* (a directory of ``<Role>.txt``
files), a deterministic scenario suite scores investigation quality, and an
LLM critic proposes prompt edits that are kept ONLY if the score strictly
improves. Offline and human-gated — prompts never mutate during a normal
``analyze`` run, and winning edits land as plain file diffs you review.

Two modes:

  1. ``--demo`` — deterministic walk-through. No LLM, no container. Dumps
     a real prompt pack to a temp dir, then runs ``promptopt.optimize``
     with a scripted critic and a fake score function: round 1 proposes a
     bad edit (discarded), round 2 a good one (adopted, written to disk).

  2. (LIVE) — the real loop against Podman + a local Ollama model:

         poddebugger eval --llm-provider ollama --model qwen3.5:9b
         poddebugger prompts dump ./prompt-pack
         poddebugger optimize --pack ./prompt-pack --rounds 3 \\
             --llm-provider ollama --model qwen3.5:9b
         git -C ./prompt-pack diff      # review before trusting

See [HLD.md §19.4](../HLD.md) and the README's "Evaluate, then evolve the
prompts" section for the design.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from poddebugger import promptopt, promptpack
from poddebugger.llm.base import LLMClient
from poddebugger.scenarios import ScenarioResult, SuiteScore

GOOD_SCOUT = ("Classify precisely; prefer ConfigError when required "
              'configuration is missing. Return JSON: {"classification": "..."}')


class ScriptedCritic(LLMClient):
    """Round 1 proposes a useless edit; round 2 the one that helps."""

    name = "scripted-critic"
    model_id = "demo"

    def __init__(self):
        self._answers = [
            {"role": "Scout", "diagnosis": "make it shoutier",
             "prompt": 'BE LOUD. Return JSON: {"classification": "..."}'},
            {"role": "Scout", "diagnosis": "teach the ConfigError pattern",
             "prompt": GOOD_SCOUT},
        ]

    def complete(self, system, user):
        return json.dumps(self._answers.pop(0))


def fake_score(pack: dict) -> SuiteScore:
    """Stands in for `poddebugger eval`: only the good Scout prompt scores."""
    points = 7 if pack.get("Scout") == GOOD_SCOUT else 4
    return SuiteScore([ScenarioResult(name="suite", points=points,
                                      max_points=7)])


def demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        pack_dir = Path(tmp) / "pack"
        print("=== 1. prompts become data ===\n")
        written = promptpack.dump_pack(pack_dir)
        print(f"  dumped {len(written)} <Role>.txt files to a pack dir\n")

        print("=== 2. the optimize loop (scripted critic, fake scorer) ===\n")
        report = promptopt.optimize(
            pack_dir, score_fn=fake_score, critic=ScriptedCritic(),
            rounds=2, log=lambda m: print(f"  {m}"))

        print("\n=== 3. what happened ===\n")
        for r in report.rounds:
            verdict = "ADOPTED" if r.adopted else "discarded"
            print(f"  round {r.round}: edit to {r.role or '?'} -> {verdict} "
                  f"({r.detail})")
        print(f"\n  score: {report.baseline_total} -> {report.final_total} "
              f"(of {report.max_total})")
        on_disk = promptpack.load_pack(pack_dir)["Scout"]
        print(f"  pack file now holds the winning prompt: "
              f"{on_disk == GOOD_SCOUT}")

    print("\nIn the live flow, `poddebugger eval` is the score function and")
    print("your model is the critic — and you review the pack's git diff")
    print("before using it with `analyze --prompt-pack`.")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        print(__doc__)
