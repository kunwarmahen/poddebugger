"""Podman platform provider — shells out to the ``podman`` CLI (JSON output).

See HLD.md section 6 for how Podman concepts map onto the K8s-shaped model.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

from ..models import Event, Workload, WorkloadRef
from .base import ContainerPlatform, ProviderError

_SECRET_KEY = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSWD|APIKEY|API_KEY|CREDENTIAL|PRIVATE)", re.I)


def _redact_env(env: list[str]) -> list[str]:
    """Mask values of env vars whose name looks sensitive."""
    out = []
    for item in env or []:
        key, sep, _ = item.partition("=")
        if sep and _SECRET_KEY.search(key):
            out.append(f"{key}=<redacted>")
        else:
            out.append(item)
    return out


class PodmanProvider(ContainerPlatform):
    name = "podman"

    def __init__(self, binary: str = "podman"):
        self._bin = binary
        self._inspect_cache: dict[str, dict] = {}

    # --- internals -----------------------------------------------------------

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
            raise ProviderError(f"podman {' '.join(args)} timed out") from exc
        if check and proc.returncode != 0:
            raise ProviderError(
                f"podman {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    def _exists(self, kind: str, name: str) -> bool:
        # `podman {container,pod} exists` exits 0 if present, 1 otherwise.
        return self._run([kind, "exists", name], check=False).returncode == 0

    def _inspect(self, name: str) -> dict:
        if name not in self._inspect_cache:
            proc = self._run(["inspect", "--type", "container", "--format", "json", name])
            data = json.loads(proc.stdout or "[]")
            if not data:
                raise ProviderError(f"podman inspect returned nothing for {name!r}")
            self._inspect_cache[name] = data[0]
        return self._inspect_cache[name]

    def _container_badness(self, name: str) -> tuple:
        """Score a container — higher is unhealthier (stopped, more restarts).

        Used to pick which container of a multi-container pod to analyze.
        """
        try:
            d = self._inspect(name)
        except (ProviderError, json.JSONDecodeError):
            return (0, 0)
        running = bool((d.get("State", {}) or {}).get("Running", False))
        return (0 if running else 1, int(d.get("RestartCount", 0) or 0))

    # --- interface -----------------------------------------------------------

    def preflight(self) -> None:
        if not shutil.which(self._bin):
            raise ProviderError(f"'{self._bin}' is not installed or not on PATH")
        self._run(["version", "--format", "{{.Client.Version}}"])

    def resolve(self, target: str, namespace: str | None = None) -> WorkloadRef:
        if self._exists("container", target):
            return WorkloadRef(name=target, platform="podman")

        if self._exists("pod", target):
            proc = self._run(["pod", "inspect", "--format", "json", target])
            pod = json.loads(proc.stdout or "{}")
            if isinstance(pod, list):  # some podman versions wrap in a list
                pod = pod[0] if pod else {}
            infra_id = pod.get("InfraContainerID") or pod.get("infraContainerID") or ""
            containers = pod.get("Containers") or pod.get("containers") or []
            app = [
                c for c in containers
                if (c.get("Id") or c.get("ID") or "") != infra_id
                and not str(c.get("Name") or c.get("name") or "").endswith("-infra")
            ]
            if not app:
                raise ProviderError(f"pod {target!r} has no application containers")
            names = [c.get("Name") or c.get("name") for c in app]
            # Analyze the unhealthiest container (stopped, or most restarts).
            chosen = max(names, key=self._container_badness)
            ref = WorkloadRef(name=chosen, container=chosen, platform="podman")
            if len(names) > 1:
                others = ", ".join(n for n in names if n != chosen)
                self._pod_note = (
                    f"pod {target!r} has multiple containers; analyzing {chosen!r}. "
                    f"Re-run with another name to inspect: {others}"
                )
            return ref

        raise ProviderError(f"no podman container or pod named {target!r}")

    def get_workload(self, ref: WorkloadRef) -> Workload:
        d = self._inspect(ref.name)
        state = d.get("State", {})
        health = (state.get("Health") or {}).get("Status", "") or ""
        exited = not state.get("Running", False)
        return Workload(
            ref=ref,
            kind="container",
            status=state.get("Status", "unknown"),
            running=bool(state.get("Running", False)),
            image=d.get("ImageName") or d.get("Image", ""),
            restart_count=int(d.get("RestartCount", 0) or 0),
            exit_code=state.get("ExitCode") if exited else None,
            oom_killed=bool(state.get("OOMKilled", False)),
            health_status=health,
            started_at=state.get("StartedAt", "") or "",
            finished_at=state.get("FinishedAt", "") or "",
            error=state.get("Error", "") or "",
        )

    def get_events(self, ref: WorkloadRef) -> list[Event]:
        proc = self._run(
            [
                "events", "--filter", f"container={ref.name}",
                "--since", "24h", "--stream=false", "--format", "json",
            ],
            check=False,
        )
        events: list[Event] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            status = e.get("Status") or e.get("status") or ""
            attrs = e.get("Attributes") or e.get("attributes") or {}
            details: dict = {}
            # Podman 4.x puts the exit code at the top level; older/other
            # versions tuck extras into Attributes.
            exit_code = e.get("ContainerExitCode", e.get("containerExitCode"))
            if exit_code is not None:
                details["exit_code"] = exit_code
            if isinstance(attrs, dict):
                for key in ("reason", "image"):
                    if attrs.get(key):
                        details[key] = attrs[key]
            msg = " ".join(f"{k}={v}" for k, v in details.items())
            events.append(
                Event(
                    timestamp=str(e.get("Time") or e.get("time") or ""),
                    type=e.get("Type") or e.get("type") or "container",
                    reason=status,
                    message=msg.strip(),
                )
            )
        return events

    def get_logs(self, ref: WorkloadRef, tail: int = 200) -> str:
        # podman streams container stdout/stderr onto our stdout/stderr.
        proc = self._run(
            ["logs", "--tail", str(tail), "--timestamps", ref.name], check=False
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        return combined.strip()

    def get_spec(self, ref: WorkloadRef) -> dict:
        d = self._inspect(ref.name)
        config = d.get("Config", {}) or {}
        host = d.get("HostConfig", {}) or {}
        return {
            "image": d.get("ImageName") or d.get("Image", ""),
            "entrypoint": config.get("Entrypoint"),
            "command": config.get("Cmd"),
            "env": _redact_env(config.get("Env", [])),
            "healthcheck": config.get("Healthcheck"),
            "labels": config.get("Labels"),
            "resources": {
                "memory_limit": host.get("Memory"),
                "memory_swap": host.get("MemorySwap"),
                "nano_cpus": host.get("NanoCpus"),
                "restart_policy": host.get("RestartPolicy"),
            },
        }

    def get_stats(self, ref: WorkloadRef) -> dict:
        """Return a one-shot CPU/memory snapshot for a running container."""
        proc = self._run(
            ["stats", "--no-stream", "--format", "json", ref.name], check=False
        )
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return {}
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return {}

        def pick(*keys):
            for k in keys:
                if data.get(k) not in (None, ""):
                    return data[k]
            return None

        snapshot = {
            "cpu_percent": pick("cpu_percent", "CPUPerc", "CPU"),
            "mem_usage": pick("mem_usage", "MemUsage"),
            "mem_percent": pick("mem_percent", "MemPerc"),
            "pids": pick("pids", "PIDs", "PIDS"),
        }
        return {k: v for k, v in snapshot.items() if v is not None}

    # --- deep inspection (Phase 3) ------------------------------------------

    def exec(self, ref: WorkloadRef, command: list[str]) -> str:
        """Run a command inside the (running) target container.

        Used for read-only deep-inspection probes. Returns combined stdout +
        stderr; a non-zero exit is not raised — the probe records whatever the
        command produced.
        """
        proc = self._run(["exec", ref.name, *command], check=False)
        return ((proc.stdout or "") + (proc.stderr or "")).strip()

    # --- remediation (Phase 5 → 7A) -----------------------------------------

    def remediate(self, ref: WorkloadRef, action: dict) -> dict:
        """Execute a whitelisted remediation action via the Phase 7A catalog."""
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
