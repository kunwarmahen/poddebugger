"""Unit tests for the investigation engine — the core agent loop.

A ScriptedLLM returns role-appropriate JSON (dispatched on the role marker in
each system prompt), so the whole loop runs deterministically with no network.
"""

import json
import tempfile
import unittest
from pathlib import Path

from poddebugger.llm.base import LLMClient, LLMError
from poddebugger.models import Event, WorkloadRef, Workload
from poddebugger.providers.base import ContainerPlatform
from poddebugger.scaffold.engine import InvestigationEngine


class FakeProvider(ContainerPlatform):
    name = "fake"

    def __init__(self, running=False):
        self._running = running

    def preflight(self):
        pass

    def resolve(self, target, namespace=None):
        return WorkloadRef(name=target, platform="fake")

    def get_workload(self, ref):
        return Workload(ref=ref, kind="container", status="exited",
                        running=self._running, image="img:1",
                        restart_count=3, exit_code=1)

    def get_events(self, ref):
        return [Event(timestamp="t", type="container", reason="died",
                      message="exit_code=1")]

    def get_logs(self, ref, tail=200):
        return "boot: starting\nERROR: connection refused to db:5432\n"

    def get_spec(self, ref):
        return {"image": "img:1", "command": ["/app/server"]}


class ScriptedLLM(LLMClient):
    """Returns canned JSON per role; Coordinator/Verifier vary across calls."""

    name = "scripted"
    model_id = "scripted-1"

    def __init__(self, coordinator_actions, verifier_verdicts, fail_roles=None,
                 auditor=None, adjudicator=None):
        self._coord = list(coordinator_actions)
        self._verdicts = list(verifier_verdicts)
        self._fail_roles = set(fail_roles or ())
        self._auditor = auditor or {"assessment": "sound", "critiques": []}
        self._adjudicator = adjudicator or {"ruling": "dismiss", "reason": "unfounded"}
        self.role_calls = []

    def complete(self, system, user):
        for role in ("Scout", "Planner", "Coordinator", "Analyst", "Prober",
                     "Verifier", "Auditor", "Adjudicator", "Reporter"):
            if f"ROLE: {role}" in system and role in self._fail_roles:
                raise LLMError(f"simulated {role} failure")
        if "ROLE: Scout" in system:
            self.role_calls.append("Scout")
            return json.dumps({
                "classification": "CrashLoopBackOff",
                "evidence": ["log line: connection refused to db:5432"],
                "leads": ["check database connectivity"],
            })
        if "ROLE: Planner" in system:
            self.role_calls.append("Planner")
            return json.dumps({
                "strategy": "confirm the DB connectivity failure",
                "sanity_checks": ["exit code 1 must match the stated cause"],
            })
        if "ROLE: Coordinator" in system:
            self.role_calls.append("Coordinator")
            return json.dumps(self._coord.pop(0))
        if "ROLE: Analyst" in system:
            self.role_calls.append("Analyst")
            return json.dumps({
                "hypotheses": [{
                    "statement": "the app exits because db:5432 is unreachable",
                    "test": "probe the database port",
                    "evidence": ["E1"],
                }],
                "new_evidence": [],
            })
        if "ROLE: Prober" in system:
            self.role_calls.append("Prober")
            return json.dumps({"probe": "recheck_status", "reason": "confirm exit"})
        if "ROLE: Verifier" in system:
            self.role_calls.append("Verifier")
            return json.dumps(self._verdicts.pop(0))
        if "ROLE: Auditor" in system:
            self.role_calls.append("Auditor")
            return json.dumps(self._auditor)
        if "ROLE: Adjudicator" in system:
            self.role_calls.append("Adjudicator")
            return json.dumps(self._adjudicator)
        if "ROLE: Reporter" in system:
            self.role_calls.append("Reporter")
            return json.dumps({
                "summary": "container crashes — database unreachable",
                "root_cause": "db:5432 refused the connection at startup",
                "confidence": 0.9,
                "evidence": ["connection refused to db:5432"],
                "suggested_fixes": [
                    {"action": "start the db service", "rationale": "app needs it",
                     "risk": "low"}
                ],
                "needs_deep_inspection": False,
            })
        raise AssertionError(f"unexpected role; system began: {system[:60]!r}")


def _engine(llm, **kw):
    return InvestigationEngine(FakeProvider(), llm, workspace_base=kw.pop("base"), **kw)


class HappyPathTest(unittest.TestCase):
    def test_analyze_verify_report(self):
        llm = ScriptedLLM(
            coordinator_actions=[
                {"action": "analyze", "target": "L1", "instruction": "check DB",
                 "reason": "open lead"},
                {"action": "done", "target": "", "instruction": "", "reason": "confirmed"},
            ],
            verifier_verdicts=[{"verdict": "VERIFIED", "confidence": 0.9,
                                "note": "logs are conclusive", "suggested_probe": ""}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            diag = eng.investigate(WorkloadRef(name="web", platform="fake"))

        self.assertIn("db:5432", diag.root_cause)
        self.assertEqual(diag.confidence, 0.9)
        # lifecycle: one hypothesis got promoted to a finding
        self.assertEqual(len(eng.state.confirmed_findings), 1)
        self.assertEqual(eng.state.hypotheses, [])
        self.assertEqual(eng.state.phase, "done")
        # the L1 lead was marked pursued
        self.assertEqual(eng.state.leads[0].status, "pursued")
        # every role was exercised
        for role in ("Scout", "Planner", "Coordinator", "Analyst",
                     "Verifier", "Reporter"):
            self.assertIn(role, llm.role_calls)


class GroundedVerificationTest(unittest.TestCase):
    def test_inconclusive_triggers_probe_then_reverify(self):
        llm = ScriptedLLM(
            coordinator_actions=[
                {"action": "analyze", "target": "L1", "instruction": "x", "reason": "y"},
                {"action": "done", "target": "", "instruction": "", "reason": "z"},
            ],
            verifier_verdicts=[
                {"verdict": "INCONCLUSIVE", "confidence": 0.4, "note": "need to confirm",
                 "suggested_probe": "recheck_status"},
                {"verdict": "VERIFIED", "confidence": 0.85, "note": "confirmed by probe",
                 "suggested_probe": ""},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            eng.investigate(WorkloadRef(name="web", platform="fake"))

        # the suggested probe ran and produced evidence
        self.assertTrue(any(e.source == "prober:recheck_status"
                            for e in eng.state.evidence))
        # re-verify promoted the hypothesis
        self.assertEqual(len(eng.state.confirmed_findings), 1)
        self.assertEqual(llm.role_calls.count("Verifier"), 2)


class ResilienceTest(unittest.TestCase):
    """No single failing agent may derail the investigation (HLD §11.1 #2)."""

    _coord = [
        {"action": "analyze", "target": "L1", "instruction": "x", "reason": "y"},
        {"action": "done", "target": "", "instruction": "", "reason": "z"},
    ]

    def test_verifier_failure_leaves_hypothesis_inconclusive(self):
        llm = ScriptedLLM(self._coord, verifier_verdicts=[], fail_roles={"Verifier"})
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            diag = eng.investigate(WorkloadRef(name="web", platform="fake"))
        self.assertIsNotNone(diag)                       # still produced a result
        self.assertEqual(eng.state.hypotheses[0].status, "inconclusive")
        self.assertTrue(any("Verifier agent failed" in n for n in eng.state.notes))

    def test_reporter_failure_falls_back_to_state(self):
        llm = ScriptedLLM(
            self._coord,
            verifier_verdicts=[{"verdict": "VERIFIED", "confidence": 0.8,
                                "note": "ok", "suggested_probe": ""}],
            fail_roles={"Reporter"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            diag = eng.investigate(WorkloadRef(name="web", platform="fake"))
        # fallback Diagnosis built from the confirmed finding
        self.assertTrue(diag.root_cause)
        self.assertEqual(diag.confidence, 0.8)


class OversightTest(unittest.TestCase):
    """Stage C — the Auditor critiques findings; the Adjudicator arbitrates."""

    _coord = [
        {"action": "analyze", "target": "L1", "instruction": "x", "reason": "y"},
        {"action": "done", "target": "", "instruction": "", "reason": "z"},
    ]
    _verified = [{"verdict": "VERIFIED", "confidence": 0.8, "note": "ok",
                  "suggested_probe": ""}]

    def test_upheld_critique_demotes_the_finding(self):
        llm = ScriptedLLM(
            self._coord, self._verified,
            auditor={"assessment": "thin",
                     "critiques": [{"target": "F1", "concern": "evidence is weak"}]},
            adjudicator={"ruling": "uphold", "reason": "not adequately supported"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            eng.investigate(WorkloadRef(name="web", platform="fake"))
        self.assertEqual(len(eng.state.confirmed_findings), 0)   # demoted
        self.assertEqual(len(eng.state.ruled_out), 1)
        self.assertIn("adjudicated", eng.state.ruled_out[0].reason)
        self.assertIn("Adjudicator", llm.role_calls)

    def test_dismissed_critique_keeps_the_finding(self):
        llm = ScriptedLLM(
            self._coord, self._verified,
            auditor={"assessment": "ok",
                     "critiques": [{"target": "F1", "concern": "double-check this"}]},
            adjudicator={"ruling": "dismiss", "reason": "evidence is solid"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            eng.investigate(WorkloadRef(name="web", platform="fake"))
        self.assertEqual(len(eng.state.confirmed_findings), 1)   # finding stands
        self.assertEqual(len(eng.state.ruled_out), 0)

    def test_strategy_critique_recorded_as_note(self):
        llm = ScriptedLLM(
            self._coord, self._verified,
            auditor={"assessment": "process issue",
                     "critiques": [{"target": "strategy",
                                    "concern": "no probing was done"}]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp))
            eng.investigate(WorkloadRef(name="web", platform="fake"))
        self.assertTrue(any("no probing was done" in n for n in eng.state.notes))
        self.assertEqual(len(eng.state.confirmed_findings), 1)   # untouched


class BudgetTest(unittest.TestCase):
    def test_iteration_cap_terminates(self):
        # Coordinator always wants to analyze; the cap must stop the loop.
        always_analyze = [{"action": "analyze", "target": "", "instruction": "x",
                           "reason": "y"}] * 20
        verdicts = [{"verdict": "INCONCLUSIVE", "confidence": 0.3, "note": "n",
                     "suggested_probe": ""}] * 20
        llm = ScriptedLLM(always_analyze, verdicts)
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp), max_iterations=3)
            diag = eng.investigate(WorkloadRef(name="web", platform="fake"))
        self.assertLessEqual(eng.state.iteration, 3)
        self.assertIsNotNone(diag)  # still produces a diagnosis

    def test_early_stop_once_findings_explain_the_failure(self):
        # Coordinator never says done; the engine must stop on its own once a
        # finding exists, well before max_iterations.
        llm = ScriptedLLM(
            coordinator_actions=[{"action": "analyze", "target": "", "instruction": "x",
                                  "reason": "y"}] * 12,
            verifier_verdicts=[{"verdict": "VERIFIED", "confidence": 0.8, "note": "ok",
                                "suggested_probe": ""}] * 12,
        )
        with tempfile.TemporaryDirectory() as tmp:
            eng = _engine(llm, base=Path(tmp), max_iterations=10)
            eng.investigate(WorkloadRef(name="web", platform="fake"))
        self.assertLessEqual(eng.state.iteration, 3)        # stopped early
        self.assertGreaterEqual(len(eng.state.confirmed_findings), 1)


if __name__ == "__main__":
    unittest.main()
