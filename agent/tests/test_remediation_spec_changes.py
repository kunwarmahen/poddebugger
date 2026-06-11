"""Tests for the Phase 13B spec-change actions: set-env / set-image / recreate."""

from __future__ import annotations

import json
import unittest

from poddebugger import remediation
from poddebugger.models import WorkloadRef
from poddebugger.providers.podman import PodmanProvider
from poddebugger.providers.kubernetes import KubernetesProvider
from tests.util import cp


# ---------------------------------------------------------------------------
# podman stub — exec/run/rm dispatched by command; inspect returns a config
# ---------------------------------------------------------------------------

def _podman_with_inspect(*, image="mysql:8", env=None, cmd=None):
    p = PodmanProvider()
    calls: list[list[str]] = []
    doc = [{
        "Name": "db", "ImageName": image,
        "State": {"Status": "exited", "Running": False, "Health": {"Status": ""},
                  "ExitCode": 1, "OOMKilled": False, "StartedAt": "", "FinishedAt": ""},
        "Config": {"Env": env or [], "Cmd": cmd, "Entrypoint": None,
                   "Healthcheck": None, "Labels": {"app": "db"}},
        "HostConfig": {"Memory": 0, "MemorySwap": 0, "NanoCpus": 0,
                       "RestartPolicy": {"Name": "always"},
                       "NetworkMode": "bridge"},
        "RestartCount": 0,
    }]

    def fake(args, check=True):
        calls.append(args)
        if args[:1] == ["inspect"]:
            return cp(json.dumps(doc))
        if args[:1] == ["rm"]:
            return cp("db\n")
        if args[:1] == ["run"]:
            return cp("newid123\n")
        return cp("")

    p._run = fake
    p.preflight = lambda: None
    p.calls = calls
    return p


# ---------------------------------------------------------------------------
# kubernetes stub
# ---------------------------------------------------------------------------

def _deploy_doc(container="app", image="app:1", env=None):
    return {
        "metadata": {"name": "web", "namespace": "prod"},
        "spec": {"template": {"spec": {"containers": [
            {"name": container, "image": image, "env": env or []}
        ]}}},
    }


def _k8s_with_controller(*, deploy):
    p = KubernetesProvider(binary="kubectl")
    calls: list[list[str]] = []

    pod = json.dumps({
        "metadata": {"name": "web-xyz", "namespace": "prod",
                     "ownerReferences": [{"kind": "ReplicaSet", "name": "web-rs",
                                          "controller": True}]},
        "spec": {"containers": [{"name": "app"}]}, "status": {},
    })
    rs = json.dumps({"metadata": {"ownerReferences": [
        {"kind": "Deployment", "name": "web", "controller": True}]}})

    def fake(args, check=True):
        calls.append(args)
        if args[:2] == ["get", "pod"]:
            return cp(pod)
        if args[:2] == ["get", "replicaset"]:
            return cp(rs)
        if args[:2] == ["get", "deployment"]:
            return cp(json.dumps(deploy))
        if args[:1] == ["patch"]:
            return cp("deployment.apps/web patched\n")
        if args[:2] == ["set", "image"]:
            return cp("deployment.apps/web image updated\n")
        return cp("")

    p._run = fake
    p.preflight = lambda: None
    p._namespace = lambda ns: ns or "prod"
    p.calls = calls
    return p


# ---------------------------------------------------------------------------
# set-env
# ---------------------------------------------------------------------------

class SetEnvParseTest(unittest.TestCase):
    def test_requires_container_and_env(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("set-env", {"env": {"A": "1"}})
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("set-env", {"container": "db"})

    def test_rejects_bad_env_key(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params(
                "set-env", {"container": "db", "env": {"9BAD": "x"}})

    def test_null_value_marks_deletion(self):
        out = remediation.parse_params(
            "set-env", {"container": "db", "env": {"OLD": None, "NEW": "v"}})
        self.assertIsNone(out["env"]["OLD"])
        self.assertEqual(out["env"]["NEW"], "v")


class SetEnvPodmanTest(unittest.TestCase):
    def test_plan_and_execute_recreates_with_merged_env(self):
        provider = _podman_with_inspect(env=["MYSQL_USER=root"], image="mysql:8")
        ref = WorkloadRef(name="db", platform="podman")
        params = remediation.parse_params(
            "set-env",
            {"container": "db", "env": {"MYSQL_ROOT_PASSWORD": "secret"}})
        plan = remediation.make_plan(provider, ref, "set-env", params)
        self.assertEqual(plan.risk, "medium")
        self.assertIn("recreates", plan.summary)
        # Reversal deletes the key we added (it didn't exist before -> null).
        rev = plan.reversal
        self.assertEqual(rev["action"], "set-env")
        self.assertIsNone(rev["params"]["env"]["MYSQL_ROOT_PASSWORD"])

        result = remediation.execute(provider, ref, plan)
        self.assertTrue(result.executed)
        # rm then run; the run carries BOTH env vars.
        run = [c for c in provider.calls if c[:1] == ["run"]][0]
        self.assertIn("MYSQL_USER=root", run)
        self.assertIn("MYSQL_ROOT_PASSWORD=secret", run)
        self.assertIn("--name", run)
        self.assertIn("mysql:8", run)

    def test_secret_value_masked_in_summary(self):
        provider = _podman_with_inspect()
        ref = WorkloadRef(name="db", platform="podman")
        params = remediation.parse_params(
            "set-env", {"container": "db", "env": {"DB_PASSWORD": "hunter2"}})
        plan = remediation.make_plan(provider, ref, "set-env", params)
        self.assertNotIn("hunter2", plan.summary)
        self.assertIn("***", plan.summary)


class SetEnvKubernetesTest(unittest.TestCase):
    def test_patches_deployment_env(self):
        deploy = _deploy_doc(env=[{"name": "EXISTING", "value": "1"}])
        provider = _k8s_with_controller(deploy=deploy)
        ref = WorkloadRef(name="web-xyz", namespace="prod", platform="kubernetes")
        params = remediation.parse_params(
            "set-env", {"container": "app", "env": {"FEATURE_FLAG": "on"}})
        plan = remediation.make_plan(provider, ref, "set-env", params)
        result = remediation.execute(provider, ref, plan)
        self.assertTrue(result.executed)
        patch = [c for c in provider.calls if c[:1] == ["patch"]][0]
        body = json.loads(patch[patch.index("-p") + 1])
        env = body["spec"]["template"]["spec"]["containers"][0]["env"]
        self.assertIn({"name": "FEATURE_FLAG", "value": "on"}, env)


# ---------------------------------------------------------------------------
# set-image
# ---------------------------------------------------------------------------

class SetImageTest(unittest.TestCase):
    def test_parse_validates_image(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("set-image", {"container": "db", "image": ""})
        out = remediation.parse_params(
            "set-image", {"container": "db", "image": "mysql:8.0"})
        self.assertEqual(out["image"], "mysql:8.0")

    def test_podman_recreates_on_new_image(self):
        provider = _podman_with_inspect(image="mysql:5.7", env=["A=1"])
        ref = WorkloadRef(name="db", platform="podman")
        params = remediation.parse_params(
            "set-image", {"container": "db", "image": "mysql:8.0"})
        plan = remediation.make_plan(provider, ref, "set-image", params)
        self.assertIn("mysql:5.7", plan.summary)
        self.assertIn("mysql:8.0", plan.summary)
        # Reversal restores the old image.
        self.assertEqual(plan.reversal["params"]["image"], "mysql:5.7")
        result = remediation.execute(provider, ref, plan)
        self.assertTrue(result.executed)
        run = [c for c in provider.calls if c[:1] == ["run"]][0]
        self.assertIn("mysql:8.0", run)
        self.assertIn("A=1", run)        # env preserved across the image change

    def test_k8s_uses_kubectl_set_image(self):
        provider = _k8s_with_controller(deploy=_deploy_doc(image="app:1"))
        ref = WorkloadRef(name="web-xyz", namespace="prod", platform="kubernetes")
        params = remediation.parse_params(
            "set-image", {"container": "app", "image": "app:2"})
        plan = remediation.make_plan(provider, ref, "set-image", params)
        result = remediation.execute(provider, ref, plan)
        self.assertTrue(result.executed)
        si = [c for c in provider.calls if c[:2] == ["set", "image"]][0]
        self.assertIn("app=app:2", si)


# ---------------------------------------------------------------------------
# recreate
# ---------------------------------------------------------------------------

class RecreateTest(unittest.TestCase):
    def test_parse_requires_at_least_one_change(self):
        with self.assertRaises(remediation.RemediationError):
            remediation.parse_params("recreate", {"container": "db"})

    def test_high_risk(self):
        provider = _podman_with_inspect()
        ref = WorkloadRef(name="db", platform="podman")
        params = remediation.parse_params(
            "recreate", {"container": "db", "image": "mysql:8",
                         "env": {"MYSQL_DATABASE": "app"}})
        plan = remediation.make_plan(provider, ref, "recreate", params)
        self.assertEqual(plan.risk, "high")

    def test_podman_recreate_merges_env_and_image(self):
        provider = _podman_with_inspect(image="old:1", env=["KEEP=1"])
        ref = WorkloadRef(name="db", platform="podman")
        params = remediation.parse_params(
            "recreate", {"container": "db", "image": "new:2",
                         "env": {"ADDED": "yes"}})
        plan = remediation.make_plan(provider, ref, "recreate", params)
        result = remediation.execute(provider, ref, plan)
        self.assertTrue(result.executed)
        run = [c for c in provider.calls if c[:1] == ["run"]][0]
        self.assertIn("new:2", run)
        self.assertIn("KEEP=1", run)
        self.assertIn("ADDED=yes", run)

    def test_k8s_recreate_single_patch(self):
        provider = _k8s_with_controller(deploy=_deploy_doc(image="app:1"))
        ref = WorkloadRef(name="web-xyz", namespace="prod", platform="kubernetes")
        params = remediation.parse_params(
            "recreate", {"container": "app", "image": "app:2",
                         "command": ["/bin/run"]})
        plan = remediation.make_plan(provider, ref, "recreate", params)
        result = remediation.execute(provider, ref, plan)
        self.assertTrue(result.executed)
        patch = [c for c in provider.calls if c[:1] == ["patch"]][0]
        body = json.loads(patch[patch.index("-p") + 1])
        cont = body["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(cont["image"], "app:2")
        self.assertEqual(cont["command"], ["/bin/run"])


# ---------------------------------------------------------------------------
# verify_recovery wiring for the new actions
# ---------------------------------------------------------------------------

class VerifyWiringTest(unittest.TestCase):
    def test_set_env_on_k8s_is_unknown(self):
        # k8s set-env replaces the pod -> ref is stale -> unknown.
        res = remediation.verify_recovery(
            None, WorkloadRef(name="web", namespace="prod", platform="kubernetes"),
            baseline={}, action="set-env", wait_seconds=5, sleep=lambda n: None)
        self.assertEqual(res["outcome"], "unknown")

    def test_set_env_on_podman_reads_same_ref(self):
        # podman recreate keeps the name; verification re-reads it.
        from poddebugger.models import Workload
        class _P:
            name = "podman"
            _inspect_cache = {}
            def get_workload(self, ref):
                return Workload(ref=ref, kind="container", status="running",
                                running=True, image="x", restart_count=0)
        res = remediation.verify_recovery(
            _P(), WorkloadRef(name="db", platform="podman"),
            baseline={"running": False, "restart_count": 0},
            action="set-env", wait_seconds=2, sleep=lambda n: None)
        self.assertEqual(res["outcome"], "recovered")


if __name__ == "__main__":
    unittest.main()
