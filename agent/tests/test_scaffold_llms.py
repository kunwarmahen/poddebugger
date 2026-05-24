"""Unit tests for per-agent LLM resolution (config overrides + AgentLLMs)."""

import os
import unittest
from unittest import mock

from poddebugger.config import LLMSpec, agent_llm_overrides
from poddebugger.llm.base import LLMClient
from poddebugger.scaffold.llms import AgentLLMs


class _Dummy(LLMClient):
    name = "dummy"
    model_id = "dummy-1"

    def complete(self, system, user):
        return "{}"


class AgentLLMOverridesTest(unittest.TestCase):
    DEFAULT = LLMSpec("ollama", "gemma4:e4b")

    def test_no_env_means_no_overrides(self):
        # Clear any PODDEBUGGER_<ROLE>_LLM_* vars for a clean read.
        clean = {k: v for k, v in os.environ.items()
                 if not (k.startswith("PODDEBUGGER_") and "_LLM_" in k
                         and k != "PODDEBUGGER_LLM_PROVIDER")}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertEqual(agent_llm_overrides(self.DEFAULT), {})

    def test_model_only_override_keeps_provider(self):
        with mock.patch.dict(os.environ,
                             {"PODDEBUGGER_SCOUT_LLM_MODEL": "qwen3.5:9b"},
                             clear=False):
            ov = agent_llm_overrides(self.DEFAULT)
        self.assertEqual(ov["scout"], LLMSpec("ollama", "qwen3.5:9b", ""))

    def test_provider_switch_resets_model(self):
        # Switching provider must not carry over the old provider's model.
        with mock.patch.dict(os.environ,
                             {"PODDEBUGGER_REPORTER_LLM_PROVIDER": "anthropic"},
                             clear=False):
            ov = agent_llm_overrides(self.DEFAULT)
        self.assertEqual(ov["reporter"], LLMSpec("anthropic", "", ""))

    def test_provider_and_model_override(self):
        with mock.patch.dict(os.environ, {
            "PODDEBUGGER_VERIFIER_LLM_PROVIDER": "anthropic",
            "PODDEBUGGER_VERIFIER_LLM_MODEL": "claude-opus-4-7",
        }, clear=False):
            ov = agent_llm_overrides(self.DEFAULT)
        self.assertEqual(ov["verifier"], LLMSpec("anthropic", "claude-opus-4-7", ""))

    def test_custom_agent_role_is_discovered(self):
        # A name outside AGENT_ROLES (a user-added agent registered via
        # extra_agents / PODDEBUGGER_EXTRA_AGENTS) should still get its
        # PODDEBUGGER_<NAME>_LLM_* overrides honored — that's the
        # contract documented in AGENT_HARNESS / custom_agent.py.
        with mock.patch.dict(os.environ, {
            "PODDEBUGGER_METRICS_LLM_PROVIDER": "anthropic",
            "PODDEBUGGER_METRICS_LLM_MODEL": "claude-haiku-4-5",
        }, clear=False):
            ov = agent_llm_overrides(self.DEFAULT)
        self.assertEqual(ov["metrics"],
                         LLMSpec("anthropic", "claude-haiku-4-5", ""))

    def test_custom_agent_model_only_inherits_default_provider(self):
        with mock.patch.dict(os.environ, {
            "PODDEBUGGER_LIBRARIAN_LLM_MODEL": "gemma4:31b",
        }, clear=False):
            ov = agent_llm_overrides(self.DEFAULT)
        self.assertEqual(ov["librarian"],
                         LLMSpec("ollama", "gemma4:31b", ""))

    def test_default_llm_env_is_not_mistaken_for_a_role(self):
        # PODDEBUGGER_LLM_PROVIDER is the *default* LLM, not a per-role
        # override for a role named "" — the regex must not match it.
        clean = {k: v for k, v in os.environ.items()
                 if not (k.startswith("PODDEBUGGER_") and "_LLM_" in k
                         and k != "PODDEBUGGER_LLM_PROVIDER")}
        clean["PODDEBUGGER_LLM_PROVIDER"] = "openai"
        with mock.patch.dict(os.environ, clean, clear=True):
            ov = agent_llm_overrides(self.DEFAULT)
        self.assertEqual(ov, {})


class AgentLLMsTest(unittest.TestCase):
    def _pool(self) -> AgentLLMs:
        return AgentLLMs(
            LLMSpec("ollama", "m1"),
            {"verifier": LLMSpec("anthropic", "claude-opus-4-7")},
        )

    def test_spec_for_resolves_override_else_default(self):
        pool = self._pool()
        self.assertEqual(pool.spec_for("scout"), LLMSpec("ollama", "m1"))
        self.assertEqual(pool.spec_for("verifier").provider, "anthropic")
        self.assertEqual(pool.spec_for("VERIFIER").provider, "anthropic")  # case-insensitive

    def test_clients_shared_by_spec(self):
        pool = self._pool()
        # roles on the same spec share one client; a different spec does not
        self.assertIs(pool.for_role("scout"), pool.for_role("planner"))
        self.assertIsNot(pool.for_role("scout"), pool.for_role("verifier"))

    def test_uniform_returns_the_one_client(self):
        dummy = _Dummy()
        pool = AgentLLMs.uniform(dummy)
        self.assertIs(pool.for_role("scout"), dummy)
        self.assertIs(pool.for_role("adjudicator"), dummy)

    def test_describe_lists_overrides(self):
        text = self._pool().describe()
        self.assertIn("ollama:m1", text)
        self.assertIn("verifier=anthropic", text)


if __name__ == "__main__":
    unittest.main()
