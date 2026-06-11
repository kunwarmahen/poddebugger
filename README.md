# PodDebugger

**An AI agent that diagnoses failing pods and containers — and, with
guardrails, fixes them.**

PodDebugger inspects a misbehaving workload (Podman or Kubernetes /
OpenShift), runs a team of role-specialized LLM agents over the collected
evidence, and produces a structured root-cause analysis. It can stop
there, propose a typed remediation from a fixed action catalog, or apply
the fix under explicit human approval.

Built on [`inquiro`](inquiro/) — a small, domain-agnostic multi-agent
investigation framework that ships alongside PodDebugger and can be reused
for other diagnostic problems (a [log-file investigator
example](examples/log_investigator/) is included).

---

## Highlights

- **Multi-agent investigation.** A team of nine specialized agents (Scout,
  Planner, Coordinator, Analyst, Prober, Verifier, Auditor, Adjudicator,
  Reporter) cooperates over a persistent, git-tracked investigation
  workspace.
- **Pluggable LLM backend.** Anthropic, OpenAI / Azure / OpenAI-compatible
  gateways, local [Ollama](https://ollama.com), and local
  [llama.cpp](https://github.com/ggml-org/llama.cpp). Each agent can use a
  different model.
- **Pluggable platform.** Podman today; Kubernetes and OpenShift via
  `kubectl` / `oc`. The agent core never shells out directly — every
  runtime call goes through a small provider interface.
- **Typed remediation catalog.** `restart`, `scale`, `set-resources`,
  `adjust-probe`, `rollback`, `set-env`, `set-image`, `recreate`. The LLM
  picks an action and proposes parameters; the catalog validates,
  bounds-checks, dry-runs (old → new), and captures a reversal for
  one-command undo.
- **Adaptive remediation.** A fix that doesn't recover the workload
  becomes evidence; the team replans and the agent tries something
  different — until it's actually fixed or the agent gives up. When a fix
  needs a value the agent can't infer (a password, an image name), it
  asks for it via `--context`.
- **Human-in-the-loop approvals.** Every mutating step (catalog action
  *or* deep-inspection probe that runs code in the container) is gated
  by an interactive `[Y]es / [A]lways / [P]ersist / [N]o` prompt,
  mirroring Claude Code's permission model. A persistent rules file lets
  you pre-approve trusted actions.
- **Crash watcher.** A small Go binary tails `podman events` or
  `kubectl get pods --watch` and analyzes every crash automatically.
- **Kubernetes operator.** Declarative `PodDiagnosticRequest` CRD with
  three guardrail modes (`SuggestOnly` / `ApproveRequired` /
  `AutoRemediate`) plus a cluster-wide `PodDebuggerApprovalPolicy` CRD
  for platform-team-owned allow / deny / requires-approval rules.
- **Web research (opt-in).** A Librarian agent looks up known issues
  via a `SearchBackend` (DuckDuckGo ships; ABC is pluggable). Queries
  are redacted (IPs, UUIDs, pod-suffix patterns) before they leave the
  host. Off by default — air-gap safe.
- **Spawns specialists on the fly (opt-in).** With `--specialists`, the
  Coordinator can mid-run conjure a domain expert — "PostgreSQL crash
  analysis", "JVM heap tuning" — writing its charter itself; the charter
  becomes the new agent's system prompt (the LLM prompting the next LLM
  call). Specialists are advisory only and budgeted per run; every
  generated prompt is committed to the run workspace for audit.
- **Learns from past incidents (opt-in).** With `--learn`, verified
  remediation outcomes are remembered (redacted, local JSON); on later
  runs a Historian step recalls similar past incidents — including
  fixes that did NOT work — so the team starts from experience instead
  of a blank slate. Learning only changes what the LLM is *told*, never
  what it is *allowed* to do.
- **Prompts as data + self-optimization (offline).** Agent system
  prompts can live in a versioned *prompt pack* (`poddebugger prompts
  dump`, `analyze --prompt-pack`). A deterministic scenario suite
  (`poddebugger eval`) scores investigation quality against known
  failures, and `poddebugger optimize` lets an LLM critic evolve the
  pack — an edit is kept only if the score strictly improves, and you
  review the diff before trusting it.

---

## Install

Use a virtualenv to keep dependencies isolated:

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# Install the framework first, then the application:
pip install -e ./inquiro
pip install -e './agent[anthropic]'  # extras: [openai] [search] [all]
```

| Extra | What it adds |
|---|---|
| `[anthropic]` | The Anthropic Python SDK |
| `[openai]`    | The OpenAI Python SDK (also covers Azure / vLLM / any OpenAI-compatible gateway) |
| `[search]`    | The `ddgs` library, required by the DuckDuckGo search backend |
| `[all]`       | All of the above |

Local LLM backends (Ollama, llama.cpp) need **no API key** and no extras —
they're plain HTTP. See [QUICKSTART.md](QUICKSTART.md) for a setup walkthrough.

---

## Usage

### Diagnose

```bash
export ANTHROPIC_API_KEY=sk-ant-...

# Podman
poddebugger analyze my-broken-container

# Kubernetes (uses your current kubeconfig context)
poddebugger analyze web-7c --platform kubernetes -n prod

# OpenShift (prefers the `oc` CLI)
poddebugger analyze web-7c --platform openshift -n prod

# Show the team's role-by-role progress
poddebugger analyze my-broken-container --verbose

# Thorough mode — raise the iteration budget
poddebugger analyze my-broken-container --deep

# Dump the collected context only — no LLM, no API key needed
poddebugger analyze my-broken-container --no-llm

# Machine-readable output
poddebugger analyze my-broken-container --json
```

### Diagnose, then fix — and keep trying until it's fixed

`--fix` doesn't stop at the first attempt. If a fix doesn't recover the
workload, the failure becomes evidence, the whole team replans, and the
agent tries something different — up to `--max-attempts` (default 3) —
until the workload recovers or it honestly gives up.

```bash
# Investigate, then ask the Remediator to propose a catalog action.
# Nothing runs yet — the proposal is printed.
poddebugger analyze my-broken-container --fix

# Same, plus apply — and loop until recovered (or out of attempts).
poddebugger analyze my-broken-container --fix --confirm

# Allow medium-risk actions (set-resources / set-env / set-image / …) too.
poddebugger analyze web-7c --platform kubernetes -n prod \
    --fix --confirm --max-risk medium
```

Some fixes need a value the agent can't infer — a database password, the
correct image name, a missing config value. Supply it with `--context`;
if you don't, the agent tells you exactly what it needs:

```bash
# The agent reads the logs, sees a missing env var, and uses your value.
poddebugger analyze mysql-0 --platform podman --fix --confirm \
    --context MYSQL_ROOT_PASSWORD=secret --context MYSQL_DATABASE=app

# If you omit a value the fix needs, the run ends with, e.g.:
#   Remediation needs values you must supply.
#     • MYSQL_ROOT_PASSWORD: the MySQL root password for set-env
#   Re-run with --context MYSQL_ROOT_PASSWORD=…
```

The result prints the attempt trail — what was tried, and what each fix's
recovery check returned (`recovered` / `still-failing` / `unknown`). The
catalog actions that change a container's definition — `set-env`,
`set-image`, `recreate` — round out the typed toolbox the agent picks
from. See [examples/adaptive_remediation_demo.py](examples/adaptive_remediation_demo.py)
for a runnable walk-through.

### Apply a catalog action directly

```bash
# Preview a plan (dry-run is the default — nothing executes)
poddebugger remediate web-7c --action restart --platform kubernetes -n prod
poddebugger remediate web-7c --action scale --param replicas=3 \
    --platform kubernetes -n prod

# Execute — add --confirm; you'll be prompted to approve.
poddebugger remediate web-7c --action scale --param replicas=3 \
    --platform kubernetes -n prod --confirm

# Roll the most recent action back. No path needed — the auto-saved
# reversal file is found by target.
poddebugger remediate web-7c --platform kubernetes -n prod --undo --confirm
```

The catalog at a glance:

| Action | Risk | Podman | Kubernetes | Parameters |
|---|---|---|---|---|
| `restart` | low | ✓ | ✓ | — |
| `scale` | low | — | ✓ | `replicas` |
| `set-resources` | medium | ✓ | ✓ | `container`, plus `memory_limit?` / `cpu_limit?` / `memory_request?` (K8s) / `cpu_request?` (K8s) |
| `adjust-probe` | medium | — | ✓ | `container`, `probe` (liveness/readiness/startup), `initial_delay?` / `period?` / `timeout?` / `failure_threshold?` |
| `rollback` | medium | — | ✓ | `revision?` |
| `set-env` | medium | ✓ | ✓ | `container`, `env` (object; a null value deletes the key) |
| `set-image` | medium | ✓ | ✓ | `container`, `image` |
| `recreate` | high | ✓ | ✓ | `container`, plus any of `image` / `env` / `command` / `args` |

A freeform `shell` action is also available behind `--allow-shell` (see
[below](#freeform-shell-action-opt-in)).

Every plan computes an `old → new` diff, a reversal payload, and runs a
post-execution recovery check (`recovered` / `still-failing` / `unknown`
/ `skipped`). Protected namespaces (`kube-system`, `openshift-*`, …) are
always refused.

### Approve before acting

Every mutating step prompts before it runs:

```text
────────────────────────────────────────────────────────────
PodDebugger wants to remediation → restart  (risk: low)
  target:  web  [podman]
  summary: restart container web
  old → new: {'state': 'running-or-failed'} → {'state': 'restarted'}
────────────────────────────────────────────────────────────
[Y]es once / [A]lways (this session) / [N]o > _
```

| Flag | Behavior |
|---|---|
| (default) | TTY: prompt. Non-TTY: refuse with a hint. |
| `--yes`   | Auto-approve everything. For trusted automation. |
| `--no-prompt` | Never prompt; refuse anything not pre-approved by a rule. CI-friendly. |
| `--approvals session` *(default)* | Prompt + remember in-memory; rules file IS consulted. |
| `--approvals persistent` | Prompt also offers `[P]ersist` — saves the rule to disk. |
| `--approvals off` | Ignore the rules file entirely. |

Manage persistent rules with the `approvals` sub-command:

```bash
poddebugger approvals add --kind remediation --action restart \
    --target-platform podman --target-name web
poddebugger approvals list
poddebugger approvals check --kind remediation --action restart \
    --target-platform podman --target-name web
poddebugger approvals remove 0
```

Rules live at `$XDG_CONFIG_HOME/poddebugger/approvals.json` (default
`~/.config/poddebugger/approvals.json`). `deny` always wins over `allow`
on the same descriptor; rules may carry an optional `expires` ISO date.

### Freeform shell action (opt-in)

The typed catalog enforces a deliberate property: the LLM picks a fixed
action name and proposes typed parameters; the engine shells out via
hard-coded argv. For diagnostic flows where a typed action isn't enough
(`jstack`, `psql`, a custom recovery script), opt in to a `shell` action
whose `command` parameter runs verbatim inside the target container.

```bash
# Off by default — argparse refuses the action without --allow-shell:
poddebugger remediate web --platform podman --action shell \
    --param command="echo hello"
# -> error: invalid choice: 'shell'

# Enable for this run:
poddebugger remediate web --platform podman --allow-shell \
    --action shell --param command="hostname; date" --confirm --yes
```

Trade-offs to be aware of:

- The catalog-membership safety boundary is replaced by the **approval
  gate** — denials still apply, non-TTY still refuses without `--yes`.
- The action is `risk: high`. Auto-apply via `analyze --fix --confirm`
  requires both `--allow-shell` **and** `--max-risk high`.
- No automatic reversal; post-remediation verification is skipped.

Combined stdout+stderr from the command is captured (truncated at 4000
chars) and surfaced in the result. See
[`examples/shell_action_demo.py`](examples/shell_action_demo.py) for a
runnable walk-through. The setting can also be enabled with
`PODDEBUGGER_ALLOW_SHELL=1` for scripted setups.

### Look up known issues (opt-in)

```bash
pip install 'poddebugger[search]'           # installs the `ddgs` library

# Enable web research for this run
poddebugger analyze my-broken-container --research --search-backend duckduckgo
```

The Librarian agent frames *one generalized query* per call; the engine
redacts it (strips IPs, UUIDs, hex IDs, pod-suffix patterns) before
issuing it. Results land as evidence tagged `web:<domain>` — leads, not
authority. The default backend is `noop` (air-gap safe). Plug your own
with `PODDEBUGGER_SEARCH_BACKEND=mypkg.mymod.MyBackend`.

### Spawn domain specialists mid-run (opt-in)

```bash
poddebugger analyze my-broken-container --specialists
```

When enabled, the Coordinator's menu gains a `specialist` action: it names
a specialty ("PostgreSQL crash analysis") and writes the charter, and the
engine composes a new agent's system prompt from both — dynamic prompting,
on the fly. Specialists are **advisory only**: ordinary agents whose output
lands as evidence and leads tagged `dynamic:<slug>` — they cannot probe,
act, or touch the remediation catalog, so enabling them adds no new
capabilities. Budget: 2 unique specialties per run (re-consulting one is
free). Every composed prompt is persisted to `specialists/<slug>.md` inside
the run workspace and captured by the per-iteration git commit, so runs
stay replayable and auditable.

### Evaluate, then evolve the prompts (offline)

```bash
# Score the agent team against known failure scenarios (needs Podman + an LLM)
poddebugger eval --llm-provider ollama --model qwen3.5:9b

# Materialize the built-in prompts as a versioned pack…
poddebugger prompts dump ./prompt-pack && git -C ./prompt-pack init -q && \
    git -C ./prompt-pack add -A && git -C ./prompt-pack commit -qm baseline

# …let an LLM critic evolve it against the eval suite…
poddebugger optimize --pack ./prompt-pack --rounds 3 \
    --llm-provider ollama --model qwen3.5:9b

# …review what changed, then use the pack
git -C ./prompt-pack diff
poddebugger analyze my-broken-container --prompt-pack ./prompt-pack
```

The eval suite (`missing-env`, `crash-loop`, `oom`, `bad-command`, `dns`)
stands real failing containers up and scores the diagnosis
deterministically: 1 point for the right failure classification, 1 point
for proposing an acceptable catalog action — **propose-only, nothing is
ever applied**. The optimizer adopts a critic's edit only when the suite
score strictly improves, writes it as a plain file change in the pack,
and never touches prompts during a normal `analyze` run. Pack loading is
validated (size caps, JSON answer-format marker preserved) so a pack can
degrade quality but not break the agents' wire format — and the catalog
validator and approval gate stay the only capability boundary either way.

### Learn from past incidents (opt-in)

```bash
# Recall similar past incidents AND record this run's verified outcome
poddebugger analyze my-broken-container --fix --confirm --learn

# Inspect / reset what has been remembered
poddebugger experience list
poddebugger experience clear
```

After a `--fix --confirm` run reaches a verified outcome (recovered or
honestly not), a redacted experience record — failure signature, what was
tried, whether it worked — is saved under
`~/.local/share/poddebugger/experience/` (override with
`PODDEBUGGER_EXPERIENCE_DIR`). On the next `--learn` run, the most similar
records land as evidence tagged `experience:<id>` right after the Scout
classifies, so the Planner and Remediator see "we've seen this before —
restart didn't help, set-env did". Similarity is deterministic scoring
(classification, exit code, OOM flag, image, keyword overlap) — no extra
LLM calls. Secret-looking values are masked and identifiers scrubbed
*before* anything reaches disk. Recalled records are prompt context only:
the catalog validator and the approval gate remain the only capability
boundary.

---

## Crash watcher

A small Go binary tails the runtime and analyzes every crash automatically.

```bash
export PATH=$PATH:/usr/local/go/bin       # if go isn't on PATH
cd controller && go build -o poddebugger-watch .

./poddebugger-watch --platform podman                   # tail podman events
./poddebugger-watch --platform kubernetes -n prod       # watch a namespace
```

It debounces crash loops and execs `poddebugger analyze … --json` per
crash. See [controller/](controller/) for flags.

---

## Run as a Kubernetes operator

Declarative diagnose-and-remediate via a custom resource.

```bash
kubectl apply -f deploy/crd.yaml -f deploy/rbac.yaml
kubectl apply -f deploy/operator.yaml          # set the image first
kubectl apply -f deploy/example-request.yaml
kubectl get pdr                                # watch the PHASE column
```

The CRD's `spec.remediationMode` is the guardrail — `SuggestOnly` (default,
diagnose only), `ApproveRequired` (wait for `spec.approved: true`), or
`AutoRemediate`. The CRD also exposes `spec.maxAutoRisk` (default `low`
— restart/scale only) and an optional `spec.allowedActions` whitelist.

For platform-team-owned policies that span many requests, apply a
cluster-scoped `PodDebuggerApprovalPolicy` (`pdap`):

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

Full install + design notes: [deploy/README.md](deploy/README.md).

---

## Configuration

Resolution order (later wins): built-in defaults → `.env` file → real
environment variables → CLI flags.

`.env` is auto-discovered by searching upward from the working directory
(override with `PODDEBUGGER_ENV_FILE`). An exported shell variable always
overrides the `.env` value. Copy [`.env.example`](.env.example) to `.env`
to get started.

| Variable | Purpose | Default |
|---|---|---|
| `PODDEBUGGER_PLATFORM` | `podman` \| `kubernetes` \| `openshift` | `podman` |
| `PODDEBUGGER_LLM_PROVIDER` | `anthropic` \| `openai` \| `ollama` \| `llamacpp` | `anthropic` |
| `PODDEBUGGER_LLM_MODEL` | model id override | provider default |
| `PODDEBUGGER_LLM_BASE_URL` | endpoint override (Azure / vLLM / internal gateway) | provider default |
| `PODDEBUGGER_<ROLE>_LLM_PROVIDER` | per-agent provider override (see below) | the default |
| `PODDEBUGGER_<ROLE>_LLM_MODEL` | per-agent model override | the default |
| `PODDEBUGGER_<ROLE>_LLM_BASE_URL` | per-agent endpoint override | the default |
| `PODDEBUGGER_EXTRA_AGENTS` | comma-separated dotted paths to custom `Agent` subclasses | — |
| `PODDEBUGGER_SEARCH_BACKEND` | with `--research`: `noop` \| `duckduckgo` \| dotted path | `noop` |
| `PODDEBUGGER_STATE_DIR` | where successful remediations save reversal payloads for `--undo` | `~/.cache/poddebugger/state/` |
| `PODDEBUGGER_APPROVALS_MODE` | `session` \| `persistent` \| `off` | `session` |
| `PODDEBUGGER_APPROVALS_FILE` | path to the persistent approval rules | `$XDG_CONFIG_HOME/poddebugger/approvals.json` |
| `PODDEBUGGER_ALLOW_SHELL` | `1` enables the freeform `shell` catalog action | unset |
| `PODDEBUGGER_LEARN` | `1` enables cross-run experience memory (same as `--learn`) | unset |
| `PODDEBUGGER_SPECIALISTS` | `1` lets the Coordinator spawn specialist agents (same as `--specialists`) | unset |
| `PODDEBUGGER_PROMPT_PACK` | prompt-pack directory (same as `--prompt-pack`) | unset (built-ins) |
| `PODDEBUGGER_EXPERIENCE_DIR` | where experience records are stored | `~/.local/share/poddebugger/experience/` |
| `PODDEBUGGER_REMEDIATION_MODE` | operator default mode | `SuggestOnly` |
| `PODDEBUGGER_LOG_LINES` | log tail size collected | `200` |
| `PODDEBUGGER_ENV_FILE` | explicit path to a `.env` file | (auto-discovered) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | credentials (not needed for local servers) | — |

### Per-agent LLM selection

`PODDEBUGGER_LLM_*` sets the default LLM for every agent. Any single agent
can be pointed at a different provider or model — for example, a cheap
local model for the routine roles plus a stronger one for the judgement-
critical ones:

```bash
PODDEBUGGER_LLM_PROVIDER=ollama
PODDEBUGGER_LLM_MODEL=gemma4:e4b
PODDEBUGGER_VERIFIER_LLM_PROVIDER=anthropic
PODDEBUGGER_VERIFIER_LLM_MODEL=claude-opus-4-7
```

See [AGENT_HARNESS.md](AGENT_HARNESS.md) for the full list of roles and
the resolution rules.

---

## Architecture

PodDebugger is a Python application built on top of the
[`inquiro`](inquiro/) framework. The split:

- **`inquiro/`** — domain-agnostic investigation primitives (`Agent`,
  `AgentContext`, `InvestigationState`, `Workspace`, `AgentLLMs`, prompt
  helpers, `extract_json`). Zero runtime dependencies. Shipped as its
  own pyproject package and tested standalone.
- **`agent/poddebugger/`** — the SRE-specific application: Podman /
  Kubernetes providers, the diagnostic context collector, the deep-
  inspection probe registry, the remediation catalog with verification,
  the approval gate, the eleven concrete agent classes, and the CLI.
- **`controller/`** — a small Go control plane: crash watcher, operator
  reconcile loop for the `PodDiagnosticRequest` and
  `PodDebuggerApprovalPolicy` CRDs.
- **`deploy/`** — Kubernetes manifests (CRDs, RBAC, operator Deployment,
  example resources).
- **`examples/log_investigator/`** — a second-domain reference app that
  uses `inquiro` for a non-pod problem, demonstrating the framework
  boundary.

See [FRAMEWORK.md](FRAMEWORK.md) for the precise framework/application
boundary and [HLD.md](HLD.md) for the full design.

---

## Layout

```
HLD.md                      # high-level design
TODO.md                     # roadmap / status / changelog
FRAMEWORK.md                # framework vs application boundary
AGENT_HARNESS.md            # plain-language walkthrough of the agent team
QUICKSTART.md               # setup walkthrough
.env.example                # config template — copy to .env

inquiro/                    # the standalone investigation framework
  pyproject.toml            # `name = "inquiro"`, zero runtime deps
  inquiro/
    agent.py · state.py · workspace.py · llm.py · llms.py
    prompts.py · json_utils.py
  tests/                    # standalone smoke suite — runs without poddebugger

agent/                      # the PodDebugger application (depends on inquiro)
  pyproject.toml
  poddebugger/
    cli.py                  # CLI entrypoint
    config.py               # env-driven config
    dotenv.py               # zero-dependency .env loader
    collector.py            # gathers context via a provider
    deepinspect.py          # read-only probes inside the container
    analyzer.py             # Diagnosis JSON parsing helpers
    models.py               # pod-domain data models
    remediation.py          # action catalog + verify_recovery + undo
    approvals.py            # human-in-the-loop approval gates
    framework/              # re-export shims pointing at `inquiro`
    providers/              # podman + kubernetes/openshift (kubectl/oc)
    llm/                    # anthropic + openai-compatible clients
    scaffold/               # the SRE layer on inquiro
      engine.py             # the loop + agent registry + dispatch
      search.py             # SearchBackend ABC + DuckDuckGo + redactor
      probes.py             # whitelisted SRE probe registry
      agents/
        scout.py · planner.py · coordinator.py · analyst.py · prober.py
        verifier.py · auditor.py · adjudicator.py · reporter.py
        remediator.py       # opt-in (analyze --fix)
        librarian.py        # opt-in (analyze --research)
  tests/                    # stdlib unittest

examples/
  README.md                 # index of the four reference examples below
  custom_agent.py           # hello-world Agent subclass (--demo + live mode)
  custom_gate.py            # hello-world ApprovalGate subclass (auditing + business hours)
  shell_action_demo.py      # opt-in freeform `shell` action walkthrough
  log_investigator/         # second-domain reference app on inquiro

controller/                 # the Go control plane
  main.go                   # flags, watch loop, debounce, alert output
  podman.go / kubernetes.go # per-runtime crash watchers
  operator.go               # CRD reconcile loop
  policy.go                 # PodDebuggerApprovalPolicy evaluation
  analyzer.go               # invokes the poddebugger CLI

deploy/                     # Kubernetes operator manifests
  crd.yaml                  # PodDiagnosticRequest + PodDebuggerApprovalPolicy
  rbac.yaml                 # least-privilege ClusterRole + binding
  operator.yaml             # operator Deployment
  example-request.yaml      # sample PodDiagnosticRequest
  example-policy.yaml       # sample PodDebuggerApprovalPolicy
  Dockerfile                # operator image (Go binary + Python CLI + kubectl)
```

---

## Tests

```bash
# Python (each package independently)
( cd agent      && python -m unittest )                    # PodDebugger
( cd inquiro    && python -m unittest discover tests )     # framework
( cd examples/log_investigator && python -m unittest discover tests )

# Go
( cd controller && go test ./... )
```

CI runs all of them on every push — see [.github/workflows/ci.yml](.github/workflows/ci.yml).

---

## Further reading

| Document | What's in it |
|---|---|
| [HLD.md](HLD.md) | Full high-level design; architecture, every component, every roadmap stage |
| [TODO.md](TODO.md) | Phased roadmap with status; doubles as a changelog |
| [QUICKSTART.md](QUICKSTART.md) | Step-by-step setup walkthrough |
| [AGENT_HARNESS.md](AGENT_HARNESS.md) | Plain-language tour of the agent team and how they cooperate |
| [FRAMEWORK.md](FRAMEWORK.md) | What's in `inquiro` vs PodDebugger — the framework boundary |
| [deploy/README.md](deploy/README.md) | Operator install, CRDs, approval policies |
| [controller/README.md](controller/README.md) | Crash watcher and operator binary |
| [inquiro/README.md](inquiro/README.md) | The standalone framework package |
| [examples/README.md](examples/README.md) | Index of the four reference examples (custom agent, custom gate, shell action demo, second-domain app) |

---

## License

TBD.
