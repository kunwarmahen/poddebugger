"""Unit tests for the Phase 11 approvals subsystem (HLD §16)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from poddebugger import approvals
from poddebugger.models import WorkloadRef


def _desc(action="restart", platform="podman", name="web", namespace=None,
          kind="remediation", risk="low") -> approvals.ActionDescriptor:
    return approvals.ActionDescriptor(
        kind=kind, action=action, risk=risk,
        target=WorkloadRef(name=name, namespace=namespace, platform=platform),
    )


# ---------------------------------------------------------------------------
# the simple gates
# ---------------------------------------------------------------------------

class AutoApproveGateTest(unittest.TestCase):
    def test_always_allow_once(self):
        gate = approvals.AutoApproveGate()
        self.assertEqual(gate.request(_desc()), approvals.Decision.ALLOW_ONCE)


class DenyGateTest(unittest.TestCase):
    def test_always_deny(self):
        gate = approvals.DenyGate()
        self.assertEqual(gate.request(_desc()), approvals.Decision.DENY)


# ---------------------------------------------------------------------------
# TTYPromptGate — driven through injected input/output
# ---------------------------------------------------------------------------

class TTYPromptGateTest(unittest.TestCase):
    def _gate(self, answers, *, allow_persist=False):
        seq = iter(answers)
        out: list[str] = []
        gate = approvals.TTYPromptGate(
            allow_persist=allow_persist,
            input_fn=lambda prompt: next(seq),
            output_fn=out.append,
        )
        return gate, out

    def test_y_means_allow_once(self):
        gate, _ = self._gate(["y"])
        self.assertEqual(gate.request(_desc()), approvals.Decision.ALLOW_ONCE)

    def test_empty_answer_defaults_to_allow_once(self):
        gate, _ = self._gate([""])
        self.assertEqual(gate.request(_desc()), approvals.Decision.ALLOW_ONCE)

    def test_a_means_session(self):
        gate, _ = self._gate(["a"])
        self.assertEqual(gate.request(_desc()), approvals.Decision.ALLOW_SESSION)

    def test_n_means_deny(self):
        gate, _ = self._gate(["n"])
        self.assertEqual(gate.request(_desc()), approvals.Decision.DENY)

    def test_persist_hidden_unless_allowed(self):
        # With allow_persist=False, [P] isn't an option — answer is rejected.
        gate, out = self._gate(["p", "y"], allow_persist=False)
        self.assertEqual(gate.request(_desc()), approvals.Decision.ALLOW_ONCE)
        # The first 'p' was rejected with an explanation.
        self.assertTrue(any("unknown answer" in line for line in out))

    def test_persist_when_enabled(self):
        gate, _ = self._gate(["p"], allow_persist=True)
        self.assertEqual(gate.request(_desc()), approvals.Decision.ALLOW_PERSISTENT)

    def test_re_prompts_on_garbage(self):
        gate, out = self._gate(["zzz", "y"])
        self.assertEqual(gate.request(_desc()), approvals.Decision.ALLOW_ONCE)
        self.assertTrue(any("zzz" in line for line in out))

    def test_descriptor_is_rendered_in_prompt(self):
        gate, out = self._gate(["y"])
        gate.request(_desc(action="scale", name="payments"))
        joined = "\n".join(out)
        self.assertIn("scale", joined)
        self.assertIn("payments", joined)


# ---------------------------------------------------------------------------
# RulesGate — session memory + rule matching + persistence
# ---------------------------------------------------------------------------

class RulesGateMatchTest(unittest.TestCase):
    def test_no_rules_falls_through_to_inner(self):
        inner = approvals.DenyGate()
        gate = approvals.RulesGate(inner, rules=[])
        self.assertEqual(gate.request(_desc()), approvals.Decision.DENY)

    def test_matching_allow_short_circuits_inner(self):
        inner = approvals.DenyGate()
        gate = approvals.RulesGate(inner, rules=[
            {"kind": "remediation", "action": "restart",
             "target": {"platform": "podman"}, "decision": "allow"},
        ])
        self.assertEqual(gate.request(_desc()), approvals.Decision.ALLOW_ONCE)

    def test_non_matching_rule_falls_through(self):
        inner = approvals.DenyGate()
        gate = approvals.RulesGate(inner, rules=[
            {"kind": "remediation", "action": "scale",
             "target": {"platform": "podman"}, "decision": "allow"},
        ])
        # restart doesn't match the scale rule
        self.assertEqual(gate.request(_desc(action="restart")),
                         approvals.Decision.DENY)

    def test_deny_beats_allow_on_same_descriptor(self):
        inner = approvals.AutoApproveGate()
        gate = approvals.RulesGate(inner, rules=[
            {"kind": "remediation", "action": "restart",
             "target": {"platform": "podman"}, "decision": "allow"},
            {"kind": "remediation", "action": "restart",
             "target": {"platform": "podman"}, "decision": "deny"},
        ])
        self.assertEqual(gate.request(_desc()), approvals.Decision.DENY)

    def test_target_name_narrowing(self):
        rules = [
            {"kind": "remediation", "action": "restart",
             "target": {"platform": "podman", "name": "web"},
             "decision": "allow"},
        ]
        gate = approvals.RulesGate(approvals.DenyGate(), rules=rules)
        self.assertEqual(gate.request(_desc(name="web")),
                         approvals.Decision.ALLOW_ONCE)
        self.assertEqual(gate.request(_desc(name="other")),
                         approvals.Decision.DENY)

    def test_namespace_narrowing(self):
        rules = [
            {"kind": "remediation", "action": "scale",
             "target": {"platform": "kubernetes", "namespace": "prod"},
             "decision": "allow"},
        ]
        gate = approvals.RulesGate(approvals.DenyGate(), rules=rules)
        self.assertEqual(
            gate.request(_desc(action="scale", platform="kubernetes",
                               namespace="prod")),
            approvals.Decision.ALLOW_ONCE,
        )
        self.assertEqual(
            gate.request(_desc(action="scale", platform="kubernetes",
                               namespace="staging")),
            approvals.Decision.DENY,
        )

    def test_session_allow_short_circuits_subsequent_prompts(self):
        # Inner gate returns ALLOW_SESSION on first call. Second call must
        # NOT re-invoke the inner gate.
        calls: list[int] = []

        class _Once(approvals.ApprovalGate):
            def request(self, d):
                calls.append(1)
                return approvals.Decision.ALLOW_SESSION

        gate = approvals.RulesGate(_Once(), rules=[])
        d1 = _desc()
        d2 = _desc()  # same shape -> same session key
        gate.request(d1)
        gate.request(d2)
        self.assertEqual(len(calls), 1)


class RulesGateExpiryTest(unittest.TestCase):
    def test_expired_allow_is_ignored(self):
        rules = [
            {"kind": "remediation", "action": "restart",
             "target": {"platform": "podman"},
             "decision": "allow", "expires": "1999-01-01"},
        ]
        gate = approvals.RulesGate(approvals.DenyGate(), rules=rules)
        self.assertEqual(gate.request(_desc()), approvals.Decision.DENY)

    def test_future_expiry_is_active(self):
        rules = [
            {"kind": "remediation", "action": "restart",
             "target": {"platform": "podman"},
             "decision": "allow", "expires": "2999-01-01"},
        ]
        gate = approvals.RulesGate(approvals.DenyGate(), rules=rules)
        self.assertEqual(gate.request(_desc()), approvals.Decision.ALLOW_ONCE)


class RulesGatePersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "approvals.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_persistent_decision_writes_a_rule(self):
        class _AlwaysPersist(approvals.ApprovalGate):
            def request(self, d): return approvals.Decision.ALLOW_PERSISTENT

        gate = approvals.RulesGate(_AlwaysPersist(), rules=[],
                                   save_path=self.path)
        gate.request(_desc(action="scale", platform="kubernetes",
                           name="web", namespace="prod"))
        # File written; subsequent rules read it back.
        loaded = approvals.load_rules(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["action"], "scale")
        self.assertEqual(loaded[0]["target"]["namespace"], "prod")

    def test_session_decision_does_not_write(self):
        class _Session(approvals.ApprovalGate):
            def request(self, d): return approvals.Decision.ALLOW_SESSION

        gate = approvals.RulesGate(_Session(), rules=[], save_path=self.path)
        gate.request(_desc())
        self.assertFalse(self.path.exists())


# ---------------------------------------------------------------------------
# load_rules / save_rules — round-trip + error handling
# ---------------------------------------------------------------------------

class LoadSaveRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "approvals.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_empty(self):
        self.assertEqual(approvals.load_rules(self.path), [])

    def test_round_trip(self):
        rules = [
            {"kind": "remediation", "action": "restart",
             "target": {"platform": "podman"}, "decision": "allow"},
            {"kind": "probe", "action": "listening_ports",
             "target": {"platform": "kubernetes", "namespace": "prod"},
             "decision": "deny", "expires": "2999-01-01"},
        ]
        approvals.save_rules(self.path, rules)
        loaded = approvals.load_rules(self.path)
        self.assertEqual(loaded, rules)

    def test_missing_version_rejected(self):
        self.path.write_text(json.dumps({"rules": []}))
        with self.assertRaises(approvals.ApprovalDenied):
            approvals.load_rules(self.path)

    def test_corrupt_json_rejected(self):
        self.path.write_text("{not json")
        with self.assertRaises(approvals.ApprovalDenied):
            approvals.load_rules(self.path)


# ---------------------------------------------------------------------------
# make_gate factory — flag combinations
# ---------------------------------------------------------------------------

class _FakeStdin:
    def __init__(self, tty: bool):
        self._tty = tty

    def isatty(self): return self._tty


class MakeGateTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PODDEBUGGER_APPROVALS_FILE", None)
        self.tmp = tempfile.TemporaryDirectory()
        # Point the default path away from $HOME so the test is hermetic.
        os.environ["PODDEBUGGER_APPROVALS_FILE"] = str(
            Path(self.tmp.name) / "approvals.json")

    def tearDown(self):
        os.environ.pop("PODDEBUGGER_APPROVALS_FILE", None)
        self.tmp.cleanup()

    def test_yes_returns_auto_approve(self):
        gate = approvals.make_gate(yes=True, stdin=_FakeStdin(True))
        self.assertIsInstance(gate, approvals.AutoApproveGate)

    def test_no_tty_session_returns_rules_wrapping_deny(self):
        gate = approvals.make_gate(stdin=_FakeStdin(False), mode="session")
        self.assertIsInstance(gate, approvals.RulesGate)
        # Inner must be DenyGate.
        self.assertIsInstance(gate._inner, approvals.DenyGate)

    def test_no_prompt_on_tty_still_uses_deny_inner(self):
        gate = approvals.make_gate(no_prompt=True, stdin=_FakeStdin(True),
                                   mode="session")
        self.assertIsInstance(gate, approvals.RulesGate)
        self.assertIsInstance(gate._inner, approvals.DenyGate)

    def test_tty_session_uses_tty_prompt_without_persist(self):
        gate = approvals.make_gate(stdin=_FakeStdin(True), mode="session")
        self.assertIsInstance(gate, approvals.RulesGate)
        self.assertIsInstance(gate._inner, approvals.TTYPromptGate)
        self.assertFalse(gate._inner._allow_persist)
        self.assertIsNone(gate._save_path)   # session can't write

    def test_tty_persistent_offers_persist_and_writes(self):
        gate = approvals.make_gate(stdin=_FakeStdin(True), mode="persistent")
        self.assertIsInstance(gate, approvals.RulesGate)
        self.assertIsInstance(gate._inner, approvals.TTYPromptGate)
        self.assertTrue(gate._inner._allow_persist)
        self.assertIsNotNone(gate._save_path)

    def test_mode_off_bypasses_rules_file(self):
        # With mode=off the returned gate is the raw inner gate — no
        # RulesGate wrapper, so the rules file is never consulted.
        gate = approvals.make_gate(stdin=_FakeStdin(True), mode="off")
        self.assertIsInstance(gate, approvals.TTYPromptGate)
        gate2 = approvals.make_gate(stdin=_FakeStdin(False), mode="off")
        self.assertIsInstance(gate2, approvals.DenyGate)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            approvals.make_gate(stdin=_FakeStdin(True), mode="weird")


# ---------------------------------------------------------------------------
# default_rules_path — env precedence
# ---------------------------------------------------------------------------

class DefaultRulesPathTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.pop("PODDEBUGGER_APPROVALS_FILE", None)
        self._saved_xdg = os.environ.pop("XDG_CONFIG_HOME", None)

    def tearDown(self):
        for k, v in (("PODDEBUGGER_APPROVALS_FILE", self._saved_env),
                     ("XDG_CONFIG_HOME", self._saved_xdg)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_env_var_override(self):
        os.environ["PODDEBUGGER_APPROVALS_FILE"] = "/tmp/custom.json"
        self.assertEqual(str(approvals.default_rules_path()), "/tmp/custom.json")

    def test_xdg_config_home(self):
        os.environ["XDG_CONFIG_HOME"] = "/tmp/xdg"
        self.assertEqual(str(approvals.default_rules_path()),
                         "/tmp/xdg/poddebugger/approvals.json")


if __name__ == "__main__":
    unittest.main()
