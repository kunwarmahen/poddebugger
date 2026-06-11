"""Remediation action catalog (Phase 7A — HLD §12).

A fixed, audited catalog of mutation actions. The LLM never emits a command —
it picks a catalog action and proposes parameters which this module validates,
bounds-checks, and dry-runs (old → new) before anything executes. Each action
also captures a reversal — the snapshot ``remediate --undo`` re-applies
(Phase 7D), and Phase 7D adds post-remediation verification so PodDebugger
reports not just what it did but whether it worked.

Public surface used by the CLI / operator:

    list_actions(platform)    -> list[str]
    get_spec(name)            -> ActionSpec
    parse_params(name, items) -> dict   # turns ['k=v', ...] into a typed dict
    make_plan(provider, ref, name, params) -> Plan
    execute(provider, ref, plan)           -> ActionResult
    capture_baseline(provider, ref)        -> dict   # Phase 7D
    verify_recovery(provider, ref, baseline, action, wait_seconds) -> dict
    save_for_undo(ref, payload)            -> Path
    load_for_undo(ref, path=None)          -> dict
    undo_from(payload)                     -> tuple[WorkloadRef, str, dict]
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .models import WorkloadRef
from .providers.base import ContainerPlatform, ProviderError


# --- exceptions --------------------------------------------------------------

class RemediationError(Exception):
    """Raised on validation, planning, or execution failures."""


# --- guardrails --------------------------------------------------------------

PROTECTED_NAMESPACES: frozenset[str] = frozenset({
    "kube-system", "kube-public", "kube-node-lease",
    "openshift", "openshift-apiserver", "openshift-authentication",
    "openshift-config", "openshift-etcd", "openshift-kube-apiserver",
    "openshift-machine-api", "openshift-monitoring", "openshift-operators",
})
_OPENSHIFT_PREFIX = "openshift-"

MAX_REPLICAS = 100
MAX_MEMORY_BYTES = 64 * 1024 ** 3       # 64 GiB
MAX_CPU_MILLICORES = 32_000             # 32 cores
_PROBE_FIELD_MAX = {
    "initial_delay": 600,    # seconds
    "period": 600,
    "timeout": 600,
    "failure_threshold": 30,
}


def _check_namespace(ref: WorkloadRef) -> None:
    ns = ref.namespace or ""
    if ns in PROTECTED_NAMESPACES or ns.startswith(_OPENSHIFT_PREFIX):
        raise RemediationError(
            f"namespace {ns!r} is protected; remediation refused"
        )


# --- value parsing -----------------------------------------------------------

_MEMORY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]i?|[kmgtp]i?)?\s*$")
_CPU_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m)?\s*$")
_PROBE_NAMES = ("liveness", "readiness", "startup")


def parse_memory(value: str | int) -> int:
    """Parse a Kubernetes/Podman memory string into bytes.

    Accepts plain integers (bytes), ``Ki/Mi/Gi/Ti/Pi`` (binary), or
    ``K/M/G/T/P`` (decimal). Raises on anything else.
    """
    if isinstance(value, int):
        if value < 0:
            raise RemediationError("memory must be non-negative")
        return value
    if not isinstance(value, str) or not value.strip():
        raise RemediationError("memory must be a non-empty string")
    m = _MEMORY_RE.match(value)
    if not m:
        raise RemediationError(f"could not parse memory {value!r}")
    num = float(m.group(1))
    suffix = (m.group(2) or "").strip()
    units = {
        "": 1,
        "K": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15,
        "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50,
    }
    key = suffix[0].upper() + suffix[1:] if suffix else ""
    if key not in units:
        raise RemediationError(f"unknown memory suffix {suffix!r}")
    out = int(num * units[key])
    if out > MAX_MEMORY_BYTES:
        raise RemediationError(
            f"memory {value!r} exceeds ceiling ({MAX_MEMORY_BYTES} bytes)"
        )
    return out


def parse_cpu(value: str | int | float) -> int:
    """Parse a CPU quantity into millicores (e.g. '500m' or '0.5' → 500)."""
    if isinstance(value, (int, float)):
        millicores = int(round(value * 1000))
    else:
        if not isinstance(value, str) or not value.strip():
            raise RemediationError("cpu must be a non-empty string")
        m = _CPU_RE.match(value)
        if not m:
            raise RemediationError(f"could not parse cpu {value!r}")
        num = float(m.group(1))
        millicores = int(round(num)) if m.group(2) == "m" else int(round(num * 1000))
    if millicores < 0:
        raise RemediationError("cpu must be non-negative")
    if millicores > MAX_CPU_MILLICORES:
        raise RemediationError(
            f"cpu exceeds ceiling ({MAX_CPU_MILLICORES} millicores)"
        )
    return millicores


def _coerce_int(value, name: str) -> int:
    if isinstance(value, bool):
        raise RemediationError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise RemediationError(f"{name} must be an integer (got {value!r})")


# --- data --------------------------------------------------------------------

@dataclass
class Plan:
    action: str
    risk: str                       # low | medium
    params: dict                    # validated/coerced params
    target: str                     # human label (container or controller)
    summary: str                    # one-line "scale deploy 'x' 3 → 5"
    old: dict = field(default_factory=dict)
    new: dict = field(default_factory=dict)
    # The {action, params} a future `remediate --undo` would replay.
    reversal: dict = field(default_factory=dict)


@dataclass
class ActionResult:
    action: str
    executed: bool
    result: str
    plan: dict = field(default_factory=dict)
    reversal: dict | None = None


@dataclass
class ActionSpec:
    name: str
    risk: str                       # low | medium
    platforms: tuple[str, ...]
    description: str
    parse: Callable[[dict], dict]
    plan: Callable[[ContainerPlatform, WorkloadRef, dict], Plan]
    execute: Callable[[ContainerPlatform, WorkloadRef, Plan], ActionResult]


# --- restart -----------------------------------------------------------------

def _parse_restart(raw: dict) -> dict:
    if raw:
        raise RemediationError("'restart' takes no parameters")
    return {}


def _plan_restart(provider: ContainerPlatform, ref: WorkloadRef, params: dict) -> Plan:
    if provider.name == "kubernetes":
        target = f"pod {ref.namespace or 'default'}/{ref.name}"
        summary = (
            f"delete {target} — controller (if any) recreates it"
        )
    else:
        target = f"container {ref.name}"
        summary = f"restart {target}"
    return Plan(
        action="restart",
        risk="low",
        params={},
        target=target,
        summary=summary,
        old={"state": "running-or-failed"},
        new={"state": "restarted"},
        reversal={},  # restart is its own reversal
    )


def _execute_restart(provider, ref: WorkloadRef, plan: Plan) -> ActionResult:
    if provider.name == "kubernetes":
        ns = ref.namespace or _k8s_namespace(provider, ref)
        proc = provider._run(["delete", "pod", ref.name, "-n", ns], check=False)
        if proc.returncode != 0:
            return ActionResult(
                "restart", False,
                _clean(proc.stderr) or "delete pod failed",
                plan=asdict(plan),
            )
        return ActionResult(
            "restart", True,
            f"pod {ns}/{ref.name} deleted — its controller will recreate it",
            plan=asdict(plan),
        )
    proc = provider._run(["restart", ref.name], check=False)
    if proc.returncode != 0:
        return ActionResult(
            "restart", False,
            (proc.stderr or proc.stdout).strip() or "restart failed",
            plan=asdict(plan),
        )
    return ActionResult(
        "restart", True,
        f"container {ref.name} restarted",
        plan=asdict(plan),
    )


# --- scale (Kubernetes only) --------------------------------------------------

def _parse_scale(raw: dict) -> dict:
    if "replicas" not in raw:
        raise RemediationError("'scale' requires 'replicas'")
    replicas = _coerce_int(raw["replicas"], "replicas")
    if replicas < 0:
        raise RemediationError("'replicas' must be >= 0")
    if replicas > MAX_REPLICAS:
        raise RemediationError(f"'replicas' exceeds ceiling ({MAX_REPLICAS})")
    extra = set(raw) - {"replicas"}
    if extra:
        raise RemediationError(f"'scale' got unexpected params: {sorted(extra)}")
    return {"replicas": replicas}


def _plan_scale(provider, ref: WorkloadRef, params: dict) -> Plan:
    _check_namespace(ref)
    controller = _resolve_controller(provider, ref)
    current = _kubectl_json(
        provider,
        ["get", controller["kind"].lower(), controller["name"],
         "-n", controller["namespace"], "-o", "json"],
    )
    old = int(current.get("spec", {}).get("replicas", 1) or 0)
    new = params["replicas"]
    target = f"{controller['kind'].lower()}/{controller['name']}"
    return Plan(
        action="scale",
        risk="low",
        params=params,
        target=f"{target} (ns={controller['namespace']})",
        summary=f"scale {target} from {old} to {new}",
        old={"replicas": old},
        new={"replicas": new},
        reversal={"action": "scale", "params": {"replicas": old}},
    )


def _execute_scale(provider, ref: WorkloadRef, plan: Plan) -> ActionResult:
    controller = _resolve_controller(provider, ref)
    args = [
        "scale", controller["kind"].lower() + "/" + controller["name"],
        "-n", controller["namespace"],
        f"--replicas={plan.params['replicas']}",
    ]
    proc = provider._run(args, check=False)
    if proc.returncode != 0:
        return ActionResult(
            "scale", False,
            _clean(proc.stderr) or "scale failed",
            plan=asdict(plan), reversal=plan.reversal,
        )
    return ActionResult(
        "scale", True,
        f"{controller['kind'].lower()}/{controller['name']} scaled to "
        f"{plan.params['replicas']}",
        plan=asdict(plan), reversal=plan.reversal,
    )


# --- set-resources -----------------------------------------------------------

_RESOURCE_KEYS = ("memory_limit", "cpu_limit", "memory_request", "cpu_request")


def _parse_set_resources(raw: dict) -> dict:
    if "container" not in raw or not str(raw["container"]).strip():
        raise RemediationError("'set-resources' requires 'container'")
    out: dict = {"container": str(raw["container"]).strip()}
    given = [k for k in _RESOURCE_KEYS if k in raw and raw[k] not in (None, "")]
    if not given:
        raise RemediationError(
            "'set-resources' needs at least one of "
            f"{', '.join(_RESOURCE_KEYS)}"
        )
    for k in given:
        if k.startswith("memory"):
            out[k] = parse_memory(raw[k])
        else:
            out[k] = parse_cpu(raw[k])
    extra = set(raw) - {"container", *_RESOURCE_KEYS}
    if extra:
        raise RemediationError(
            f"'set-resources' got unexpected params: {sorted(extra)}"
        )
    return out


def _format_bytes(n: int) -> str:
    """Render bytes as the smallest binary-suffixed value (e.g. 268435456→256Mi)."""
    if n == 0:
        return "0"
    for suffix, unit in (("Ti", 2 ** 40), ("Gi", 2 ** 30),
                        ("Mi", 2 ** 20), ("Ki", 2 ** 10)):
        if n % unit == 0:
            return f"{n // unit}{suffix}"
    return str(n)


def _format_cpu(millicores: int) -> str:
    if millicores % 1000 == 0:
        return str(millicores // 1000)
    return f"{millicores}m"


def _plan_set_resources(provider, ref: WorkloadRef, params: dict) -> Plan:
    if provider.name == "podman":
        # Podman: read from the inspect cache, the only field that survives
        # `podman update` is the memory limit (cpus → NanoCpus).
        spec = provider.get_spec(ref)
        rs = spec.get("resources", {}) or {}
        old = {
            "memory_limit": int(rs.get("memory_limit") or 0),
            "cpu_limit": (int(rs.get("nano_cpus") or 0) // 1_000_000),  # ns→m
        }
        # Podman supports the limit fields only — reject *request* params.
        unsupported = [k for k in params if k.endswith("_request")]
        if unsupported:
            raise RemediationError(
                "podman 'set-resources' does not support resource *requests*; "
                "drop " + ", ".join(unsupported)
            )
        new = dict(old)
        if "memory_limit" in params:
            new["memory_limit"] = params["memory_limit"]
        if "cpu_limit" in params:
            new["cpu_limit"] = params["cpu_limit"]
        target = f"container {ref.name}"
        summary = (
            f"set resources on {target}: "
            + ", ".join(
                f"{k}={_format_bytes(new[k]) if k.startswith('memory') else _format_cpu(new[k])}"
                for k in new if new[k] != old.get(k)
            )
            or f"set resources on {target} (no-op)"
        )
        # Symmetric reversal — for every param the caller is changing, put its
        # pre-action value back. Zero values are kept (they mean "no limit"),
        # which the parser accepts. Without this, undoing a set-from-zero
        # action would produce an unparseable params={"container": ...}.
        reversal_params: dict = {"container": params["container"]}
        for k in (k for k in params if k != "container"):
            fmt = _format_bytes if k.startswith("memory") else _format_cpu
            reversal_params[k] = fmt(old.get(k, 0))
        return Plan(
            action="set-resources",
            risk="medium",
            params=params,
            target=target,
            summary=summary,
            old=old,
            new=new,
            reversal={"action": "set-resources", "params": reversal_params},
        )

    # Kubernetes: patch the controller (Deployment).
    _check_namespace(ref)
    controller = _resolve_controller(provider, ref)
    current = _kubectl_json(
        provider,
        ["get", controller["kind"].lower(), controller["name"],
         "-n", controller["namespace"], "-o", "json"],
    )
    container_name = params["container"]
    containers = current.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    target_container = next(
        (c for c in containers if c.get("name") == container_name), None
    )
    if target_container is None:
        raise RemediationError(
            f"container {container_name!r} not in {controller['kind'].lower()}/"
            f"{controller['name']} (have: {[c.get('name') for c in containers]})"
        )
    old_resources = target_container.get("resources", {}) or {}
    new_resources = _merge_resources(old_resources, params)
    target = f"{controller['kind'].lower()}/{controller['name']}/{container_name}"
    return Plan(
        action="set-resources",
        risk="medium",
        params=params,
        target=f"{target} (ns={controller['namespace']})",
        summary=(
            f"set resources on {target}: "
            + _diff_resources(old_resources, new_resources)
        ),
        old=old_resources,
        new=new_resources,
        reversal={"action": "set-resources",
                  "params": _k8s_resources_to_params(container_name, old_resources)},
    )


def _merge_resources(old: dict, params: dict) -> dict:
    limits = dict(old.get("limits", {}) or {})
    requests = dict(old.get("requests", {}) or {})
    if "memory_limit" in params:
        limits["memory"] = _format_bytes(params["memory_limit"])
    if "cpu_limit" in params:
        limits["cpu"] = _format_cpu(params["cpu_limit"])
    if "memory_request" in params:
        requests["memory"] = _format_bytes(params["memory_request"])
    if "cpu_request" in params:
        requests["cpu"] = _format_cpu(params["cpu_request"])
    out = {}
    if limits:
        out["limits"] = limits
    if requests:
        out["requests"] = requests
    return out


def _diff_resources(old: dict, new: dict) -> str:
    pieces = []
    for section in ("limits", "requests"):
        for key, val in (new.get(section, {}) or {}).items():
            prev = (old.get(section, {}) or {}).get(key)
            if prev != val:
                pieces.append(f"{section}.{key} {prev or '∅'} → {val}")
    return ", ".join(pieces) or "no-op"


def _k8s_resources_to_params(container: str, resources: dict) -> dict:
    """Inverse of _merge_resources — surface a K8s resources block as params."""
    limits = resources.get("limits", {}) or {}
    requests = resources.get("requests", {}) or {}
    out: dict = {"container": container}
    if "memory" in limits:
        out["memory_limit"] = limits["memory"]
    if "cpu" in limits:
        out["cpu_limit"] = limits["cpu"]
    if "memory" in requests:
        out["memory_request"] = requests["memory"]
    if "cpu" in requests:
        out["cpu_request"] = requests["cpu"]
    return out


def _execute_set_resources(provider, ref: WorkloadRef, plan: Plan) -> ActionResult:
    if provider.name == "podman":
        args = ["update"]
        if "memory_limit" in plan.params:
            args += ["--memory", str(plan.params["memory_limit"])]
        if "cpu_limit" in plan.params:
            cpus = plan.params["cpu_limit"] / 1000.0
            args += ["--cpus", f"{cpus:g}"]
        args += [ref.name]
        proc = provider._run(args, check=False)
        if proc.returncode != 0:
            return ActionResult(
                "set-resources", False,
                (proc.stderr or proc.stdout).strip() or "podman update failed",
                plan=asdict(plan), reversal=plan.reversal,
            )
        return ActionResult(
            "set-resources", True,
            f"container {ref.name} resources updated",
            plan=asdict(plan), reversal=plan.reversal,
        )

    controller = _resolve_controller(provider, ref)
    patch = _build_resources_patch(plan.params["container"], plan.new)
    args = [
        "patch", controller["kind"].lower(), controller["name"],
        "-n", controller["namespace"],
        "--type=strategic", "-p", json.dumps(patch),
    ]
    proc = provider._run(args, check=False)
    if proc.returncode != 0:
        return ActionResult(
            "set-resources", False,
            _clean(proc.stderr) or "patch failed",
            plan=asdict(plan), reversal=plan.reversal,
        )
    return ActionResult(
        "set-resources", True,
        f"{controller['kind'].lower()}/{controller['name']} "
        f"container {plan.params['container']!r} resources updated",
        plan=asdict(plan), reversal=plan.reversal,
    )


def _build_resources_patch(container: str, resources: dict) -> dict:
    return {
        "spec": {"template": {"spec": {"containers": [
            {"name": container, "resources": resources}
        ]}}}
    }


# --- adjust-probe (Kubernetes only) -------------------------------------------

_PROBE_FIELD_TO_K8S = {
    "initial_delay": "initialDelaySeconds",
    "period": "periodSeconds",
    "timeout": "timeoutSeconds",
    "failure_threshold": "failureThreshold",
}


def _parse_adjust_probe(raw: dict) -> dict:
    if "container" not in raw or not str(raw["container"]).strip():
        raise RemediationError("'adjust-probe' requires 'container'")
    probe = str(raw.get("probe", "")).strip()
    if probe not in _PROBE_NAMES:
        raise RemediationError(
            f"'probe' must be one of {_PROBE_NAMES} (got {probe!r})"
        )
    fields = {k: v for k, v in raw.items() if k in _PROBE_FIELD_MAX}
    if not fields:
        raise RemediationError(
            "'adjust-probe' needs at least one of "
            f"{', '.join(_PROBE_FIELD_MAX)}"
        )
    out: dict = {"container": str(raw["container"]).strip(), "probe": probe}
    for k, v in fields.items():
        n = _coerce_int(v, k)
        if n < 0:
            raise RemediationError(f"{k} must be >= 0")
        if n > _PROBE_FIELD_MAX[k]:
            raise RemediationError(
                f"{k} exceeds ceiling ({_PROBE_FIELD_MAX[k]})"
            )
        out[k] = n
    extra = set(raw) - {"container", "probe", *_PROBE_FIELD_MAX}
    if extra:
        raise RemediationError(
            f"'adjust-probe' got unexpected params: {sorted(extra)}"
        )
    return out


def _plan_adjust_probe(provider, ref: WorkloadRef, params: dict) -> Plan:
    _check_namespace(ref)
    controller = _resolve_controller(provider, ref)
    current = _kubectl_json(
        provider,
        ["get", controller["kind"].lower(), controller["name"],
         "-n", controller["namespace"], "-o", "json"],
    )
    container_name = params["container"]
    containers = current.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    target_container = next(
        (c for c in containers if c.get("name") == container_name), None
    )
    if target_container is None:
        raise RemediationError(
            f"container {container_name!r} not in {controller['kind'].lower()}/"
            f"{controller['name']}"
        )
    probe_key = params["probe"] + "Probe"
    old_probe = dict(target_container.get(probe_key, {}) or {})
    new_probe = dict(old_probe)
    for k, k8s in _PROBE_FIELD_TO_K8S.items():
        if k in params:
            new_probe[k8s] = params[k]
    target = f"{controller['kind'].lower()}/{controller['name']}/{container_name}"
    return Plan(
        action="adjust-probe",
        risk="medium",
        params=params,
        target=f"{target} (ns={controller['namespace']})",
        summary=f"adjust {params['probe']} probe on {target}: "
                + _diff_probe(old_probe, new_probe),
        old=old_probe,
        new=new_probe,
        reversal={"action": "adjust-probe",
                  "params": _probe_to_params(container_name, params["probe"], old_probe)},
    )


def _diff_probe(old: dict, new: dict) -> str:
    pieces = []
    for k, v in new.items():
        prev = old.get(k)
        if prev != v:
            pieces.append(f"{k} {prev or '∅'} → {v}")
    return ", ".join(pieces) or "no-op"


def _probe_to_params(container: str, probe: str, k8s_probe: dict) -> dict:
    out: dict = {"container": container, "probe": probe}
    for k, k8s in _PROBE_FIELD_TO_K8S.items():
        if k8s in k8s_probe:
            out[k] = k8s_probe[k8s]
    return out


def _execute_adjust_probe(provider, ref: WorkloadRef, plan: Plan) -> ActionResult:
    controller = _resolve_controller(provider, ref)
    probe_key = plan.params["probe"] + "Probe"
    patch = {"spec": {"template": {"spec": {"containers": [
        {"name": plan.params["container"], probe_key: plan.new}
    ]}}}}
    args = [
        "patch", controller["kind"].lower(), controller["name"],
        "-n", controller["namespace"],
        "--type=strategic", "-p", json.dumps(patch),
    ]
    proc = provider._run(args, check=False)
    if proc.returncode != 0:
        return ActionResult(
            "adjust-probe", False,
            _clean(proc.stderr) or "patch failed",
            plan=asdict(plan), reversal=plan.reversal,
        )
    return ActionResult(
        "adjust-probe", True,
        f"{controller['kind'].lower()}/{controller['name']} "
        f"{plan.params['probe']} probe updated",
        plan=asdict(plan), reversal=plan.reversal,
    )


# --- rollback (Kubernetes only) ----------------------------------------------

def _parse_rollback(raw: dict) -> dict:
    out: dict = {}
    if "revision" in raw and raw["revision"] not in (None, ""):
        rev = _coerce_int(raw["revision"], "revision")
        if rev < 1:
            raise RemediationError("'revision' must be >= 1")
        out["revision"] = rev
    extra = set(raw) - {"revision"}
    if extra:
        raise RemediationError(f"'rollback' got unexpected params: {sorted(extra)}")
    return out


def _plan_rollback(provider, ref: WorkloadRef, params: dict) -> Plan:
    _check_namespace(ref)
    controller = _resolve_controller(provider, ref)
    current = _kubectl_json(
        provider,
        ["get", controller["kind"].lower(), controller["name"],
         "-n", controller["namespace"], "-o", "json"],
    )
    annotations = (current.get("metadata", {}) or {}).get("annotations", {}) or {}
    current_revision = annotations.get("deployment.kubernetes.io/revision")
    target_rev = params.get("revision")
    target = f"{controller['kind'].lower()}/{controller['name']}"
    summary = (
        f"roll back {target} from revision {current_revision or '?'} "
        f"to {'revision ' + str(target_rev) if target_rev else 'previous revision'}"
    )
    return Plan(
        action="rollback",
        risk="medium",
        params=params,
        target=f"{target} (ns={controller['namespace']})",
        summary=summary,
        old={"revision": current_revision},
        new={"revision": str(target_rev) if target_rev else "previous"},
        # Reversal: re-rolling forward needs the revision we left from.
        reversal=(
            {"action": "rollback", "params": {"revision": int(current_revision)}}
            if current_revision and current_revision.isdigit()
            else {}
        ),
    )


def _execute_rollback(provider, ref: WorkloadRef, plan: Plan) -> ActionResult:
    controller = _resolve_controller(provider, ref)
    args = [
        "rollout", "undo",
        controller["kind"].lower() + "/" + controller["name"],
        "-n", controller["namespace"],
    ]
    if "revision" in plan.params:
        args.append(f"--to-revision={plan.params['revision']}")
    proc = provider._run(args, check=False)
    if proc.returncode != 0:
        return ActionResult(
            "rollback", False,
            _clean(proc.stderr) or "rollout undo failed",
            plan=asdict(plan), reversal=plan.reversal,
        )
    return ActionResult(
        "rollback", True,
        f"{controller['kind'].lower()}/{controller['name']} rolled back",
        plan=asdict(plan), reversal=plan.reversal,
    )


# --- spec-change actions: set-env / set-image / recreate (Phase 13) ----------
#
# These mutate the container's *definition* — environment variables, image,
# command. On Kubernetes they're clean `kubectl patch` / `kubectl set image`
# operations on the owning Deployment. On Podman a container's env and image
# are immutable once created, so the only honest way to change them is to
# recreate the container — capture its full run spec, remove it, and re-run
# with the modified field. ``_podman_recreate`` is the shared workhorse.

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# registry/name:tag@digest — lenient; we only guard against obvious garbage.
_IMAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-/:@]*[A-Za-z0-9]$"
)


def _parse_env_map(raw_env) -> dict:
    """Validate an env map: keys must be shell-legal; values are strings.

    A ``null`` value (Python ``None``) marks the key for deletion.
    """
    if not isinstance(raw_env, dict) or not raw_env:
        raise RemediationError("'env' must be a non-empty object of KEY: value")
    out: dict = {}
    for key, value in raw_env.items():
        if not _ENV_KEY_RE.match(str(key)):
            raise RemediationError(
                f"invalid env key {key!r} (must match [A-Za-z_][A-Za-z0-9_]*)"
            )
        out[str(key)] = None if value is None else str(value)
    return out


def _validate_image(image: str) -> str:
    image = str(image).strip()
    if not image or not _IMAGE_RE.match(image):
        raise RemediationError(f"invalid image reference {image!r}")
    return image


# -- podman: reconstruct a `podman run` from an inspect, with modifications ----

def _podman_run_spec(provider, ref: WorkloadRef) -> dict:
    """Capture the fields needed to re-create a Podman container."""
    d = provider._inspect(ref.name)
    config = d.get("Config", {}) or {}
    host = d.get("HostConfig", {}) or {}
    return {
        "name": ref.name,
        "image": d.get("ImageName") or d.get("Image", ""),
        "env": list(config.get("Env", []) or []),       # ["K=V", ...]
        "entrypoint": config.get("Entrypoint"),
        "cmd": config.get("Cmd"),
        "labels": config.get("Labels") or {},
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "",
        "network_mode": host.get("NetworkMode") or "",
    }


def _env_list_to_map(env_list: list) -> dict:
    out: dict = {}
    for item in env_list or []:
        key, sep, value = str(item).partition("=")
        if sep:
            out[key] = value
    return out


def _merge_env(current: list, changes: dict) -> list:
    """Apply ``changes`` (KEY -> value | None-to-delete) onto a ``K=V`` list."""
    merged = _env_list_to_map(current)
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return [f"{k}={v}" for k, v in merged.items()]


def _podman_recreate(provider, ref: WorkloadRef, spec: dict, new_env: list,
                     new_image: str) -> ActionResult:
    """Remove the container and re-run it with the modified env / image.

    Best-effort rebuild: preserves name, restart policy, network mode,
    entrypoint and command. Ports/volumes are intentionally NOT carried over
    in this first cut — the catalog warns about that in the action
    description. A failed re-run leaves the old container removed; the
    reversal payload records the prior spec so a human (or ``--undo``) can
    restore it.
    """
    args = ["run", "-d", "--name", ref.name]
    rp = spec.get("restart_policy")
    if rp and rp != "no":
        args += ["--restart", rp]
    net = spec.get("network_mode")
    if net and net not in ("", "default", "bridge"):
        args += ["--network", net]
    for kv in new_env:
        args += ["-e", kv]
    for lk, lv in (spec.get("labels") or {}).items():
        # Skip podman's own bookkeeping labels.
        if not str(lk).startswith("io.podman") and not str(lk).startswith("PODMAN"):
            args += ["--label", f"{lk}={lv}"]
    entrypoint = spec.get("entrypoint")
    if entrypoint:
        # entrypoint may be a list; podman wants a single string for --entrypoint
        args += ["--entrypoint", json.dumps(entrypoint) if isinstance(entrypoint, list) else str(entrypoint)]
    args += [new_image]
    cmd = spec.get("cmd")
    if cmd:
        args += list(cmd)

    # Remove the old container first (force — it may be running or exited).
    rm = provider._run(["rm", "-f", ref.name], check=False)
    if rm.returncode != 0:
        return None, _clean(rm.stderr) or "could not remove the old container"

    run = provider._run(args, check=False)
    if run.returncode != 0:
        return None, (run.stderr or run.stdout).strip() or "podman run failed"
    return run, None


# -- set-env ------------------------------------------------------------------

def _parse_set_env(raw: dict) -> dict:
    if "container" not in raw or not str(raw["container"]).strip():
        raise RemediationError("'set-env' requires 'container'")
    env = _parse_env_map(raw.get("env"))
    extra = set(raw) - {"container", "env"}
    if extra:
        raise RemediationError(f"'set-env' got unexpected params: {sorted(extra)}")
    return {"container": str(raw["container"]).strip(), "env": env}


def _plan_set_env(provider, ref: WorkloadRef, params: dict) -> Plan:
    changes = params["env"]
    summary_bits = ", ".join(
        f"{k}=<unset>" if v is None else f"{k}={'***' if _looks_secret(k) else v}"
        for k, v in changes.items()
    )
    if provider.name == "podman":
        spec = _podman_run_spec(provider, ref)
        old_env = _env_list_to_map(spec["env"])
        old_relevant = {k: old_env.get(k) for k in changes}
        return Plan(
            action="set-env", risk="medium", params=params,
            target=f"container {ref.name}",
            summary=f"set env on {ref.name}: {summary_bits} (recreates the container)",
            old={"env": old_relevant}, new={"env": {k: v for k, v in changes.items()}},
            reversal=_set_env_reversal(params["container"], old_env, changes),
        )
    # Kubernetes — patch the Deployment.
    _check_namespace(ref)
    controller = _resolve_controller(provider, ref)
    current = _kubectl_json(
        provider, ["get", controller["kind"].lower(), controller["name"],
                   "-n", controller["namespace"], "-o", "json"])
    container_name = params["container"]
    c = _k8s_container(current, container_name)
    old_env = {e.get("name"): e.get("value") for e in (c.get("env") or [])
               if isinstance(e, dict)}
    old_relevant = {k: old_env.get(k) for k in changes}
    target = f"{controller['kind'].lower()}/{controller['name']}/{container_name}"
    return Plan(
        action="set-env", risk="medium", params=params,
        target=f"{target} (ns={controller['namespace']})",
        summary=f"set env on {target}: {summary_bits}",
        old={"env": old_relevant}, new={"env": {k: v for k, v in changes.items()}},
        reversal=_set_env_reversal(container_name, old_env, changes),
    )


def _set_env_reversal(container: str, old_env: dict, changes: dict) -> dict:
    """The set-env that restores the keys we're about to change.

    A key that didn't exist before is reversed by deleting it (null value).
    """
    restore: dict = {}
    for key in changes:
        prior = old_env.get(key)
        restore[key] = prior  # None if it didn't exist -> delete on undo
    return {"action": "set-env",
            "params": {"container": container, "env": restore}}


def _execute_set_env(provider, ref: WorkloadRef, plan: Plan) -> ActionResult:
    changes = plan.params["env"]
    if provider.name == "podman":
        spec = _podman_run_spec(provider, ref)
        new_env = _merge_env(spec["env"], changes)
        run, err = _podman_recreate(provider, ref, spec, new_env, spec["image"])
        if run is None:
            return ActionResult("set-env", False, err,
                                plan=asdict(plan), reversal=plan.reversal)
        return ActionResult("set-env", True,
                            f"container {ref.name} recreated with updated env",
                            plan=asdict(plan), reversal=plan.reversal)
    # Kubernetes — strategic-merge patch the named env entries.
    controller = _resolve_controller(provider, ref)
    env_entries = []
    for key, value in changes.items():
        if value is None:
            continue  # strategic merge can't delete by patch; skip (set to "" instead)
        env_entries.append({"name": key, "value": value})
    deletions = [k for k, v in changes.items() if v is None]
    patch = {"spec": {"template": {"spec": {"containers": [
        {"name": plan.params["container"], "env": env_entries}
    ]}}}}
    proc = provider._run([
        "patch", controller["kind"].lower(), controller["name"],
        "-n", controller["namespace"], "--type=strategic",
        "-p", json.dumps(patch),
    ], check=False)
    if proc.returncode != 0:
        return ActionResult("set-env", False, _clean(proc.stderr) or "patch failed",
                            plan=asdict(plan), reversal=plan.reversal)
    msg = f"{controller['kind'].lower()}/{controller['name']} env updated"
    if deletions:
        msg += f" (note: deletions {deletions} need a manual remove on k8s)"
    return ActionResult("set-env", True, msg,
                        plan=asdict(plan), reversal=plan.reversal)


# -- set-image ----------------------------------------------------------------

def _parse_set_image(raw: dict) -> dict:
    if "container" not in raw or not str(raw["container"]).strip():
        raise RemediationError("'set-image' requires 'container'")
    if "image" not in raw or not str(raw["image"]).strip():
        raise RemediationError("'set-image' requires 'image'")
    image = _validate_image(raw["image"])
    extra = set(raw) - {"container", "image"}
    if extra:
        raise RemediationError(f"'set-image' got unexpected params: {sorted(extra)}")
    return {"container": str(raw["container"]).strip(), "image": image}


def _plan_set_image(provider, ref: WorkloadRef, params: dict) -> Plan:
    new_image = params["image"]
    if provider.name == "podman":
        spec = _podman_run_spec(provider, ref)
        return Plan(
            action="set-image", risk="medium", params=params,
            target=f"container {ref.name}",
            summary=f"set image on {ref.name}: {spec['image']} → {new_image} (recreates)",
            old={"image": spec["image"]}, new={"image": new_image},
            reversal={"action": "set-image",
                      "params": {"container": params["container"], "image": spec["image"]}},
        )
    _check_namespace(ref)
    controller = _resolve_controller(provider, ref)
    current = _kubectl_json(
        provider, ["get", controller["kind"].lower(), controller["name"],
                   "-n", controller["namespace"], "-o", "json"])
    container_name = params["container"]
    c = _k8s_container(current, container_name)
    old_image = c.get("image", "")
    target = f"{controller['kind'].lower()}/{controller['name']}/{container_name}"
    return Plan(
        action="set-image", risk="medium", params=params,
        target=f"{target} (ns={controller['namespace']})",
        summary=f"set image on {target}: {old_image} → {new_image}",
        old={"image": old_image}, new={"image": new_image},
        reversal={"action": "set-image",
                  "params": {"container": container_name, "image": old_image}},
    )


def _execute_set_image(provider, ref: WorkloadRef, plan: Plan) -> ActionResult:
    new_image = plan.params["image"]
    if provider.name == "podman":
        spec = _podman_run_spec(provider, ref)
        run, err = _podman_recreate(provider, ref, spec, spec["env"], new_image)
        if run is None:
            return ActionResult("set-image", False, err,
                                plan=asdict(plan), reversal=plan.reversal)
        return ActionResult("set-image", True,
                            f"container {ref.name} recreated on image {new_image}",
                            plan=asdict(plan), reversal=plan.reversal)
    controller = _resolve_controller(provider, ref)
    proc = provider._run([
        "set", "image",
        f"{controller['kind'].lower()}/{controller['name']}",
        f"{plan.params['container']}={new_image}",
        "-n", controller["namespace"],
    ], check=False)
    if proc.returncode != 0:
        return ActionResult("set-image", False, _clean(proc.stderr) or "set image failed",
                            plan=asdict(plan), reversal=plan.reversal)
    return ActionResult("set-image", True,
                        f"{controller['kind'].lower()}/{controller['name']} "
                        f"image set to {new_image}",
                        plan=asdict(plan), reversal=plan.reversal)


# -- recreate (podman: rebuild; kubernetes: combined patch) -------------------

def _parse_recreate(raw: dict) -> dict:
    if "container" not in raw or not str(raw["container"]).strip():
        raise RemediationError("'recreate' requires 'container'")
    out: dict = {"container": str(raw["container"]).strip()}
    if raw.get("image"):
        out["image"] = _validate_image(raw["image"])
    if raw.get("env") not in (None, ""):
        out["env"] = _parse_env_map(raw["env"])
    if raw.get("command") not in (None, ""):
        out["command"] = _as_str_list(raw["command"], "command")
    if raw.get("args") not in (None, ""):
        out["args"] = _as_str_list(raw["args"], "args")
    if len(out) == 1:
        raise RemediationError(
            "'recreate' needs at least one of image, env, command, args")
    extra = set(raw) - {"container", "image", "env", "command", "args"}
    if extra:
        raise RemediationError(f"'recreate' got unexpected params: {sorted(extra)}")
    return out


def _as_str_list(value, name: str) -> list:
    if isinstance(value, str):
        # allow a single string -> one-element list
        return [value]
    if isinstance(value, list) and all(isinstance(x, (str, int, float)) for x in value):
        return [str(x) for x in value]
    raise RemediationError(f"{name} must be a string or a list of strings")


def _plan_recreate(provider, ref: WorkloadRef, params: dict) -> Plan:
    changed = [k for k in ("image", "env", "command", "args") if k in params]
    if provider.name == "podman":
        spec = _podman_run_spec(provider, ref)
        return Plan(
            action="recreate", risk="high", params=params,
            target=f"container {ref.name}",
            summary=f"recreate {ref.name} (changing: {', '.join(changed)})",
            old={"image": spec["image"], "env": _env_list_to_map(spec["env"]),
                 "cmd": spec.get("cmd")},
            new={k: params[k] for k in changed},
            reversal={},  # full recreate isn't auto-reversible; spec is in `old`
        )
    _check_namespace(ref)
    controller = _resolve_controller(provider, ref)
    current = _kubectl_json(
        provider, ["get", controller["kind"].lower(), controller["name"],
                   "-n", controller["namespace"], "-o", "json"])
    c = _k8s_container(current, params["container"])
    target = f"{controller['kind'].lower()}/{controller['name']}/{params['container']}"
    return Plan(
        action="recreate", risk="high", params=params,
        target=f"{target} (ns={controller['namespace']})",
        summary=f"recreate {target} (changing: {', '.join(changed)})",
        old={"image": c.get("image"), "command": c.get("command"),
             "args": c.get("args")},
        new={k: params[k] for k in changed},
        reversal={},
    )


def _execute_recreate(provider, ref: WorkloadRef, plan: Plan) -> ActionResult:
    params = plan.params
    if provider.name == "podman":
        spec = _podman_run_spec(provider, ref)
        new_image = params.get("image") or spec["image"]
        new_env = _merge_env(spec["env"], params["env"]) if "env" in params else spec["env"]
        if "command" in params or "args" in params:
            spec = dict(spec)
            if "command" in params:
                spec["entrypoint"] = params["command"]
                spec["cmd"] = params.get("args")
            elif "args" in params:
                spec["cmd"] = params["args"]
        run, err = _podman_recreate(provider, ref, spec, new_env, new_image)
        if run is None:
            return ActionResult("recreate", False, err,
                                plan=asdict(plan), reversal=plan.reversal)
        return ActionResult("recreate", True,
                            f"container {ref.name} recreated",
                            plan=asdict(plan), reversal=plan.reversal)
    # Kubernetes — one strategic-merge patch with all changed fields.
    controller = _resolve_controller(provider, ref)
    container_patch: dict = {"name": params["container"]}
    if "image" in params:
        container_patch["image"] = params["image"]
    if "command" in params:
        container_patch["command"] = params["command"]
    if "args" in params:
        container_patch["args"] = params["args"]
    if "env" in params:
        container_patch["env"] = [
            {"name": k, "value": v} for k, v in params["env"].items() if v is not None
        ]
    patch = {"spec": {"template": {"spec": {"containers": [container_patch]}}}}
    proc = provider._run([
        "patch", controller["kind"].lower(), controller["name"],
        "-n", controller["namespace"], "--type=strategic",
        "-p", json.dumps(patch),
    ], check=False)
    if proc.returncode != 0:
        return ActionResult("recreate", False, _clean(proc.stderr) or "patch failed",
                            plan=asdict(plan), reversal=plan.reversal)
    return ActionResult("recreate", True,
                        f"{controller['kind'].lower()}/{controller['name']} recreated",
                        plan=asdict(plan), reversal=plan.reversal)


def _k8s_container(deployment: dict, container_name: str) -> dict:
    containers = (deployment.get("spec", {}).get("template", {})
                  .get("spec", {}).get("containers", []))
    c = next((x for x in containers if x.get("name") == container_name), None)
    if c is None:
        raise RemediationError(
            f"container {container_name!r} not in the workload "
            f"(have: {[x.get('name') for x in containers]})")
    return c


_SECRET_KEY_RE = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSWD|KEY|CREDENTIAL)", re.I)


def _looks_secret(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


# --- catalog -----------------------------------------------------------------

CATALOG: dict[str, ActionSpec] = {
    "restart": ActionSpec(
        name="restart", risk="low",
        platforms=("podman", "kubernetes"),
        description="restart the container (Podman) or delete the pod so its "
                    "controller recreates it (Kubernetes)",
        parse=_parse_restart, plan=_plan_restart, execute=_execute_restart,
    ),
    "scale": ActionSpec(
        name="scale", risk="low",
        platforms=("kubernetes",),
        description="scale the workload controller (Deployment) replicas",
        parse=_parse_scale, plan=_plan_scale, execute=_execute_scale,
    ),
    "set-resources": ActionSpec(
        name="set-resources", risk="medium",
        platforms=("podman", "kubernetes"),
        description="set the container's CPU/memory limits and (K8s) requests",
        parse=_parse_set_resources, plan=_plan_set_resources,
        execute=_execute_set_resources,
    ),
    "adjust-probe": ActionSpec(
        name="adjust-probe", risk="medium",
        platforms=("kubernetes",),
        description="adjust a liveness/readiness/startup probe's timings",
        parse=_parse_adjust_probe, plan=_plan_adjust_probe,
        execute=_execute_adjust_probe,
    ),
    "rollback": ActionSpec(
        name="rollback", risk="medium",
        platforms=("kubernetes",),
        description="kubectl rollout undo on the workload's controller",
        parse=_parse_rollback, plan=_plan_rollback, execute=_execute_rollback,
    ),
    "set-env": ActionSpec(
        name="set-env", risk="medium",
        platforms=("podman", "kubernetes"),
        description="set/replace environment variables on the container "
                    "(K8s patches the Deployment; Podman recreates the "
                    "container). A null value deletes the key.",
        parse=_parse_set_env, plan=_plan_set_env, execute=_execute_set_env,
    ),
    "set-image": ActionSpec(
        name="set-image", risk="medium",
        platforms=("podman", "kubernetes"),
        description="change the container's image (K8s `kubectl set image`; "
                    "Podman recreates the container on the new image).",
        parse=_parse_set_image, plan=_plan_set_image, execute=_execute_set_image,
    ),
    "recreate": ActionSpec(
        name="recreate", risk="high",
        platforms=("podman", "kubernetes"),
        description="recreate the container with a modified spec — any subset "
                    "of image, env, command, args. Throws away the running "
                    "container; ports/volumes are not carried over on Podman.",
        parse=_parse_recreate, plan=_plan_recreate, execute=_execute_recreate,
    ),
}


# --- shell action (opt-in — HLD §12.9) --------------------------------------
#
# The `shell` action runs an arbitrary command inside the target container.
# It is NOT in the catalog by default because it weakens the safety
# property that "the LLM never emits a command" — with `shell` enabled, the
# LLM proposes a command string that the engine executes verbatim. Opt in
# via :func:`enable_shell_action` or, from the CLI, the `--allow-shell`
# flag.
#
# Even when enabled, the action is `risk: high`, which keeps it out of the
# default `--max-risk low` auto-apply tier. A run that wants the agent to
# auto-pick shell needs BOTH `--allow-shell` AND `--max-risk high`.

# CLI users may set this for direct invocations too (`remediate --action
# shell`); the env var avoids needing to pass `--allow-shell` every time.
_SHELL_ENV_FLAG = "PODDEBUGGER_ALLOW_SHELL"
_SHELL_MAX_OUTPUT_CHARS = 4000


def _parse_shell(raw: dict) -> dict:
    cmd = str(raw.get("command", "")).strip()
    if not cmd:
        raise RemediationError("'shell' requires a non-empty 'command' string")
    extra = set(raw) - {"command"}
    if extra:
        raise RemediationError(f"'shell' got unexpected params: {sorted(extra)}")
    return {"command": cmd}


def _plan_shell(provider: ContainerPlatform, ref: WorkloadRef, params: dict) -> Plan:
    if provider.name == "kubernetes":
        _check_namespace(ref)
    cmd = params["command"]
    return Plan(
        action="shell",
        risk="high",
        params=params,
        target=f"container {ref.name}",
        summary=f"shell: {cmd if len(cmd) <= 80 else cmd[:77] + '...'}",
        old={},
        new={"executed_command": cmd},
        # No automatic reversal — shell side effects are open-ended.
        reversal={},
    )


def _execute_shell(provider, ref: WorkloadRef, plan: Plan) -> ActionResult:
    cmd = plan.params["command"]
    try:
        output = provider.exec(ref, ["sh", "-c", cmd])
    except ProviderError as exc:
        return ActionResult(
            "shell", False, str(exc),
            plan=asdict(plan), reversal=None,
        )
    if len(output) > _SHELL_MAX_OUTPUT_CHARS:
        output = output[:_SHELL_MAX_OUTPUT_CHARS] + "\n…(truncated)"
    return ActionResult(
        "shell", True,
        output or "(no output)",
        plan=asdict(plan), reversal=None,
    )


def enable_shell_action() -> None:
    """Opt-in registration for the freeform ``shell`` catalog action.

    Off by default. Call this once at startup (the CLI does so when
    ``--allow-shell`` is passed, or when ``PODDEBUGGER_ALLOW_SHELL=1`` is
    set in the environment).

    Trade-offs versus the typed catalog actions:

    - The LLM (or a CLI user) now supplies a command string that the
      engine executes verbatim. The safety boundary is the *approval
      gate*, not the catalog — so this is meaningful only when paired
      with a real :class:`poddebugger.approvals.ApprovalGate`.
    - Risk is hard-wired to ``"high"``. The agent path
      (``analyze --fix --confirm``) only auto-applies it when
      ``--max-risk high`` is also passed.
    - No automatic reversal; ``verify_recovery`` skips this action.
    - Output is captured (truncated at 4000 chars) and surfaced in the
      ``ActionResult.result`` field.
    """
    CATALOG["shell"] = ActionSpec(
        name="shell", risk="high",
        platforms=("podman", "kubernetes"),
        description=(
            "Run an arbitrary shell command inside the target container. "
            "Use only when no typed action fits — has no automatic reversal "
            "and no recovery verification."
        ),
        parse=_parse_shell, plan=_plan_shell, execute=_execute_shell,
    )


def shell_action_enabled() -> bool:
    """Whether the ``shell`` action is currently in the catalog."""
    return "shell" in CATALOG


# Auto-enable from the environment so users don't have to thread
# `--allow-shell` through every invocation in scripted setups.
if os.environ.get(_SHELL_ENV_FLAG, "").strip().lower() in ("1", "true", "yes"):
    enable_shell_action()


# --- public API --------------------------------------------------------------

def list_actions(platform: str | None = None) -> list[str]:
    if platform is None:
        return list(CATALOG)
    plat = "kubernetes" if platform == "openshift" else platform
    return [name for name, spec in CATALOG.items() if plat in spec.platforms]


def get_spec(name: str) -> ActionSpec:
    if name not in CATALOG:
        raise RemediationError(
            f"unknown action {name!r} (catalog: {sorted(CATALOG)})"
        )
    return CATALOG[name]


def parse_params(name: str, items: list[str] | dict) -> dict:
    """Validate raw params (CLI ``--param k=v`` list, or a dict) into a typed dict."""
    spec = get_spec(name)
    raw: dict
    if isinstance(items, dict):
        raw = dict(items)
    else:
        raw = {}
        for kv in items or []:
            key, sep, value = str(kv).partition("=")
            if not sep:
                raise RemediationError(
                    f"param {kv!r} must be in key=value form"
                )
            raw[key.strip()] = value.strip()
    return spec.parse(raw)


def make_plan(
    provider: ContainerPlatform,
    ref: WorkloadRef,
    name: str,
    params: dict,
) -> Plan:
    spec = get_spec(name)
    platform = "kubernetes" if provider.name == "openshift" else provider.name
    if platform not in spec.platforms:
        raise RemediationError(
            f"action {name!r} not supported on platform {provider.name!r} "
            f"(supported: {spec.platforms})"
        )
    try:
        return spec.plan(provider, ref, params)
    except ProviderError as exc:
        raise RemediationError(str(exc)) from exc


def execute(
    provider: ContainerPlatform,
    ref: WorkloadRef,
    plan: Plan,
    *,
    gate=None,
) -> ActionResult:
    """Run a validated plan, optionally gated by a Phase 11 approval gate.

    ``gate`` is any object with ``request(ActionDescriptor) -> Decision``
    (typically a :class:`poddebugger.approvals.ApprovalGate`). When set,
    the gate is consulted before the shell-out; a ``DENY`` result returns
    an unexecuted :class:`ActionResult` with the refusal recorded. When
    ``gate`` is ``None`` (the default), behavior is unchanged — back-compat
    for callers that haven't migrated yet (the CLI always passes a gate
    in Phase 11).
    """
    spec = get_spec(plan.action)
    if gate is not None:
        from .approvals import ActionDescriptor, is_allowed
        descriptor = ActionDescriptor(
            kind="remediation",
            action=plan.action,
            target=ref,
            risk=plan.risk,
            summary=plan.summary,
            plan=asdict(plan),
        )
        decision = gate.request(descriptor)
        if not is_allowed(decision):
            return ActionResult(
                plan.action, False,
                f"refused by approval gate ({decision.value})",
                plan=asdict(plan), reversal=plan.reversal or None,
            )
    try:
        return spec.execute(provider, ref, plan)
    except ProviderError as exc:
        return ActionResult(
            plan.action, False, str(exc),
            plan=asdict(plan), reversal=plan.reversal or None,
        )


def render_plan(plan: Plan) -> str:
    """Human-readable preview of a plan (used by ``--dry-run`` and confirm prompts)."""
    lines = [
        f"action:  {plan.action}  ({plan.risk} risk)",
        f"target:  {plan.target}",
        f"summary: {plan.summary}",
    ]
    if plan.params:
        lines.append(f"params:  {plan.params}")
    if plan.old or plan.new:
        lines.append(f"old → new: {plan.old} → {plan.new}")
    if plan.reversal:
        lines.append(f"reversal: {plan.reversal}")
    return "\n".join(lines)


# --- helpers shared by per-platform implementations ---------------------------

_KLOG_NOISE = re.compile(r"^[EWIF]\d{4}\s")


def _clean(stderr: str) -> str:
    lines = [ln for ln in (stderr or "").splitlines()
             if ln.strip() and not _KLOG_NOISE.match(ln)]
    return " ".join(lines).strip() or (stderr or "").strip()


def _k8s_namespace(provider, ref: WorkloadRef) -> str:
    ns = ref.namespace
    if ns:
        return ns
    # Fall back to whatever the K8s provider already cached.
    return getattr(provider, "_ns_cache", None) or "default"


def _kubectl_json(provider, args: list[str]) -> dict:
    proc = provider._run(args, check=False)
    if proc.returncode != 0:
        raise RemediationError(
            f"{provider.name} {' '.join(args)} failed: "
            f"{_clean(proc.stderr) or 'unknown error'}"
        )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RemediationError(
            f"{provider.name} {' '.join(args)} returned non-JSON output"
        ) from exc


# --- Phase 7D: post-remediation verification + undo persistence -------------

# Actions whose effect is observable on the same workload ref. ``restart`` on
# Kubernetes deletes the pod (the controller recreates a *different* pod), so
# the original ref becomes stale — we mark verification as "unknown" rather
# than attempt a brittle controller-walk in this iteration.
_VERIFY_SAME_REF = {"restart", "set-resources", "adjust-probe", "scale", "rollback",
                    "set-env", "set-image", "recreate"}
# On Kubernetes these create a *new* pod (different name), so the original ref
# goes stale exactly like `restart`. On Podman they recreate the container
# under the SAME name, so re-reading the ref works.
_VERIFY_NEW_POD_ON_K8S = {"restart", "set-env", "set-image", "recreate"}
DEFAULT_VERIFY_WAIT = 5


def capture_baseline(provider: ContainerPlatform, ref: WorkloadRef) -> dict:
    """Snapshot the fields verify_recovery compares against.

    Tolerant by design: a provider error returns an empty dict so the caller
    can still execute (verification just reports ``unknown``).
    """
    try:
        w = provider.get_workload(ref)
    except ProviderError as exc:
        return {"error": str(exc)}
    return {
        "status": w.status,
        "running": bool(w.running),
        "restart_count": int(w.restart_count or 0),
        "exit_code": w.exit_code,
        "oom_killed": bool(w.oom_killed),
        "health_status": w.health_status,
    }


def verify_recovery(
    provider: ContainerPlatform,
    ref: WorkloadRef,
    baseline: dict,
    action: str,
    wait_seconds: int = DEFAULT_VERIFY_WAIT,
    *,
    sleep=None,
) -> dict:
    """Re-read the workload after an action and judge recovery.

    Outcome (``HLD §12.7``):
    - ``recovered``   — the workload is running and no new failure observed.
    - ``still-failing`` — restart_count climbed or the workload is back in a
      failing state.
    - ``unknown``     — the ref no longer points at a single observable pod
      (e.g. Kubernetes ``restart``), or the provider could not be read.
    - ``skipped``     — ``wait_seconds <= 0``.

    ``sleep`` is injected so tests can drive verification without waiting.
    """
    if wait_seconds is None or wait_seconds <= 0:
        return {"outcome": "skipped",
                "reason": "verification disabled (wait_seconds <= 0)",
                "baseline": baseline,
                "observed": {},
                "waited_seconds": 0}

    # Some actions destroy/recreate the original ref — don't pretend to verify.
    if action in _VERIFY_NEW_POD_ON_K8S and ref.platform in ("kubernetes", "openshift"):
        return {"outcome": "unknown",
                "reason": "the change replaces the pod; its controller creates a "
                          "new pod with a different name — check the workload",
                "baseline": baseline, "observed": {},
                "waited_seconds": 0}
    if action not in _VERIFY_SAME_REF:
        return {"outcome": "skipped",
                "reason": f"no verification rule for action {action!r}",
                "baseline": baseline, "observed": {}, "waited_seconds": 0}

    (sleep or time.sleep)(wait_seconds)

    try:
        # The K8s provider caches pod inspects; bust it so we see fresh state.
        if hasattr(provider, "_pod_cache"):
            provider._pod_cache.clear()
        if hasattr(provider, "_inspect_cache"):
            provider._inspect_cache.clear()
        observed = capture_baseline(provider, ref)
    except ProviderError as exc:
        return {"outcome": "unknown",
                "reason": f"could not re-read workload: {exc}",
                "baseline": baseline, "observed": {},
                "waited_seconds": wait_seconds}

    if "error" in observed:
        return {"outcome": "unknown",
                "reason": observed["error"],
                "baseline": baseline, "observed": observed,
                "waited_seconds": wait_seconds}

    rc_before = int(baseline.get("restart_count", 0) or 0)
    rc_after = int(observed.get("restart_count", 0) or 0)
    # restart_count climbed past the action itself → still crashing.
    if rc_after > rc_before + (1 if action == "restart" else 0):
        return {"outcome": "still-failing",
                "reason": f"restart_count rose from {rc_before} to {rc_after}",
                "baseline": baseline, "observed": observed,
                "waited_seconds": wait_seconds}
    if observed.get("oom_killed") and not baseline.get("oom_killed"):
        return {"outcome": "still-failing",
                "reason": "container was OOM-killed after the action",
                "baseline": baseline, "observed": observed,
                "waited_seconds": wait_seconds}
    if not observed.get("running"):
        # If the baseline wasn't running either, "recovered" would be a lie.
        if not baseline.get("running"):
            return {"outcome": "unknown",
                    "reason": "workload still not running — controller may "
                              "still be creating it; re-check shortly",
                    "baseline": baseline, "observed": observed,
                    "waited_seconds": wait_seconds}
        return {"outcome": "still-failing",
                "reason": f"workload not running (status={observed.get('status')!r})",
                "baseline": baseline, "observed": observed,
                "waited_seconds": wait_seconds}
    return {"outcome": "recovered",
            "reason": f"workload is running and stable (restart_count={rc_after})",
            "baseline": baseline, "observed": observed,
            "waited_seconds": wait_seconds}


# --- undo persistence -------------------------------------------------------

_SAFE_KEY = re.compile(r"[^A-Za-z0-9._-]+")


def _state_root() -> Path:
    """Where we auto-save the last successful remediation, keyed by ref.

    Honors ``PODDEBUGGER_STATE_DIR`` for tests / locked-down environments;
    falls back to ``$XDG_STATE_HOME/poddebugger`` then
    ``~/.cache/poddebugger/state``.
    """
    explicit = os.environ.get("PODDEBUGGER_STATE_DIR")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "poddebugger" / "state"


def _undo_file(ref: WorkloadRef) -> Path:
    key = f"{ref.platform}-{ref.namespace or '_'}-{ref.name}"
    safe = _SAFE_KEY.sub("_", key).strip("_") or "default"
    return _state_root() / f"last-remediation-{safe}.json"


def save_for_undo(ref: WorkloadRef, payload: dict) -> Path:
    """Persist a successful remediation so ``remediate --undo`` can read it.

    Best-effort — IOErrors are swallowed and the path is still returned (the
    CLI's primary path must not fail just because state can't be written).
    """
    path = _undo_file(ref)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str))
    except OSError:
        pass
    return path


def load_for_undo(ref: WorkloadRef | None = None, path: str | Path | None = None) -> dict:
    """Read a persisted remediation payload — by explicit path, or by ref."""
    if path is not None:
        p = Path(path)
    elif ref is not None:
        p = _undo_file(ref)
    else:
        raise RemediationError("load_for_undo needs either a ref or a path")
    if not p.exists():
        raise RemediationError(
            f"no saved remediation to undo at {p}. Pass --undo PATH to undo "
            "a specific result file, or apply an action first to populate it."
        )
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RemediationError(f"could not read saved remediation {p}: {exc}") from exc


def undo_from(payload: dict) -> tuple[WorkloadRef, str, dict]:
    """Extract (ref, action, params) for an undo from a saved CLI payload.

    Expects the shape emitted by ``remediate --confirm --json``:
    ``{action, plan, reversal, target, ...}``. Rejects payloads missing the
    ``reversal`` block (the original action was self-reversing, e.g.
    ``restart``) or missing the ``target`` block (pre-7D save).
    """
    if not isinstance(payload, dict):
        raise RemediationError("saved payload is not an object")
    reversal = payload.get("reversal")
    if not isinstance(reversal, dict) or not reversal.get("action"):
        raise RemediationError(
            "saved payload has no reversal — the original action was "
            "self-reversing (e.g. restart) or pre-7D and didn't capture one"
        )
    target = payload.get("target")
    if not isinstance(target, dict) or not target.get("name"):
        raise RemediationError(
            "saved payload has no target block — re-run the original action "
            "with the Phase 7D CLI to capture it, or supply --undo PATH for a "
            "newer file"
        )
    ref = WorkloadRef(
        name=str(target.get("name", "")),
        namespace=target.get("namespace") or None,
        container=target.get("container") or None,
        platform=str(target.get("platform") or "podman"),
    )
    return ref, str(reversal["action"]), dict(reversal.get("params") or {})


def _resolve_controller(provider, ref: WorkloadRef) -> dict:
    """Walk a pod's ownerReferences up to a top-level workload controller.

    Returns ``{kind, name, namespace}`` for the Deployment / StatefulSet /
    DaemonSet that owns the pod. Raises ``RemediationError`` if the pod is
    standalone or the chain can't be resolved.
    """
    if hasattr(provider, "get_controller"):
        controller = provider.get_controller(ref)
        if controller is None:
            raise RemediationError(
                f"pod {ref} has no workload controller — cannot mutate it"
            )
        return controller
    raise RemediationError(
        f"{provider.name} provider does not support controller-level actions"
    )
