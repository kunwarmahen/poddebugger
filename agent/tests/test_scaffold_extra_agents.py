"""Stage 9C — the public extension API: ``extra_agents=`` and
``PODDEBUGGER_EXTRA_AGENTS``."""

import os
import unittest
from unittest import mock

from poddebugger.llm.base import LLMClient
from poddebugger.providers.base import ContainerPlatform
from poddebugger.scaffold import (
    ActionAgent,
    InvestigationEngine,
    load_agents_from_env,
)


class _FakeLLM(LLMClient):
    name = "fake"
    model_id = "fake-1"

    def complete(self, system, user):
        return "{}"


class _FakeProvider(ContainerPlatform):
    name = "fake"

    def preflight(self):
        pass

    def resolve(self, target, namespace=None):
        raise NotImplementedError

    def get_workload(self, ref):
        raise NotImplementedError

    def get_events(self, ref):
        return []

    def get_logs(self, ref, tail=200):
        return ""

    def get_spec(self, ref):
        return {}


# A real, importable ActionAgent class the env-loader test can name by path.
class SampleActionAgent(ActionAgent):
    name = "metrics"
    action_name = "metrics"
    description = "Fetch fake metrics."
    system_prompt = "You are the Metrics agent. Return JSON."

    def build_user_prompt(self, ac):
        return ""

    def apply(self, ac, response):
        return None


# A custom Reporter that overrides the built-in by name.
class CustomReporter(ActionAgent):
    name = "Reporter"  # overrides the built-in
    action_name = "report"
    description = "Custom reporter."
    system_prompt = "You are the Reporter (custom). Return JSON."

    def build_user_prompt(self, ac):
        return ""

    def apply(self, ac, response):
        return None


def _engine(extra_agents=None):
    return InvestigationEngine(_FakeProvider(), _FakeLLM(), extra_agents=extra_agents)


class ExtraAgentsParamTest(unittest.TestCase):
    def test_extra_action_agent_is_registered(self):
        agent = SampleActionAgent()
        eng = _engine(extra_agents=[agent])
        self.assertIs(eng._agents["metrics"], agent)
        self.assertIs(eng._action_agents["metrics"], agent)

    def test_built_ins_are_still_present_alongside_extras(self):
        eng = _engine(extra_agents=[SampleActionAgent()])
        for name in ("Scout", "Planner", "Coordinator", "Analyst", "Prober",
                     "Verifier", "Auditor", "Adjudicator", "Reporter"):
            self.assertIn(name, eng._agents)
        self.assertIn("metrics", eng._agents)

    def test_extra_agent_overrides_built_in_on_name(self):
        custom = CustomReporter()
        eng = _engine(extra_agents=[custom])
        self.assertIs(eng._agents["Reporter"], custom)


class EnvLoaderTest(unittest.TestCase):
    def test_loads_classes_from_env_var(self):
        path = f"{__name__}.SampleActionAgent"
        with mock.patch.dict(os.environ, {"PODDEBUGGER_EXTRA_AGENTS": path},
                             clear=False):
            agents = load_agents_from_env()
        self.assertEqual(len(agents), 1)
        self.assertIsInstance(agents[0], SampleActionAgent)

    def test_bad_path_is_skipped_not_fatal(self):
        with mock.patch.dict(os.environ,
                             {"PODDEBUGGER_EXTRA_AGENTS": "does.not.exist.Agent"},
                             clear=False):
            agents = load_agents_from_env()
        self.assertEqual(agents, [])

    def test_empty_env_returns_empty(self):
        with mock.patch.dict(os.environ, {"PODDEBUGGER_EXTRA_AGENTS": ""},
                             clear=False):
            self.assertEqual(load_agents_from_env(), [])

    def test_non_agent_class_is_skipped(self):
        # `dict` is importable and instantiable but is not an Agent.
        with mock.patch.dict(os.environ,
                             {"PODDEBUGGER_EXTRA_AGENTS": "builtins.dict"},
                             clear=False):
            agents = load_agents_from_env()
        self.assertEqual(agents, [])

    def test_engine_constructor_consults_env(self):
        path = f"{__name__}.SampleActionAgent"
        with mock.patch.dict(os.environ, {"PODDEBUGGER_EXTRA_AGENTS": path},
                             clear=False):
            eng = _engine()
        self.assertIn("metrics", eng._agents)


# --- Stage 9D: the Coordinator's menu is rendered from the registry ------


class DynamicMenuTest(unittest.TestCase):
    def test_action_menu_includes_built_ins_and_extras(self):
        eng = _engine(extra_agents=[SampleActionAgent()])
        menu = dict(eng._action_menu())
        # built-ins
        self.assertIn("analyze", menu)
        self.assertIn("probe", menu)
        # custom
        self.assertEqual(menu["metrics"], "Fetch fake metrics.")

    def test_coordinator_prompt_includes_custom_action_description(self):
        from poddebugger.models import DiagnosticContext, Workload, WorkloadRef
        from poddebugger.scaffold.agents import AgentContext, Coordinator
        from poddebugger.scaffold.state import InvestigationState

        coord = Coordinator()
        state = InvestigationState(target="web", platform="podman")
        ctx = DiagnosticContext(workload=Workload(ref=WorkloadRef(name="web")))
        ac = AgentContext(
            provider=_FakeProvider(), ref=WorkloadRef(name="web"),
            state=state, ctx=ctx, llm=_FakeLLM(),
            extras={
                "iterations_left": 5,
                "actions": [
                    ("analyze", "Analyst description"),
                    ("metrics", "Fetch fake metrics."),
                ],
            },
        )
        prompt = coord.build_user_prompt(ac)
        self.assertIn('"metrics": Fetch fake metrics.', prompt)
        self.assertIn('"analyze": Analyst description', prompt)

    def test_coordinator_rejects_unknown_action(self):
        from poddebugger.models import DiagnosticContext, Workload, WorkloadRef
        from poddebugger.scaffold.agents import AgentContext, Coordinator
        from poddebugger.scaffold.state import InvestigationState

        coord = Coordinator()
        state = InvestigationState(target="web", platform="podman")
        ctx = DiagnosticContext(workload=Workload(ref=WorkloadRef(name="web")))
        ac = AgentContext(
            provider=_FakeProvider(), ref=WorkloadRef(name="web"),
            state=state, ctx=ctx, llm=_FakeLLM(),
            extras={"actions": [("analyze", "...")]},
        )
        # Coordinator returns an action not in the menu — coerced to "done".
        action, _, _ = coord.apply(ac, {"action": "totally_made_up", "target": "",
                                        "instruction": "", "reason": ""})
        self.assertEqual(action, "done")


if __name__ == "__main__":
    unittest.main()
