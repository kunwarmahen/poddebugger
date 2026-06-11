"""Sandbox runner for Coder-agent scripts (Stage 13D — HLD §18.6).

A script never runs inside the target workload. It runs in a **sibling
sandbox container**:

* Podman — ``podman run --rm --network container:<target> <image> …``
  joins the target's network namespace (so it can probe the target's
  ports/DNS view) but shares no filesystem and no process namespace.
  If the target is not running, the sandbox runs on the default network.
* Kubernetes — ``kubectl debug <pod> --image=<image> --attach --quiet``
  attaches an ephemeral debug container to the pod (process + network
  visibility per cluster policy, no filesystem write into the target).

Safety properties:

* **Fail closed** — with no approval gate, execution is refused. (Probes
  default open for legacy parity; arbitrary scripts do not.)
* The gate descriptor is ``kind="code", action="<language>:<hash12>"``,
  risk ``high``, with the full script in the prompt — so a human sees
  exactly what will run, and a persistent rule can pre-approve exactly
  one (language, script) pair by its hash, never "all code".
* Output is captured and truncated; a failing script is still
  information (the exit code is part of the result).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass

from ..approvals import ActionDescriptor, is_allowed
from ..models import WorkloadRef
from ..providers.base import ContainerPlatform, ProviderError

LANGUAGES = {
    "bash": ("sh", "-c"),
    "python": ("python3", "-c"),
}

OUTPUT_CAP = 8000
DEFAULT_TIMEOUT = 120
PURPOSES = ("probe", "fix", "build")

#: Built locally from sandbox/Dockerfile (`podman build -t
#: poddebugger-coder-sandbox sandbox/`); override with
#: PODDEBUGGER_CODER_IMAGE once a registry copy exists.
_DEFAULT_IMAGE = "localhost/poddebugger-coder-sandbox:latest"


def default_image() -> str:
    return os.environ.get("PODDEBUGGER_CODER_IMAGE") or _DEFAULT_IMAGE


def script_hash(language: str, script: str) -> str:
    return hashlib.sha256(f"{language}\n{script}".encode()).hexdigest()


@dataclass
class CodeResult:
    executed: bool = False
    denied: bool = False
    output: str = ""
    exit_code: int | None = None
    error: str = ""
    hash: str = ""


def _gate_descriptor(ref: WorkloadRef, language: str, script: str,
                     purpose: str, digest: str) -> ActionDescriptor:
    first_line = next((l for l in script.splitlines() if l.strip()), "")
    return ActionDescriptor(
        kind="code",
        action=f"{language}:{digest[:12]}",
        target=ref,
        risk="high",
        summary=f"[{purpose}] {first_line[:80]}",
        plan={"language": language, "purpose": purpose, "script": script},
    )


def _sandbox_argv(provider: ContainerPlatform, ref: WorkloadRef,
                  language: str, script: str, image: str) -> list[str]:
    interpreter = LANGUAGES[language]
    if ref.platform == "podman":
        net: list[str] = []
        try:
            if provider.get_workload(ref).running:
                net = ["--network", f"container:{ref.name}"]
        except ProviderError:
            pass  # target unreadable — run on the default network
        return ["run", "--rm", *net, image, *interpreter, script]
    # kubernetes / openshift: ephemeral debug container on the pod
    args = ["debug", ref.name]
    if ref.namespace:
        args += ["-n", ref.namespace]
    args += [f"--image={image}", "--attach", "--quiet"]
    if ref.container:
        args += [f"--target={ref.container}"]
    return [*args, "--", *interpreter, script]


def run_code(provider: ContainerPlatform, ref: WorkloadRef, language: str,
             script: str, *, purpose: str = "probe", gate=None,
             image: str | None = None,
             timeout: int = DEFAULT_TIMEOUT) -> CodeResult:
    """Gate, then execute one script in a sandbox sibling. Never raises for
    a failing script — its exit code and output are the result."""
    language = (language or "").strip().lower()
    if language not in LANGUAGES:
        return CodeResult(error=f"unsupported language {language!r} "
                                f"(supported: {sorted(LANGUAGES)})")
    if not (script or "").strip():
        return CodeResult(error="empty script")
    if purpose not in PURPOSES:
        purpose = "probe"
    digest = script_hash(language, script)
    result = CodeResult(hash=digest)

    # Fail closed: arbitrary code with no gate is refused.
    if gate is None:
        result.denied = True
        result.error = ("no approval gate supplied — code execution is "
                        "deny-by-default")
        return result
    decision = gate.request(_gate_descriptor(ref, language, script,
                                             purpose, digest))
    if not is_allowed(decision):
        result.denied = True
        result.error = "denied by approval gate"
        return result

    binary = getattr(provider, "_bin", None) or (
        "podman" if ref.platform == "podman" else "kubectl")
    argv = _sandbox_argv(provider, ref, language, script,
                         image or default_image())
    try:
        proc = subprocess.run([binary, *argv], capture_output=True,
                              text=True, timeout=timeout)
    except FileNotFoundError:
        result.error = f"'{binary}' not found on PATH"
        return result
    except subprocess.TimeoutExpired:
        result.error = f"sandbox script timed out after {timeout}s"
        return result

    out = ((proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")).strip()
    if len(out) > OUTPUT_CAP:
        out = out[:OUTPUT_CAP] + "\n...(output truncated)"
    result.executed = True
    result.exit_code = proc.returncode
    result.output = out
    return result
