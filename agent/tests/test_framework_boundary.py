"""Validates the Phase 10B framework/application boundary.

The PodDebugger scaffold modules now re-export their framework primitives
from :mod:`poddebugger.framework`. This test asserts that the re-exports
are the **same objects** (``is`` identity), not copies — so subclass /
isinstance / hashable behavior stays consistent.
"""

import unittest


class FrameworkReExportIdentityTest(unittest.TestCase):
    def test_state_entities_are_re_exported_identically(self):
        from poddebugger.framework.state import (
            DispatchRecord, Evidence, Finding, Hypothesis,
            InvestigationState, Lead, RuledOut, SanityCheck,
        )
        from poddebugger.scaffold import state as scaffold_state
        for sym in (DispatchRecord, Evidence, Finding, Hypothesis,
                    InvestigationState, Lead, RuledOut, SanityCheck):
            self.assertIs(getattr(scaffold_state, sym.__name__), sym)

    def test_workspace_re_exported_identically(self):
        from poddebugger.framework.workspace import Workspace
        from poddebugger.scaffold.workspace import Workspace as ScaffoldWorkspace
        self.assertIs(ScaffoldWorkspace, Workspace)

    def test_agent_bases_re_exported_identically(self):
        from poddebugger.framework.agent import (
            ActionAgent, Agent, AgentContext, HookAgent, LIFECYCLE_POINTS,
        )
        from poddebugger.scaffold.agents.base import (
            ActionAgent as SA, Agent as A, AgentContext as AC,
            HookAgent as HA, LIFECYCLE_POINTS as LP,
        )
        self.assertIs(A, Agent)
        self.assertIs(SA, ActionAgent)
        self.assertIs(HA, HookAgent)
        self.assertIs(AC, AgentContext)
        self.assertIs(LP, LIFECYCLE_POINTS)

    def test_llm_base_re_exported_identically(self):
        from poddebugger.framework.llm import LLMClient, LLMError
        from poddebugger.llm.base import LLMClient as LC, LLMError as LE
        self.assertIs(LC, LLMClient)
        self.assertIs(LE, LLMError)

    def test_prompt_helpers_re_exported_identically(self):
        from poddebugger.framework.prompts import (
            PREAMBLE, evidence_block, list_block, sanity_block,
        )
        from poddebugger.scaffold.prompts import (
            PREAMBLE as P, evidence_block as EB,
            list_block as LB, sanity_block as SB,
        )
        self.assertEqual(P, PREAMBLE)         # strings compare by value
        self.assertIs(LB, list_block)
        self.assertIs(SB, sanity_block)
        self.assertIs(EB, evidence_block)

    def test_llmspec_unified(self):
        from poddebugger.config import LLMSpec as ConfigSpec
        from poddebugger.framework.llms import LLMSpec as FrameworkSpec
        self.assertIs(ConfigSpec, FrameworkSpec)

    def test_extract_json_re_exported(self):
        from poddebugger.framework.json_utils import extract_json
        from poddebugger.analyzer import _extract_json
        self.assertIs(_extract_json, extract_json)


class FrameworkAgentLLMsBehaviorTest(unittest.TestCase):
    """The scaffold's AgentLLMs subclasses the framework's — same behavior."""

    def test_scaffold_agent_llms_is_a_framework_agent_llms(self):
        from poddebugger.framework.llms import AgentLLMs as FrameworkLLMs
        from poddebugger.scaffold.llms import AgentLLMs as ScaffoldLLMs
        self.assertTrue(issubclass(ScaffoldLLMs, FrameworkLLMs))

    def test_uniform_classmethod_returns_subclass_when_called_on_subclass(self):
        # Ensures `AgentLLMs.uniform(client)` still hands back our PodDebugger
        # subclass, not a bare framework instance — so downstream `isinstance`
        # checks against scaffold.AgentLLMs keep passing.
        from poddebugger.scaffold.llms import AgentLLMs

        class _FakeClient:
            name = "fake"
            model_id = "x"
            def complete(self, system, user): return "{}"

        result = AgentLLMs.uniform(_FakeClient())
        self.assertIsInstance(result, AgentLLMs)


class FrameworkAgentContextIsDomainAgnosticTest(unittest.TestCase):
    """AgentContext must not require PodDebugger-specific types."""

    def test_context_accepts_arbitrary_provider_ref_ctx(self):
        from poddebugger.framework.agent import AgentContext
        from poddebugger.framework.state import InvestigationState

        class _FakeLLM:
            name = "x"
            model_id = "x"
            def complete(self, system, user): return "{}"

        # No pod types involved — the framework must be happy with arbitrary
        # domain shapes.
        ac = AgentContext(
            provider="not-a-container-platform",
            ref={"id": "log-line-42"},
            state=InvestigationState(target="log-line-42", platform="logs"),
            ctx={"raw": "ERROR: boom"},
            llm=_FakeLLM(),
        )
        self.assertEqual(ac.ref["id"], "log-line-42")


if __name__ == "__main__":
    unittest.main()
