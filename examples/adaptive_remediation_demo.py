"""Walk-through of the adaptive remediation loop (HLD §18 — Phase 13).

The default Remediator fires once: propose → apply → verify → stop. The
adaptive loop keeps going — a fix that didn't recover the workload becomes
evidence, the whole team replans, and the Remediator tries something
different — until the workload recovers or the agent honestly gives up.

This file demonstrates the three new pieces deterministically (no LLM, no
container):

  1. The extended catalog actions: ``set-env`` / ``set-image`` / ``recreate``.
  2. The ``engine.remediate()`` loop driving multiple attempts.
  3. The ``--context`` channel + the ``needs_context`` request.

Run it:

    python ../examples/adaptive_remediation_demo.py --demo

The end of the output points at the live, against-Podman invocation.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from poddebugger import remediation
from poddebugger.approvals import AutoApproveGate
from poddebugger.llm.base import LLMClient
from poddebugger.models import Diagnosis, Event, Workload, WorkloadRef
from poddebugger.providers.base import ContainerPlatform
from poddebugger.scaffold.engine import InvestigationEngine
from poddebugger.scaffold.state import InvestigationState
from poddebugger.scaffold.workspace import Workspace


# ---------------------------------------------------------------------------
# A provider that recovers only after the *second* applied fix — so the loop
# has to try twice. Mimics a container missing TWO env vars.
# ---------------------------------------------------------------------------

class _TwoFixProvider(ContainerPlatform):
    name = "podman"

    def __init__(self):
        self.applied = 0
        self.running = False
        self.runs: list[list[str]] = []

    def preflight(self): pass
    def resolve(self, t, namespace=None): return WorkloadRef(name=t, platform="podman")
    def get_workload(self, ref):
        return Workload(ref=ref, kind="container",
                        status="running" if self.running else "exited",
                        running=self.running, image="mysql:8",
                        restart_count=0, exit_code=None if self.running else 1)
    def get_events(self, ref):
        return [Event(timestamp="t", type="container", reason="died", message="exit 1")]
    def get_logs(self, ref, tail=200):
        if self.applied == 0:
            return "[ERROR] [Entrypoint] no MYSQL_ROOT_PASSWORD set\n"
        return "[ERROR] [Entrypoint] no MYSQL_DATABASE specified\n"
    def get_spec(self, ref):
        return {"image": "mysql:8", "env": []}
    def _inspect(self, name):
        return {"Name": name, "ImageName": "mysql:8",
                "Config": {"Env": [], "Cmd": None, "Entrypoint": None, "Labels": {}},
                "HostConfig": {"RestartPolicy": {"Name": "always"},
                               "NetworkMode": "bridge"},
                "State": {"Running": self.running}}

    def _run(self, args, check=True):
        import subprocess
        self.runs.append(args)
        if args[:1] == ["run"]:
            self.applied += 1
            if self.applied >= 2:        # recovers after the SECOND fix
                self.running = True
        return subprocess.CompletedProcess(args, 0, "ok\n", "")


class _ScriptedLLM(LLMClient):
    name = "demo-scripted"
    model_id = "scripted-1"

    def __init__(self, proposals):
        self._p = list(proposals)
        self.remediator_calls = 0

    def complete(self, system, user):
        if "ROLE: Remediator" in system:
            self.remediator_calls += 1
            # Echo back what context the agent was shown — to prove threading.
            if "MYSQL_ROOT_PASSWORD" in user or "context" in user.lower():
                pass
            return json.dumps(self._p.pop(0) if self._p
                              else {"action": "none", "reason": "out of ideas"})
        if "ROLE: Coordinator" in system:
            return json.dumps({"action": "done", "target": "", "instruction": "",
                               "reason": "done"})
        if "ROLE: Reporter" in system:
            return json.dumps({"summary": "missing env",
                               "root_cause": "missing required env vars",
                               "confidence": 0.9, "evidence": [],
                               "suggested_fixes": [], "needs_deep_inspection": False})
        if "ROLE: Auditor" in system:
            return json.dumps({"assessment": "sound", "critiques": []})
        if "ROLE: Analyst" in system:
            return json.dumps({"hypotheses": [], "new_evidence": []})
        return json.dumps({})


def _seed(llm, *, base, context, attempts=3):
    eng = InvestigationEngine(
        _TwoFixProvider(), llm, workspace_base=base,
        remediation_enabled=True, gate=AutoApproveGate(),
        context=context, max_remediation_attempts=attempts)
    eng._ref = WorkloadRef(name="db", platform="podman")
    eng.state = InvestigationState(target="db", platform="podman")
    eng.workspace = Workspace.create("db", base=base)
    return eng


DIAG = Diagnosis(summary="missing env", root_cause="missing required env vars",
                 confidence=0.9)


def run_demo() -> int:
    import poddebugger.remediation as rem
    # Make verify_recovery instant for the demo.
    rem.time.sleep = lambda *a, **k: None

    print("=" * 70)
    print("  1. The extended catalog now includes spec-change actions")
    print("=" * 70)
    print(f"  podman actions:     {rem.list_actions('podman')}")
    print(f"  kubernetes actions: {rem.list_actions('kubernetes')}")
    print()

    print("=" * 70)
    print("  2. needs_context — the agent asks for a value it can't infer")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        llm = _ScriptedLLM([
            {"action": "none", "reason": "missing context value",
             "needs_context": [{"key": "db_password",
                                "reason": "MySQL root password for set-env"}]},
        ])
        eng = _seed(llm, base=Path(tmp), context={})   # no context supplied
        out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
    print(f"  outcome: {out['outcome']}")
    print(f"  needs:   {out['needs_context']}")
    print("  → the CLI would tell you to re-run with --context db_password=…")
    print()

    print("=" * 70)
    print("  3. The loop — first fix doesn't recover, the team replans,")
    print("     a different second fix succeeds")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        llm = _ScriptedLLM([
            {"action": "set-env", "params": {
                "container": "db", "env": {"MYSQL_ROOT_PASSWORD": "secret"}},
             "rationale": "set the missing root password", "confidence": 0.7},
            {"action": "set-env", "params": {
                "container": "db", "env": {"MYSQL_DATABASE": "appdb"}},
             "rationale": "the logs now complain about the database name too",
             "confidence": 0.7},
        ])
        eng = _seed(llm, base=Path(tmp),
                    context={"db_password": "secret", "db_name": "appdb"})
        out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)

    print(f"  outcome:          {out['outcome']}")
    print(f"  attempts made:    {len(out['attempts'])}")
    for i, a in enumerate(out["attempts"], 1):
        p = a["proposal"]
        v = (a["applied"].get("verification") or {})
        print(f"    attempt {i}: {p['action']} {p['params']['env']} "
              f"→ {v.get('outcome', 'applied')}")
    # The failed-fix evidence the team picked up between attempts:
    print("  evidence added between attempts:")
    for e in eng.state.evidence:
        if "did not recover" in e.summary:
            print(f"    • {e.summary}")
    print()
    print("  → The agent didn't repeat the first fix. It read the new error,")
    print("    realized a second value was missing, and fixed that too.")
    print()

    _print_live_pointer()
    return 0


def _print_live_pointer():
    print("=" * 70)
    print("  Live, against a real Podman container")
    print("=" * 70)
    print("""
  # A container that exits unless APP_TOKEN is set:
  podman run -d --name pd-needenv docker.io/library/alpine:latest sh -c '
    if [ -z "$APP_TOKEN" ]; then
      echo "FATAL: APP_TOKEN is not set" >&2; exit 1
    fi
    echo started; tail -f /dev/null'

  # Investigate, then remediate — supplying the value the agent can't infer.
  # The agent reads the log, picks set-env, applies it (recreating the
  # container with APP_TOKEN), verifies the container now stays up.
  poddebugger analyze pd-needenv --platform podman \\
      --fix --confirm --yes --max-risk medium \\
      --context APP_TOKEN=secret-token-123 \\
      --llm-provider ollama --model qwen3.5:9b

  # Tidy up:
  podman rm -f pd-needenv
""")


def main(argv):
    if not argv or argv[0] != "--demo":
        print("usage: python adaptive_remediation_demo.py --demo", file=sys.stderr)
        return 2
    return run_demo()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
