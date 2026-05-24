"""CLI integration tests for Phase 11 approvals on `remediate`."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from poddebugger import approvals, cli
from poddebugger.providers.podman import PodmanProvider
from tests.util import cp


def _stub_provider():
    """Minimal podman stub that always says 'restart succeeded' if asked."""
    p = PodmanProvider()
    calls: list[list[str]] = []

    def fake(args, check=True):
        calls.append(args)
        if args[:2] == ["container", "exists"]:
            return cp("", rc=0)
        if args[:1] == ["restart"]:
            return cp("web\n", rc=0)
        return cp("", rc=0)

    p._run = fake
    p.preflight = lambda: None
    p.calls = calls
    return p


class _Tty:
    """Pretends to be an interactive stdin."""
    def isatty(self): return True


class _NonTty:
    def isatty(self): return False


class RemediateYesBypassesGateTest(unittest.TestCase):
    """`--yes` collapses to AutoApprove — no prompt, action runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PODDEBUGGER_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("PODDEBUGGER_STATE_DIR", None)
        self.tmp.cleanup()

    def test_remediate_with_yes_executes_without_prompt(self):
        p = _stub_provider()
        with patch("poddebugger.cli.get_provider", lambda _: p):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([
                    "remediate", "web",
                    "--platform", "podman",
                    "--action", "restart",
                    "--confirm", "--yes", "--no-verify", "--json",
                ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertTrue(data["executed"])
            self.assertIn(["restart", "web"], p.calls)


class RemediateNonTtyRefusesTest(unittest.TestCase):
    """A non-TTY without --yes lands on the deny gate; action does NOT run."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PODDEBUGGER_STATE_DIR"] = self.tmp.name
        # Point at a non-existent path so the factory doesn't try to load
        # rules. (An empty file would fail JSON parsing.)
        from pathlib import Path
        os.environ["PODDEBUGGER_APPROVALS_FILE"] = str(
            Path(self.tmp.name) / "no-rules.json")

    def tearDown(self):
        for k in ("PODDEBUGGER_STATE_DIR", "PODDEBUGGER_APPROVALS_FILE"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_remediate_no_tty_no_yes_is_refused(self):
        p = _stub_provider()
        with patch("poddebugger.cli.get_provider", lambda _: p), \
             patch("poddebugger.approvals.sys.stdin", _NonTty()):
            buf, err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                rc = cli.main([
                    "remediate", "web",
                    "--platform", "podman",
                    "--action", "restart",
                    "--confirm", "--no-verify", "--json",
                ])
            self.assertEqual(rc, 1)
            data = json.loads(buf.getvalue())
            self.assertFalse(data["executed"])
            self.assertIn("refused by approval gate", data["result"])
            # The podman restart command was NEVER invoked.
            self.assertNotIn(["restart", "web"], p.calls)


class RemediateRulesAllowTest(unittest.TestCase):
    """A pre-existing allow rule lets a non-TTY run succeed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PODDEBUGGER_STATE_DIR"] = self.tmp.name
        from pathlib import Path
        self.rules_file = Path(self.tmp.name) / "approvals.json"
        os.environ["PODDEBUGGER_APPROVALS_FILE"] = str(self.rules_file)
        # Pre-seed an allow rule.
        approvals.save_rules(self.rules_file, [
            {"kind": "remediation", "action": "restart",
             "target": {"platform": "podman"}, "decision": "allow"},
        ])

    def tearDown(self):
        for k in ("PODDEBUGGER_STATE_DIR", "PODDEBUGGER_APPROVALS_FILE"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def test_non_tty_executes_when_a_matching_rule_exists(self):
        p = _stub_provider()
        with patch("poddebugger.cli.get_provider", lambda _: p), \
             patch("poddebugger.approvals.sys.stdin", _NonTty()):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([
                    "remediate", "web",
                    "--platform", "podman",
                    "--action", "restart",
                    "--confirm", "--no-verify", "--json",
                ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertTrue(data["executed"])

    def test_approvals_off_disables_rule_consultation(self):
        # Same setup as above, but `--approvals off` makes the gate ignore
        # the rules file → non-TTY → deny.
        p = _stub_provider()
        with patch("poddebugger.cli.get_provider", lambda _: p), \
             patch("poddebugger.approvals.sys.stdin", _NonTty()):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([
                    "remediate", "web",
                    "--platform", "podman",
                    "--action", "restart",
                    "--confirm", "--no-verify", "--json",
                    "--approvals", "off",
                ])
            self.assertEqual(rc, 1)


class ApprovalsSubcommandTest(unittest.TestCase):
    """The `poddebugger approvals` sub-command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from pathlib import Path
        self.rules_file = Path(self.tmp.name) / "approvals.json"
        os.environ["PODDEBUGGER_APPROVALS_FILE"] = str(self.rules_file)

    def tearDown(self):
        os.environ.pop("PODDEBUGGER_APPROVALS_FILE", None)
        self.tmp.cleanup()

    def test_add_list_remove_round_trip(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main([
                "approvals", "add",
                "--kind", "remediation",
                "--action", "restart",
                "--target-platform", "podman",
                "--target-name", "web",
            ])
        self.assertEqual(rc, 0)
        rules = approvals.load_rules(self.rules_file)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["target"]["name"], "web")

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["approvals", "list"])
        self.assertIn("restart", buf.getvalue())
        self.assertIn("podman", buf.getvalue())

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["approvals", "remove", "0"])
        self.assertEqual(rc, 0)
        self.assertEqual(approvals.load_rules(self.rules_file), [])

    def test_check_returns_allow_when_rule_matches(self):
        approvals.save_rules(self.rules_file, [
            {"kind": "remediation", "action": "restart",
             "target": {"platform": "podman"}, "decision": "allow"},
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main([
                "approvals", "check",
                "--kind", "remediation",
                "--action", "restart",
                "--target-platform", "podman",
            ])
        self.assertEqual(rc, 0)
        self.assertIn("ALLOW", buf.getvalue())

    def test_check_returns_deny_with_no_match(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main([
                "approvals", "check",
                "--kind", "remediation",
                "--action", "scale",
                "--target-platform", "kubernetes",
            ])
        self.assertIn("DENY", buf.getvalue())

    def test_remove_out_of_range_returns_nonzero(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cli.main(["approvals", "remove", "5"])
        self.assertEqual(rc, 1)
        self.assertIn("no rule", err.getvalue())


if __name__ == "__main__":
    unittest.main()
