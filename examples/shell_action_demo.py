"""Hello-World for the opt-in ``shell`` catalog action.

The Remediator catalog is normally a *fixed menu of typed actions* — the
LLM picks a name (`restart`, `scale`, ...) and proposes typed params; the
engine validates and shells out via hard-coded argv lists. This is the
"the LLM never emits a command" safety property.

`shell` is the **opt-in exception**: when enabled, the catalog gains an
action whose only parameter is a `command` string that runs verbatim
inside the target container. It weakens the catalog-membership boundary,
so it's off by default. When enabled, the approval gate is the safety
property — denials still apply.

Three modes here:

  1. ``--demo`` — deterministic walk-through. No LLM, no container.
     Stubs the provider; shows the action's plan, executes it through
     ``remediation.execute``, and contrasts with what the approval gate
     refuses. Always runs.

  2. ``--gated`` — same as ``--demo`` but layers a ``DenyGate`` so you
     see the refusal path explicitly.

  3. (LIVE) — pointer at the bottom showing the exact CLI invocation
     against a real Podman container. Requires Podman + a running
     container; *no flag here*, just docs.

The CLI flag for enabling the shell action is ``--allow-shell`` (or the
``PODDEBUGGER_ALLOW_SHELL=1`` env var). See [HLD.md §12.9](../HLD.md) and
[README.md](../README.md#freeform-shell-action-opt-in) for the design.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from poddebugger import approvals, remediation
from poddebugger.models import WorkloadRef
from poddebugger.providers.base import ContainerPlatform, ProviderError


# --- a stub provider so the demo runs without Podman / Kubernetes ----------


class _StubProvider(ContainerPlatform):
    """Mimics a podman provider: ``exec`` returns canned output."""

    name = "podman"

    def __init__(self, exec_output: str = ""):
        self._output = exec_output
        self.exec_calls: list[list[str]] = []

    def preflight(self): pass
    def resolve(self, target, namespace=None):
        return WorkloadRef(name=target, platform="podman")
    def get_workload(self, ref):
        from poddebugger.models import Workload
        return Workload(ref=ref, kind="container", status="running",
                        running=True, image="alpine:latest")
    def get_events(self, ref): return []
    def get_logs(self, ref, tail=200): return ""
    def get_spec(self, ref): return {}

    def exec(self, ref, command):
        self.exec_calls.append(command)
        return self._output


# --- the demos ------------------------------------------------------------


def _ensure_shell_off():
    """Tests / demos that want a clean slate strip `shell` from the catalog
    so we always show the off→on transition explicitly."""
    remediation.CATALOG.pop("shell", None)


def run_demo() -> int:
    """Walk through the off→on→executed→denied lifecycle deterministically."""
    print("===================================================================")
    print("  1. Default state — `shell` is NOT in the catalog")
    print("===================================================================")
    _ensure_shell_off()
    print(f"  shell_action_enabled(): {remediation.shell_action_enabled()}")
    print(f"  list_actions():         {remediation.list_actions()}")
    print(f"  parse_params(\"shell\", [...]) raises:")
    try:
        remediation.parse_params("shell", ["command=echo hi"])
    except remediation.RemediationError as exc:
        print(f"    -> RemediationError: {exc}")
    print()

    print("===================================================================")
    print("  2. Opt in via enable_shell_action()")
    print("     (CLI flag --allow-shell or env PODDEBUGGER_ALLOW_SHELL=1 do this)")
    print("===================================================================")
    remediation.enable_shell_action()
    print(f"  shell_action_enabled(): {remediation.shell_action_enabled()}")
    print(f"  list_actions():         {remediation.list_actions()}")
    spec = remediation.get_spec("shell")
    print(f"  shell spec.risk:        {spec.risk!r}")
    print(f"  shell spec.platforms:   {spec.platforms}")
    print()

    print("===================================================================")
    print("  3. Build a plan — dry-run preview")
    print("===================================================================")
    provider = _StubProvider(exec_output="hostname: container-123\n")
    ref = WorkloadRef(name="pd-shell-demo", platform="podman")
    params = remediation.parse_params(
        "shell", ["command=hostname; uname -r"],
    )
    plan = remediation.make_plan(provider, ref, "shell", params)
    print(remediation.render_plan(plan))
    print()

    print("===================================================================")
    print("  4. Execute through the catalog (gate=AutoApprove)")
    print("===================================================================")
    result = remediation.execute(
        provider, ref, plan,
        gate=approvals.AutoApproveGate(),
    )
    print(f"  executed: {result.executed}")
    print(f"  result:   {result.result!r}")
    print(f"  provider.exec was called with: {provider.exec_calls[-1]}")
    print()

    print("===================================================================")
    print("  5. Same plan, but the gate refuses (DenyGate)")
    print("===================================================================")
    provider2 = _StubProvider(exec_output="(should not see this)")
    result = remediation.execute(
        provider2, ref, plan, gate=approvals.DenyGate(),
    )
    print(f"  executed: {result.executed}")
    print(f"  result:   {result.result}")
    print(f"  provider.exec was called: {bool(provider2.exec_calls)}")
    print("  -> The gate is the safety property once you opt into shell.")
    print()

    print("===================================================================")
    print("  6. A RulesGate with an allow rule lets the same shell run.")
    print("===================================================================")
    rules = [
        {"kind": "remediation", "action": "shell",
         "target": {"platform": "podman", "name": "pd-shell-demo"},
         "decision": "allow"},
    ]
    gate = approvals.RulesGate(approvals.DenyGate(), rules)
    provider3 = _StubProvider(exec_output="OK\n")
    result = remediation.execute(provider3, ref, plan, gate=gate)
    print(f"  executed: {result.executed}")
    print(f"  result:   {result.result!r}")
    print()

    print("===================================================================")
    print("  7. ...and a deny rule on the same descriptor refuses (deny > allow)")
    print("===================================================================")
    rules.append({
        "kind": "remediation", "action": "shell",
        "target": {"platform": "podman", "name": "pd-shell-demo"},
        "decision": "deny",
    })
    gate = approvals.RulesGate(approvals.AutoApproveGate(), rules)
    provider4 = _StubProvider()
    result = remediation.execute(provider4, ref, plan, gate=gate)
    print(f"  executed: {result.executed}")
    print(f"  result:   {result.result}")
    print()

    _print_live_pointer()
    return 0


def _print_live_pointer():
    print("===================================================================")
    print("  Live test against a real Podman container")
    print("===================================================================")
    print("""
  # 1. Start something we can shell into:
  podman run -d --name pd-shell-demo docker.io/library/alpine:latest \\
      tail -f /dev/null

  # 2. Without --allow-shell, argparse refuses the action:
  poddebugger remediate pd-shell-demo --platform podman \\
      --action shell --param command="echo hi"
  # -> error: argument --action: invalid choice: 'shell' (...)

  # 3. Dry-run with --allow-shell — prints the plan but does not run.
  poddebugger remediate pd-shell-demo --platform podman --allow-shell \\
      --action shell --param command="hostname; date"

  # 4. Execute. --yes auto-approves the gate so it runs unattended.
  poddebugger remediate pd-shell-demo --platform podman --allow-shell \\
      --action shell --param command="hostname; date" \\
      --confirm --yes --no-verify

  # 5. Without --yes on a non-TTY (CI, pipe), the gate denies:
  poddebugger remediate pd-shell-demo --platform podman --allow-shell \\
      --action shell --param command="echo no" --confirm --no-verify | cat
  # -> remediation [shell] FAILED: refused by approval gate (deny)

  # Tidy up:
  podman rm -f pd-shell-demo
""")


# --- entry --------------------------------------------------------------


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("--demo", "--gated"):
        print("Hello-World for the opt-in `shell` catalog action.", file=sys.stderr)
        print("", file=sys.stderr)
        print("usage:", file=sys.stderr)
        print("  python shell_action_demo.py --demo   # deterministic walk-through",
              file=sys.stderr)
        return 2
    if argv[0] == "--demo":
        return run_demo()
    if argv[0] == "--gated":
        # Alias for --demo (the demo already shows the gated path); kept for
        # symmetry with custom_gate.py's --apply.
        return run_demo()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
