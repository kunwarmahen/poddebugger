"""Phase 11B tests — deep-inspection probes routed through the approval gate."""

from __future__ import annotations

import unittest

from poddebugger import deepinspect
from poddebugger.approvals import (
    ActionDescriptor,
    ApprovalGate,
    AutoApproveGate,
    Decision,
    DenyGate,
)
from poddebugger.models import WorkloadRef
from poddebugger.providers.base import ContainerPlatform
from poddebugger.scaffold import probes as scaffold_probes


class _RecordingGate(ApprovalGate):
    def __init__(self, decision: Decision):
        self.calls: list[ActionDescriptor] = []
        self._decision = decision

    def request(self, descriptor: ActionDescriptor) -> Decision:
        self.calls.append(descriptor)
        return self._decision


class _StubProvider(ContainerPlatform):
    name = "podman"

    def __init__(self):
        self.exec_calls: list[list[str]] = []

    def preflight(self): pass
    def resolve(self, target, namespace=None): return WorkloadRef(name=target, platform="podman")
    def get_workload(self, ref): raise NotImplementedError
    def get_events(self, ref): return []
    def get_logs(self, ref, tail=200): return ""
    def get_spec(self, ref): return {}

    def exec(self, ref, command):
        self.exec_calls.append(command)
        # Shell-check probe (`echo poddebugger-shell-ok`) must succeed for
        # deepinspect to proceed past its initial sniff.
        if len(command) >= 3 and "poddebugger-shell-ok" in command[2]:
            return "poddebugger-shell-ok"
        # Subsequent probes just return placeholder output.
        return f"output for: {command[-1][:30]}"


class DeepInspectGatedTest(unittest.TestCase):
    def test_no_gate_runs_all_probes_legacy(self):
        # gate=None preserves pre-Phase-11 behavior — every probe runs.
        prov = _StubProvider()
        result = deepinspect.run(prov, WorkloadRef(name="web", platform="podman"))
        self.assertEqual(len(result), len(deepinspect.PROBES))
        # The shell check + one exec per probe = len(PROBES) + 1.
        self.assertEqual(len(prov.exec_calls), len(deepinspect.PROBES) + 1)

    def test_auto_approve_gate_runs_all_probes(self):
        prov = _StubProvider()
        gate = AutoApproveGate()
        result = deepinspect.run(prov, WorkloadRef(name="web", platform="podman"),
                                 gate=gate)
        self.assertEqual(len(result), len(deepinspect.PROBES))

    def test_deny_gate_skips_all_probes(self):
        prov = _StubProvider()
        gate = DenyGate()
        result = deepinspect.run(prov, WorkloadRef(name="web", platform="podman"),
                                 gate=gate)
        # Single "deep_inspect: refused..." entry, no probes ran.
        self.assertEqual(set(result.keys()), {"deep_inspect"})
        self.assertIn("refused", result["deep_inspect"])
        self.assertEqual(prov.exec_calls, [])     # not even the shell check ran

    def test_gate_receives_a_single_bundled_descriptor(self):
        prov = _StubProvider()
        gate = _RecordingGate(Decision.ALLOW_ONCE)
        deepinspect.run(prov, WorkloadRef(name="web", platform="podman"),
                        gate=gate)
        # Exactly one descriptor — the whole bundle, not 9 separate ones.
        self.assertEqual(len(gate.calls), 1)
        d = gate.calls[0]
        self.assertEqual(d.kind, "probe")
        self.assertEqual(d.action, "deep_inspect")
        self.assertEqual(d.target.name, "web")
        self.assertIn(str(len(deepinspect.PROBES)), d.summary)


class RunProbeGatingTest(unittest.TestCase):
    """run_probe gates `deep_inspect` only — the read-only probes aren't gated."""

    def test_logs_more_does_not_consult_gate(self):
        class _ReadOnlyProvider(_StubProvider):
            def get_logs(self, ref, tail=200): return "log line"

        gate = _RecordingGate(Decision.DENY)
        out = scaffold_probes.run_probe(
            "logs_more", _ReadOnlyProvider(),
            WorkloadRef(name="web", platform="podman"), gate=gate,
        )
        self.assertIn("log line", out)
        self.assertEqual(gate.calls, [])

    def test_recheck_status_does_not_consult_gate(self):
        class _ReadOnlyProvider(_StubProvider):
            def get_workload(self, ref):
                from poddebugger.models import Workload
                return Workload(ref=ref, kind="container", status="running",
                                running=True, image="img:1")

        gate = _RecordingGate(Decision.DENY)
        out = scaffold_probes.run_probe(
            "recheck_status", _ReadOnlyProvider(),
            WorkloadRef(name="web", platform="podman"), gate=gate,
        )
        self.assertIn("running", out)
        self.assertEqual(gate.calls, [])

    def test_deep_inspect_consults_gate(self):
        gate = _RecordingGate(Decision.DENY)
        out = scaffold_probes.run_probe(
            "deep_inspect", _StubProvider(),
            WorkloadRef(name="web", platform="podman"), gate=gate,
        )
        self.assertIn("refused", out)
        self.assertEqual(len(gate.calls), 1)
        self.assertEqual(gate.calls[0].action, "deep_inspect")


if __name__ == "__main__":
    unittest.main()
