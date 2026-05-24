# poddebugger-watch — Go control plane

The Go control plane: it **watches a container runtime for crashes** and runs
the `poddebugger` analyzer on each one automatically.

It is a thin orchestrator. Crash *detection* lives here (Go); crash *diagnosis*
is delegated to the `poddebugger` CLI — the Python "brain". The contract
between them is simply:

```
poddebugger analyze <target> --platform <p> [--namespace <ns>] --json
```

No gRPC, no long-running service — the watcher just execs the CLI and parses
its `--json` output. Zero external Go dependencies (stdlib only); like the
Python providers, it shells out to `podman` / `kubectl`.

## Build

```bash
export PATH=$PATH:/usr/local/go/bin       # if go isn't on PATH
cd controller
go build -o poddebugger-watch .
```

## Run

```bash
# Watch Podman; analyze each crashed container (needs an LLM key, or --no-llm)
./poddebugger-watch --platform podman

# Watch Kubernetes for CrashLoopBackOff / OOMKilled / image-pull failures
./poddebugger-watch --platform kubernetes --namespace prod

# Test wiring without an LLM — prints collected context instead of a diagnosis
./poddebugger-watch --platform podman --no-llm
```

The `poddebugger` CLI must be reachable. If it is not on `PATH` (e.g. it lives
in a venv), point at it explicitly:

```bash
./poddebugger-watch --analyzer /path/to/.venv/bin/poddebugger
```

## Flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--platform` | `podman` | runtime to watch: `podman` or `kubernetes` |
| `--analyzer` | `poddebugger` | path to the poddebugger CLI |
| `--kubectl` | `kubectl` | `kubectl` or `oc` binary (kubernetes platform) |
| `--namespace` | _(all)_ | kubernetes namespace to watch |
| `--cooldown` | `5m` | per-target re-analysis cooldown (crash-loop debounce) |
| `--timeout` | `3m` | max time for one analysis |
| `--no-llm` | `false` | pass `--no-llm` to the analyzer (context only) |

## How it works

```
podman events / kubectl get pods --watch   →   crash detected
                                                     │
                                            per-target cooldown
                                                     │
                              exec: poddebugger analyze … --json
                                                     │
                                       parse Diagnosis → print CRASH ALERT
```

- **Podman** — tails `podman events`; a container `died` with a non-zero exit
  code is a crash.
- **Kubernetes** — tails `kubectl get pods --watch -o json`; a pod in
  `CrashLoopBackOff`, an image-pull failure, or `OOMKilled` is a crash.
- **Cooldown** — a crash-looping workload fires many events; each target is
  analyzed at most once per `--cooldown` window.

## Status

- **Podman watcher** — implemented and tested end-to-end.
- **Kubernetes watcher** — implemented (stdlib `kubectl` watch); not yet
  validated against a live cluster.
- **Operator + CRD + guardrailed remediation** — built; see
  [../deploy/](../deploy/).
- **Remediation action catalog** — the watcher and operator invoke
  `poddebugger remediate` with any of the catalog actions (`restart`,
  `scale`, `set-resources`, `adjust-probe`, `rollback`) plus the
  appropriate `--param`s; the operator picks the action via
  `spec.remediationAction` on the CR or the Remediator agent's proposal.
- **Cluster-wide approval policy** — the `PodDebuggerApprovalPolicy`
  CRD lets platform admins pre-approve / refuse / downgrade catalog
  actions across many PDRs. Evaluation lives in [policy.go](policy.go);
  example resource in
  [../deploy/example-policy.yaml](../deploy/example-policy.yaml).
