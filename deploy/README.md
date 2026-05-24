# PodDebugger operator

The **operator** turns PodDebugger into an in-cluster service. Instead of
running `poddebugger analyze` by hand, you create a `PodDiagnosticRequest`
custom resource and the operator fills in its `.status` with a root-cause
analysis — and, behind guardrails, can remediate.

It is the `poddebugger-watch` binary (from [`controller/`](../controller/))
run with `--operator`: it watches `PodDiagnosticRequest` CRs via
`kubectl`, execs the `poddebugger` CLI per request, and patches the
result back onto the CR. No controller-runtime dependency — same
shell-out approach as the rest of the project.

## Contents

| File | Purpose |
|------|---------|
| `crd.yaml` | the `PodDiagnosticRequest` custom resource definition |
| `rbac.yaml` | namespace, ServiceAccount, ClusterRole, ClusterRoleBinding |
| `operator.yaml` | the operator Deployment |
| `example-request.yaml` | a sample `PodDiagnosticRequest` |
| `Dockerfile` | builds the operator image (Go binary + Python CLI + kubectl) |

## Install

```bash
# 1. Build and push the operator image (from the repo root)
docker build -f deploy/Dockerfile -t <registry>/poddebugger-operator:latest .
docker push <registry>/poddebugger-operator:latest
# then set that image in operator.yaml

# 2. Apply the CRD and RBAC
kubectl apply -f deploy/crd.yaml
kubectl apply -f deploy/rbac.yaml

# 3. Provide the LLM key (optional — omit for an OpenAI-compatible/local model)
kubectl -n poddebugger create secret generic poddebugger-llm \
  --from-literal=api-key=sk-ant-...

# 4. Deploy the operator
kubectl apply -f deploy/operator.yaml
```

The operator needs **no privileged access and no custom SCC** — it only reads
workload state and, for the `restart` remediation, deletes a pod. OpenShift's
default `restricted-v2` SCC is sufficient.

### LLM configuration

`operator.yaml` sets `PODDEBUGGER_LLM_PROVIDER` and the API key for the
investigation's default model. To run individual agents on different models
(see [AGENT_HARNESS.md](../AGENT_HARNESS.md)), add `PODDEBUGGER_<ROLE>_LLM_*`
entries to the operator container's `env:` block — for example:

```yaml
- name: PODDEBUGGER_VERIFIER_LLM_PROVIDER
  value: anthropic
- name: PODDEBUGGER_VERIFIER_LLM_MODEL
  value: claude-opus-4-7
```

### Custom agents in the operator

To extend the agent team in-cluster, package your custom `Agent` subclasses
into the operator image and point `PODDEBUGGER_EXTRA_AGENTS` at their dotted
import paths via the Deployment env:

```yaml
- name: PODDEBUGGER_EXTRA_AGENTS
  value: mypkg.metrics.MetricsAgent,mypkg.runbooks.RunbookAgent
```

See [AGENT_HARNESS.md](../AGENT_HARNESS.md) and
[examples/custom_agent.py](../examples/custom_agent.py).

## Use

```bash
kubectl apply -f deploy/example-request.yaml
kubectl get pdr                       # PHASE column fills in
kubectl get pdr diagnose-foo -o yaml  # full .status: rootCause, fixes, ...
```

## Guardrails — `spec.remediationMode`

| Mode | Behavior |
|------|----------|
| `SuggestOnly` *(default)* | Diagnose only. `.status.suggestedFixes` is populated; nothing is changed. |
| `ApproveRequired` | Diagnose + ask the Remediator agent to propose a catalog action. Phase `AwaitingApproval`. Set `spec.approved: true` to apply the proposal (or `spec.remediationAction` to override it first). |
| `AutoRemediate` | Diagnose + propose + apply, provided the proposal's risk is at or below `spec.maxAutoRisk` and the action is in `spec.allowedActions` (if set). Higher-risk proposals fall back to `AwaitingApproval` automatically. |

Remediation actions are a **fixed whitelist**, never LLM-generated commands
— they come from the action catalog (`restart`, `scale`,
`set-resources`, `adjust-probe`, `rollback`; see
[HLD §12](../HLD.md#12-active-remediation-phase-7)). Each action has a typed
parameter schema, bounds/blast-radius checks, dry-run plans (old → new), and
reversal capture. The LLM's free-text `suggestedFixes` are advisory output
for humans — they are not executed.

### Picking the action

Two ways to choose what runs:

1. **Let the Remediator agent pick it.** Leave `spec.remediationAction` empty.
   On a non-`SuggestOnly` request the operator runs `poddebugger analyze
   --fix`, which validates the agent's proposal against the catalog and
   stores it under `.status.proposedRemediation` (action, params, risk,
   rationale, expected effect, reversal). Under `ApproveRequired` you review
   the proposal first; under `AutoRemediate` the operator applies it if
   `risk ≤ spec.maxAutoRisk`.

2. **Override manually.** Set `spec.remediationAction` (and any
   `spec.remediationParams`) yourself. The override always wins — even the
   risk gate is bypassed for an explicit user choice (the
   `spec.allowedActions` whitelist still applies).

### Risk-tier policy

`spec.maxAutoRisk` (`low` *(default)* | `medium`) is the ceiling for
unattended actions. A proposal whose risk exceeds the ceiling is **not**
applied — the request transitions to `AwaitingApproval` and `.status.message`
explains the refusal. Override the ceiling explicitly to opt into medium-risk
auto-remediation (e.g. `set-resources`, `adjust-probe`, `rollback`).

### `.status.proposedRemediation`

```yaml
status:
  proposedRemediation:
    action: scale
    params: {replicas: "3"}
    risk: low
    rationale: "increase capacity — connection refusals correlate with load"
    expectedEffect: "fewer 503s; readiness probe should pass"
    confidence: 0.7
    validated: true
    reversal:
      action: scale
      params: {replicas: "2"}
```

When the agent returns `action: "none"`, `reason` carries why. When a
proposal fails catalog validation, `validationError` carries the message and
`validated` is `false`.

### Post-remediation verification

After applying a remediation, the operator re-checks the workload and
records the outcome on `.status.remediation.verification`:

```yaml
status:
  remediation:
    action: scale
    executed: true
    result: "deployment/web scaled to 3"
    verification:
      outcome: recovered          # recovered | still-failing | unknown | skipped
      reason: "workload is running and stable (restart_count=2)"
      waitedSeconds: 5
```

The CRD also surfaces the outcome as the `Outcome` printer column on
`kubectl get pdr`. Kubernetes `restart` is honestly reported as
`unknown` — the pod was deleted; its replacement has a different name.

## CLI ↔ operator approvals — same model, two surfaces

The interactive CLI prompts ([HLD §16](../HLD.md#16-human-in-the-loop-interactive-approvals-phase-11))
and the operator's CRD guardrails are two flavors of the **same**
human-in-the-loop pattern:

| Interactive (CLI)              | Declarative (operator / CRD)                    |
|---|---|
| TTY prompt `[Y]es once`        | A human edits the CR — `spec.approved: true`    |
| `--yes` (auto-approve this run)| `spec.remediationMode: AutoRemediate` *(subject to `maxAutoRisk` + `allowedActions`)* |
| `--no-prompt` (refuse anything not pre-approved) | `spec.remediationMode: SuggestOnly` |
| Persistent rule in `~/.config/poddebugger/approvals.json` | `spec.allowedActions: [...]` + `spec.maxAutoRisk` on the CR, or a `PodDebuggerApprovalPolicy` rule (below) |
| `deny` rule in the rules file  | `PodDebuggerApprovalPolicy` rule with `decision: deny` |

The same model applies on both surfaces — the cluster-wide policy CRD
described next is the operator-side equivalent of the CLI rules file.

## Cluster-wide approval policy

`PodDebuggerApprovalPolicy` is a **cluster-scoped** CRD that lets a
platform / SRE team pre-approve, refuse, or downgrade catalog actions
across many `PodDiagnosticRequest`s at once — the operator equivalent of
the CLI's `~/.config/poddebugger/approvals.json` rules file. See
[example-policy.yaml](example-policy.yaml) for a copy-and-edit starter
and [HLD §17](../HLD.md#17-cluster-wide-approval-policy-phase-12-) for
the design.

```yaml
apiVersion: aiops.poddebugger.io/v1alpha1
kind: PodDebuggerApprovalPolicy
metadata:
  name: prod-allowlist
spec:
  scope:
    namespaceSelector:
      matchLabels: {tier: prod}
  rules:
    - {kind: remediation, action: restart,       decision: allow}
    - {kind: remediation, action: rollback,      decision: deny}
    - {kind: remediation, action: set-resources, decision: requires-approval}
```

**Decision values**:

| Decision | Effect under AutoRemediate |
|---|---|
| `allow` | The action runs; bypasses the CR's `maxAutoRisk` (the policy is the platform admin's explicit OK). |
| `deny` | The CR transitions to `AwaitingApproval` with a `refused by PodDebuggerApprovalPolicy` message. A human can still set `spec.approved=true` to apply anyway. |
| `requires-approval` | Downgrades AutoRemediate → ApproveRequired for this action — the action waits for `spec.approved=true`. |

**Precedence ladder** (HLD §17.2 — top wins):

1. Explicit `spec.allowedActions` on the CR (opt-out — the CR owner's
   list is authoritative).
2. Matching `PodDebuggerApprovalPolicy` rules (filtered by
   `scope.namespaceSelector` and then by `kind` + `action`). When
   multiple match, **deny wins** over **requires-approval** wins over
   **allow**.
3. Default behavior: the CR's `spec.maxAutoRisk` gate.

When the CR's `spec.allowedActions` permits an action that a policy
*would have denied*, the operator still honors the CR but appends a
`NOTE: PodDebuggerApprovalPolicy denied ...` line to
`.status.message` so the divergence is visible during audit.

**Match semantics** (mirrors the CLI rules file):
- `scope.namespaceSelector.matchLabels` (empty = match every namespace)
- `rules[].action` empty = match every catalog action (catch-all)
- `rules[].expires` (ISO date) — expired rules are silently ignored;
  malformed values are treated as expired so the admin notices on
  `kubectl get pdap`.

**Cluster-scoped** so a single CRD can apply to many namespaces.
**Read-only** for the operator (no write verbs needed beyond the
existing PDR RBAC — see [rbac.yaml](rbac.yaml)). The operator degrades
gracefully when the CRD isn't installed — older clusters without it
work unchanged.

```bash
# Install + apply the example
kubectl apply -f deploy/crd.yaml -f deploy/example-policy.yaml
kubectl get pdap

# Label a namespace so a scoped policy starts applying
kubectl label namespace app-prod tier=prod
```

## Freeform `shell` action — operator considerations

The opt-in `shell` action (see
[HLD §12.9](../HLD.md#129-the-opt-in-shell-action)) is *off by default* in
the operator path too. The operator never sets `--allow-shell` itself;
to enable it, set `PODDEBUGGER_ALLOW_SHELL=1` in the operator
Deployment's env. Even then, the standard precedence ladder applies:

- The CRD's `spec.maxAutoRisk` defaults to `low`, which excludes `shell`
  (risk `high`). A `shell` proposal under `AutoRemediate` falls back to
  `AwaitingApproval` until a human sets `spec.approved: true`.
- A `PodDebuggerApprovalPolicy` with
  `{decision: deny, action: shell}` is the cluster-wide off switch.
- RBAC: the existing `pods/exec: [create]` verb in
  [rbac.yaml](rbac.yaml) already covers `shell` (same primitive as deep
  inspection). No new permission needed.

The combination — env-flag opt-in, `high` risk gate, policy CRD veto,
existing RBAC — is the operator's analog of the CLI's `--allow-shell` +
approval gate. Enable it only when your team needs the escape hatch.

## CR phases

`Pending → Complete` (SuggestOnly) · `→ AwaitingApproval → Remediated`
(ApproveRequired) · `→ Remediated` (AutoRemediate) · `Failed` on error.
Under `AutoRemediate`, a refused proposal lands in `AwaitingApproval` rather
than `Failed` — a human can still approve it manually.

## Not included

- Helm chart / OLM bundle (plain manifests only).
- Validated against a live cluster — the manifests and operator are built and
  statically checked; an end-to-end run needs a reachable cluster.
- Per-rule `target.name` matching on `PodDebuggerApprovalPolicy` (the
  current schema matches by namespace selector + action only; pod-name
  regex matching is a follow-up).
