"""Unit tests for the Podman provider — parsing, redaction, container picking."""

import json
import unittest

from poddebugger.models import WorkloadRef
from poddebugger.providers.base import ProviderError
from poddebugger.providers.podman import PodmanProvider, _redact_env
from tests.util import cp, podman_inspect

REF = WorkloadRef(name="web", platform="podman")


class RedactionTest(unittest.TestCase):
    def test_secret_keys_are_masked(self):
        out = _redact_env(["DB_PASSWORD=hunter2", "API_TOKEN=abc", "LOG_LEVEL=info"])
        self.assertIn("DB_PASSWORD=<redacted>", out)
        self.assertIn("API_TOKEN=<redacted>", out)
        self.assertIn("LOG_LEVEL=info", out)  # non-secret kept verbatim

    def test_handles_values_with_equals_signs(self):
        out = _redact_env(["URL=https://h/p?a=b"])
        self.assertEqual(out, ["URL=https://h/p?a=b"])


class WorkloadTest(unittest.TestCase):
    def test_exited_container_reports_exit_code(self):
        p = PodmanProvider()
        p._run = lambda args, check=True: cp(
            podman_inspect(running=False, status="exited", exit_code=1, restarts=3)
        )
        w = p.get_workload(REF)
        self.assertEqual(w.status, "exited")
        self.assertFalse(w.running)
        self.assertEqual(w.exit_code, 1)
        self.assertEqual(w.restart_count, 3)

    def test_oom_killed_flag(self):
        p = PodmanProvider()
        p._run = lambda args, check=True: cp(podman_inspect(oom=True, running=False))
        self.assertTrue(p.get_workload(REF).oom_killed)

    def test_running_container(self):
        p = PodmanProvider()
        p._run = lambda args, check=True: cp(
            podman_inspect(running=True, status="running")
        )
        w = p.get_workload(REF)
        self.assertTrue(w.running)
        self.assertIsNone(w.exit_code)  # not meaningful while running


class EventsTest(unittest.TestCase):
    def test_died_event_extracts_exit_code(self):
        events = (
            '{"Status":"create","Type":"container","Time":"t1"}\n'
            '{"Status":"died","Type":"container","Time":"t2","ContainerExitCode":137}\n'
        )
        p = PodmanProvider()
        p._run = lambda args, check=True: cp(events)
        evs = p.get_events(REF)
        self.assertEqual(len(evs), 2)
        died = [e for e in evs if e.reason == "died"][0]
        self.assertIn("exit_code=137", died.message)


class SpecTest(unittest.TestCase):
    def test_spec_redacts_env(self):
        p = PodmanProvider()
        p._run = lambda args, check=True: cp(
            podman_inspect(env=["SECRET_KEY=s3cr3t", "PORT=8080"])
        )
        spec = p.get_spec(REF)
        self.assertIn("SECRET_KEY=<redacted>", spec["env"])
        self.assertIn("PORT=8080", spec["env"])


class StatsTest(unittest.TestCase):
    def test_stats_parsed_and_filtered(self):
        p = PodmanProvider()
        p._run = lambda args, check=True: cp(
            json.dumps([{"cpu_percent": "4.0%", "mem_usage": "10MB / 1GB",
                         "mem_percent": "1.0%", "pids": "7"}])
        )
        stats = p.get_stats(REF)
        self.assertEqual(stats["cpu_percent"], "4.0%")
        self.assertEqual(stats["pids"], "7")

    def test_stats_empty_on_failure(self):
        p = PodmanProvider()
        p._run = lambda args, check=True: cp("", rc=1)
        self.assertEqual(p.get_stats(REF), {})


class ResolvePodTest(unittest.TestCase):
    def test_picks_unhealthiest_container(self):
        pod = json.dumps({
            "InfraContainerID": "infra",
            "Containers": [
                {"Name": "mypod-infra", "Id": "infra"},
                {"Name": "web", "Id": "w"},
                {"Name": "sidecar", "Id": "s"},
            ],
        })

        def fake(args, check=True):
            if args[:2] == ["container", "exists"]:
                return cp(rc=1)
            if args[:2] == ["pod", "exists"]:
                return cp(rc=0)
            if args[:2] == ["pod", "inspect"]:
                return cp(pod)
            if args[0] == "inspect":
                name = args[-1]
                running = name == "sidecar"  # web is the unhealthy one
                restarts = 5 if name == "web" else 0
                return cp(podman_inspect(name=name, running=running, restarts=restarts))
            return cp()

        p = PodmanProvider()
        p._run = fake
        ref = p.resolve("mypod")
        self.assertEqual(ref.name, "web")
        self.assertIn("sidecar", p._pod_note)


class RemediateTest(unittest.TestCase):
    def test_restart_executes(self):
        p = PodmanProvider()
        p._run = lambda args, check=True: cp("web\n")
        res = p.remediate(REF, {"type": "restart"})
        self.assertTrue(res["executed"])
        self.assertEqual(res["action"], "restart")

    def test_unsupported_action_rejected(self):
        p = PodmanProvider()
        p._run = lambda args, check=True: cp()
        with self.assertRaises(ProviderError):
            p.remediate(REF, {"type": "scale"})


if __name__ == "__main__":
    unittest.main()
