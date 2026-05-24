"""Standalone smoke tests for the inquiro framework.

Critically, these tests do NOT import anything from PodDebugger or any other
application — that's the boundary validation. If they pass with only
``inquiro`` on the Python path, the framework is genuinely self-contained.
"""

import json
import tempfile
import unittest
from pathlib import Path

from inquiro import (
    ActionAgent,
    Agent,
    AgentContext,
    AgentLLMs,
    Evidence,
    Finding,
    Hypothesis,
    HookAgent,
    InvestigationState,
    LIFECYCLE_POINTS,
    LLMClient,
    LLMError,
    LLMSpec,
    Lead,
    PREAMBLE,
    SanityCheck,
    Workspace,
    evidence_block,
    extract_json,
    list_block,
    sanity_block,
)


# ----------------------------------------------------------------------------
# state lifecycle
# ----------------------------------------------------------------------------

class StateLifecycleTest(unittest.TestCase):
    def test_lead_to_hypothesis_to_finding(self):
        st = InvestigationState(target="ci-build-42", platform="ci")
        lead = st.add_lead("the test timed out", source="logs")
        ev = st.add_evidence("test ran for 600s", source="logs")
        hyp = st.add_hypothesis("infinite loop", evidence_ids=[ev.id])
        self.assertEqual(lead.id, "L1")
        self.assertEqual(hyp.id, "H1")
        finding = st.promote(hyp.id)
        self.assertEqual(finding.id, "F1")
        self.assertEqual(st.confirmed_findings, [finding])
        self.assertEqual(st.hypotheses, [])

    def test_round_trip_json(self):
        st = InvestigationState(target="x", platform="logs")
        st.add_lead("a lead")
        st.add_evidence("a fact")
        h = st.add_hypothesis("maybe")
        st.promote(h.id)
        roundtrip = InvestigationState.from_dict(st.to_dict())
        self.assertEqual(roundtrip.target, "x")
        self.assertEqual(len(roundtrip.confirmed_findings), 1)
        self.assertEqual(len(roundtrip.leads), 1)


# ----------------------------------------------------------------------------
# workspace
# ----------------------------------------------------------------------------

class WorkspaceTest(unittest.TestCase):
    def test_commit_persists_state_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace.create("target-a", base=Path(tmp))
            st = InvestigationState(target="target-a", platform="generic")
            st.add_evidence("e1")
            ws.commit(st, "initial")
            # state.json + state.md must exist regardless of git.
            self.assertTrue((ws.path / "state.json").exists())
            self.assertTrue((ws.path / "state.md").exists())
            data = json.loads((ws.path / "state.json").read_text())
            self.assertEqual(data["target"], "target-a")


# ----------------------------------------------------------------------------
# agents
# ----------------------------------------------------------------------------

class _FakeLLM(LLMClient):
    name = "fake"
    model_id = "fake-1"
    def complete(self, system, user): return "{}"


class AgentContractTest(unittest.TestCase):
    def test_agent_requires_name_and_prompt(self):
        class MissingName(Agent):
            def build_user_prompt(self, ac): return ""
            def apply(self, ac, r): return None
        with self.assertRaises(TypeError):
            MissingName()

        class HasOnlyName(Agent):
            name = "ok"
            def build_user_prompt(self, ac): return ""
            def apply(self, ac, r): return None
        with self.assertRaises(TypeError):
            HasOnlyName()

    def test_action_agent_requires_action_and_description(self):
        class MissingAction(ActionAgent):
            name = "Pickle"
            system_prompt = "x"
            def build_user_prompt(self, ac): return ""
            def apply(self, ac, r): return None
        with self.assertRaises(TypeError):
            MissingAction()

    def test_hook_agent_requires_valid_lifecycle(self):
        class BadHook(HookAgent):
            name = "Spy"
            system_prompt = "x"
            lifecycle = "not-a-real-hook"
            def build_user_prompt(self, ac): return ""
            def apply(self, ac, r): return None
        with self.assertRaises(TypeError):
            BadHook()

    def test_well_formed_agent_constructs(self):
        class OK(ActionAgent):
            name = "OK"
            action_name = "do-it"
            description = "ok"
            system_prompt = "x"
            def build_user_prompt(self, ac): return "y"
            def apply(self, ac, r): return r
        a = OK()
        self.assertEqual(a.action_name, "do-it")
        self.assertIn("OK", repr(a))


class AgentContextTest(unittest.TestCase):
    def test_helpers_mutate_state(self):
        st = InvestigationState(target="x")
        ac = AgentContext(
            provider="anything",
            ref="anything",
            state=st,
            ctx="anything",
            llm=_FakeLLM(),
        )
        ac.add_evidence("e1", source="test")
        ac.add_lead("lead", source="test")
        ac.add_hypothesis("hyp")
        ac.record_dispatch("R", "act", "summary")
        self.assertEqual(len(st.evidence), 1)
        self.assertEqual(len(st.leads), 1)
        self.assertEqual(len(st.hypotheses), 1)
        self.assertEqual(len(st.dispatch_history), 1)


# ----------------------------------------------------------------------------
# AgentLLMs
# ----------------------------------------------------------------------------

class AgentLLMsTest(unittest.TestCase):
    def test_uniform_returns_same_client_for_every_role(self):
        c = _FakeLLM()
        ag = AgentLLMs.uniform(c)
        self.assertIs(ag.for_role("Scout"), c)
        self.assertIs(ag.for_role("Planner"), c)

    def test_builder_called_lazily_and_cached(self):
        built: list[tuple] = []
        def build(p, m, b):
            built.append((p, m, b))
            return _FakeLLM()
        ag = AgentLLMs(LLMSpec("p"), builder=build)
        a = ag.for_role("X")
        b = ag.for_role("Y")  # same spec → cached
        self.assertIs(a, b)
        self.assertEqual(built, [("p", "", "")])

    def test_overrides_get_their_own_clients(self):
        built: list[tuple] = []
        def build(p, m, b):
            built.append((p, m, b))
            return _FakeLLM()
        ag = AgentLLMs(
            LLMSpec("p"), overrides={"Special": LLMSpec("q", "m2")},
            builder=build,
        )
        ag.for_role("Default")
        ag.for_role("Special")
        self.assertEqual(built, [("p", "", ""), ("q", "m2", "")])

    def test_describe_includes_overrides(self):
        ag = AgentLLMs(LLMSpec("p", "m"),
                       overrides={"Verifier": LLMSpec("q", "m2")})
        desc = ag.describe()
        self.assertIn("p:m", desc)
        self.assertIn("verifier=q:m2", desc)


# ----------------------------------------------------------------------------
# extract_json
# ----------------------------------------------------------------------------

class ExtractJsonTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_strips_code_fence(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_recovers_object_from_prose(self):
        out = extract_json('Sure! Here is the answer:\n{"a": 1}\n— done.')
        self.assertEqual(out, {"a": 1})

    def test_raises_when_no_object(self):
        with self.assertRaises(LLMError):
            extract_json("no json here")


# ----------------------------------------------------------------------------
# prompt helpers
# ----------------------------------------------------------------------------

class PromptHelpersTest(unittest.TestCase):
    def test_preamble_is_a_non_empty_string(self):
        self.assertIsInstance(PREAMBLE, str)
        self.assertTrue(PREAMBLE)

    def test_list_block_renders_items(self):
        out = list_block([1, 2, 3], lambda i: f"- {i}")
        self.assertIn("- 1", out)
        self.assertIn("- 3", out)

    def test_list_block_empty_uses_placeholder(self):
        self.assertEqual(list_block([], lambda i: f"- {i}", empty="(nope)"), "(nope)")

    def test_sanity_and_evidence_blocks_handle_empty_state(self):
        st = InvestigationState(target="x")
        self.assertEqual(sanity_block(st), "(none)")
        self.assertEqual(evidence_block(st), "(no evidence yet)")


# ----------------------------------------------------------------------------
# lifecycle points
# ----------------------------------------------------------------------------

class LifecyclePointsTest(unittest.TestCase):
    def test_contains_expected_hooks(self):
        for p in ("pre_loop", "post_loop", "before_reporter"):
            self.assertIn(p, LIFECYCLE_POINTS)


if __name__ == "__main__":
    unittest.main()
