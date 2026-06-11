"""Tests for the Phase 15C prompt optimizer (HLD §19.4) — fully offline:
the score function and the critic LLM are injected fakes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from poddebugger.llm.base import LLMClient, LLMError
from poddebugger.promptopt import DEFAULT_TARGET_ROLES, optimize
from poddebugger.promptpack import dump_pack, load_pack
from poddebugger.scenarios import ScenarioResult, SuiteScore

NEW_SCOUT = "Sharper Scout instructions. Return JSON: {\"classification\": \"...\"}"


def _score(points: int, maximum: int = 10) -> SuiteScore:
    return SuiteScore([ScenarioResult(
        name="missing-env", points=points, max_points=maximum,
        classification="Unknown", expected_classification=("ConfigError",),
        expected_action=("set-env",), action="")])


class FakeCritic(LLMClient):
    name = "fake-critic"
    model_id = "fake-1"

    def __init__(self, responses):
        self._responses = list(responses)
        self.user_prompts: list[str] = []

    def complete(self, system, user):
        self.user_prompts.append(user)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)


class PackScorer:
    """score_fn: better score iff the pack carries the improved Scout."""

    def __init__(self, base=4, improved=7):
        self.base, self.improved = base, improved
        self.calls = 0

    def __call__(self, pack: dict) -> SuiteScore:
        self.calls += 1
        return _score(self.improved if pack.get("Scout") == NEW_SCOUT
                      else self.base)


class OptimizeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pack_dir = Path(self._tmp.name)
        dump_pack(self.pack_dir)
        self.addCleanup(self._tmp.cleanup)

    def test_adopts_an_improving_edit(self):
        critic = FakeCritic([{"role": "Scout", "diagnosis": "too vague",
                              "prompt": NEW_SCOUT}])
        report = optimize(self.pack_dir, score_fn=PackScorer(),
                          critic=critic, rounds=1)
        self.assertEqual(report.baseline_total, 4)
        self.assertEqual(report.final_total, 7)
        self.assertTrue(report.rounds[0].adopted)
        # The winning prompt landed in the pack file.
        self.assertEqual(load_pack(self.pack_dir)["Scout"], NEW_SCOUT)

    def test_discards_a_non_improving_edit(self):
        critic = FakeCritic([{"role": "Scout", "prompt": NEW_SCOUT}])
        scorer = PackScorer(base=4, improved=4)  # no gain
        report = optimize(self.pack_dir, score_fn=scorer,
                          critic=critic, rounds=1)
        self.assertFalse(report.rounds[0].adopted)
        self.assertEqual(report.final_total, 4)
        self.assertNotEqual(load_pack(self.pack_dir)["Scout"], NEW_SCOUT)

    def test_rejects_non_target_role(self):
        critic = FakeCritic([{"role": "Coordinator", "prompt": NEW_SCOUT}])
        scorer = PackScorer()
        report = optimize(self.pack_dir, score_fn=scorer,
                          critic=critic, rounds=1)
        self.assertFalse(report.rounds[0].adopted)
        self.assertIn("not a target role", report.rounds[0].detail)
        self.assertEqual(scorer.calls, 1)  # baseline only — no re-score

    def test_rejects_prompt_without_protocol_marker(self):
        critic = FakeCritic([{"role": "Scout", "prompt": "be smarter please"}])
        report = optimize(self.pack_dir, score_fn=PackScorer(),
                          critic=critic, rounds=1)
        self.assertFalse(report.rounds[0].adopted)

    def test_critic_failure_skips_the_round(self):
        critic = FakeCritic([LLMError("model offline"),
                             {"role": "Scout", "prompt": NEW_SCOUT}])
        report = optimize(self.pack_dir, score_fn=PackScorer(),
                          critic=critic, rounds=2)
        self.assertFalse(report.rounds[0].adopted)
        self.assertTrue(report.rounds[1].adopted)

    def test_perfect_baseline_stops_immediately(self):
        critic = FakeCritic([])  # would raise if consulted
        report = optimize(self.pack_dir,
                          score_fn=lambda pack: _score(10, 10),
                          critic=critic, rounds=3)
        self.assertEqual(report.rounds, [])
        self.assertEqual(report.final_total, 10)

    def test_second_round_critic_sees_adopted_prompt(self):
        critic = FakeCritic([
            {"role": "Scout", "prompt": NEW_SCOUT},
            {"role": "Scout", "prompt": NEW_SCOUT + " "},
        ])
        optimize(self.pack_dir, score_fn=PackScorer(), critic=critic, rounds=2)
        self.assertNotIn(NEW_SCOUT, critic.user_prompts[0])
        self.assertIn(NEW_SCOUT, critic.user_prompts[1])

    def test_default_target_roles_exclude_control_flow(self):
        self.assertNotIn("Coordinator", DEFAULT_TARGET_ROLES)
        self.assertIn("Reporter", DEFAULT_TARGET_ROLES)


if __name__ == "__main__":
    unittest.main()
