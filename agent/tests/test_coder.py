"""Tests for Stage 13D — the Coder agent + sandbox runner (HLD §18.6)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from poddebugger.approvals import (
    AutoApproveGate,
    DenyGate,
    RulesGate,
    _render_descriptor,
)
from poddebugger.llm.base import LLMError
from poddebugger.models import Workload, WorkloadRef
from poddebugger.scaffold import sandbox
from poddebugger.scaffold.engine import InvestigationEngine
from poddebugger.scaffold.sandbox import CodeResult, run_code, script_hash

from tests.test_scaffold_engine import FakeProvider, ScriptedLLM
from tests.util import cp


class _Provider:
    """Minimal provider with the bits the sandbox touches."""

    _bin = "podman"

    def __init__(self, running=True):
        self._running = running

    def get_workload(self, ref):
        return Workload(ref=ref, running=self._running,
                        status="running" if self._running else "exited")


def _podman_ref(name="db"):
    return WorkloadRef(name=name, platform="podman")


def _k8s_ref():
    return WorkloadRef(name="web-1", namespace="prod", container="app",
                       platform="kubernetes")


# --- sandbox runner -----------------------------------------------------------


class GateBehaviorTest(unittest.TestCase):
    def test_no_gate_is_deny_by_default(self):
        r = run_code(_Provider(), _podman_ref(), "bash", "echo hi")
        self.assertTrue(r.denied)
        self.assertIn("deny-by-default", r.error)

    def test_gate_deny_blocks_without_executing(self):
        with mock.patch.object(sandbox.subprocess, "run") as sub:
            r = run_code(_Provider(), _podman_ref(), "bash", "echo hi",
                         gate=DenyGate())
        self.assertTrue(r.denied)
        sub.assert_not_called()

    def test_rules_gate_allows_exact_script_hash_only(self):
        digest = script_hash("bash", "echo hi")[:12]
        rules = [{"kind": "code", "action": f"bash:{digest}",
                  "target": {}, "decision": "allow"}]
        gate = RulesGate(DenyGate(), rules)
        with mock.patch.object(sandbox.subprocess, "run",
                               return_value=cp("hi\n")):
            allowed = run_code(_Provider(), _podman_ref(), "bash", "echo hi",
                               gate=gate)
            blocked = run_code(_Provider(), _podman_ref(), "bash", "echo bye",
                               gate=gate)
        self.assertTrue(allowed.executed)
        self.assertTrue(blocked.denied)  # different hash -> inner DenyGate

    def test_descriptor_prompt_shows_the_script(self):
        ref = _podman_ref()
        d = sandbox._gate_descriptor(ref, "bash", "echo one\necho two",
                                     "probe", script_hash("bash", "x"))
        text = _render_descriptor(d)
        self.assertIn("code → bash:", text)
        self.assertIn("| echo one", text)
        self.assertIn("| echo two", text)
        self.assertIn("risk: high", text)


class ArgvTest(unittest.TestCase):
    def _argv_for(self, provider, ref, language="bash"):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return cp("ok\n")

        with mock.patch.object(sandbox.subprocess, "run", fake_run):
            run_code(provider, ref, language, "echo hi",
                     gate=AutoApproveGate(), image="img:1")
        return captured["argv"]

    def test_podman_running_target_joins_its_network(self):
        argv = self._argv_for(_Provider(running=True), _podman_ref())
        self.assertEqual(argv[:3], ["podman", "run", "--rm"])
        self.assertIn("container:db", " ".join(argv))
        self.assertEqual(argv[-3:], ["sh", "-c", "echo hi"])
        self.assertIn("img:1", argv)

    def test_podman_exited_target_uses_default_network(self):
        argv = self._argv_for(_Provider(running=False), _podman_ref())
        self.assertNotIn("--network", argv)

    def test_kubernetes_uses_ephemeral_debug_container(self):
        provider = _Provider()
        provider._bin = "kubectl"
        argv = self._argv_for(provider, _k8s_ref(), language="python")
        joined = " ".join(argv)
        self.assertEqual(argv[:3], ["kubectl", "debug", "web-1"])
        self.assertIn("-n prod", joined)
        self.assertIn("--image=img:1", joined)
        self.assertIn("--target=app", joined)
        self.assertIn("--attach", joined)
        self.assertEqual(argv[-3:], ["python3", "-c", "echo hi"])


class ExecutionTest(unittest.TestCase):
    def _run(self, proc_result, **kw):
        with mock.patch.object(sandbox.subprocess, "run",
                               return_value=proc_result):
            return run_code(_Provider(), _podman_ref(), "bash", "x",
                            gate=AutoApproveGate(), **kw)

    def test_captures_output_and_exit_code(self):
        r = self._run(cp("out line\n", rc=3, stderr="err line\n"))
        self.assertTrue(r.executed)
        self.assertEqual(r.exit_code, 3)  # a failing script is information
        self.assertIn("out line", r.output)
        self.assertIn("err line", r.output)

    def test_output_truncated(self):
        r = self._run(cp("x" * (sandbox.OUTPUT_CAP + 500)))
        self.assertIn("(output truncated)", r.output)
        self.assertLessEqual(len(r.output), sandbox.OUTPUT_CAP + 50)

    def test_timeout_becomes_error(self):
        with mock.patch.object(
                sandbox.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("podman", 120)):
            r = run_code(_Provider(), _podman_ref(), "bash", "x",
                         gate=AutoApproveGate())
        self.assertFalse(r.executed)
        self.assertIn("timed out", r.error)

    def test_missing_binary_becomes_error(self):
        with mock.patch.object(sandbox.subprocess, "run",
                               side_effect=FileNotFoundError):
            r = run_code(_Provider(), _podman_ref(), "bash", "x",
                         gate=AutoApproveGate())
        self.assertIn("not found", r.error)

    def test_input_validation(self):
        self.assertIn("unsupported language",
                      run_code(_Provider(), _podman_ref(), "perl", "x",
                               gate=AutoApproveGate()).error)
        self.assertIn("empty script",
                      run_code(_Provider(), _podman_ref(), "bash", "  ",
                               gate=AutoApproveGate()).error)

    def test_hash_depends_on_language_and_body(self):
        self.assertNotEqual(script_hash("bash", "x"), script_hash("python", "x"))
        self.assertNotEqual(script_hash("bash", "x"), script_hash("bash", "y"))


# --- engine dispatch ------------------------------------------------------------


class CoderScriptedLLM(ScriptedLLM):
    """ScriptedLLM that also answers the Coder role from a queue."""

    def __init__(self, coordinator_actions, coder_answers):
        super().__init__(coordinator_actions=coordinator_actions,
                         verifier_verdicts=[])
        self._coder = list(coder_answers)

    def complete(self, system, user):
        if "ROLE: Coder" in system:
            return json.dumps(self._coder.pop(0))
        return super().complete(system, user)


def _code_action(instruction="probe the db port"):
    return {"action": "code", "target": "", "instruction": instruction,
            "reason": "need a custom probe"}


def _done():
    return {"action": "done", "target": "", "instruction": "", "reason": "ok"}


PROPOSAL = {"language": "bash", "script": "nc -z db 5432; echo rc=$?",
            "purpose": "probe", "rationale": "test the port"}


class EngineCoderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _engine(self, llm, **kw):
        kw.setdefault("coder_enabled", True)
        kw.setdefault("gate", AutoApproveGate())
        return InvestigationEngine(FakeProvider(), llm,
                                   workspace_base=self.base, **kw)

    def test_menu_gating(self):
        llm = CoderScriptedLLM([_done()], [])
        on = self._engine(llm)
        off = self._engine(llm, coder_enabled=False)
        self.assertIn("code", [n for n, _ in on._action_menu()])
        self.assertNotIn("code", [n for n, _ in off._action_menu()])

    def test_dispatch_runs_script_and_records_evidence(self):
        llm = CoderScriptedLLM([_code_action(), _done()], [PROPOSAL])
        eng = self._engine(llm)
        digest = script_hash("bash", PROPOSAL["script"])
        with mock.patch.object(
                sandbox, "run_code",
                return_value=CodeResult(executed=True, output="rc=0",
                                        exit_code=0, hash=digest)) as rc:
            eng.investigate(WorkloadRef(name="c1", platform="fake"))
        rc.assert_called_once()
        self.assertIs(rc.call_args.kwargs["gate"], eng.gate)
        ev = [e for e in eng.state.evidence
              if e.source == f"coder:probe:{digest[:8]}"]
        self.assertEqual(len(ev), 1)
        self.assertIn("rc=0", ev[0].detail)
        self.assertIn(PROPOSAL["script"], ev[0].detail)

    def test_identical_script_runs_once(self):
        llm = CoderScriptedLLM([_code_action(), _code_action(), _done()],
                               [PROPOSAL, PROPOSAL])
        eng = self._engine(llm)
        with mock.patch.object(
                sandbox, "run_code",
                return_value=CodeResult(executed=True, output="x",
                                        exit_code=0)) as rc:
            eng.investigate(WorkloadRef(name="c1", platform="fake"))
        self.assertEqual(rc.call_count, 1)

    def test_coder_declines_honestly(self):
        llm = CoderScriptedLLM(
            [_code_action(), _done()],
            [{"script": "", "reason": "the probes already cover this"}])
        eng = self._engine(llm)
        with mock.patch.object(sandbox, "run_code") as rc:
            eng.investigate(WorkloadRef(name="c1", platform="fake"))
        rc.assert_not_called()
        self.assertTrue(any(d.role == "Coder" and d.action == "skip"
                            for d in eng.state.dispatch_history))

    def test_denied_script_becomes_note_not_evidence(self):
        llm = CoderScriptedLLM([_code_action(), _done()], [PROPOSAL])
        eng = self._engine(llm)
        with mock.patch.object(
                sandbox, "run_code",
                return_value=CodeResult(denied=True,
                                        error="denied by approval gate")):
            eng.investigate(WorkloadRef(name="c1", platform="fake"))
        self.assertTrue(any("not run" in n for n in eng.state.notes))
        self.assertFalse(any(e.source.startswith("coder:probe")
                             for e in eng.state.evidence))

    def test_failed_run_becomes_error_evidence(self):
        llm = CoderScriptedLLM([_code_action(), _done()], [PROPOSAL])
        eng = self._engine(llm)
        with mock.patch.object(
                sandbox, "run_code",
                return_value=CodeResult(error="'podman' not found on PATH")):
            eng.investigate(WorkloadRef(name="c1", platform="fake"))
        self.assertTrue(any(e.source == "coder:error"
                            for e in eng.state.evidence))


if __name__ == "__main__":
    unittest.main()
