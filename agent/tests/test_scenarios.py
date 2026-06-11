"""Tests for the Phase 15C scenario eval harness (HLD §19.4).

The Podman lifecycle and the investigation step are injected, so these
tests prove the machinery without containers or an LLM.
"""

from __future__ import annotations

import unittest

from poddebugger.scenarios import (
    BUILTIN_SCENARIOS,
    Scenario,
    SuiteScore,
    render_table,
    run_scenario,
    run_suite,
)
from tests.util import cp


def _scenario(**over) -> Scenario:
    base = dict(
        name="missing-env", description="test",
        image="alpine", command=("sh", "-c", "exit 1"),
        expect_classification=("ConfigError",),
        expect_action=("set-env",),
        settle_seconds=0,
    )
    base.update(over)
    return Scenario(**base)


class FakePodman:
    def __init__(self, run_rc=0):
        self.calls: list[list[str]] = []
        self.run_rc = run_rc

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "run":
            return cp("cid\n", rc=self.run_rc, stderr="boom" if self.run_rc else "")
        return cp("")


def _no_sleep(_):
    pass


class LifecycleTest(unittest.TestCase):
    def test_container_created_then_removed(self):
        podman = FakePodman()
        run_scenario(_scenario(), lambda c, s: ("ConfigError", "set-env"),
                     podman=podman, sleep=_no_sleep)
        ops = [c[0] for c in podman.calls]
        self.assertEqual(ops, ["rm", "run", "rm"])  # pre-clean, run, teardown
        run_cmd = podman.calls[1]
        self.assertIn("pd-eval-missing-env", run_cmd)
        self.assertIn("alpine", run_cmd)

    def test_run_failure_scores_zero_without_investigating(self):
        podman = FakePodman(run_rc=125)
        called = []
        r = run_scenario(_scenario(), lambda c, s: called.append(1),
                         podman=podman, sleep=_no_sleep)
        self.assertEqual(r.points, 0)
        self.assertIn("podman run failed", r.error)
        self.assertEqual(called, [])

    def test_investigate_exception_still_removes_container(self):
        podman = FakePodman()

        def boom(c, s):
            raise RuntimeError("engine exploded")

        r = run_scenario(_scenario(), boom, podman=podman, sleep=_no_sleep)
        self.assertIn("engine exploded", r.error)
        self.assertEqual([c[0] for c in podman.calls], ["rm", "run", "rm"])


class ScoringTest(unittest.TestCase):
    def _run(self, scenario, classification, action):
        return run_scenario(scenario, lambda c, s: (classification, action),
                            podman=FakePodman(), sleep=_no_sleep)

    def test_full_marks(self):
        r = self._run(_scenario(), "ConfigError", "set-env")
        self.assertEqual((r.points, r.max_points), (2, 2))
        self.assertTrue(r.classification_ok)
        self.assertTrue(r.action_ok)

    def test_classification_match_is_case_insensitive(self):
        r = self._run(_scenario(), "configerror", "set-env")
        self.assertTrue(r.classification_ok)

    def test_wrong_classification_right_action(self):
        r = self._run(_scenario(), "Unknown", "set-env")
        self.assertEqual(r.points, 1)
        self.assertFalse(r.classification_ok)
        self.assertTrue(r.action_ok)

    def test_unscored_action_means_one_point_max(self):
        s = _scenario(expect_action=None)
        r = self._run(s, "ConfigError", "")
        self.assertEqual((r.points, r.max_points), (1, 1))
        self.assertIsNone(r.action_ok)

    def test_suite_totals_and_table(self):
        scenarios = [_scenario(), _scenario(name="crash-loop",
                                            expect_action=None)]
        score = run_suite(scenarios, lambda c, s: ("ConfigError", "set-env"),
                          podman=FakePodman(), sleep=_no_sleep)
        self.assertIsInstance(score, SuiteScore)
        self.assertEqual((score.total, score.max_total), (3, 3))
        table = render_table(score)
        self.assertIn("TOTAL", table)
        self.assertIn("3/3", table)
        self.assertIn("(not scored)", table)


class BuiltinSuiteTest(unittest.TestCase):
    def test_builtins_are_well_formed(self):
        self.assertGreaterEqual(len(BUILTIN_SCENARIOS), 5)
        for s in BUILTIN_SCENARIOS.values():
            self.assertTrue(s.expect_classification)
            self.assertTrue(s.container.startswith("pd-eval-"))
            if s.expect_action is not None:
                self.assertTrue(s.expect_action)


if __name__ == "__main__":
    unittest.main()
