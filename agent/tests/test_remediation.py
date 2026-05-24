"""Unit tests for the Phase 7A remediation catalog (HLD §12)."""

import json
import unittest

from poddebugger import remediation
from poddebugger.models import WorkloadRef
from poddebugger.providers.base import ProviderError
from poddebugger.providers.kubernetes import KubernetesProvider
from poddebugger.providers.podman import PodmanProvider
from tests.util import cp


# ----------------------------------------------------------------------------
# value parsers
# ----------------------------------------------------------------------------

class MemoryParseTest(unittest.TestCase):
    def test_binary_suffixes(self):
        self.assertEqual(remediation.parse_memory("1Ki"), 1024)
        self.assertEqual(remediation.parse_memory("256Mi"), 256 * 1024 ** 2)
        self.assertEqual(remediation.parse_memory("2Gi"), 2 * 1024 ** 3)

    def test_decimal_suffixes(self):
        self.assertEqual(remediation.parse_memory("1M"), 10 ** 6)
        self.assertEqual(remediation.parse_memory("2G"), 2 * 10 ** 9)

    def test_plain_bytes(self):
        self.assertEqual(remediation.parse_memory("1024"), 1024)
        self.assertEqual(remediation.parse_memory(2048), 2048)

    def test_rejects_garbage(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_memory("abc")
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_memory("")

    def test_rejects_over_ceiling(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_memory("1Pi")  # 1 PiB > 64 GiB ceiling


class CpuParseTest(unittest.TestCase):
    def test_millicores(self):
        self.assertEqual(remediation.parse_cpu("500m"), 500)

    def test_fractional_cores(self):
        self.assertEqual(remediation.parse_cpu("0.5"), 500)
        self.assertEqual(remediation.parse_cpu("2"), 2000)
        self.assertEqual(remediation.parse_cpu(1.5), 1500)

    def test_rejects_negative(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_cpu("-1")

    def test_rejects_over_ceiling(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_cpu("64")  # 64 cores > 32-core ceiling


# ----------------------------------------------------------------------------
# parse_params
# ----------------------------------------------------------------------------

class ParseParamsTest(unittest.TestCase):
    def test_restart_takes_no_params(self):
        self.assertEqual(remediation.parse_params("restart", []), {})
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("restart", ["foo=bar"])

    def test_scale_requires_replicas(self):
        out = remediation.parse_params("scale", ["replicas=3"])
        self.assertEqual(out, {"replicas": 3})

    def test_scale_rejects_negative(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("scale", ["replicas=-1"])

    def test_scale_rejects_ceiling(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("scale", ["replicas=9999"])

    def test_scale_rejects_unknown(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("scale", ["replicas=3", "weird=x"])

    def test_set_resources_needs_at_least_one_value(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("set-resources", ["container=app"])

    def test_set_resources_coerces_units(self):
        out = remediation.parse_params(
            "set-resources",
            ["container=app", "memory_limit=256Mi", "cpu_limit=500m"],
        )
        self.assertEqual(out["container"], "app")
        self.assertEqual(out["memory_limit"], 256 * 1024 ** 2)
        self.assertEqual(out["cpu_limit"], 500)

    def test_adjust_probe_requires_probe(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params(
                "adjust-probe", ["container=app", "period=10"]
            )

    def test_adjust_probe_validates_choice(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params(
                "adjust-probe",
                ["container=app", "probe=whatever", "period=10"],
            )

    def test_adjust_probe_needs_at_least_one_field(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params(
                "adjust-probe", ["container=app", "probe=liveness"]
            )

    def test_adjust_probe_happy(self):
        out = remediation.parse_params(
            "adjust-probe",
            ["container=app", "probe=liveness", "initial_delay=20", "period=10"],
        )
        self.assertEqual(out, {
            "container": "app", "probe": "liveness",
            "initial_delay": 20, "period": 10,
        })

    def test_rollback_optional_revision(self):
        self.assertEqual(remediation.parse_params("rollback", []), {})
        self.assertEqual(
            remediation.parse_params("rollback", ["revision=3"]),
            {"revision": 3},
        )

    def test_malformed_param(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("scale", ["replicas"])

    def test_unknown_action(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("frob", [])


# ----------------------------------------------------------------------------
# list_actions / platform support
# ----------------------------------------------------------------------------

class CatalogTest(unittest.TestCase):
    def test_lists_all_actions_no_filter(self):
        names = remediation.list_actions()
        self.assertIn("restart", names)
        self.assertIn("scale", names)
        self.assertIn("adjust-probe", names)

    def test_podman_filter_drops_k8s_only_actions(self):
        names = remediation.list_actions("podman")
        self.assertIn("restart", names)
        self.assertIn("set-resources", names)
        self.assertNotIn("scale", names)
        self.assertNotIn("adjust-probe", names)
        self.assertNotIn("rollback", names)

    def test_openshift_treated_as_kubernetes(self):
        names = remediation.list_actions("openshift")
        self.assertIn("scale", names)
        self.assertIn("rollback", names)


# ----------------------------------------------------------------------------
# protected namespaces
# ----------------------------------------------------------------------------

class GuardrailTest(unittest.TestCase):
    def test_protected_namespace_refused(self):
        provider = KubernetesProvider(binary="kubectl")
        provider._run = lambda args, check=True: cp("{}")
        ref = WorkloadRef(name="x", namespace="kube-system", platform="kubernetes")
        with self.assertRaises(remediation.RemediationError):
            remediation.make_plan(provider, ref, "scale", {"replicas": 3})

    def test_openshift_prefix_refused(self):
        provider = KubernetesProvider(binary="kubectl")
        provider._run = lambda args, check=True: cp("{}")
        ref = WorkloadRef(
            name="x", namespace="openshift-monitoring", platform="kubernetes"
        )
        with self.assertRaises(remediation.RemediationError):
            remediation.make_plan(provider, ref, "scale", {"replicas": 3})


# ----------------------------------------------------------------------------
# plan: restart
# ----------------------------------------------------------------------------

class RestartPlanTest(unittest.TestCase):
    def test_podman_restart_plan(self):
        provider = PodmanProvider()
        ref = WorkloadRef(name="web", platform="podman")
        plan = remediation.make_plan(provider, ref, "restart", {})
        self.assertEqual(plan.action, "restart")
        self.assertEqual(plan.risk, "low")
        self.assertIn("web", plan.summary)

    def test_kubernetes_restart_plan(self):
        provider = KubernetesProvider(binary="kubectl")
        ref = WorkloadRef(name="web", namespace="prod", platform="kubernetes")
        plan = remediation.make_plan(provider, ref, "restart", {})
        self.assertEqual(plan.action, "restart")
        self.assertIn("web", plan.summary)
        self.assertIn("prod", plan.target)


# ----------------------------------------------------------------------------
# plan: scale (Kubernetes)
# ----------------------------------------------------------------------------

def _stub_kube(provider, responses):
    """Drive a KubernetesProvider's ``_run`` from a list of (stdout, rc) pairs."""
    seen = []

    def fake(args, check=True):
        seen.append(args)
        if not responses:
            return cp("", rc=0)
        stdout, rc = responses.pop(0)
        return cp(stdout, rc=rc)

    provider._run = fake
    return seen


def _pod_doc(owner_kind="ReplicaSet", owner_name="web-7c"):
    return json.dumps({
        "metadata": {
            "name": "web-7c-xyz",
            "namespace": "prod",
            "ownerReferences": [
                {"kind": owner_kind, "name": owner_name, "controller": True}
            ],
        },
        "spec": {"containers": [{"name": "app"}]},
        "status": {},
    })


def _rs_doc(deploy="web"):
    return json.dumps({
        "metadata": {
            "ownerReferences": [
                {"kind": "Deployment", "name": deploy, "controller": True}
            ],
        }
    })


class ScalePlanTest(unittest.TestCase):
    def test_plan_diffs_replicas(self):
        provider = KubernetesProvider(binary="kubectl")
        deployment = json.dumps({"spec": {"replicas": 2}})
        _stub_kube(provider, [
            (_pod_doc(), 0),                # get_controller: pod inspect
            (_rs_doc(), 0),                 # get_controller: replicaset hop
            (deployment, 0),                # plan_fn: get deployment
        ])
        ref = WorkloadRef(name="web-7c-xyz", namespace="prod", platform="kubernetes")
        plan = remediation.make_plan(provider, ref, "scale", {"replicas": 5})
        self.assertEqual(plan.old, {"replicas": 2})
        self.assertEqual(plan.new, {"replicas": 5})
        # reversal should restore old replica count
        self.assertEqual(plan.reversal,
                         {"action": "scale", "params": {"replicas": 2}})
        self.assertIn("2", plan.summary)
        self.assertIn("5", plan.summary)


class ScaleExecuteTest(unittest.TestCase):
    def test_execute_invokes_kubectl_scale(self):
        provider = KubernetesProvider(binary="kubectl")
        deployment = json.dumps({"spec": {"replicas": 2}})
        # plan stage: pod, rs, deployment GET
        # execute stage: pod, rs (to re-resolve controller), then scale
        seen = _stub_kube(provider, [
            (_pod_doc(), 0), (_rs_doc(), 0), (deployment, 0),
            (_pod_doc(), 0), (_rs_doc(), 0),
            ("deployment.apps/web scaled\n", 0),
        ])
        ref = WorkloadRef(name="web-7c-xyz", namespace="prod", platform="kubernetes")
        plan = remediation.make_plan(provider, ref, "scale", {"replicas": 5})
        # Drop the cached pod so execute walks ownerReferences fresh.
        provider._pod_cache.clear()
        result = remediation.execute(provider, ref, plan)
        self.assertTrue(result.executed)
        self.assertEqual(seen[-1],
                         ["scale", "deployment/web", "-n", "prod", "--replicas=5"])
        self.assertEqual(result.reversal,
                         {"action": "scale", "params": {"replicas": 2}})


# ----------------------------------------------------------------------------
# plan: set-resources (Podman + Kubernetes)
# ----------------------------------------------------------------------------

class SetResourcesPodmanTest(unittest.TestCase):
    def test_plan_captures_reversal_from_inspect(self):
        provider = PodmanProvider()
        inspect = json.dumps([{
            "Name": "web",
            "ImageName": "img:1",
            "State": {"Status": "running", "Running": True, "Health": {"Status": ""}},
            "Config": {"Env": [], "Cmd": [], "Entrypoint": None, "Healthcheck": None, "Labels": {}},
            "HostConfig": {
                "Memory": 256 * 1024 ** 2,
                "MemorySwap": 0,
                "NanoCpus": 500 * 1_000_000,  # 0.5 cores = 500m
                "RestartPolicy": {"Name": "always"},
            },
        }])
        provider._run = lambda args, check=True: cp(inspect)
        ref = WorkloadRef(name="web", platform="podman")
        params = remediation.parse_params(
            "set-resources",
            ["container=web", "memory_limit=512Mi"],
        )
        plan = remediation.make_plan(provider, ref, "set-resources", params)
        self.assertEqual(plan.action, "set-resources")
        self.assertEqual(plan.risk, "medium")
        self.assertEqual(plan.old["memory_limit"], 256 * 1024 ** 2)
        self.assertEqual(plan.new["memory_limit"], 512 * 1024 ** 2)
        self.assertEqual(plan.reversal["action"], "set-resources")
        self.assertEqual(plan.reversal["params"]["memory_limit"], "256Mi")

    def test_reversal_round_trips_through_parse(self):
        """Phase 7D: the reversal must parse cleanly so --undo can replay it.

        Regression: a baseline with no memory limit (HostConfig.Memory == 0)
        used to produce reversal params = {container: ...} only, which the
        catalog parser rejected with "needs at least one of ...".
        """
        provider = PodmanProvider()
        inspect = json.dumps([{
            "Name": "web", "ImageName": "img:1",
            "State": {"Status": "running", "Running": True, "Health": {"Status": ""}},
            "Config": {"Env": [], "Cmd": [], "Entrypoint": None,
                       "Healthcheck": None, "Labels": {}},
            "HostConfig": {"Memory": 0, "MemorySwap": 0, "NanoCpus": 0,
                           "RestartPolicy": {"Name": "always"}},
        }])
        provider._run = lambda args, check=True: cp(inspect)
        ref = WorkloadRef(name="web", platform="podman")
        params = remediation.parse_params(
            "set-resources",
            ["container=web", "memory_limit=256Mi"],
        )
        plan = remediation.make_plan(provider, ref, "set-resources", params)
        # The reversal must include memory_limit even though baseline was 0.
        self.assertIn("memory_limit", plan.reversal["params"])
        # And it must parse — that's what `remediate --undo` will do.
        replay = remediation.parse_params(
            plan.reversal["action"], plan.reversal["params"]
        )
        self.assertEqual(replay["memory_limit"], 0)

    def test_podman_rejects_requests(self):
        provider = PodmanProvider()
        inspect = json.dumps([{
            "Name": "web", "ImageName": "img:1",
            "State": {"Status": "running", "Running": True, "Health": {"Status": ""}},
            "Config": {"Env": [], "Cmd": [], "Entrypoint": None, "Healthcheck": None, "Labels": {}},
            "HostConfig": {"Memory": 0, "MemorySwap": 0, "NanoCpus": 0,
                           "RestartPolicy": {"Name": "always"}},
        }])
        provider._run = lambda args, check=True: cp(inspect)
        ref = WorkloadRef(name="web", platform="podman")
        params = remediation.parse_params(
            "set-resources",
            ["container=web", "memory_request=128Mi"],
        )
        with self.assertRaises(remediation.RemediationError):
            remediation.make_plan(provider, ref, "set-resources", params)


class SetResourcesKubernetesTest(unittest.TestCase):
    def test_plan_diffs_resources(self):
        provider = KubernetesProvider(binary="kubectl")
        deployment = json.dumps({
            "spec": {"template": {"spec": {"containers": [
                {"name": "app",
                 "resources": {"limits": {"memory": "256Mi"}}}
            ]}}}
        })
        _stub_kube(provider, [
            (_pod_doc(), 0), (_rs_doc(), 0), (deployment, 0),
        ])
        ref = WorkloadRef(name="web-7c-xyz", namespace="prod", platform="kubernetes")
        params = remediation.parse_params(
            "set-resources",
            ["container=app", "memory_limit=512Mi"],
        )
        plan = remediation.make_plan(provider, ref, "set-resources", params)
        self.assertEqual(plan.old, {"limits": {"memory": "256Mi"}})
        self.assertEqual(plan.new, {"limits": {"memory": "512Mi"}})
        self.assertEqual(plan.reversal["params"]["memory_limit"], "256Mi")
        self.assertIn("256Mi → 512Mi", plan.summary)


# ----------------------------------------------------------------------------
# plan: adjust-probe
# ----------------------------------------------------------------------------

class AdjustProbeTest(unittest.TestCase):
    def test_plan_records_old_probe_and_diff(self):
        provider = KubernetesProvider(binary="kubectl")
        deployment = json.dumps({
            "spec": {"template": {"spec": {"containers": [
                {"name": "app",
                 "livenessProbe": {
                     "initialDelaySeconds": 5,
                     "periodSeconds": 10,
                 }}
            ]}}}
        })
        _stub_kube(provider, [
            (_pod_doc(), 0), (_rs_doc(), 0), (deployment, 0),
        ])
        ref = WorkloadRef(name="web-7c-xyz", namespace="prod", platform="kubernetes")
        params = remediation.parse_params(
            "adjust-probe",
            ["container=app", "probe=liveness", "initial_delay=30"],
        )
        plan = remediation.make_plan(provider, ref, "adjust-probe", params)
        self.assertEqual(plan.old["initialDelaySeconds"], 5)
        self.assertEqual(plan.new["initialDelaySeconds"], 30)
        # Reversal carries the prior probe.
        rev = plan.reversal
        self.assertEqual(rev["params"]["probe"], "liveness")
        self.assertEqual(rev["params"]["initial_delay"], 5)


# ----------------------------------------------------------------------------
# plan: rollback
# ----------------------------------------------------------------------------

class RollbackTest(unittest.TestCase):
    def test_plan_captures_current_revision(self):
        provider = KubernetesProvider(binary="kubectl")
        deployment = json.dumps({
            "metadata": {"annotations": {
                "deployment.kubernetes.io/revision": "7"
            }},
            "spec": {"template": {"spec": {"containers": [{"name": "app"}]}}},
        })
        _stub_kube(provider, [
            (_pod_doc(), 0), (_rs_doc(), 0), (deployment, 0),
        ])
        ref = WorkloadRef(name="web-7c-xyz", namespace="prod", platform="kubernetes")
        plan = remediation.make_plan(provider, ref, "rollback", {})
        self.assertEqual(plan.old, {"revision": "7"})
        # Reversal: re-roll forward to revision 7.
        self.assertEqual(plan.reversal,
                         {"action": "rollback", "params": {"revision": 7}})

    def test_execute_passes_revision_flag(self):
        provider = KubernetesProvider(binary="kubectl")
        deployment = json.dumps({
            "metadata": {"annotations": {
                "deployment.kubernetes.io/revision": "7"
            }},
            "spec": {"template": {"spec": {"containers": [{"name": "app"}]}}},
        })
        seen = _stub_kube(provider, [
            (_pod_doc(), 0), (_rs_doc(), 0), (deployment, 0),
            (_pod_doc(), 0), (_rs_doc(), 0),
            ("deployment.apps/web rolled back\n", 0),
        ])
        ref = WorkloadRef(name="web-7c-xyz", namespace="prod", platform="kubernetes")
        plan = remediation.make_plan(provider, ref, "rollback", {"revision": 5})
        provider._pod_cache.clear()
        result = remediation.execute(provider, ref, plan)
        self.assertTrue(result.executed)
        self.assertIn("--to-revision=5", seen[-1])


# ----------------------------------------------------------------------------
# unsupported-platform routing
# ----------------------------------------------------------------------------

class PlatformGuardTest(unittest.TestCase):
    def test_scale_rejected_on_podman(self):
        provider = PodmanProvider()
        ref = WorkloadRef(name="web", platform="podman")
        with self.assertRaises(remediation.RemediationError):
            remediation.make_plan(provider, ref, "scale", {"replicas": 3})


# ----------------------------------------------------------------------------
# legacy provider.remediate(...) compatibility
# ----------------------------------------------------------------------------

class LegacyRemediateShimTest(unittest.TestCase):
    def test_podman_restart_via_shim_returns_executed(self):
        provider = PodmanProvider()
        provider._run = lambda args, check=True: cp("web\n")
        res = provider.remediate(
            WorkloadRef(name="web", platform="podman"),
            {"type": "restart"},
        )
        self.assertTrue(res["executed"])
        self.assertEqual(res["action"], "restart")
        # The new shim also surfaces plan/reversal alongside the legacy fields.
        self.assertIn("plan", res)

    def test_unknown_action_raises_provider_error(self):
        provider = PodmanProvider()
        provider._run = lambda args, check=True: cp()
        with self.assertRaises(ProviderError):
            provider.remediate(
                WorkloadRef(name="web", platform="podman"),
                {"type": "frob"},
            )


if __name__ == "__main__":
    unittest.main()
