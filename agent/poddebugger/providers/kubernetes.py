"""Kubernetes / OpenShift platform provider (Phase 2).

Shells out to ``kubectl`` (or ``oc`` for OpenShift) with JSON output — the same
zero-dependency approach as the Podman provider. The agent core is unchanged;
only this implementation differs.

Read-only: status, events, logs (current + previous), and a redacted spec.
Ephemeral debug-container injection and remediation land in later phases.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

from ..models import Event, Workload, WorkloadRef
from .base import ContainerPlatform, ProviderError

_SECRET_KEY = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|CREDENTIAL|PRIVATE)", re.I)
# klog-style noise lines kubectl/oc emit on stderr, e.g. "E0521 21:27:55.93 ..."
_KLOG_NOISE = re.compile(r"^[EWIF]\d{4}\s")


def _clean_stderr(stderr: str) -> str:
    """Drop klog noise so only the actionable error line remains."""
    lines = [ln for ln in stderr.splitlines() if ln.strip() and not _KLOG_NOISE.match(ln)]
    return " ".join(lines).strip() or stderr.strip()


def _redact_env(env: list) -> list:
    """Redact sensitive values from a container's env list.

    Kubernetes env entries are dicts: ``{name, value}`` or ``{name, valueFrom}``.
    ``valueFrom`` references (secretKeyRef/configMapKeyRef) never carry the
    literal value, so they are summarized rather than dropped.
    """
    out = []
    for item in env or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        if "valueFrom" in item:
            src = next(iter(item["valueFrom"]), "reference")
            out.append(f"{name}=<from {src}>")
        elif _SECRET_KEY.search(name):
            out.append(f"{name}=<redacted>")
        else:
            out.append(f"{name}={item.get('value', '')}")
    return out


def _pick_container_state(cs: dict) -> tuple[str, dict]:
    """Return (state_key, state_body) for a containerStatus' ``state``."""
    state = cs.get("state", {}) or {}
    for key in ("running", "waiting", "terminated"):
        if key in state:
            return key, state[key] or {}
    return "unknown", {}


class KubernetesProvider(ContainerPlatform):
    name = "kubernetes"

    def __init__(self, binary: str | None = None, prefer_oc: bool = False):
        self._bin = binary or self._detect_binary(prefer_oc)
        self._pod_cache: dict[tuple[str, str], dict] = {}
        self._ns_cache: str | None = None

    # --- internals -----------------------------------------------------------

    @staticmethod
    def _detect_binary(prefer_oc: bool) -> str:
        order = ["oc", "kubectl"] if prefer_oc else ["kubectl", "oc"]
        for candidate in order:
            if shutil.which(candidate):
                return candidate
        return order[0]  # let preflight raise a clear error

    def _run(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                [self._bin, *args],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"'{self._bin}' not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"{self._bin} {' '.join(args)} timed out") from exc
        if check and proc.returncode != 0:
            raise ProviderError(
                f"{self._bin} {' '.join(args)} failed: "
                f"{_clean_stderr(proc.stderr) or proc.stdout.strip()}"
            )
        return proc

    def _namespace(self, namespace: str | None) -> str:
        if namespace:
            return namespace
        if self._ns_cache is None:
            proc = self._run(
                ["config", "view", "--minify", "--output", "jsonpath={..namespace}"],
                check=False,
            )
            self._ns_cache = (proc.stdout or "").strip() or "default"
        return self._ns_cache

    def _get_pod(self, ref: WorkloadRef) -> dict:
        ns = ref.namespace or self._namespace(None)
        key = (ns, ref.name)
        if key not in self._pod_cache:
            proc = self._run(["get", "pod", ref.name, "-n", ns, "-o", "json"])
            self._pod_cache[key] = json.loads(proc.stdout or "{}")
        return self._pod_cache[key]

    def _container_statuses(self, pod: dict) -> dict[str, dict]:
        return {
            cs.get("name", ""): cs
            for cs in (pod.get("status", {}).get("containerStatuses") or [])
        }

    def _target_container(self, ref: WorkloadRef, pod: dict) -> str:
        """Pick which container of the pod to analyze.

        Honors an explicit ``ref.container``; otherwise picks the unhealthiest
        one (not ready / most restarts), recording a note when it had to choose.
        """
        spec_containers = [c.get("name", "") for c in pod.get("spec", {}).get("containers", [])]
        if not spec_containers:
            raise ProviderError(f"pod {ref} has no containers")

        if ref.container:
            if ref.container not in spec_containers:
                raise ProviderError(
                    f"container {ref.container!r} not in pod {ref} "
                    f"(have: {', '.join(spec_containers)})"
                )
            return ref.container

        statuses = self._container_statuses(pod)

        def badness(name: str) -> tuple:
            cs = statuses.get(name, {})
            return (0 if cs.get("ready", True) else 1, int(cs.get("restartCount", 0) or 0))

        chosen = max(spec_containers, key=badness)
        if len(spec_containers) > 1:
            others = ", ".join(c for c in spec_containers if c != chosen)
            self._pod_note = (
                f"pod {ref} has multiple containers; analyzing {chosen!r}. "
                f"Re-run with --container to inspect: {others}"
            )
        return chosen

    # --- interface -----------------------------------------------------------

    def preflight(self) -> None:
        if not shutil.which(self._bin):
            raise ProviderError(
                f"'{self._bin}' is not installed or not on PATH "
                "(need kubectl or oc for the kubernetes platform)"
            )
        self._run(["version", "--client", "-o", "json"])

    def resolve(self, target: str, namespace: str | None = None) -> WorkloadRef:
        ns = self._namespace(namespace)
        proc = self._run(["get", "pod", target, "-n", ns, "-o", "name"], check=False)
        if proc.returncode != 0:
            raise ProviderError(
                f"no pod {target!r} in namespace {ns!r}: "
                f"{_clean_stderr(proc.stderr) or 'not found'}"
            )
        return WorkloadRef(name=target, namespace=ns, platform="kubernetes")

    def get_workload(self, ref: WorkloadRef) -> Workload:
        pod = self._get_pod(ref)
        meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
        container = self._target_container(ref, pod)
        ref.container = ref.container or container

        spec_container = next(
            (c for c in spec.get("containers", []) if c.get("name") == container), {}
        )
        cs = self._container_statuses(pod).get(container, {})

        phase = status.get("phase", "Unknown")
        state_key, state_body = _pick_container_state(cs)
        last_key, last_body = _pick_container_state({"state": cs.get("lastState", {})})

        reason = state_body.get("reason", "")
        container_desc = state_key + (f": {reason}" if reason else "")

        # Exit code / OOM may live on the current OR the previous (last) state.
        exit_code = state_body.get("exitCode")
        if exit_code is None:
            exit_code = last_body.get("exitCode")
        oom = "OOMKilled" in (state_body.get("reason", ""), last_body.get("reason", ""))

        error = state_body.get("message", "") or last_body.get("message", "")

        return Workload(
            ref=ref,
            kind="pod",
            status=f"{phase} / {container_desc}",
            running=(state_key == "running"),
            image=spec_container.get("image", "") or cs.get("image", ""),
            restart_count=int(cs.get("restartCount", 0) or 0),
            exit_code=exit_code,
            oom_killed=oom,
            health_status="ready" if cs.get("ready") else "not-ready",
            started_at=(
                state_body.get("startedAt")
                or last_body.get("startedAt")
                or status.get("startTime", "")
                or ""
            ),
            finished_at=state_body.get("finishedAt") or last_body.get("finishedAt") or "",
            error=error,
        )

    def get_events(self, ref: WorkloadRef) -> list[Event]:
        ns = ref.namespace or self._namespace(None)
        proc = self._run(
            [
                "get", "events", "-n", ns,
                "--field-selector", f"involvedObject.name={ref.name}",
                "-o", "json",
            ],
            check=False,
        )
        if proc.returncode != 0:
            return []
        items = json.loads(proc.stdout or "{}").get("items", [])
        events: list[Event] = []
        for e in items:
            ts = e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp") or ""
            count = e.get("count")
            message = e.get("message", "")
            if count and count > 1:
                message = f"{message} (x{count})"
            events.append(
                Event(
                    timestamp=str(ts),
                    type=e.get("type", "Normal"),
                    reason=e.get("reason", ""),
                    message=message,
                )
            )
        events.sort(key=lambda ev: ev.timestamp)
        return events

    def get_logs(self, ref: WorkloadRef, tail: int = 200) -> str:
        ns = ref.namespace or self._namespace(None)
        pod = self._get_pod(ref)
        container = ref.container or self._target_container(ref, pod)
        base = ["logs", ref.name, "-n", ns, "-c", container, "--tail", str(tail)]

        sections: list[str] = []
        # Previous container logs are the most useful for CrashLoopBackOff.
        prev = self._run([*base, "--previous"], check=False)
        if prev.returncode == 0 and prev.stdout.strip():
            sections.append("=== previous container logs ===\n" + prev.stdout.strip())

        current = self._run(base, check=False)
        if current.returncode == 0 and current.stdout.strip():
            sections.append("=== current container logs ===\n" + current.stdout.strip())
        elif not sections:
            err = current.stderr.strip()
            if err:
                raise ProviderError(f"could not read logs: {err}")

        return "\n\n".join(sections)

    def get_spec(self, ref: WorkloadRef) -> dict:
        pod = self._get_pod(ref)
        spec = pod.get("spec", {})
        container = ref.container or self._target_container(ref, pod)
        c = next((x for x in spec.get("containers", []) if x.get("name") == container), {})
        return {
            "container": container,
            "image": c.get("image", ""),
            "command": c.get("command"),
            "args": c.get("args"),
            "env": _redact_env(c.get("env", [])),
            "resources": c.get("resources", {}),
            "liveness_probe": c.get("livenessProbe"),
            "readiness_probe": c.get("readinessProbe"),
            "startup_probe": c.get("startupProbe"),
            "pod": {
                "restart_policy": spec.get("restartPolicy"),
                "node_name": spec.get("nodeName"),
                "service_account": spec.get("serviceAccountName"),
            },
        }

    # --- deep inspection (Phase 3) ------------------------------------------

    def exec(self, ref: WorkloadRef, command: list[str]) -> str:
        """Run a command inside the (running) target container via ``exec``.

        Used for read-only deep-inspection probes. A non-zero exit is not
        raised — the probe records whatever the command produced.
        """
        ns = ref.namespace or self._namespace(None)
        args = ["exec", ref.name, "-n", ns]
        if ref.container:
            args += ["-c", ref.container]
        args += ["--", *command]
        proc = self._run(args, check=False)
        return ((proc.stdout or "") + (proc.stderr or "")).strip()

    # --- remediation (Phase 5 → 7A) -----------------------------------------

    def get_controller(self, ref: WorkloadRef) -> dict | None:
        """Resolve a pod's top-level workload controller (Deployment/STS/DS/RS).

        Walks ``metadata.ownerReferences``: Pod → ReplicaSet → Deployment. The
        chain is one hop for STS/DS, two for Deployment.
        """
        ns = ref.namespace or self._namespace(None)
        pod = self._get_pod(ref)
        owners = (pod.get("metadata", {}) or {}).get("ownerReferences", []) or []
        owner = next((o for o in owners if o.get("controller")), None) or (
            owners[0] if owners else None
        )
        if not owner:
            return None
        kind = owner.get("kind", "")
        name = owner.get("name", "")
        if kind == "ReplicaSet":
            rs_proc = self._run(
                ["get", "replicaset", name, "-n", ns, "-o", "json"], check=False
            )
            if rs_proc.returncode == 0:
                rs = json.loads(rs_proc.stdout or "{}")
                rs_owners = (rs.get("metadata", {}) or {}).get("ownerReferences", []) or []
                rs_owner = next(
                    (o for o in rs_owners if o.get("controller")), None
                ) or (rs_owners[0] if rs_owners else None)
                if rs_owner:
                    return {
                        "kind": rs_owner.get("kind", ""),
                        "name": rs_owner.get("name", ""),
                        "namespace": ns,
                    }
            # Fall through: act on the RS directly.
        return {"kind": kind, "name": name, "namespace": ns}

    def remediate(self, ref: WorkloadRef, action: dict) -> dict:
        """Execute a whitelisted remediation action.

        Thin shim over the Phase 7A catalog so callers that still pass the
        legacy ``{"type": "restart"}`` shape keep working. The CLI / operator
        should prefer ``remediation.make_plan`` + ``remediation.execute``.
        """
        from .. import remediation

        name = action.get("type") or action.get("action") or ""
        params = {k: v for k, v in action.items() if k not in ("type", "action")}
        try:
            cleaned = remediation.parse_params(name, params)
            plan = remediation.make_plan(self, ref, name, cleaned)
        except remediation.RemediationError as exc:
            raise ProviderError(str(exc)) from exc
        result = remediation.execute(self, ref, plan)
        return {
            "action": result.action,
            "executed": result.executed,
            "result": result.result,
            "plan": result.plan,
            "reversal": result.reversal,
        }
