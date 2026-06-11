"""Engine wiring tests for Phase 15A — Historian recall + outcome recording."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from poddebugger.approvals import AutoApproveGate
from poddebugger.experience import ExperienceStore, make_record
from poddebugger.models import WorkloadRef
from poddebugger.scaffold.engine import InvestigationEngine
from poddebugger.scaffold.state import InvestigationState
from poddebugger.scaffold.workspace import Workspace

from tests.test_remediation_loop import DIAG, _LoopProvider, _ScriptedLoopLLM
from tests.test_scaffold_engine import FakeProvider, ScriptedLLM


def _coord_done():
    return [{"action": "done", "target": "", "instruction": "", "reason": "ok"}]


def _matching_record(**over):
    """A record similar to what FakeProvider/ScriptedLLM produce."""
    sig = {
        "platform": "fake",
        "classification": "CrashLoopBackOff",
        "image": "img:1",
        "exit_code": 1,
        "oom_killed": False,
        "keywords": ["connection", "refused"],
    }
    rec = make_record(sig, summary="same crash seen before",
                      root_cause="db was down",
                      attempts=[{"action": "restart", "params": {},
                                 "outcome": "still-failing"}],
                      outcome="unresolved")
    for k, v in over.items():
        setattr(rec, k, v)
    return rec


# verify_recovery sleeps; patch it out so remediation tests run instantly.
def setUpModule():
    global _sleep_patch
    _sleep_patch = mock.patch("poddebugger.remediation.time.sleep",
                              lambda *a, **k: None)
    _sleep_patch.start()


def tearDownModule():
    _sleep_patch.stop()


# --- recall (investigate path) -------------------------------------------------


class RecallTest(unittest.TestCase):
    def _investigate(self, store):
        llm = ScriptedLLM(coordinator_actions=_coord_done(), verifier_verdicts=[])
        eng = InvestigationEngine(
            FakeProvider(), llm, workspace_base=self._tmp,
            learning_enabled=store is not None, experience_store=store,
        )
        eng.investigate(WorkloadRef(name="c1", platform="fake"))
        return eng

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name) / "runs"
        self.addCleanup(self._tmpdir.cleanup)

    def test_recalls_similar_record_as_evidence(self):
        store = ExperienceStore(Path(self._tmpdir.name) / "exp")
        store.save(_matching_record())
        eng = self._investigate(store)
        recalled = [e for e in eng.state.evidence
                    if e.source.startswith("experience:")]
        self.assertEqual(len(recalled), 1)
        self.assertIn("did NOT work", recalled[0].summary)
        self.assertIn("restart", recalled[0].detail)
        self.assertTrue(any(d.role == "Historian"
                            for d in eng.state.dispatch_history))

    def test_unrelated_record_not_recalled(self):
        store = ExperienceStore(Path(self._tmpdir.name) / "exp")
        store.save(_matching_record(classification="ImagePullBackOff",
                                    image="other:9", exit_code=42,
                                    keywords=["registry", "auth"]))
        eng = self._investigate(store)
        self.assertFalse(any(e.source.startswith("experience:")
                             for e in eng.state.evidence))

    def test_disabled_by_default(self):
        eng = self._investigate(None)
        self.assertFalse(eng.learning_enabled)
        self.assertFalse(any(e.source.startswith("experience:")
                             for e in eng.state.evidence))

    def test_recall_failure_degrades_to_note(self):
        class BrokenStore(ExperienceStore):
            def find_similar(self, signature, k=3):
                raise RuntimeError("disk on fire")

        store = BrokenStore(Path(self._tmpdir.name) / "exp")
        eng = self._investigate(store)
        self.assertTrue(any("experience recall failed" in n
                            for n in eng.state.notes))

    def test_signature_captured_for_later_recording(self):
        store = ExperienceStore(Path(self._tmpdir.name) / "exp")
        eng = self._investigate(store)
        self.assertEqual(eng._signature["classification"], "CrashLoopBackOff")
        self.assertEqual(eng._signature["exit_code"], 1)


# --- recording (remediate path) -------------------------------------------------


def _loop_engine(llm, store, *, base, max_attempts=3):
    eng = InvestigationEngine(
        None, llm, workspace_base=base, remediation_enabled=True,
        max_remediation_attempts=max_attempts, gate=AutoApproveGate(),
        learning_enabled=store is not None, experience_store=store,
    )
    provider = _LoopProvider(recover_after=1)
    eng.provider = provider
    eng._ref = WorkloadRef(name="db", platform="podman")
    eng.state = InvestigationState(target="db", platform="podman")
    eng.workspace = Workspace.create("db", base=base)
    return eng, provider


class RecordTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._base = Path(self._tmpdir.name) / "runs"
        self.store = ExperienceStore(Path(self._tmpdir.name) / "exp")
        self.addCleanup(self._tmpdir.cleanup)

    def test_resolved_outcome_is_recorded(self):
        llm = _ScriptedLoopLLM([
            {"action": "set-env", "params": {
                "container": "db", "env": {"MYSQL_ROOT_PASSWORD": "secret"}},
             "rationale": "set the password", "confidence": 0.8},
        ])
        eng, _ = _loop_engine(llm, self.store, base=self._base)
        out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(out["outcome"], "resolved")
        records = self.store.load_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].outcome, "recovered")
        self.assertEqual(records[0].attempts[0]["action"], "set-env")
        # The secret value never reaches disk.
        self.assertEqual(
            records[0].attempts[0]["params"]["env"]["MYSQL_ROOT_PASSWORD"], "***")

    def test_unresolved_outcome_is_recorded(self):
        llm = _ScriptedLoopLLM([
            {"action": "restart", "params": {}, "confidence": 0.4},
            {"action": "none", "reason": "out of ideas"},
        ])
        eng, provider = _loop_engine(llm, self.store, base=self._base)
        provider._recover_after = None  # never recovers
        out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(out["outcome"], "unresolved")
        records = self.store.load_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].outcome, "unresolved")

    def test_propose_only_records_nothing(self):
        llm = _ScriptedLoopLLM([
            {"action": "restart", "params": {}, "confidence": 0.4},
        ])
        eng, _ = _loop_engine(llm, self.store, base=self._base)
        out = eng.remediate(DIAG, apply=False)
        self.assertEqual(out["outcome"], "proposed")
        self.assertEqual(self.store.load_all(), [])

    def test_needs_context_records_nothing(self):
        llm = _ScriptedLoopLLM([
            {"action": "none", "reason": "missing value",
             "needs_context": [{"key": "db_password", "reason": "for set-env"}]},
        ])
        eng, _ = _loop_engine(llm, self.store, base=self._base)
        out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(out["outcome"], "needs_context")
        self.assertEqual(self.store.load_all(), [])

    def test_learning_disabled_records_nothing(self):
        llm = _ScriptedLoopLLM([
            {"action": "restart", "params": {}, "confidence": 0.4},
        ])
        eng, _ = _loop_engine(llm, None, base=self._base)
        eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(self.store.load_all(), [])

    def test_store_failure_degrades_to_note(self):
        class BrokenStore(ExperienceStore):
            def save(self, record):
                raise RuntimeError("disk full")

        llm = _ScriptedLoopLLM([
            {"action": "restart", "params": {}, "confidence": 0.4},
        ])
        eng, _ = _loop_engine(llm, BrokenStore(self.store.path), base=self._base)
        out = eng.remediate(DIAG, apply=True, max_risk="high", verify_wait=1)
        self.assertEqual(out["outcome"], "resolved")  # the run itself is fine
        self.assertTrue(any("experience record failed" in n
                            for n in eng.state.notes))


if __name__ == "__main__":
    unittest.main()
