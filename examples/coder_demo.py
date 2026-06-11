"""Hello-World for the Coder agent + sandbox sidecar (Stage 13D — HLD §18.6).

The Coder is the last of the opt-in teammates: it *writes a short script*
(bash or python) when the whitelisted probes can't answer a question —
"is the target's database port actually reachable from inside its network
namespace?" — and the engine runs it in a **sandbox sibling container**:

  * shares the target's NETWORK namespace (its ports, its DNS view),
  * shares NOTHING else — no filesystem, no process table,
  * is gated at risk `high`: the human sees the FULL script before it runs,
  * is deny-by-default: no gate (non-TTY, CI) means no execution.

Persistent rules pre-approve exactly one script: the gate descriptor's
action is `<language>:<sha256-hash[:12]>`, so a rule can never say
"allow all code".

Two modes:

  1. ``--demo`` — deterministic walk-through. No LLM, no Podman. Shows
     the Coder's proposal contract, the gate prompt a human would see
     (full script body), the deny-by-default property, and a
     rules-by-hash pre-approval — with a stubbed executor so nothing
     actually runs.

  2. (LIVE) — the real flow against Podman + a local Ollama model:

         podman build -t poddebugger-coder-sandbox sandbox/
         poddebugger analyze my-broken-container --coder --verbose \\
             --llm-provider ollama --model qwen3.5:9b

     Watch for `coordinator: code` and `coder: ran <hash>` in the
     verbose log; the script + its output land as Evidence tagged
     `coder:<purpose>:<hash8>`.

See [HLD.md §18.6](../HLD.md) and [sandbox/README.md](../sandbox/README.md).
"""

from __future__ import annotations

import sys

from poddebugger.approvals import (
    DenyGate,
    RulesGate,
    TTYPromptGate,
    _render_descriptor,
)
from poddebugger.models import WorkloadRef
from poddebugger.scaffold import sandbox
from poddebugger.scaffold.agents import Coder

SCRIPT = """for port in 5432 6379 8080; do
  nc -z -w 2 127.0.0.1 $port && echo "port $port OPEN" || echo "port $port closed"
done"""


class _FakeProc:
    returncode = 0
    stdout = "port 5432 closed\nport 6379 OPEN\nport 8080 closed\n"
    stderr = ""


def demo() -> None:
    ref = WorkloadRef(name="my-app", platform="podman")

    print("=== 1. the Coder's proposal contract ===\n")
    proposal = Coder().apply(None, {
        "language": "bash", "script": SCRIPT, "purpose": "probe",
        "rationale": "check which dependency ports are reachable in the "
                     "target's network namespace"})
    print(f"  language={proposal['language']} purpose={proposal['purpose']}")
    print(f"  rationale: {proposal['rationale']}\n")

    digest = sandbox.script_hash("bash", SCRIPT)
    print("=== 2. what the human sees at the gate ===")
    descriptor = sandbox._gate_descriptor(ref, "bash", SCRIPT, "probe", digest)
    print(_render_descriptor(descriptor))

    print("\n=== 3. deny-by-default ===\n")
    r = sandbox.run_code(None, ref, "bash", SCRIPT)  # no gate at all
    print(f"  no gate -> denied={r.denied}: {r.error}")
    r = sandbox.run_code(None, ref, "bash", SCRIPT, gate=DenyGate())
    print(f"  DenyGate (non-TTY default) -> denied={r.denied}")

    print("\n=== 4. a human approves interactively (scripted 'y') ===\n")
    gate = TTYPromptGate(input_fn=lambda _: "y", output_fn=lambda _: None)
    real_run = sandbox.subprocess.run
    sandbox.subprocess.run = lambda *a, **k: _FakeProc()  # stub executor
    try:
        r = sandbox.run_code(_StubProvider(), ref, "bash", SCRIPT, gate=gate)
    finally:
        sandbox.subprocess.run = real_run
    print(f"  executed={r.executed} exit={r.exit_code}")
    for line in r.output.splitlines():
        print(f"    {line}")

    print("\n=== 5. pre-approving exactly this script by hash ===\n")
    rule = {"kind": "code", "action": f"bash:{digest[:12]}",
            "target": {}, "decision": "allow"}
    print(f"  poddebugger approvals add --kind code --action bash:{digest[:12]}")
    gate = RulesGate(DenyGate(), [rule])
    sandbox.subprocess.run = lambda *a, **k: _FakeProc()
    try:
        same = sandbox.run_code(_StubProvider(), ref, "bash", SCRIPT, gate=gate)
        other = sandbox.run_code(_StubProvider(), ref, "bash",
                                 "echo something else", gate=gate)
    finally:
        sandbox.subprocess.run = real_run
    print(f"  same script   -> executed={same.executed}")
    print(f"  other script  -> denied={other.denied}  (hash mismatch falls "
          "to the deny default)")

    print("\nThe sandbox shares the target's network only — the image adds")
    print("containment, the gate grants permission. See sandbox/README.md.")


class _StubProvider:
    _bin = "podman"

    def get_workload(self, ref):
        raise sandbox.ProviderError("offline demo")  # default network path


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        print(__doc__)
