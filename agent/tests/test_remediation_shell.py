"""Tests for the opt-in `shell` catalog action (HLD §12.9)."""

from __future__ import annotations

import os
import unittest

from poddebugger import remediation
from poddebugger.models import WorkloadRef
from poddebugger.providers.base import ContainerPlatform, ProviderError


class _StubProvider(ContainerPlatform):
    name = "podman"

    def __init__(self, exec_output: str = "hello\n", raise_on_exec=None):
        self.exec_calls: list[list[str]] = []
        self._out = exec_output
        self._raise = raise_on_exec

    def preflight(self): pass
    def resolve(self, target, namespace=None): return WorkloadRef(name=target, platform="podman")
    def get_workload(self, ref): raise NotImplementedError
    def get_events(self, ref): return []
    def get_logs(self, ref, tail=200): return ""
    def get_spec(self, ref): return {}

    def exec(self, ref, command):
        self.exec_calls.append(command)
        if self._raise is not None:
            raise self._raise
        return self._out


def _ensure_shell_disabled():
    """Phase-12 isolation: many tests want a virgin catalog. Remove the
    shell action if a previous test or env var enabled it."""
    remediation.CATALOG.pop("shell", None)


class ShellOffByDefaultTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.pop("PODDEBUGGER_ALLOW_SHELL", None)
        _ensure_shell_disabled()

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["PODDEBUGGER_ALLOW_SHELL"] = self._saved_env
        _ensure_shell_disabled()

    def test_shell_not_in_default_catalog(self):
        self.assertNotIn("shell", remediation.list_actions())
        self.assertNotIn("shell", remediation.list_actions("podman"))
        self.assertFalse(remediation.shell_action_enabled())

    def test_parse_params_unknown_action_until_enabled(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("shell", ["command=echo hi"])


class ShellEnabledTest(unittest.TestCase):
    def setUp(self):
        _ensure_shell_disabled()
        remediation.enable_shell_action()

    def tearDown(self):
        _ensure_shell_disabled()

    def test_enable_registers_action(self):
        self.assertIn("shell", remediation.list_actions())
        self.assertTrue(remediation.shell_action_enabled())

    def test_spec_is_high_risk_on_both_platforms(self):
        spec = remediation.get_spec("shell")
        self.assertEqual(spec.risk, "high")
        self.assertIn("podman", spec.platforms)
        self.assertIn("kubernetes", spec.platforms)

    def test_parse_requires_command(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("shell", [])
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("shell", ["command="])

    def test_parse_rejects_unknown_params(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("shell", ["command=ls", "rogue=1"])

    def test_parse_returns_clean_dict(self):
        params = remediation.parse_params("shell", ["command=ls /tmp"])
        self.assertEqual(params, {"command": "ls /tmp"})

    def test_plan_carries_command_and_no_reversal(self):
        provider = _StubProvider()
        ref = WorkloadRef(name="web", platform="podman")
        params = remediation.parse_params("shell", ["command=ls /tmp"])
        plan = remediation.make_plan(provider, ref, "shell", params)
        self.assertEqual(plan.action, "shell")
        self.assertEqual(plan.risk, "high")
        self.assertIn("ls /tmp", plan.summary)
        self.assertEqual(plan.reversal, {})

    def test_plan_long_command_is_truncated_in_summary(self):
        provider = _StubProvider()
        ref = WorkloadRef(name="web", platform="podman")
        cmd = "echo " + "x" * 200
        params = remediation.parse_params("shell", [f"command={cmd}"])
        plan = remediation.make_plan(provider, ref, "shell", params)
        self.assertTrue(plan.summary.endswith("..."))
        self.assertLess(len(plan.summary), 120)

    def test_plan_refuses_protected_namespaces(self):
        provider = _StubProvider()
        provider.name = "kubernetes"
        ref = WorkloadRef(name="x", namespace="kube-system",
                          platform="kubernetes")
        with self.assertRaises(remediation.RemediationError):
            remediation.make_plan(provider, ref, "shell",
                                  {"command": "id"})

    def test_execute_runs_via_provider_exec(self):
        provider = _StubProvider(exec_output="hello\n")
        ref = WorkloadRef(name="web", platform="podman")
        params = remediation.parse_params("shell", ["command=echo hello"])
        plan = remediation.make_plan(provider, ref, "shell", params)
        result = remediation.execute(provider, ref, plan)
        self.assertTrue(result.executed)
        self.assertEqual(result.result, "hello\n")
        # The provider got sh -c "echo hello".
        self.assertEqual(provider.exec_calls[-1], ["sh", "-c", "echo hello"])

    def test_execute_truncates_long_output(self):
        big = "x" * 5000
        provider = _StubProvider(exec_output=big)
        ref = WorkloadRef(name="web", platform="podman")
        params = remediation.parse_params("shell", ["command=cat /huge"])
        plan = remediation.make_plan(provider, ref, "shell", params)
        result = remediation.execute(provider, ref, plan)
        self.assertTrue(result.executed)
        self.assertIn("…(truncated)", result.result)
        self.assertLess(len(result.result), 4200)

    def test_execute_records_provider_error(self):
        provider = _StubProvider(raise_on_exec=ProviderError("exec timed out"))
        ref = WorkloadRef(name="web", platform="podman")
        params = remediation.parse_params("shell", ["command=sleep 1000"])
        plan = remediation.make_plan(provider, ref, "shell", params)
        result = remediation.execute(provider, ref, plan)
        self.assertFalse(result.executed)
        self.assertIn("timed out", result.result)

    def test_gate_can_refuse_shell_like_any_other_action(self):
        from poddebugger.approvals import DenyGate
        provider = _StubProvider()
        ref = WorkloadRef(name="web", platform="podman")
        params = remediation.parse_params("shell", ["command=ls"])
        plan = remediation.make_plan(provider, ref, "shell", params)
        result = remediation.execute(provider, ref, plan, gate=DenyGate())
        self.assertFalse(result.executed)
        self.assertIn("refused", result.result)
        # The provider exec was NEVER invoked — gate stopped us.
        self.assertEqual(provider.exec_calls, [])


class EnvVarAutoEnableTest(unittest.TestCase):
    """``PODDEBUGGER_ALLOW_SHELL=1`` enables the shell action at import time."""

    def test_env_var_registers_via_module_function(self):
        # We can't test the import-time path without spawning a subprocess,
        # but we can verify the function the env var calls works the same.
        _ensure_shell_disabled()
        try:
            remediation.enable_shell_action()
            self.assertTrue(remediation.shell_action_enabled())
        finally:
            _ensure_shell_disabled()


class CatalogMenuMentionsShellWhenEnabledTest(unittest.TestCase):
    def setUp(self):
        _ensure_shell_disabled()

    def tearDown(self):
        _ensure_shell_disabled()

    def test_menu_does_not_mention_shell_by_default(self):
        from poddebugger.scaffold.agents.remediator import catalog_menu
        menu = catalog_menu("podman")
        self.assertNotIn("shell", menu.lower())

    def test_menu_mentions_shell_and_warns_when_enabled(self):
        from poddebugger.scaffold.agents.remediator import catalog_menu
        remediation.enable_shell_action()
        menu = catalog_menu("podman")
        self.assertIn("shell", menu.lower())
        self.assertIn("freeform", menu.lower())  # the warning footer


if __name__ == "__main__":
    unittest.main()
