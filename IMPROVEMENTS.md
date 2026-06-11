# PodDebugger — Improvement Plan (post-Phase-13 review)

Reviewed 2026-06-10 against the full codebase. All originally planned phases
(1–12) plus Phase 13A–C are built; combined suites are green (297 PodDebugger
+ 21 inquiro + 2 log_investigator Python tests, 29 Go tests). This document
is the forward plan: what to improve, in what order, and *how* — so any
future session can pick an item and start without re-deriving the design.

Companion docs: [HLD.md](HLD.md) (design), [TODO.md](TODO.md) (stage
tracking), [FRAMEWORK.md](FRAMEWORK.md) (inquiro boundary). When an item
here gets picked up, promote it into TODO.md as a numbered phase/stage and
design it in HLD.md first — that workflow (see §7) is what kept quality high.

---

## 1. Finish the designed-but-unbuilt (highest leverage, designs exist)

These were deliberately deferred; the designs are already in HLD §18 and
TODO Phase 13, so they are the cheapest wins per unit of value.

### 1.1 Stage 13D — Coder agent + sandbox sidecar
The one remaining piece of Phase 13. Full design in HLD §18.5 and TODO
Stage 13D: `ActionAgent(action_name="code")` emitting
`{language, script, rationale, purpose}`; runs in an ephemeral debug
container (`kubectl debug --target=` on K8s, `--network container:<target>`
sibling on Podman); gated as `ActionDescriptor(kind="code", action="run",
risk="high")` with the script body shown in the prompt; persistent rules
match by language + script-hash; output truncated 8000 chars → Evidence
`coder:<purpose>:<hash[:8]>`; opt-in `--coder` / `PODDEBUGGER_ENABLE_CODER=1`.

**How:** build in the proven order — catalog/runner plumbing first with a
fake provider, then the agent + engine wiring, then the gate descriptor,
then CLI flags, then live-verify on Podman with ollama. Settle the sandbox
image registry question (GHCR is the default answer — free for public
images, same org as the repo) before writing the Dockerfile under
`sandbox/`. ~20 tests as scoped in TODO.

### 1.2 Operator `spec.context` threading
CLI `--context KEY=VALUE` exists; the CRD half was deferred. Add
`spec.context: map[string]string` to `PodDiagnosticRequest` in
[deploy/crd.yaml](deploy/crd.yaml), thread through
[controller/types.go](controller/types.go) → `RunAnalysis` in
[controller/analyzer.go](controller/analyzer.go) as repeated
`--context k=v` args (sorted, like `--param`). Mirror the existing
`remediationParams` plumbing — it is the exact same shape. ~4 Go tests
(decode, arg construction, empty map, sort stability).

### 1.3 `--context-secret KEY=VALUE`
Deferred from 13C. Decision needed (flagged in TODO open questions):
redaction scope. Recommended answer: redact from workspace git commits AND
verbose/CLI output, but let the value flow to the LLM (that's its purpose);
document plainly that a cloud LLM sees it, so use a local model for real
secrets. **How:** keep a `secret_keys: set[str]` on the engine; a single
`redact_secrets(text)` helper applied at the two output boundaries
(Workspace commit serialization, CLI/verbose printers) — same
single-safety-boundary pattern as `redact_query()` in
[scaffold/search.py](agent/poddebugger/scaffold/search.py).

### 1.4 Phase 14 — build-and-try-image pipeline
User-requested capability: agent writes a Dockerfile, builds, pushes, then
`set-image`. Distinct pipeline; design it in HLD before building. Depends
on 13D (the Coder agent produces the Dockerfile as `purpose: build`).
Sketch: new `build` catalog action (risk high) with params
`{dockerfile, tag, push_registry?}`; Podman builds locally (no push needed
— `set-image` to the local tag); K8s requires a registry the cluster can
pull from (needs `--context registry=...`). Reversal = prior image ref.
Gate shows the full Dockerfile. This is the riskiest feature in the plan —
keep it opt-in behind its own flag (`--allow-build`).

---

## 2. Code health (do alongside feature work)

### 2.1 Split `remediation.py` (1698 lines)
Largest file in the repo and still growing (13B added three actions, 7E a
fourth family). Split into a package, preserving the import path:
`remediation/__init__.py` (re-export everything — same shim trick as
`poddebugger/framework/`), `catalog.py` (ActionSpec, registry, parsers),
`actions/` (one module per action family: restart/scale, set_resources,
spec_changes, shell), `persistence.py` (save/load/undo), `verify.py`
(verify_recovery). The 100+ existing remediation tests are the safety net;
zero test edits should be needed if re-exports are faithful.

### 2.2 Split `cli.py` (811 lines)
Extract a `cli/` package: `analyze.py`, `remediate.py`, `approvals.py`,
shared `render.py`. Keep `cli.py` as the argparse assembly + dispatch.
Lower priority than 2.1 — do it when a feature next touches the CLI.

### 2.3 Fix repo-root test discovery
`python -m unittest discover -s agent/tests -t agent` from the repo root
fails with `cannot import name ... from 'inquiro' (unknown location)` —
the outer `inquiro/` project folder shadows the installed package as a
namespace package. CI dodges it via `working-directory: agent`, but every
human (and agent session) trips on it. **Fix:** move to src-layout —
`inquiro/src/inquiro/` + `[tool.setuptools] package-dir`; then the repo
root no longer contains an importable `inquiro/` directory. Alternative
(cheaper): a top-level `Makefile`/`run-tests.sh` that cds correctly and
runs all three suites + Go — worth adding even if src-layout also lands,
as the single command for "is everything green?".

### 2.4 Lint + type checking in CI
CI today is compile + tests (Python) and gofmt/vet (Go). Add `ruff check`
(fast, zero-config start) and `mypy --ignore-missing-imports` on
`agent/poddebugger` + `inquiro`. The codebase is dataclass-heavy and
already mostly annotated, so mypy adoption should be cheap. Add as a
separate non-blocking CI job first; flip to required once clean. Also pin
GitHub Action steps to SHAs and add dependabot config (supply-chain
hygiene, 10 minutes of work).

### 2.5 Structured run logging
`--verbose` prints role-by-role progress; there is no machine-readable
trace beyond the workspace git commits. Add an optional JSONL event log
(`engine.event_sink` callback; CLI `--trace FILE`): one event per agent
call/probe/gate decision with timestamps and token counts. This is the
foundation for 4.1 (eval harness) and 3.3 (streaming status) — build it
first.

---

## 3. Operational maturity

### 3.1 Live cluster validation (oldest open item)
Phases 2/4/5/7C/12 are cluster-untested — blocked on "no reachable
cluster". Unblock with **kind** (Kubernetes-in-Docker runs fine on Podman
4.x: `KIND_EXPERIMENTAL_PROVIDER=podman kind create cluster`). Then walk
the checklist: KubernetesProvider analyze on a CrashLoopBackOff pod →
watcher → operator reconcile → 7C auto-remediate → Phase 12 policy
deny/allow. Capture each as a `deploy/VALIDATION.md` log. Optionally
automate the core path as a CI job (kind has a GitHub Action) — even just
"operator reconciles and patches .status with --no-llm" would catch
contract drift between the Go and Python halves, which is currently only
covered by unit tests on each side independently.

### 3.2 Helm chart / install story
`deploy/` has raw YAML. A minimal chart (namespace, RBAC, CRDs, operator
Deployment, values for image/LLM env) makes the operator installable in
one command. Do after 3.1 proves the manifests against a real cluster.

### 3.3 Streaming status to the CR (deferred from Phase 6D)
Per-iteration mirroring onto `.status` needs the Python side to emit
progress. With 2.5's event sink, the cheapest contract is: engine appends
JSONL to stdout-adjacent fd or a file the Go side tails, OR (simpler)
`analyze --json` gains `--status-file PATH` the operator polls between
reconciles. Keep the exec contract; don't introduce gRPC for this alone.

### 3.4 Watcher service mode
TODO Phase 4 leftover: keep the brain warm instead of exec-per-event. Only
worth it if exec latency actually hurts (measure first — model load
dominates for ollama anyway, and the exec adds ~1s). If built: a tiny
stdlib `http.server` in Python (`poddebugger serve`) with one POST
endpoint taking the analyze args, Go switches via `--brain-url`. Decide
gRPC-vs-HTTP cross-cutting question in favor of HTTP+JSON (stdlib both
sides, contract already JSON).

### 3.5 LLM resilience + cost accounting
Two gaps in the LLM layer: no retry/backoff on transient failures (a flaky
ollama restart mid-investigation degrades agents unnecessarily — the
engine tolerates it, but a single retry would often save the run), and no
token/cost tracking. Add to `inquiro.llm` base: optional
`retries=1, backoff=2.0` on `complete()`, and a `usage` accumulator
(prompt/completion tokens per agent name) surfaced in the Diagnosis
footer and `--trace`. Anthropic/OpenAI responses already carry usage;
ollama reports eval counts.

---

## 4. New capabilities (rank by demonstrated need)

### 4.1 Scenario/eval harness — ✅ built (as part of Phase 15C)

> Status: shipped as `poddebugger/scenarios.py` + the `poddebugger eval`
> subcommand (TODO Stage 15C). Five built-in scenarios; deterministic
> classification + propose-only action scoring; out of CI as planned.
The agent's *quality* is only verified by occasional live runs; the unit
tests verify plumbing with scripted LLMs. Build a `scenarios/` suite:
each scenario = a Podman container spec that fails a known way (OOM, bad
env, bad image, crash loop, port conflict, full disk, DNS failure) + an
assertion on the Diagnosis (category match, or remediation action match —
deterministic, not LLM-judged). Runner: `python -m scenarios run --llm
ollama` stands containers up, runs `analyze --fix --dry-run`, scores, and
prints a table. This becomes the regression suite for prompt changes and
the benchmark when trying new models. Keep it OUT of CI (needs Podman +
a model); it's a local/nightly tool. The existing
`examples/adaptive_remediation_demo.py` live path is the seed — generalize
it.

### 4.2 MCP server mode
Expose PodDebugger as MCP tools (`analyze`, `remediate`, `approvals`) so
Claude Code / other MCP clients can drive it conversationally. Natural
fit: the Diagnosis JSON contract already exists, and the approval gate
maps onto the client's own permission prompts. Implementation: a thin
`poddebugger mcp` subcommand speaking MCP stdio; tools call the same
functions the CLI does. No new safety surface — `DenyGate` semantics by
default, mutations require explicit tool params mirroring `--yes`.

### 4.3 Investigation resume
The Workspace already commits per-iteration state. Add
`analyze --resume RUN_DIR`: rehydrate `InvestigationState` from the last
commit (state JSON round-trip already exists), continue the loop with
fresh budgets. Useful for `needs_context` exits — today the user re-runs
from scratch with `--context`; with resume the team keeps its evidence.
This is mostly framework (inquiro) work: `Workspace.load_latest()` +
engine constructor accepting an initial state.

### 4.4 Report export + notifications
`Diagnosis` → markdown report file (`--report out.md`: verdict, evidence
table, attempts, remediation log) is trivial and high-value for handoffs.
Notifications (Slack webhook on watcher events) are a small follow-up —
a `notify.py` with a webhook URL env var, called by the watcher on
non-zero-exit diagnoses.

### 4.5 Prometheus/metrics evidence source
Cross-cutting TODO ("metrics source for Podman"). Design as a
`MetricsBackend` ABC mirroring `SearchBackend` exactly (Noop default,
opt-in flag, engine-level single boundary): `query(promql) -> series`.
K8s clusters commonly have Prometheus; Podman gets `podman stats` (already
collected) and cAdvisor if present. The Analyst prompt gains a metrics
block when enabled. The `examples/custom_agent.py` MetricsAgent stub shows
where the agent-side hook goes.

### 4.6 Self-learning loop (dynamic prompts + cross-run memory) — ✅ all three layers built

> Status: promoted to **Phase 15** (HLD §19, TODO.md) and fully shipped:
> Layer 2 (experience store + Historian recall) as Stage 15A, Layer 1
> (dynamic specialist agents) as Stage 15B, Layer 3 (prompt packs + eval
> harness + optimizer) as Stage 15C.

Three layers, independent and increasing in ambition. The safety framing
for all of them: learning changes what the LLM is *told*, never what it is
*allowed to do* — catalog validation and the approval gate remain the only
capability boundary, so a "learned" bad idea still can't execute anything
a fresh run couldn't.

**Layer 1 — on-the-fly prompt/agent generation (small engine experiment).**
Prompts are already composed at runtime from state (evidence, attempt
history, catalog menu, `--context`), so "define the prompt on the fly" is
partly built. The next step is *meta-prompting*: a Strategist step where
the LLM writes the tailored brief for the next agent call (e.g., the
Coordinator drafts Analyst's instruction based on what the evidence now
suggests). `AgentContext.instruction`/`extras` already carry free-form
text, so this is wiring, not new framework. Beyond that: dynamic
specialist spawning — `make_agent(name, system_prompt)` where the
Coordinator can request e.g. a "PostgreSQL specialist" mid-run with an
LLM-written system prompt. inquiro's `Agent` base makes that a ~20-line
factory; register the dynamic agent for this run only, tag its outputs
`dynamic:<name>` in Evidence for auditability, cap spawns per run
(budget), and persist the generated prompt in the workspace commit so the
run stays replayable.

**Layer 2 — cross-run experience memory (the practical "ever-learning";
buildable now).** After each run that reaches a verified outcome, persist
an experience record: failure signature (classification, exit code, image,
key error strings), the diagnosis, the remediation taken, and the verify
outcome. Plain JSON files under `~/.local/share/poddebugger/experience/`
(stdlib, no DB, no embeddings — keyword/signature match for retrieval, the
same grep-ranking idea as the runbook agent below). On new runs, a
**Historian** step (mirror of the Librarian pattern: engine-level, opt-in
via `--learn` / `learning_enabled=True`) retrieves top-k similar past
cases and injects them into Scout/Analyst/Remediator prompts as "past
similar incidents and what worked". Failed fixes are as valuable as
successes — "set-resources did NOT recover this signature last time"
prevents thrashing across runs the way 13A's attempt history prevents it
within a run. Redact via the same secret-masking used in plan summaries
BEFORE persisting. This makes the system genuinely improve with use —
the learning is data, not code.

**Layer 3 — offline prompt-optimization loop (depends on 4.1).** The eval
harness is the fitness function; without it, "self-improving prompts" is
un-measurable drift. Loop: run scenarios → score → an optimizer LLM
critiques the failures and proposes prompt edits → re-run scenarios →
keep the variant iff the score improves. For this to be safe and
reviewable, prompts must become *data*: a versioned "prompt pack"
directory (the per-agent system prompts as files, loadable via
`PODDEBUGGER_PROMPT_PACK=DIR`) that the optimizer mutates — never code.
Run it nightly/locally against ollama; a human reviews the git diff of a
winning pack before it's adopted. This is the DSPy/OPRO pattern with
stdlib plumbing.

What NOT to build: online self-modification of prompts during production
runs (un-auditable drift — Layer 3 is deliberately offline + human-gated),
and fine-tuning loops (cost and complexity nothing here needs yet).

Sequencing: Layer 2 first (independent, immediately useful, pure
add-on like Phase 8), Layer 1 as a contained experiment behind a flag,
Layer 3 only after the eval harness (4.1) exists — which strengthens the
case for building 4.1 early.

### 4.7 Runbook/knowledge agent
Let orgs point PodDebugger at their own runbooks: a directory of markdown
files, grep-ranked (stdlib, no embeddings dependency — keep zero-dep) by
keywords from the failure classification; top chunks land as Evidence
`runbook:<file>`. An `ActionAgent(action_name="consult_runbooks")` like
the Librarian. Embeddings/RAG can come later behind an extra, like
`[search]`.

---

## 5. inquiro framework growth

- **Publish to PyPI** (it's zero-dep and self-tested; version 0.1.0 is
  ready). Until then, document the `pip install -e inquiro/` step more
  prominently — a fresh clone currently can't `pip install -e agent/`
  without it since `inquiro>=0.1.0` isn't on PyPI.
- **Src-layout** (see 2.3) at the same time as publishing.
- **Engine extraction**: the `MiniEngine` in examples/log_investigator
  proved apps can write their own loops, but the full
  `InvestigationEngine` (budgets, dispatch, audit, replan) is still
  PodDebugger-private. Extracting a generic `inquiro.engine` is the next
  big framework milestone — wait until a second *real* domain app exists
  so the abstraction is pulled by need, not speculation (same discipline
  as Phase 10).
- **Move loop/replan primitives** (13A's attempt-history Evidence pattern)
  into inquiro docs as a recipe even before engine extraction.

## 6. Security review pass (cross-cutting TODO, never done systematically)

One focused session: (a) secret redaction audit — env *values* matching
secret-looking patterns in collected spec/logs, not just key names; cover
the collector, deep-inspect output, workspace commits, and prompts;
(b) RBAC least-privilege re-check against what the operator actually
execs; (c) injection review of everything that flows into shell-outs
(`kubectl`/`podman` args are list-form already — verify no string
interpolation crept in; the 7E shell action is gate-protected by design);
(d) `ddgs`/search exfiltration re-check. Write findings into HLD as a
"Security posture" section.

---

## 7. How we implement (capture of the working method — keep doing this)

The pattern that produced 12 green phases, recorded so future sessions
follow it:

1. **Design first, in HLD.md** — a numbered section with the safety
   boundary named explicitly (catalog validation, gate, redaction point).
   Every mutating feature gets exactly ONE safety boundary, enforced at
   the engine/catalog level, never inside agent prompts.
2. **Stage it in TODO.md** (13A/13B/... granularity), checked off as built.
3. **Opt-in by default** — new capabilities ship disabled
   (`remediation_enabled=`, `research_enabled=`, `--allow-shell`,
   `--coder`); the read-only analyze path must never grow a mutation
   side-effect.
4. **Risk-tiered + gated** — every new action declares `low|medium|high`;
   the approval gate is consent, printed output is informational.
5. **Stdlib-only runtime** — no new runtime deps in poddebugger/inquiro;
   optional deps go behind extras (`[search]`) with lazy imports.
6. **Tests with fakes first** — stdlib `unittest`, scripted LLMs, fake
   providers; run from `agent/` (`python -m unittest discover`). Target:
   every new branch in validation/gating logic has a test.
7. **Live-verify before declaring done** — real Podman container + local
   ollama (`qwen3.5:9b`); never burn cloud credits for verification
   without asking.
8. **Docs sweep in the same change** — README, QUICKSTART, HLD status
   markers, deploy/README, examples/. Add a runnable `examples/*_demo.py`
   with an offline `--demo` mode for every user-facing feature.
9. **Contract stability** — the `Diagnosis` JSON is the Go↔Python contract;
   only ADD fields (Go ignores unknowns). Old import paths get re-export
   shims, never breaks.
10. **Update the memory file** (`poddebugger-status.md`) with what was
    built, decisions made, and live-verification evidence.

## 8. Suggested order

| # | Item | Why now | Size |
|---|------|---------|------|
| 1 | 2.3 test-runner script (+ src-layout) | every session trips on it | S |
| 2 | 4.1 scenario/eval harness | protects all future prompt/model changes | M |
| 3 | 1.1 Coder agent (13D) | completes Phase 13; design done | L |
| 4 | 1.2 operator `spec.context` | small, closes a 13C gap | S |
| 5 | 3.1 kind-based live cluster validation | oldest unverified surface | M |
| 6 | 2.1 split remediation.py | before the next catalog action lands | M |
| 7 | 2.4 ruff + mypy + CI hygiene | cheap, compounding | S |
| 8 | 1.3 `--context-secret` | finishes 13C | S |
| 9 | 3.5 LLM retry + token accounting | quality of life, enables cost view | S |
| 10 | 1.4 Phase 14 build-image | user-requested; design in HLD first | L |

Items 4.2–4.7, 3.2–3.4, §5, §6 are unordered backlog — promote when a
concrete need shows up.
