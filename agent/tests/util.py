"""Shared helpers for the poddebugger unit tests."""

from __future__ import annotations

import json
import subprocess


def cp(stdout: str = "", rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    """Build a CompletedProcess for stubbing a provider's ``_run``."""
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def podman_inspect(
    name: str = "c1",
    status: str = "exited",
    running: bool = False,
    exit_code: int = 0,
    oom: bool = False,
    restarts: int = 0,
    health: str = "",
    error: str = "",
    env: list | None = None,
) -> str:
    """Render a one-element `podman inspect` JSON array."""
    doc = {
        "Name": name,
        "ImageName": "registry.example.com/app:1.0",
        "RestartCount": restarts,
        "State": {
            "Status": status,
            "Running": running,
            "ExitCode": exit_code,
            "OOMKilled": oom,
            "Error": error,
            "StartedAt": "2026-05-21T10:00:00Z",
            "FinishedAt": "2026-05-21T10:01:00Z",
            "Health": {"Status": health},
        },
        "Config": {
            "Env": env or [],
            "Cmd": ["/app/server"],
            "Entrypoint": None,
            "Healthcheck": None,
            "Labels": {},
        },
        "HostConfig": {
            "Memory": 268435456,
            "MemorySwap": 0,
            "NanoCpus": 0,
            "RestartPolicy": {"Name": "always"},
        },
    }
    return json.dumps([doc])
