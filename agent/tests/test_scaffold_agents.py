"""Unit tests for the Phase 9A agent base classes.

The contract: every Agent subclass must declare ``name`` and ``system_prompt``
plus implement the two abstract methods. ActionAgent adds ``action_name`` /
``description``; HookAgent adds a valid ``lifecycle``. The AgentContext
helpers must delegate cleanly to InvestigationState.
"""

import unittest

from poddebugger.scaffold.agents import (
    LIFECYCLE_POINTS,
    ActionAgent,
    Agent,
    AgentContext,
    HookAgent,
)
from poddebugger.scaffold.state import InvestigationState


def _placeholder():
    """A stand-in for the AgentContext fields a helper test doesn't exercise."""
    return object()


# ---------------------------------------------------------------------------
# A canonical, valid concrete agent — used in the positive-path tests and
# reused as the parent for ActionAgent / HookAgent test subclasses so that
# only one class needs to override the abstract methods.
# ---------------------------------------------------------------------------


class _ConcreteAgent(Agent):
    name = "demo"
    system_prompt = "You are the demo agent. Return JSON."

    def build_user_prompt(self, ac):
        return f"target={ac.state.target}"

    def apply(self, ac, response):
        ac.add_evidence(str(response.get("note", "")), source=self.name)
        return None


# ---------------------------------------------------------------------------


class AgentContractTest(unittest.TestCase):
    def test_concrete_subclass_can_be_used(self):
        agent = _ConcreteAgent()
        self.assertEqual(agent.name, "demo")
        self.assertIn("demo agent", agent.system_prompt)
        self.assertIn("demo", repr(agent))

    def test_subclass_without_name_fails(self):
        class NoName(_ConcreteAgent):
            name = ""

        with self.assertRaises(TypeError):
            NoName()

    def test_subclass_without_system_prompt_fails(self):
        class NoPrompt(_ConcreteAgent):
            system_prompt = ""

        with self.assertRaises(TypeError):
            NoPrompt()

    def test_abstract_methods_must_be_overridden(self):
        class Partial(Agent):
            name = "partial"
            system_prompt = "..."
            # missing build_user_prompt + apply

        with self.assertRaises(TypeError):
            Partial()


class ActionAgentTest(unittest.TestCase):
    def _make(self, **overrides):
        attrs = {
            "name": "metrics",
            "system_prompt": "You are the Metrics agent.",
            "action_name": "metrics",
            "description": "Pull recent CPU/memory metrics.",
            "build_user_prompt": lambda self, ac: "",
            "apply": lambda self, ac, response: None,
        }
        attrs.update(overrides)
        return type("CustomAction", (ActionAgent,), attrs)

    def test_valid_action_agent(self):
        agent = self._make()()
        self.assertEqual(agent.action_name, "metrics")
        self.assertEqual(agent.description, "Pull recent CPU/memory metrics.")

    def test_missing_action_name_fails(self):
        cls = self._make(action_name="")
        with self.assertRaises(TypeError):
            cls()

    def test_missing_description_fails(self):
        cls = self._make(description="")
        with self.assertRaises(TypeError):
            cls()


class HookAgentTest(unittest.TestCase):
    def _make(self, lifecycle):
        return type("CustomHook", (HookAgent,), {
            "name": "h",
            "system_prompt": "...",
            "lifecycle": lifecycle,
            "build_user_prompt": lambda self, ac: "",
            "apply": lambda self, ac, response: None,
        })

    def test_each_documented_lifecycle_is_accepted(self):
        for point in LIFECYCLE_POINTS:
            self._make(point)()

    def test_invalid_lifecycle_fails(self):
        with self.assertRaises(TypeError):
            self._make("never")()

    def test_missing_lifecycle_fails(self):
        with self.assertRaises(TypeError):
            self._make("")()


class AgentContextHelpersTest(unittest.TestCase):
    """The convenience helpers must update InvestigationState — no other path."""

    def _ctx(self) -> AgentContext:
        state = InvestigationState(target="web", platform="podman")
        return AgentContext(
            provider=_placeholder(),
            ref=_placeholder(),
            state=state,
            ctx=_placeholder(),
            llm=_placeholder(),
        )

    def test_add_evidence_appends_to_state(self):
        ac = self._ctx()
        ev = ac.add_evidence("hit a connection refused", source="logs")
        self.assertEqual(ac.state.evidence, [ev])
        self.assertEqual(ev.source, "logs")

    def test_add_lead_appends_to_state(self):
        ac = self._ctx()
        lead = ac.add_lead("check DB connectivity", source="scout")
        self.assertEqual(ac.state.leads, [lead])

    def test_add_hypothesis_appends_to_state(self):
        ac = self._ctx()
        hyp = ac.add_hypothesis("DB unreachable", test="probe the port",
                                evidence_ids=["E1"])
        self.assertEqual(ac.state.hypotheses, [hyp])
        self.assertEqual(hyp.evidence_ids, ["E1"])

    def test_record_dispatch_appends_to_state(self):
        ac = self._ctx()
        ac.state.iteration = 3
        ac.record_dispatch("Analyst", "hypothesize", "1 hypothesis")
        self.assertEqual(len(ac.state.dispatch_history), 1)
        self.assertEqual(ac.state.dispatch_history[0].iteration, 3)


if __name__ == "__main__":
    unittest.main()
