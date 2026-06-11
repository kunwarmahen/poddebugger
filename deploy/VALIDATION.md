# Live cluster validation log

First-ever validation of PodDebugger's Kubernetes surface against a real
cluster (IMPROVEMENTS.md §3.1; previously every K8s path was
fixture-tested only).

**Environment** (2026-06-11): kind v0.23.0 on rootless Podman 4.9.3
(`KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name
pd-validate`), Kubernetes via kindest/node default for v0.23, kubectl
client present, LLM = local Ollama `qwen3.5:9b`. Test workloads: a bare
crash-loop pod (`pd-crash`, exits on a missing config file), an OOM pod
(`pd-oom`, 16Mi limit + memory hog), and a Deployment (`pd-app`) that
crash-loops until `APP_TOKEN` is set.

## Results

| Surface | Result | Notes |
|---|---|---|
| `KubernetesProvider` collect (crash-loop) | ✅ | status `Running / terminated: Error`, restart count, exit 1, 9 events incl. `Warning/BackOff`, current logs; previous-logs request degrades gracefully when containerd GC'd the old container |
| `KubernetesProvider` collect (OOM) | ✅ | `exit_code: 137`, `oom_killed: true` read from `lastState` as designed; backoff error surfaced |
| Multi-agent analyze (LLM) | ✅ | Scout classified `OOMKilled` live; full diagnosis produced |
| `remediate --action restart` | ✅ | pod deleted; Deployment controller recreated it |
| `remediate --action set-env` | ✅ | owner Deployment resolved from the pod, strategic-merge patch applied, reversal captured, secret masked in summary; **workload actually recovered** (crash-loop → Running 1/1); verification honestly `unknown` (new pod name) per 7D design |
| `remediate --action scale` | ✅ | Deployment 1 → 2, both pods Running |
| `--undo` state file | ✅ | written to `~/.cache/poddebugger/state/` keyed by target |
| Go watcher (`--platform kubernetes`) | ✅ | `kubectl get pods --watch` stream parsed; detected `pd-crash` CrashLoopBackOff immediately and exec'd the analyzer |
| CRDs (`deploy/crd.yaml`) | ✅ | both CRDs applied cleanly; `pdr` short name + printer columns render |
| Catalog guard on bare pods | ✅ | OOM pod's `analyze --fix` proposal correctly collapsed to `none` — "pod has no workload controller — cannot mutate it"; validation error preserved for audit |
| Operator reconcile (SuggestOnly) | ✅ | `diag-crash` → phase `Complete`, confidence 0.95, accurate root cause patched into `.status`; printer columns populate |
| Operator crash resilience | ✅ | first operator run hit its process timeout mid-reconcile: clean shutdown, CR left unpatched, next operator instance re-reconciled it from scratch (stateless design held) |
| Operator AutoRemediate (7C) | ✅ | `diag-auto` (explicit `remediationAction: restart`) → analyzed, auto-applied under the risk gate, pod deleted + recreated by its controller, phase `Remediated`; honest `unknown` verification outcome (new pod name) — required the `--yes` fix below |
| Phase 12 policy deny | ✅ | cluster-wide `deny restart` policy honored under AutoRemediate: CR landed in `AwaitingApproval` (not `Failed`) with the "set spec.approved=true to apply anyway" hint — the human can still override, per HLD §17 |
| Human override of a policy deny | ✅ | `kubectl patch … spec.approved=true` → phase `Remediated`, restart applied, `status.remediation.savedTo` populated (camelCase encoding confirmed on the wire) |

## Bugs found and fixed during validation

1. **`set-env`/`recreate` unreachable via `--param`** — `_parse_env_map`
   required a real dict, but the CLI's `--param env=…` and the operator's
   `spec.remediationParams` can only deliver *strings*, so these actions
   could never be invoked from those surfaces (the Remediator path passes
   real JSON and was unaffected). Fixed: the parsers now accept JSON
   object/array strings (`remediation.py: _parse_env_map`,
   `_as_str_list`); regression tests added.
2. **Operator remediations always refused by the approval gate** — a
   Phase 5/7C ↔ Phase 11 integration gap: the operator execs
   `poddebugger remediate --confirm` without a TTY, where the Phase 11
   gate denies by default, so every AutoRemediate/approved remediation
   ended `Failed: refused by approval gate (deny)`. The CR is the human's
   durable authorization, so `RunRemediation` now passes `--yes`
   (`controller/analyzer.go`; args extracted to `remediationArgs` with a
   Go regression test).
3. **`controller/types.go` had drifted out of gofmt** (struct-tag
   alignment) — `gofmt -w` applied.
4. **Status patch rejected when the proposal carried object params** — the
   Remediator proposed `set-env` (params.env is an object), but the CRD
   declared `proposedRemediation.params` as a *string* map → the whole
   status patch failed and the CR was left phaseless. Fixed: `params` is
   now `x-kubernetes-preserve-unknown-fields: true` (13B actions carry
   maps and lists); `remediation.savedTo` added to the schema while there.
5. **Status patch leaked the CLI's snake_case field names** —
   `expected_effect` / `saved_to` / `waited_seconds` were silently pruned
   by the structural schema (the CRD names them camelCase). The Go types
   now decode snake_case (CLI side) but encode camelCase via custom
   `MarshalJSON` (status side), per the contract already documented in
   `types.go`; Go regression test added.

## Reproducing

```bash
go install sigs.k8s.io/kind@v0.23.0
KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster --name pd-validate
kubectl apply -f deploy/crd.yaml
# … create failing pods, PDRs, policies as above …
KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name pd-validate
```

The operator was run *locally* against the cluster:
`PODDEBUGGER_LLM_PROVIDER=ollama PODDEBUGGER_LLM_MODEL=qwen3.5:9b
./poddebugger-watch --operator --timeout 12m` (with the Python CLI on
PATH). Size the `--timeout` to your model — a local 9B model needs
8–12 minutes per analysis.

## Caveats / not yet validated

- OpenShift (`oc`) — binary detected, no cluster available.
- In-cluster operator Deployment (`deploy/operator.yaml` image build +
  RBAC under a real ServiceAccount) — the operator was validated running
  *locally* against the cluster, which exercises the same kubectl calls
  but not the in-cluster image/RBAC packaging.
- `kubectl debug` Coder sandbox on K8s — needs the sandbox image in a
  registry the cluster can pull from.
