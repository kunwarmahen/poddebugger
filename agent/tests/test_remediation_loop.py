"""Tests for the Phase 13A adaptive remediation loop + 13C context channel."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from poddebugger.approvals import AutoApproveGate, DenyGate
from poddebugger.llm.base import LLMClient
from poddebugger.models import Diagnosis, Event, Workload, WorkloadRef
from poddebugger.providers.base import ContainerPlatform
from poddebugger.scaffold.engine import InvestigationEngine
from poddebugger.scaffold.state import InvestigationState
from poddebugger.scaffold.workspace import Workspace


# ---------------------------------------------------------------------------
# a provider whose recovery state the test controls + records exec calls
# ---------------------------------------------------------------------------

class _LoopProvider(ContainerPlatform):
    """Podman-shaped. ``running`` flips to True once a fix 'works'."""

    name = "podman"

    def __init__(self, recover_after: int | None = 1):
        # recover_after=1 → workload recovers after the 1st applied fix.
        # None → never recovers.
        self._recover_after = recover_after
        self._applied = 0
        self._running = False
        self.exec_calls: list[list[str]] = []
        self._inspect_cache = {}

    def preflight(self): pass
    def resolve(self, target, namespace=None):
        return WorkloadRef(name=target, platform="podman")

    def get_workload(self, ref):
        return Workload(ref=ref, kind="container",
                        status="running" if self._running else "exited",
                        running=self._running, image="mysql:8",
                        restart_count=0, exit_code=None if self._running else 1)

    def get_events(self, ref): return [
        Event(timestamp="t", type="container", reason="died", message="exit 1")]
    def get_logs(self, ref, tail=200):
        return "[ERROR] no MYSQL_ROOT_PASSWORD set\n"
    def get_spec(self, ref):
        return {"image": "mysql:8", "env": []}

    # podman recreate path uses _inspect + _run
    def _inspect(self, name):
        return {"Name": name, "ImageName": "mysql:8",
                "Config": {"Env": [], "Cmd": None, "Entrypoint": None,
                           "Labels": {}},
                "HostConfig": {"RestartPolicy": {"Name": "always"},
                               "NetworkMode": "bridge"},
                "State": {"Running": self._running}}

    def _run(self, args, check=True):
        from tests.util import cp
        self.exec_calls.append(args)
        if args[:1] == ["rm"]:
            return cp("db\n")
        if args[:1] == ["run"]:
            self._applied += 1
            if (self._recover_after is not None
                    and self._applied >= self._recover_after):
                self._running = True
            return cp("newid\n")
        if args[:1] == ["restart"]:
            self._applied += 1
            if (self._recover_after is not None
                    and self._applied >= self._recover_after):
                self._running = True
            return cp("db\n")
        return cp("")


class _ScriptedLoopLLM(LLMClient):
    """Returns Remediator proposals from a queue; minimal stubs for replan
    roles (Coordinator says done, Reporter echoes the diagnosis)."""

    name = "loop-scripted"
    model_id = "scripted-1"

    def __init__(self, proposals: list[dict]):
        self._proposals = list(proposals)
        self.remediator_calls = 0

    def complete(self, system, user):
        if "ROLE: Remediator" in system:
            self.remediator_calls += 1
            if self._proposals:
                return json.dumps(self._proposals.pop(0))
            return json.dumps({"action": "none", "reason": "out of ideas"})
        if "ROLE: Coordinator" in system:
            return json.dumps({"action": "done", "target": "", "instruction": "",
                               "reason": "nothing more to do"})
        if "ROLE: Reporter" in system:
            return json.dumps({
                "summary": "missing env", "root_cause": "no root password",
                "confidence": 0.9, "evidence": [], "suggested_fixes": [],
                "needs_deep_inspection": False})
        if "ROLE: Auditor" in system:
            return json.dumps({"assessment": "sound", "critiques": []})
        # Analyst / Verifier / others during replan: harmless stubs.
        if "ROLE: Analyst" in system:
            return json.dumps({"hypotheses": [], "new_evidence": []})
        if "ROLE: Verifier" in system:
            return json.dumps({"verdict": "INCONCLUSIVE", "confidence": 0.0,
                               "note": "", "suggested_probe": ""})
        return json.dumps({})


def _engine(llm, *, base, context=None, max_attempts=3, gate=None):
    eng = InvestigationEngine(
        _LoopProvider(recover_after=None) if False else None, llm,
        workspace_base=base, remediation_enabled=True,
        context=context, max_remediation_attempts=max_attempts,
        gate=gate or AutoApproveGate(),
    )
    return eng


def _seed(eng, provider):
    eng.provider = provider
    eng._ref = WorkloadRef(name="db", platform="podman")
    eng.state = InvestigationState(target="db", platform="podman")
    eng.workspace = Workspace.create("db", base=eng.workspace_base)
    return eng


DIAG = Diagnosis(summary="missing env", root_cause="no root password",
                 confidence=0.9)


# verify_recovery sleeps `verify_wait` seconds; patch it out so the loop
# tests run instantly.
def setUpModule():
    global _sleep_patch
    _sleep_patch = mock.patch("poddebugger.remediation.time.sleep",
                              lambda *a, **k: None)
    _sleep_patch.start()


def tearDownModule():
    _sleep_patch.stop()


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

class ResolveFirstTryTest(unittest.TestCase):
    def test_recovers_on_first_fix(self):
        provider = _LoopProvider(recover_after=1)
        llm = _ScriptedLoopLLM([
            {"action": "set-env", "params": {
                "container": "db", "env": {"MYSQL_ROOT_PASSWORD": "secret"}},
             "rationale": "set the missing password", "confidence": 0.8},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            _seed(eng, provider)
            out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(out["outcome"], "resolved")
        self.assertEqual(len(out["attempts"]), 1)
        self.assertEqual(llm.remediator_calls, 1)


class ResolveSecondTryTest(unittest.TestCase):
    def test_first_fix_fails_then_second_recovers(self):
        # recover only after the 2nd applied fix.
        provider = _LoopProvider(recover_after=2)
        llm = _ScriptedLoopLLM([
            {"action": "set-env", "params": {
                "container": "db", "env": {"MYSQL_ROOT_PASSWORD": "secret"}},
             "rationale": "set password", "confidence": 0.7},
            {"action": "set-env", "params": {
                "container": "db", "env": {"MYSQL_DATABASE": "app"}},
             "rationale": "also set the db name", "confidence": 0.7},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            _seed(eng, provider)
            out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(out["outcome"], "resolved")
        self.assertEqual(len(out["attempts"]), 2)
        # The failed-fix evidence was recorded before the 2nd attempt.
        self.assertTrue(any("did not recover" in e.summary
                            for e in eng.state.evidence))


class GiveUpAfterBudgetTest(unittest.TestCase):
    def test_unresolved_when_nothing_works(self):
        provider = _LoopProvider(recover_after=None)  # never recovers
        llm = _ScriptedLoopLLM([
            {"action": "restart", "params": {}, "confidence": 0.5},
            {"action": "restart", "params": {}, "confidence": 0.5},
            {"action": "restart", "params": {}, "confidence": 0.5},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp), max_attempts=3)
            _seed(eng, provider)
            out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(out["outcome"], "unresolved")
        self.assertEqual(len(out["attempts"]), 3)


class AgentGivesUpHonestlyTest(unittest.TestCase):
    def test_action_none_after_a_failed_attempt_is_unresolved(self):
        provider = _LoopProvider(recover_after=None)
        llm = _ScriptedLoopLLM([
            {"action": "restart", "params": {}, "confidence": 0.4},
            {"action": "none", "reason": "the diagnosis seems wrong; restart "
                                        "did not help and I have no better idea"},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp), max_attempts=3)
            _seed(eng, provider)
            out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(out["outcome"], "unresolved")
        self.assertEqual(len(out["attempts"]), 1)  # only the restart was applied


class NeedsContextTest(unittest.TestCase):
    def test_agent_requests_missing_value(self):
        provider = _LoopProvider(recover_after=1)
        llm = _ScriptedLoopLLM([
            {"action": "none", "reason": "missing context value",
             "needs_context": [{"key": "db_password",
                                "reason": "MySQL root password for set-env"}]},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            _seed(eng, provider)
            out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(out["outcome"], "needs_context")
        self.assertEqual(out["needs_context"][0]["key"], "db_password")
        self.assertEqual(len(out["attempts"]), 0)   # nothing applied

    def test_context_is_passed_to_the_agent(self):
        # When context is supplied, the Remediator's prompt should contain it.
        provider = _LoopProvider(recover_after=1)
        captured = {}

        class _CaptureLLM(_ScriptedLoopLLM):
            def complete(self, system, user):
                if "ROLE: Remediator" in system:
                    captured["prompt"] = user
                return super().complete(system, user)

        llm = _CaptureLLM([
            {"action": "set-env", "params": {
                "container": "db", "env": {"MYSQL_ROOT_PASSWORD": "secret"}},
             "confidence": 0.8},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp),
                          context={"db_password": "secret"})
            _seed(eng, provider)
            eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertIn("db_password", captured["prompt"])
        self.assertIn("secret", captured["prompt"])


class ProposeOnlyTest(unittest.TestCase):
    def test_apply_false_just_proposes(self):
        provider = _LoopProvider(recover_after=1)
        llm = _ScriptedLoopLLM([
            {"action": "restart", "params": {}, "confidence": 0.6},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            _seed(eng, provider)
            out = eng.remediate(DIAG, apply=False)
        self.assertEqual(out["outcome"], "proposed")
        self.assertEqual(out["final_proposal"]["action"], "restart")
        self.assertEqual(provider.exec_calls, [])   # nothing executed


class GateRefusalTest(unittest.TestCase):
    def test_gate_deny_stops_the_loop(self):
        provider = _LoopProvider(recover_after=1)
        llm = _ScriptedLoopLLM([
            {"action": "restart", "params": {}, "confidence": 0.6},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp), gate=DenyGate())
            _seed(eng, provider)
            out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(out["outcome"], "refused")
        # The restart command never reached the provider.
        self.assertNotIn(["restart", "db"], provider.exec_calls)


if __name__ == "__main__":
    unittest.main()
