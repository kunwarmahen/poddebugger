"""Unit tests for the Kubernetes provider — parsing, redaction, remediation."""

import json
import unittest

from poddebugger.models import WorkloadRef
from poddebugger.providers.base import ProviderError
from poddebugger.providers.kubernetes import (
    KubernetesProvider,
    _clean_stderr,
    _pick_container_state,
    _redact_env,
)
from tests.util import cp

REF = WorkloadRef(name="web-7c", namespace="prod", platform="kubernetes")


def k8s_pod(waiting=None, terminated=None, last_terminated=None, running=False,
            ready=False, restarts=0, phase="Running", env=None):
    cs = {"name": "web", "ready": ready, "restartCount": restarts,
          "image": "img:1", "state": {}, "lastState": {}}
    if waiting:
        cs["state"]["waiting"] = {"reason": waiting, "message": "back-off 5m0s"}
    if terminated:
        cs["state"]["terminated"] = terminated
    if running:
        cs["state"]["running"] = {"startedAt": "2026-05-21T10:00:00Z"}
    if last_terminated:
        cs["lastState"]["terminated"] = last_terminated
    return json.dumps({
        "metadata": {"name": "web-7c", "namespace": "prod"},
        "spec": {"restartPolicy": "Always",
                 "containers": [{"name": "web", "image": "img:1", "env": env or []}]},
        "status": {"phase": phase, "containerStatuses": [cs]},
    })


class RedactionTest(unittest.TestCase):
    def test_secret_value_masked(self):
        out = _redact_env([{"name": "API_TOKEN", "value": "abc"}])
        self.assertEqual(out, ["API_TOKEN=<redacted>"])

    def test_valuefrom_summarized(self):
        out = _redact_env([{"name": "DB", "valueFrom": {"secretKeyRef": {"name": "s"}}}])
        self.assertEqual(out, ["DB=<from secretKeyRef>"])

    def test_plain_value_kept(self):
        self.assertEqual(_redact_env([{"name": "PORT", "value": "8080"}]), ["PORT=8080"])


class HelpersTest(unittest.TestCase):
    def test_clean_stderr_drops_klog_noise(self):
        raw = ("E0521 21:27:55.93 memcache.go:265 noise\n"
               "Unable to connect to the server: no such host")
        self.assertEqual(_clean_stderr(raw), "Unable to connect to the server: no such host")

    def test_pick_container_state(self):
        key, body = _pick_container_state({"state": {"waiting": {"reason": "X"}}})
        self.assertEqual(key, "waiting")
        self.assertEqual(body["reason"], "X")


class WorkloadTest(unittest.TestCase):
    def test_crashloop_with_oom_from_last_state(self):
        p = KubernetesProvider(binary="kubectl")
        p._run = lambda args, check=True: cp(k8s_pod(
            waiting="CrashLoopBackOff", restarts=7,
            last_terminated={"exitCode": 137, "reason": "OOMKilled", "finishedAt": "t"}))
        w = p.get_workload(WorkloadRef(name="web-7c", namespace="prod",
                                       platform="kubernetes"))
        self.assertIn("CrashLoopBackOff", w.status)
        self.assertEqual(w.exit_code, 137)
        self.assertTrue(w.oom_killed)
        self.assertFalse(w.running)
        self.assertEqual(w.restart_count, 7)

    def test_running_pod(self):
        p = KubernetesProvider(binary="kubectl")
        p._run = lambda args, check=True: cp(k8s_pod(running=True, ready=True))
        w = p.get_workload(WorkloadRef(name="web-7c", namespace="prod",
                                       platform="kubernetes"))
        self.assertTrue(w.running)


class EventsTest(unittest.TestCase):
    def test_events_sorted_with_repeat_count(self):
        items = {"items": [
            {"type": "Warning", "reason": "BackOff", "message": "restarting",
             "lastTimestamp": "2026-05-21T10:06:00Z", "count": 12},
            {"type": "Normal", "reason": "Pulled", "message": "image present",
             "lastTimestamp": "2026-05-21T10:00:00Z"},
        ]}
        p = KubernetesProvider(binary="kubectl")
        p._run = lambda args, check=True: cp(json.dumps(items))
        evs = p.get_events(REF)
        self.assertEqual(evs[0].reason, "Pulled")          # sorted by time
        self.assertIn("(x12)", evs[1].message)             # repeat count


class RemediateTest(unittest.TestCase):
    def test_restart_deletes_pod(self):
        seen = []

        def fake(args, check=True):
            seen.append(args)
            return cp('pod "web-7c" deleted\n')

        p = KubernetesProvider(binary="kubectl")
        p._run = fake
        res = p.remediate(REF, {"type": "restart"})
        self.assertTrue(res["executed"])
        self.assertEqual(seen[-1], ["delete", "pod", "web-7c", "-n", "prod"])

    def test_unsupported_action_rejected(self):
        p = KubernetesProvider(binary="kubectl")
        p._run = lambda args, check=True: cp()
        with self.assertRaises(ProviderError):
            p.remediate(REF, {"type": "rollback"})


if __name__ == "__main__":
    unittest.main()
