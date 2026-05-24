# PodDebugger — Quickstart

Get from zero to a root-cause analysis in a few minutes. For the design see
[HLD.md](HLD.md); for the roadmap see [TODO.md](TODO.md).

---

## 1. Prerequisites

| Tool | Needed for | Check |
|------|-----------|-------|
| Python 3.10+ | the agent CLI | `python3 --version` |
| Podman 4.x+ | the target runtime — Podman platform | `podman --version` |
| `kubectl` or `oc` | the target runtime — Kubernetes/OpenShift platform | `kubectl version --client` |
| An LLM backend | the analysis step — a cloud API key, or a local Ollama/llama.cpp server (skip with `--no-llm`) | — |
| Go 1.22+ | building the crash watcher — optional | `go version` |

You only need the runtime tool for the platform you target — **Podman** *or*
**kubectl/oc**. Go is needed only to build the optional crash watcher
([§7](#7-the-crash-watcher)); the analysis CLI itself needs only Python.

---

## 2. Install the agent (in a virtualenv)

Create and activate a virtual environment so dependencies stay isolated:

```bash
# from the repo root
python3 -m venv .venv
source .venv/bin/activate            # Linux/macOS
# .venv\Scripts\activate             # Windows (PowerShell)
```

Then install the framework first and the agent on top — PodDebugger depends
on the `inquiro` framework that ships alongside it:

```bash
pip install -e ./inquiro             # the standalone investigation framework
pip install -e './agent[anthropic]'  # or '[openai]', '[search]', '[all]'
```

`'[search]'` adds the optional `ddgs` library for the web-research backend;
`'[all]'` includes anthropic + openai + ddgs.

This installs the `poddebugger` command **inside the venv**. Activate the venv
(`source .venv/bin/activate`) in any new shell before running it.

> **Using Anaconda/Miniconda?** If your prompt shows `(base)` alongside
> `(.venv)`, conda's `bin/` can shadow the venv — `pip`/`poddebugger` then
> resolve to the wrong place. Run `conda deactivate` first so only `(.venv)`
> is active, and confirm with `which pip python` (both should be under
> `.venv/`).

To run without installing — still inside the activated venv:

```bash
cd agent && python -m poddebugger ...
```

> Local-LLM extras: `ollama` and `llamacpp` providers reuse the OpenAI client,
> so install with `'.[openai]'` (or `'.[all]'`) to use them.

---

## 3. Create a sample broken container

First, give the agent something to diagnose. This container starts, fails to
reach a database, and exits non-zero — a realistic crash:

```bash
podman run --name pd-sample docker.io/library/alpine:latest sh -c '
  echo "boot: starting payments-service v1.4.2"
  echo "config: DB_HOST=db DB_PORT=5432"
  echo "ERROR: dial tcp db:5432: connect: connection refused" >&2
  echo "FATAL: could not initialise database pool, exiting" >&2
  exit 1
'
```

It exits immediately; that's expected. Confirm it's there:

```bash
podman ps -a --filter name=pd-sample
```

You can verify PodDebugger can see it before wiring up an LLM — this needs
no API key and no server:

```bash
poddebugger analyze pd-sample --no-llm
```

That prints the status, events, logs, and spec the agent would analyze.

### Other failure modes to try

```bash
# Out-of-memory style crash
podman run --name pd-oom --memory 16m docker.io/library/alpine:latest sh -c '
  echo "allocating buffers..."; cat /dev/zero | head -c 100m | tail
'

# CrashLoop-style: bad command
podman run --name pd-badcmd docker.io/library/alpine:latest /usr/bin/does-not-exist
```

---

## 4. Pick an LLM backend

PodDebugger supports cloud APIs and **local inference servers**.

**Recommended: use a `.env` file** so settings persist across shells:

```bash
cp .env.example .env      # then edit .env
```

The shipped `.env` defaults to local **Ollama** (no key, offline). PodDebugger
finds `.env` automatically by searching upward from where you run it. Each
setting below can go in `.env` *or* be exported in your shell — an exported
variable wins over `.env`.

### Option A — Anthropic Claude (default)

In `.env`: `PODDEBUGGER_LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY=...`
— or export it:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### Option B — OpenAI / Azure / OpenAI-compatible gateway

```bash
export OPENAI_API_KEY=sk-...
export PODDEBUGGER_LLM_PROVIDER=openai
# export PODDEBUGGER_LLM_BASE_URL=https://your-gateway/v1   # optional
```

### Option C — Local server: Ollama or llama.cpp (no API key, runs offline)

```bash
# Ollama — install from https://ollama.com, then pull a model
ollama pull qwen3.5:9b           # any capable instruct model works
poddebugger analyze pd-sample --llm-provider ollama --model qwen3.5:9b

# llama.cpp — run llama-server, then:
poddebugger analyze pd-sample --llm-provider llamacpp --model local-model
```

Defaults: `ollama` → `http://localhost:11434/v1`, `llamacpp` →
`http://localhost:8080/v1`. Override with `PODDEBUGGER_LLM_BASE_URL` if your
server runs elsewhere. Local diagnosis is slower (tens of seconds on CPU) but
free and fully offline — good for air-gapped clusters. Use a capable
instruction-tuned model; tiny models may not return well-formed JSON.

No LLM at all? Stay with `--no-llm` (shown in §3) — it needs no credentials
and no server.

### Option D — a different model per agent (optional)

The investigation runs nine agents (see [AGENT_HARNESS.md](AGENT_HARNESS.md)).
By default they all use the LLM above, but each can be overridden in `.env`
with `PODDEBUGGER_<ROLE>_LLM_PROVIDER` / `_MODEL` / `_BASE_URL`. A common setup
is a fast local default with the judgement-critical agents on a stronger model:

```bash
PODDEBUGGER_LLM_PROVIDER=ollama
PODDEBUGGER_LLM_MODEL=gemma4:e4b
PODDEBUGGER_VERIFIER_LLM_PROVIDER=anthropic
PODDEBUGGER_VERIFIER_LLM_MODEL=claude-opus-4-7
PODDEBUGGER_REPORTER_LLM_PROVIDER=anthropic
PODDEBUGGER_REPORTER_LLM_MODEL=claude-opus-4-7
```

`<ROLE>` is one of `SCOUT PLANNER COORDINATOR ANALYST PROBER VERIFIER AUDITOR
ADJUDICATOR REPORTER`. The `analyze` footer prints what each run used.

### Option E — add your own agent (optional)

The investigation harness is a framework. To add a custom teammate
(a metrics agent, a runbook lookup agent, a ticket-system agent…), subclass
`ActionAgent` and register it via `.env`:

```bash
PODDEBUGGER_EXTRA_AGENTS=mypkg.metrics.MetricsAgent
```

Multiple agents are comma-separated. The Coordinator picks up the new action
automatically; per-agent LLM routing works for custom agents too (use the
agent's `name` for `PODDEBUGGER_<NAME>_LLM_*`). A working template lives at
[examples/custom_agent.py](examples/custom_agent.py); full design in
[AGENT_HARNESS.md](AGENT_HARNESS.md) and [HLD.md §14](HLD.md). The
[examples/](examples/) directory also has hello-worlds for custom approval
gates and the opt-in `shell` action.

---

## 5. Run the investigation

```bash
poddebugger analyze pd-sample
```

`analyze` runs the **multi-agent investigation** (HLD §11): a team of
role-specialized agents — Scout, Planner, Coordinator, Analyst, Prober,
Verifier, Auditor, Adjudicator, Reporter — works the failure over a persistent,
git-tracked investigation. Easy crashes finish in a few iterations; harder ones
loop longer. Expect a root-cause summary, confidence, evidence, and ranked
fixes — plus the path to the run's audit trail.

```bash
poddebugger analyze pd-sample --verbose     # watch the agents work, role by role
poddebugger analyze pd-sample --json        # machine-readable output
poddebugger analyze pd-sample --no-llm      # collected context only, no investigation
poddebugger analyze pd-sample --deep        # thorough mode — larger iteration budget
```

Try the other sample containers too: `poddebugger analyze pd-oom`,
`poddebugger analyze pd-badcmd`.

> On a local Ollama model the investigation makes ~10–20 calls and takes a
> couple of minutes; on a cloud model it is faster. Each run is saved as a git
> repository under `~/.cache/poddebugger/runs/` — one commit per iteration, so
> you can replay exactly how the agents reached the diagnosis.

---

## 6. Kubernetes / OpenShift

The same CLI works against a cluster — only `--platform` changes. PodDebugger
shells out to `kubectl` (or `oc` for OpenShift, auto-detected), so it uses
whatever cluster your current kubeconfig context points at.

```bash
# Kubernetes — diagnose a pod in a namespace
poddebugger analyze web-7c --platform kubernetes -n prod

# OpenShift — prefers the `oc` CLI
poddebugger analyze web-7c --platform openshift -n prod

# Context only, no LLM
poddebugger analyze web-7c --platform kubernetes -n prod --no-llm
```

Notes:

- `--namespace`/`-n` is optional — it falls back to your kubeconfig context's
  namespace, then `default`.
- For a multi-container pod, PodDebugger analyzes the unhealthiest container
  and notes the rest; target a specific one with `--container <name>`.
- For `CrashLoopBackOff`, the **previous** container's logs are collected
  automatically — that's where the crash actually shows up.
- Set the platform persistently in `.env` with `PODDEBUGGER_PLATFORM=kubernetes`.

No local cluster? `kind create cluster` (https://kind.sigs.k8s.io) gives you
one in a minute; deploy a failing pod and point PodDebugger at it.

---

## 7. The crash watcher

`poddebugger-watch` is a small Go control plane that tails the runtime and runs
the analyzer automatically on **every crash** — no manual `analyze` call. The
CLI works without it; this is the always-on option.

### Build it

Needs Go 1.22+ (`go version`). If Go isn't installed:

```bash
GO_VERSION=1.23.4
curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tgz
sudo rm -rf /usr/local/go && sudo tar -C /usr/local -xzf /tmp/go.tgz
export PATH=$PATH:/usr/local/go/bin       # append to ~/.bashrc to persist
```

Then build the binary:

```bash
cd controller
go build -o poddebugger-watch .
```

### Run it

```bash
# Watch Podman — analyze each crashed container
./poddebugger-watch --platform podman

# Watch a Kubernetes namespace
./poddebugger-watch --platform kubernetes --namespace prod

# Test the wiring with no LLM (prints collected context per crash)
./poddebugger-watch --platform podman --no-llm --analyzer ../.venv/bin/poddebugger
```

The watcher execs the `poddebugger` CLI — if it isn't on `PATH` (e.g. it lives
in a venv), point at it with `--analyzer`. A crash-looping workload is analyzed
at most once per `--cooldown` window (default 5m). Full flag reference:
[controller/README.md](controller/README.md).

---

## 8. The operator

For an always-on, declarative setup on Kubernetes/OpenShift, run PodDebugger as
an **operator**. You create a `PodDiagnosticRequest` custom resource; the
operator fills in its `.status` with the diagnosis — and, behind guardrails,
can remediate.

```bash
# 1. Build & push the operator image (from the repo root)
docker build -f deploy/Dockerfile -t <registry>/poddebugger-operator:latest .
docker push <registry>/poddebugger-operator:latest   # set this image in deploy/operator.yaml

# 2. Install
kubectl apply -f deploy/crd.yaml -f deploy/rbac.yaml
kubectl -n poddebugger create secret generic poddebugger-llm \
  --from-literal=api-key=sk-ant-...
kubectl apply -f deploy/operator.yaml

# 3. Request a diagnosis
kubectl apply -f deploy/example-request.yaml
kubectl get pdr                          # PHASE: Pending -> Complete
kubectl get pdr diagnose-foo -o yaml     # full .status
```

`spec.remediationMode` is the guardrail — `SuggestOnly` (default), or
`ApproveRequired` / `AutoRemediate` to let it run a catalog action.
Remediation is a fixed whitelist, never an LLM-generated command. Full notes:
[deploy/README.md](deploy/README.md).

---

## 9. Remediate

PodDebugger ships a **typed action catalog** for mutation: `restart`, `scale`,
`set-resources`, `adjust-probe`, `rollback`. The LLM never emits a command —
it picks an action and proposes parameters, which code validates and bounds-
checks against the schema before anything runs.

```bash
# Always preview first — dry-run is the default
poddebugger remediate web-7c --platform kubernetes -n prod \
    --action scale --param replicas=3

# Sample output:
#   action:  scale  (low risk)
#   target:  deployment/web (ns=prod)
#   summary: scale deployment/web from 2 to 3
#   params:  {'replicas': 3}
#   old → new: {'replicas': 2} → {'replicas': 3}
#   reversal: {'action': 'scale', 'params': {'replicas': 2}}
#
#   dry run — re-run with --confirm to execute

# Execute — add --confirm
poddebugger remediate web-7c --platform kubernetes -n prod \
    --action scale --param replicas=3 --confirm

# Set resources on a Podman container
poddebugger remediate my-app --platform podman \
    --action set-resources --param container=my-app \
    --param memory_limit=512Mi --param cpu_limit=500m
```

Every plan computes a **reversal** — the `{action, params}` a future
`remediate --undo` will replay. Protected namespaces
(`kube-system`, `openshift-*`, …) are always refused. See the catalog table
in [README.md](README.md#remediate) for per-action parameters and
risk tiers.

### Let the agent pick the action

`poddebugger analyze --fix` chains the investigation into the **Remediator**
agent — once the team agrees on a verdict, the Remediator picks a catalog
action that addresses it. The engine validates the proposal against the
catalog before it ever runs.

```bash
# Investigate, then show the proposed remediation alongside the verdict
poddebugger analyze pd-oom --platform podman --fix

# ... and apply it if it's low-risk (restart / scale)
poddebugger analyze pd-oom --platform podman --fix --confirm

# Allow medium-risk too (set-resources / adjust-probe / rollback)
poddebugger analyze web-7c --platform kubernetes -n prod \
    --fix --confirm --max-risk medium
```

If no catalog action fits, the Remediator returns `{"action": "none",
"reason": "..."}` and nothing runs — the team is honest about its limits.

### Roll a fix back & verify it worked

Successful applies are auto-saved, so undo needs no extra flags:

```bash
# Apply something — verification runs automatically (default 5s wait)
poddebugger remediate huli_time-redis --platform podman \
    --action set-resources --param container=huli_time-redis \
    --param memory_limit=256Mi --confirm

# ... realize 256Mi was wrong; roll it back
poddebugger remediate huli_time-redis --platform podman --undo --confirm

# Tune the verification wait, or skip it
poddebugger remediate web-7c -n prod --platform kubernetes \
    --action scale --param replicas=3 --confirm --verify-wait 10
poddebugger remediate web-7c -n prod --platform kubernetes \
    --action scale --param replicas=3 --confirm --no-verify
```

The JSON output (or terminal block) reports the outcome — `recovered`,
`still-failing`, `unknown`, or `skipped` — so you know not just *what*
PodDebugger did but *whether it worked*.

### Look up known issues

The **Librarian** agent can search the web for a known signature matching
your failure. Off by default (air-gap safe). Enable with `--research`.

```bash
pip install 'poddebugger[search]'    # adds the optional `ddgs` library

# Investigate with web research, using the DuckDuckGo backend (no API key)
poddebugger analyze pd-oom --platform podman \
    --research --search-backend duckduckgo
```

The Librarian frames ONE generalized query per call; the engine **redacts**
it (strips IPs, UUIDs, hex IDs, pod-suffix patterns) before any HTTP
request, then runs it through the backend. Hits land in the investigation
as Evidence tagged `web:<domain>` — leads, not authority.

### Approve before acting

Every mutating step — catalog actions and deep-inspection probes — is
gated by an interactive prompt, mirroring Claude Code's permission model.
Default behavior:

| Context | What happens |
|---|---|
| Interactive TTY, no flags | Prompts `[Y]es once / [A]lways (session) / [N]o` before each step. |
| Non-TTY (CI / pipe) | Refuses anything not pre-approved (exit 1, with a hint). |
| `--yes` | Auto-approve everything. Right for trusted automation. |

```bash
# Run on a TTY — you'll be prompted before the restart fires:
poddebugger remediate pd-oom --platform podman --action restart --confirm

# Auto-approve (skip the prompt):
poddebugger remediate pd-oom --platform podman --action restart --confirm --yes

# Pre-approve a class of actions so future runs don't prompt:
poddebugger approvals add --kind remediation --action restart \
    --target-platform podman --target-name pd-oom
poddebugger approvals list           # see what you've allowed
poddebugger approvals check ...      # what would the rules decide?
poddebugger approvals remove 0       # take a rule back

# Persistent mode: the prompt also offers [P]ersist, which saves the rule.
poddebugger remediate pd-oom --platform podman --action restart \
    --confirm --approvals persistent
```

The **same prompts apply** to deep-inspection probes (`analyze --deep`) —
running code in the target container is gated too. The Kubernetes
operator's existing `spec.remediationMode: ApproveRequired` is the async
equivalent.

To plug in your own gate (audit-logger, Slack approver, OPA hook), see
[examples/custom_gate.py](examples/custom_gate.py) — it ships an
`AuditingGate` decorator and a `BusinessHoursGate` policy as starting
points.

### Run an arbitrary command in the container (opt-in)

The typed catalog (`restart` / `scale` / `set-resources` / `adjust-probe`
/ `rollback`) is the default safety boundary. For investigations that
need an escape hatch — `jstack`, an ad-hoc `psql`, a custom recovery
script — opt in to a freeform `shell` action:

```bash
# Off by default — argparse refuses the action:
poddebugger remediate pd-sample --platform podman \
    --action shell --param command="echo hi"
# -> error: argument --action: invalid choice: 'shell' (...)

# Enable for this run:
poddebugger remediate pd-sample --platform podman --allow-shell \
    --action shell --param command="hostname; date" --confirm --yes
```

`shell` is `risk: high` (a new tier above `medium`), so the agent path
needs both `--allow-shell` *and* `--max-risk high`. Set
`PODDEBUGGER_ALLOW_SHELL=1` to enable it for scripted setups.
Walkthrough: [examples/shell_action_demo.py](examples/shell_action_demo.py).

---

## 10. Clean up

```bash
podman rm -f pd-sample pd-oom pd-badcmd 2>/dev/null
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `'podman' is not installed` | Install Podman, or use `--platform kubernetes`/`openshift`. |
| `'kubectl' is not installed` | Install `kubectl` or `oc` for the Kubernetes/OpenShift platform. |
| `anthropic SDK not installed` | `pip install -e '.[anthropic]'` (or `'[openai]'`). |
| `auth failed` | Export the matching `*_API_KEY`; check `PODDEBUGGER_LLM_PROVIDER`. |
| Want to inspect inputs only | Add `--no-llm`. |
| `no podman container or pod named ...` | Check `podman ps -a`; the container may have been pruned. |
| `no pod '...' in namespace ...` | Check `kubectl get pods -n <ns>`; pass the right `-n`. |
| `Unable to connect to the server` | Your kubeconfig context points at an unreachable cluster — check `kubectl cluster-info`. |
| watcher: `executable file not found` | Pass `--analyzer /path/to/poddebugger` (e.g. the venv's `bin/poddebugger`). |
| watcher: analysis fails with "no container" | The container was pruned before analysis ran — real crash-loops persist; avoid `--rm` on test containers. |
