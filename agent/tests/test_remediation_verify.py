"""Unit tests for Phase 7D — post-remediation verification + undo persistence."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from poddebugger import remediation
from poddebugger.models import Workload, WorkloadRef
from poddebugger.providers.base import ContainerPlatform, ProviderError


# ----------------------------------------------------------------------------
# fakes
# ----------------------------------------------------------------------------

class _FakeProvider(ContainerPlatform):
    """A provider that returns whatever Workload the test sets next."""

    name = "podman"

    def __init__(self, snapshots: list[Workload | ProviderError]):
        self._snapshots = list(snapshots)

    def preflight(self):
        pass

    def resolve(self, target, namespace=None):
        return WorkloadRef(name=target, platform=self.name)

    def get_workload(self, ref):
        if not self._snapshots:
            raise AssertionError("get_workload called more times than scripted")
        snap = self._snapshots.pop(0)
        if isinstance(snap, ProviderError):
            raise snap
        return snap

    def get_events(self, ref): return []
    def get_logs(self, ref, tail=200): return ""
    def get_spec(self, ref): return {}


def _wl(*, running, restart_count=0, status="running", exit_code=None, oom=False):
    return Workload(
        ref=WorkloadRef(name="web", platform="podman"),
        kind="container", status=status, running=running,
        image="img:1", restart_count=restart_count,
        exit_code=exit_code, oom_killed=oom,
    )


# ----------------------------------------------------------------------------
# capture_baseline
# ----------------------------------------------------------------------------

class CaptureBaselineTest(unittest.TestCase):
    def test_records_observable_fields(self):
        prov = _FakeProvider([_wl(running=True, restart_count=2, status="running")])
        b = remediation.capture_baseline(prov, WorkloadRef(name="web", platform="podman"))
        self.assertTrue(b["running"])
        self.assertEqual(b["restart_count"], 2)
        self.assertEqual(b["status"], "running")

    def test_tolerates_provider_failure(self):
        prov = _FakeProvider([ProviderError("offline")])
        b = remediation.capture_baseline(prov, WorkloadRef(name="web", platform="podman"))
        self.assertIn("error", b)


# ----------------------------------------------------------------------------
# verify_recovery
# ----------------------------------------------------------------------------

class VerifyRecoveryTest(unittest.TestCase):
    def setUp(self):
        # All tests use a no-op sleep — verification is logical, not wall-clock.
        self.sleep_calls: list[int] = []
        self.sleep = lambda n: self.sleep_calls.append(n)

    def test_recovered_when_running_and_no_new_restarts(self):
        prov = _FakeProvider([_wl(running=True, restart_count=2, status="running")])
        baseline = {"running": False, "restart_count": 1,
                    "status": "exited", "oom_killed": False}
        res = remediation.verify_recovery(
            prov, WorkloadRef(name="web", platform="podman"),
            baseline, action="restart", wait_seconds=5, sleep=self.sleep,
        )
        # restart bumps restart_count by 1; that still counts as recovered.
        self.assertEqual(res["outcome"], "recovered")
        self.assertEqual(self.sleep_calls, [5])

    def test_still_failing_when_restart_count_climbs(self):
        # set-resources baseline restart_count=3; post=5 → still crashing
        prov = _FakeProvider([_wl(running=True, restart_count=5, status="running")])
        baseline = {"running": True, "restart_count": 3,
                    "status": "running", "oom_killed": False}
        res = remediation.verify_recovery(
            prov, WorkloadRef(name="web", platform="podman"),
            baseline, action="set-resources", wait_seconds=5, sleep=self.sleep,
        )
        self.assertEqual(res["outcome"], "still-failing")
        self.assertIn("restart_count", res["reason"])

    def test_still_failing_when_oom_after_action(self):
        prov = _FakeProvider([_wl(running=False, restart_count=3,
                                  status="exited", exit_code=137, oom=True)])
        baseline = {"running": True, "restart_count": 3,
                    "status": "running", "oom_killed": False}
        res = remediation.verify_recovery(
            prov, WorkloadRef(name="web", platform="podman"),
            baseline, action="set-resources", wait_seconds=3, sleep=self.sleep,
        )
        self.assertEqual(res["outcome"], "still-failing")

    def test_still_failing_when_was_running_now_not(self):
        prov = _FakeProvider([_wl(running=False, restart_count=2, status="exited")])
        baseline = {"running": True, "restart_count": 2,
                    "status": "running", "oom_killed": False}
        res = remediation.verify_recovery(
            prov, WorkloadRef(name="web", platform="podman"),
            baseline, action="set-resources", wait_seconds=2, sleep=self.sleep,
        )
        self.assertEqual(res["outcome"], "still-failing")

    def test_unknown_when_baseline_not_running_and_still_not(self):
        # No regression; controller may simply not have recreated yet.
        prov = _FakeProvider([_wl(running=False, restart_count=1, status="created")])
        baseline = {"running": False, "restart_count": 1,
                    "status": "created", "oom_killed": False}
        res = remediation.verify_recovery(
            prov, WorkloadRef(name="web", platform="podman"),
            baseline, action="set-resources", wait_seconds=1, sleep=self.sleep,
        )
        self.assertEqual(res["outcome"], "unknown")

    def test_k8s_restart_is_unknown_by_design(self):
        # Kubernetes restart deletes the pod — the ref is stale; we don't lie.
        prov = _FakeProvider([])  # never read
        res = remediation.verify_recovery(
            prov, WorkloadRef(name="web", namespace="prod", platform="kubernetes"),
            baseline={}, action="restart", wait_seconds=5, sleep=self.sleep,
        )
        self.assertEqual(res["outcome"], "unknown")
        self.assertEqual(self.sleep_calls, [])  # no waste — we never slept

    def test_skipped_when_wait_seconds_zero(self):
        prov = _FakeProvider([])
        res = remediation.verify_recovery(
            prov, WorkloadRef(name="web", platform="podman"),
            baseline={}, action="restart", wait_seconds=0, sleep=self.sleep,
        )
        self.assertEqual(res["outcome"], "skipped")

    def test_provider_failure_yields_unknown(self):
        prov = _FakeProvider([ProviderError("inspect timed out")])
        res = remediation.verify_recovery(
            prov, WorkloadRef(name="web", platform="podman"),
            baseline={"running": True, "restart_count": 1},
            action="set-resources", wait_seconds=2, sleep=self.sleep,
        )
        self.assertEqual(res["outcome"], "unknown")


# ----------------------------------------------------------------------------
# save_for_undo / load_for_undo / undo_from
# ----------------------------------------------------------------------------

class UndoPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["PODDEBUGGER_STATE_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("PODDEBUGGER_STATE_DIR", None)
        self.tmp.cleanup()

    def test_round_trip_by_ref(self):
        ref = WorkloadRef(name="web", namespace="prod", platform="kubernetes")
        payload = {
            "action": "scale",
            "executed": True,
            "reversal": {"action": "scale", "params": {"replicas": 2}},
            "target": {"name": "web", "namespace": "prod",
                       "container": None, "platform": "kubernetes"},
        }
        path = remediation.save_for_undo(ref, payload)
        self.assertTrue(Path(path).exists())
        loaded = remediation.load_for_undo(ref=ref)
        self.assertEqual(loaded["reversal"]["action"], "scale")

    def test_round_trip_by_path(self):
        ref = WorkloadRef(name="web", platform="podman")
        payload = {"action": "set-resources",
                   "reversal": {"action": "set-resources",
                                "params": {"container": "web"}},
                   "target": {"name": "web", "platform": "podman"}}
        path = remediation.save_for_undo(ref, payload)
        loaded = remediation.load_for_undo(path=path)
        self.assertEqual(loaded["reversal"]["action"], "set-resources")

    def test_missing_file_raises_clear_error(self):
        with self.assertRaises(remediation.RemediationError) as ctx:
            remediation.load_for_undo(
                ref=WorkloadRef(name="missing", platform="podman")
            )
        self.assertIn("no saved remediation", str(ctx.exception))

    def test_corrupt_file_raises_clear_error(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text("{not json")
        with self.assertRaises(remediation.RemediationError):
            remediation.load_for_undo(path=path)

    def test_keys_distinguish_namespaces(self):
        a = WorkloadRef(name="web", namespace="prod", platform="kubernetes")
        b = WorkloadRef(name="web", namespace="staging", platform="kubernetes")
        remediation.save_for_undo(a, {"action": "scale", "target": {
            "name": "web", "namespace": "prod", "platform": "kubernetes"},
            "reversal": {"action": "scale", "params": {"replicas": 1}}})
        remediation.save_for_undo(b, {"action": "scale", "target": {
            "name": "web", "namespace": "staging", "platform": "kubernetes"},
            "reversal": {"action": "scale", "params": {"replicas": 7}}})
        self.assertEqual(
            remediation.load_for_undo(ref=a)["reversal"]["params"]["replicas"], 1
        )
        self.assertEqual(
            remediation.load_for_undo(ref=b)["reversal"]["params"]["replicas"], 7
        )

    def test_save_does_not_raise_when_dir_unwritable(self):
        os.environ["PODDEBUGGER_STATE_DIR"] = "/proc/1/no-such-dir"
        ref = WorkloadRef(name="web", platform="podman")
        # must not raise — best-effort persistence
        remediation.save_for_undo(ref, {"action": "restart"})


class UndoFromTest(unittest.TestCase):
    def test_extracts_ref_action_params_from_full_payload(self):
        payload = {
            "action": "scale",
            "reversal": {"action": "scale", "params": {"replicas": 2}},
            "target": {"name": "web", "namespace": "prod",
                       "container": None, "platform": "kubernetes"},
        }
        ref, action, params = remediation.undo_from(payload)
        self.assertEqual(ref.name, "web")
        self.assertEqual(ref.namespace, "prod")
        self.assertEqual(ref.platform, "kubernetes")
        self.assertEqual(action, "scale")
        self.assertEqual(params, {"replicas": 2})

    def test_rejects_payload_without_reversal(self):
        with self.assertRaises(remediation.RemediationError) as ctx:
            remediation.undo_from({"action": "restart",
                                   "target": {"name": "web", "platform": "podman"}})
        self.assertIn("no reversal", str(ctx.exception))

    def test_rejects_payload_without_target(self):
        with self.assertRaises(remediation.RemediationError) as ctx:
            remediation.undo_from({
                "reversal": {"action": "scale", "params": {"replicas": 2}},
            })
        self.assertIn("no target", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
