"""Tests for Phase 15B — on-the-fly specialist agents (HLD §19.3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from poddebugger.llm.base import LLMClient, LLMError
from poddebugger.models import WorkloadRef
from poddebugger.scaffold.agents import Specialist, make_specialist, specialty_slug
from poddebugger.scaffold.engine import InvestigationEngine

from tests.test_scaffold_engine import FakeProvider, ScriptedLLM


# ---------------------------------------------------------------------------
# the factory / agent contract
# ---------------------------------------------------------------------------

class SpecialtySlugTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(specialty_slug("PostgreSQL crash analysis"),
                         "postgresql-crash-analysis")

    def test_strips_punctuation_and_caps_length(self):
        self.assertEqual(specialty_slug("  DNS / networking!!  "), "dns-networking")
        self.assertLessEqual(len(specialty_slug("x" * 200)), 60)


class FactoryTest(unittest.TestCase):
    def test_instance_overrides_name_and_prompt(self):
        s = make_specialist("JVM heap tuning", charter="Why is the heap exhausted?")
        self.assertEqual(s.name, "Specialist:jvm-heap-tuning")
        self.assertIn("JVM heap tuning", s.system_prompt)
        self.assertIn("Why is the heap exhausted?", s.system_prompt)
        self.assertIn("ADVISORY ONLY", s.system_prompt)

    def test_default_charter_when_none_written(self):
        s = make_specialist("Redis")
        self.assertIn("Advise the team as a Redis expert.", s.system_prompt)

    def test_empty_specialty_refused(self):
        with self.assertRaises(ValueError):
            make_specialist("   ")
        with self.assertRaises(ValueError):
            make_specialist("!!!")  # slugs to nothing

    def test_prompt_document_carries_the_composed_prompt(self):
        s = make_specialist("Redis", charter="check persistence config")
        doc = s.prompt_document()
        self.assertIn("# Specialist: Redis", doc)
        self.assertIn("check persistence config", doc)
        self.assertIn(s.system_prompt.splitlines()[-1], doc)


class ApplyTest(unittest.TestCase):
    def test_outputs_tagged_dynamic(self):
        from poddebugger.scaffold.agents.base import AgentContext
        from poddebugger.scaffold.state import InvestigationState

        s = make_specialist("Redis ops")
        state = InvestigationState(target="c1", platform="podman")
        ac = AgentContext(provider=None, ref=None, state=state, ctx=None, llm=None)
        out = s.apply(ac, {
            "observations": ["AOF rewrite loops every 2s"],
            "leads": ["check appendfsync setting"],
            "assessment": "persistence misconfigured",
        })
        self.assertEqual(out["assessment"], "persistence misconfigured")
        self.assertTrue(all(e.source == "dynamic:redis-ops" for e in state.evidence))
        self.assertEqual(len(state.evidence), 2)  # observation + assessment
        self.assertEqual(state.leads[0].source, "dynamic:redis-ops")
        self.assertEqual(state.dispatch_history[-1].role, "Specialist:redis-ops")


# ---------------------------------------------------------------------------
# engine wiring — a ScriptedLLM that handles the Specialist role too
# ---------------------------------------------------------------------------

class SpecialistScriptedLLM(ScriptedLLM):
    """ScriptedLLM that also answers any spawned Specialist."""

    def __init__(self, coordinator_actions, fail_specialist=False):
        super().__init__(coordinator_actions=coordinator_actions,
                         verifier_verdicts=[])
        self._fail_specialist = fail_specialist
        self.specialist_systems: list[str] = []

    def complete(self, system, user):
        if "a domain specialist the Coordinator consulted" in system:
            self.specialist_systems.append(system)
            if self._fail_specialist:
                raise LLMError("specialist model unavailable")
            return json.dumps({
                "observations": ["specialist observation"],
                "leads": [],
                "assessment": "specialist verdict",
            })
        return super().complete(system, user)


def _consult(specialty, instruction="explain the crash"):
    return {"action": "specialist", "target": specialty,
            "instruction": instruction, "reason": "need an expert"}


def _done():
    return {"action": "done", "target": "", "instruction": "", "reason": "ok"}


def _engine(llm, base, **kw):
    return InvestigationEngine(FakeProvider(), llm, workspace_base=base, **kw)


class EngineDispatchTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)

    def test_menu_gating(self):
        llm = SpecialistScriptedLLM([_done()])
        on = _engine(llm, self.base, specialists_enabled=True)
        off = _engine(llm, self.base)
        self.assertIn("specialist", [n for n, _ in on._action_menu()])
        self.assertNotIn("specialist", [n for n, _ in off._action_menu()])

    def test_consult_lands_tagged_evidence_and_persists_prompt(self):
        llm = SpecialistScriptedLLM([_consult("PostgreSQL recovery"), _done()])
        eng = _engine(llm, self.base, specialists_enabled=True)
        eng.investigate(WorkloadRef(name="c1", platform="fake"))

        tagged = [e for e in eng.state.evidence
                  if e.source == "dynamic:postgresql-recovery"]
        self.assertEqual(len(tagged), 2)  # observation + assessment
        # The Coordinator's charter made it into the composed system prompt.
        self.assertIn("explain the crash", llm.specialist_systems[0])
        # The prompt document is persisted in the workspace for audit.
        doc = eng.workspace.path / "specialists" / "postgresql-recovery.md"
        self.assertTrue(doc.exists())
        self.assertIn("PostgreSQL recovery", doc.read_text())

    def test_budget_enforced_and_reconsult_is_free(self):
        llm = SpecialistScriptedLLM([
            _consult("Postgres"), _consult("Redis"),
            _consult("Postgres", "second question"),   # re-consult: free
            _consult("Kafka"),                          # over budget: skipped
            _done(),
        ])
        eng = _engine(llm, self.base, specialists_enabled=True,
                      max_specialists=2)
        eng.investigate(WorkloadRef(name="c1", platform="fake"))

        self.assertEqual(set(eng._specialists), {"postgres", "redis"})
        self.assertEqual(len(llm.specialist_systems), 3)  # 2 spawns + 1 re-consult
        self.assertTrue(any("budget" in n for n in eng.state.notes))
        self.assertFalse(any("kafka" in e.source for e in eng.state.evidence))

    def test_disabled_engine_never_dispatches(self):
        # Coordinator tries to consult, but the action isn't in the menu, so
        # its apply() coerces the unknown action to "done".
        llm = SpecialistScriptedLLM([_consult("Postgres"), _done()])
        eng = _engine(llm, self.base)  # specialists_enabled defaults to False
        eng.investigate(WorkloadRef(name="c1", platform="fake"))
        self.assertEqual(llm.specialist_systems, [])
        self.assertEqual(eng._specialists, {})

    def test_empty_specialty_skipped_with_dispatch_record(self):
        llm = SpecialistScriptedLLM([_consult("  "), _done()])
        eng = _engine(llm, self.base, specialists_enabled=True)
        eng.investigate(WorkloadRef(name="c1", platform="fake"))
        self.assertEqual(eng._specialists, {})
        self.assertTrue(any(d.role == "Specialist" and d.action == "skip"
                            for d in eng.state.dispatch_history))

    def test_specialist_llm_failure_degrades(self):
        llm = SpecialistScriptedLLM([_consult("Postgres"), _done()],
                                    fail_specialist=True)
        eng = _engine(llm, self.base, specialists_enabled=True)
        diagnosis = eng.investigate(WorkloadRef(name="c1", platform="fake"))
        self.assertIsNotNone(diagnosis)  # the run survives
        self.assertTrue(any("Specialist:postgres agent failed" in n
                            for n in eng.state.notes))

    def test_specialists_reset_between_runs(self):
        llm = SpecialistScriptedLLM([_consult("Postgres"), _done()])
        eng = _engine(llm, self.base, specialists_enabled=True)
        eng.investigate(WorkloadRef(name="c1", platform="fake"))
        self.assertEqual(set(eng._specialists), {"postgres"})
        llm2 = SpecialistScriptedLLM([_done()])
        eng.llms = type(eng.llms).uniform(llm2)
        eng.investigate(WorkloadRef(name="c1", platform="fake"))
        self.assertEqual(eng._specialists, {})


if __name__ == "__main__":
    unittest.main()
