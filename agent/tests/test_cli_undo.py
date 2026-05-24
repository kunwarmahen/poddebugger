"""End-to-end CLI tests for `remediate --undo` (Phase 7D).

Exercises the argparse plumbing and the save→undo round-trip through the
actual ``cli.main`` entry point, with the provider's ``_run`` stubbed.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from poddebugger import cli, remediation
from poddebugger.providers.podman import PodmanProvider
from tests.util import cp


def _stub_podman_provider(state: dict):
    """A PodmanProvider whose ``_run`` dispatches by command, not call order.

    ``state["memory"]`` is the current memory limit in bytes — ``update``
    mutates it so the post-execute inspect reflects the new state. Every
    ``_run`` call is recorded on ``provider.calls``.
    """
    p = PodmanProvider()
    calls: list[list[str]] = []

    def _inspect_doc() -> str:
        return json.dumps([{
            "Name": "web", "ImageName": "img:1",
            "State": {"Status": "running", "Running": True,
                      "Health": {"Status": ""}, "ExitCode": 0,
                      "OOMKilled": False, "StartedAt": "", "FinishedAt": ""},
            "Config": {"Env": [], "Cmd": [], "Entrypoint": None,
                       "Healthcheck": None, "Labels": {}},
            "HostConfig": {"Memory": state["memory"], "MemorySwap": 0,
                           "NanoCpus": 0,
                           "RestartPolicy": {"Name": "always"}},
            "RestartCount": 0,
        }])

    def fake(args, check=True):
        calls.append(args)
        if args[:2] == ["container", "exists"]:
            return cp("", rc=0)
        if args[:1] == ["inspect"]:
            return cp(_inspect_doc())
        if args[:1] == ["update"]:
            # mutate the simulated memory limit to match --memory
            if "--memory" in args:
                state["memory"] = int(args[args.index("--memory") + 1])
            return cp("container updated\n", rc=0)
        return cp("", rc=0)

    p._run = fake
    p.preflight = lambda: None
    p.calls = calls
    return p


class UndoFlowTest(unittest.TestCase):
    """A full apply → undo cycle for `set-resources` on Podman."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PODDEBUGGER_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("PODDEBUGGER_STATE_DIR", None)
        self.tmp.cleanup()

    def test_apply_then_undo_round_trip(self):
        # Shared "cluster state" — both phases use it so the simulated
        # memory limit flows naturally from apply → undo.
        state = {"memory": 256 * 1024 ** 2}
        providers: dict = {}

        # ----- step 1: apply (256Mi → 512Mi) -----
        def _provider_apply(_platform):
            p = _stub_podman_provider(state)
            providers["apply"] = p
            return p

        with patch("poddebugger.cli.get_provider", _provider_apply), \
             patch("poddebugger.remediation.time.sleep") as sleep_mock:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([
                    "remediate", "web",
                    "--platform", "podman",
                    "--action", "set-resources",
                    "--param", "container=web",
                    "--param", "memory_limit=512Mi",
                    "--confirm", "--yes", "--json",
                    "--verify-wait", "2",
                ])
            self.assertEqual(rc, 0)
            sleep_mock.assert_called_once_with(2)
            data = json.loads(buf.getvalue())
            self.assertTrue(data["executed"])
            self.assertEqual(data["verification"]["outcome"], "recovered")
            self.assertIsNotNone(data["saved_to"])
            self.assertTrue(Path(data["saved_to"]).exists())

        # state["memory"] should now be 512Mi.
        self.assertEqual(state["memory"], 512 * 1024 ** 2)

        # ----- step 2: undo (512Mi → 256Mi) -----
        def _provider_undo(_platform):
            p = _stub_podman_provider(state)
            providers["undo"] = p
            return p

        with patch("poddebugger.cli.get_provider", _provider_undo), \
             patch("poddebugger.remediation.time.sleep"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([
                    "remediate", "web",
                    "--platform", "podman",
                    "--undo", "",       # use the auto-saved file
                    "--confirm", "--yes", "--json",
                    "--verify-wait", "2",
                ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertTrue(data["executed"])
            self.assertEqual(data["action"], "set-resources")

        # Undo ran a podman update — confirm it targeted the baseline value.
        update_args = [a for a in providers["undo"].calls if a[:1] == ["update"]]
        self.assertTrue(update_args, "expected an `update` invocation")
        self.assertIn("--memory", update_args[0])
        idx = update_args[0].index("--memory")
        self.assertEqual(update_args[0][idx + 1], str(256 * 1024 ** 2))
        # And state is now back to 256Mi — full round trip.
        self.assertEqual(state["memory"], 256 * 1024 ** 2)


class UndoErrorPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PODDEBUGGER_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("PODDEBUGGER_STATE_DIR", None)
        self.tmp.cleanup()

    def test_undo_with_no_saved_file_is_a_clear_error(self):
        def _provider(_):
            p = PodmanProvider()
            p._run = lambda args, check=True: cp("")
            p.preflight = lambda: None
            return p

        with patch("poddebugger.cli.get_provider", _provider):
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli.main([
                    "remediate", "web",
                    "--platform", "podman",
                    "--undo", "",
                    "--confirm",
                ])
            self.assertEqual(rc, 1)
            self.assertIn("no saved remediation", err.getvalue())

    def test_undo_dry_run_renders_plan_without_executing(self):
        # Pre-seed the save file with a known reversal.
        ref_payload = {
            "action": "set-resources",
            "executed": True,
            "target": {"name": "web", "namespace": None,
                       "container": None, "platform": "podman"},
            "reversal": {
                "action": "set-resources",
                "params": {"container": "web", "memory_limit": "256Mi"},
            },
        }
        from poddebugger.models import WorkloadRef
        remediation.save_for_undo(WorkloadRef(name="web", platform="podman"),
                                  ref_payload)

        # Dry-run: planner reads inspect once; no podman update should run.
        inspect = json.dumps([{
            "Name": "web", "ImageName": "img:1",
            "State": {"Status": "running", "Running": True,
                      "Health": {"Status": ""}, "ExitCode": 0,
                      "OOMKilled": False, "StartedAt": "", "FinishedAt": ""},
            "Config": {"Env": [], "Cmd": [], "Entrypoint": None,
                       "Healthcheck": None, "Labels": {}},
            "HostConfig": {"Memory": 512 * 1024 ** 2, "MemorySwap": 0,
                           "NanoCpus": 0, "RestartPolicy": {"Name": "always"}},
            "RestartCount": 0,
        }])

        seen: list[list[str]] = []

        def _provider(_):
            p = PodmanProvider()
            def fake(args, check=True):
                seen.append(args)
                return cp(inspect)
            p._run = fake
            p.preflight = lambda: None
            return p

        with patch("poddebugger.cli.get_provider", _provider):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([
                    "remediate", "web",
                    "--platform", "podman",
                    "--undo", "",
                    "--json",
                ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertFalse(data["executed"])
            # No `update` arg should appear — dry run only.
            self.assertFalse(any(args[:1] == ["update"] for args in seen))


class ApplyVerifyDisabledTest(unittest.TestCase):
    """--no-verify and --verify-wait 0 both short-circuit the recovery check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PODDEBUGGER_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("PODDEBUGGER_STATE_DIR", None)
        self.tmp.cleanup()

    def test_no_verify_skips_the_recheck(self):
        inspect = json.dumps([{
            "Name": "web", "ImageName": "img:1",
            "State": {"Status": "running", "Running": True,
                      "Health": {"Status": ""}, "ExitCode": 0,
                      "OOMKilled": False, "StartedAt": "", "FinishedAt": ""},
            "Config": {"Env": [], "Cmd": [], "Entrypoint": None,
                       "Healthcheck": None, "Labels": {}},
            "HostConfig": {"Memory": 0, "MemorySwap": 0, "NanoCpus": 0,
                           "RestartPolicy": {"Name": "always"}},
            "RestartCount": 0,
        }])

        def _provider(_):
            p = PodmanProvider()
            p._run = lambda args, check=True: cp(inspect if args[:1] == ["inspect"] else "")
            p.preflight = lambda: None
            return p

        with patch("poddebugger.cli.get_provider", _provider), \
             patch("poddebugger.remediation.time.sleep") as sleep_mock:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([
                    "remediate", "web",
                    "--platform", "podman",
                    "--action", "restart",
                    "--confirm", "--yes", "--json", "--no-verify",
                ])
            self.assertEqual(rc, 0)
            sleep_mock.assert_not_called()
            data = json.loads(buf.getvalue())
            self.assertEqual(data["verification"]["outcome"], "skipped")


if __name__ == "__main__":
    unittest.main()
