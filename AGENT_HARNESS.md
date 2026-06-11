# How PodDebugger's Agent Harness Works

_A plain-language guide — no Kubernetes or AI background needed._

---

## In one sentence

When a container or pod fails, PodDebugger doesn't just ask an AI _"what's
wrong?"_ once and print the answer. It runs a **team of nine specialist
agents** that investigate the failure together — proposing ideas, checking them
against the real system, challenging each other — and only then writes the
verdict.

---

## Why a team instead of one question?

Ask an AI _"why did this crash?"_ once and you get a **guess**. A confident
guess, often a good one — but it can't check itself, and when it's wrong it's
wrong with full confidence.

PodDebugger's harness works like a real **incident-response team** — or a
detective squad. One agent forms a theory; another tests it against the
evidence; a third audits the whole case for mistakes. Each agent does one small
job well, and no single agent's mistake can decide the outcome on its own.

---

## The nine agents

Think of a detective team working a case:

| Agent           | Detective role     | What it does                                                                                                                         |
| --------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| **Scout**       | First on the scene | Gathers the facts — logs, events, status, config — and says what _kind_ of failure this looks like.                                  |
| **Planner**     | Lead investigator  | Sets the line of inquiry and writes down the rules the final answer must obey (e.g. _"the exit code must match the stated cause"_).  |
| **Coordinator** | Dispatcher         | Each round, decides the next move: think more, go check something, or _"we're done."_                                                |
| **Analyst**     | Theorist           | Studies the evidence and proposes theories — _"the app can't reach its database."_                                                   |
| **Prober**      | Field agent        | Goes and **checks the live system** — fetches more logs, looks inside the running container — to bring back hard facts.              |
| **Verifier**    | Fact-checker       | Takes each theory and rules it **confirmed**, **refuted**, or _"need more evidence"_ — and can send the Prober to settle a question. |
| **Auditor**     | Internal affairs   | Reviews the whole case for mistakes — contradictions, thin evidence, skipped checks.                                                 |
| **Adjudicator** | Judge              | When the Auditor challenges a confirmed finding, independently rules: does it stand, or is it thrown out?                            |
| **Reporter**    | Writes the report  | Produces the final diagnosis: root cause, confidence, evidence, suggested fixes.                                                     |

---

## How they work together

```
            a pod crashes
                  │
                  ▼
          ┌───────────────┐
          │     SCOUT     │   gather the facts, name the failure type
          └───────┬───────┘
                  ▼
          ┌───────────────┐
          │    PLANNER    │   set the strategy + the rules the answer must obey
          └───────┬───────┘
                  │
   ╔══════════════▼═══════════════════════════════════════╗
   ║              THE INVESTIGATION LOOP                  ║
   ║                                                      ║
   ║          ┌───────────────┐                           ║
   ║   ┌─────▶│  COORDINATOR  │  pick the next move        ║
   ║   │      └───┬───────┬───┘                            ║
   ║   │      "analyze"  "probe"                           ║
   ║   │          │       │                                ║
   ║   │     ┌────▼───┐ ┌─▼────────┐                       ║
   ║   │     │ANALYST │ │  PROBER  │  check the live        ║
   ║   │     │theories│ │  system  │  system for facts      ║
   ║   │     └────┬───┘ └─┬────────┘                       ║
   ║   │          │       │                                ║
   ║   │     ┌────▼───────▼────┐                           ║
   ║   │     │    VERIFIER     │  confirm / refute a theory ║
   ║   │     └────────┬────────┘                           ║
   ║   └──────────────┘  (loop until the failure is        ║
   ║                      explained, or the budget runs)   ║
   ╚══════════════════╪═══════════════════════════════════╝
                      ▼
          ┌───────────────────────┐
          │ AUDITOR + ADJUDICATOR │  review the case;
          │                       │  overturn weak findings
          └───────────┬───────────┘
                      ▼
          ┌───────────────────────┐
          │       REPORTER        │  the final diagnosis
          └───────────────────────┘
```

That's the **default team** — runs on every investigation. Three more
teammates plug into the same loop when their feature flag is on, and
every mutating step is gated by an approval prompt:

```
                  COORDINATOR (the dispatcher)
                       │
        ┌──────────────┼──────────────┐
        │              │              │
     "analyze"      "probe"        "research"  ← only with --research
        │              │              │
        ▼              ▼              ▼
     ANALYST        PROBER         LIBRARIAN
                       │              │
                       │     +redact: strip IPs / pod
                       │     suffixes, then search;
                       │     evidence tagged "web:..."
                       │
                  ╔════▼═════════════════════╗
                  ║   APPROVAL GATE          ║   only for the
                  ║   [Y]es / [A]lways / [N] ║   `deep_inspect`
                  ╚══════════════════════════╝   probe

      After the verdict (analyze --fix):

              ┌───────────────┐
              │   REMEDIATOR  │   picks a catalog action; engine validates
              └───────┬───────┘   against the typed catalog (restart, scale,
                      │           set-resources, adjust-probe, rollback,
                      │           opt-in shell). LLM never emits a command.
                ╔═════▼═════════════════════╗
                ║      APPROVAL GATE        ║   refuse / allow once /
                ║   [Y]es / [A]lways / [N]  ║   always-this-run / persist
                ╚═════╤═════════════════════╝
                      ▼
              executes the typed action,
              captures a reversal, checks recovery
```

The gate is the safety boundary on mutating steps. Defaults: TTY →
prompt, non-TTY → refuse. A persistent rules file lets you pre-approve
trusted actions. The Kubernetes operator does the same thing async via
`spec.remediationMode: ApproveRequired`.

---

## The life of a clue

Every idea travels through the same pipeline. Nothing becomes a "fact" without
passing the fact-checker — and even a confirmed fact can still be overturned.

```
   a hint in the logs
          │
          ▼
       LEAD  ───────▶  HYPOTHESIS  ───────▶  CONFIRMED FINDING
  "worth a look"     "a theory + a way     "verified against
                      to test it"            the evidence"
                           │                       │
                           │ refuted               │ Auditor challenges it,
                           ▼                       │ Adjudicator agrees
                       RULED OUT  ◀────────────────┘
                     "a dead end —
                      don't revisit"
```

---

## A walk-through

A pod crashes on startup. Here's a real run (shortened):

1. **Scout** reads the logs — `connection refused to redis:6379` — and the exit
   code `1`. It calls this a **NetworkError** and seeds a few leads.
2. **Planner** writes the strategy ("confirm the cache-connectivity failure")
   and the rules: _the exit code must match the cause._
3. **Coordinator** says: _analyze._
4. **Analyst** proposes a theory: _the app exits because Redis is unreachable._
5. **Verifier** checks it against the logs — they're conclusive — **confirmed.**
6. **Coordinator** sees the failure is explained and says _done._
7. **Auditor** reviews the case and files a concern about one finding;
   **Adjudicator** weighs it and rules — keeping or demoting the finding.
8. **Reporter** writes the verdict: _the app aborts at startup because it has
   no retry logic for an unavailable Redis_ — with confidence, evidence, and
   two suggested fixes.

An easy crash like this finishes in **2–3 rounds**. A genuinely puzzling one
keeps probing — that's the point: the effort scales to the difficulty.

---

## It knows when to stop

- **Easy failure** (the logs say it plainly) → a few rounds, then done.
- **Hard failure** → keeps forming theories and probing the live system.
- A **budget** (a cap on rounds and AI calls) guarantees it always finishes.

---

## Every run is saved — the audit trail

Each investigation is recorded as its own **git repository**, with **one commit
per round**, under:

```
~/.cache/poddebugger/runs/<timestamp>-<workload>/
```

You can open it and replay _exactly_ how the agents reached the diagnosis —
every theory, every probe, every verdict. Nothing is a black box.

---

## If an agent stumbles

AI calls sometimes fail — especially small local models (an empty or garbled
reply). The harness is built so **one failed agent never sinks the
investigation**: the call is retried, then the agent degrades gracefully and
the team carries on. If even the final Reporter fails, PodDebugger still
assembles a diagnosis from what the team already confirmed.

---

## Choosing which AI model each agent uses

By default **every agent uses the same model** — whatever you set as the
default LLM. But the nine agents don't all need the same horsepower: the Scout
and Coordinator do routine work, while the **Verifier, Adjudicator and
Reporter** make the judgement calls that decide the answer.

So you can give each agent its own model. In your `.env` file:

```bash
# Default for every agent — a fast, free local model
PODDEBUGGER_LLM_PROVIDER=ollama
PODDEBUGGER_LLM_MODEL=gemma4:e4b

# ...but send the three judgement-critical agents to a stronger model
PODDEBUGGER_VERIFIER_LLM_PROVIDER=anthropic
PODDEBUGGER_VERIFIER_LLM_MODEL=claude-opus-4-7
PODDEBUGGER_ADJUDICATOR_LLM_PROVIDER=anthropic
PODDEBUGGER_ADJUDICATOR_LLM_MODEL=claude-opus-4-7
PODDEBUGGER_REPORTER_LLM_PROVIDER=anthropic
PODDEBUGGER_REPORTER_LLM_MODEL=claude-opus-4-7
```

The override variables — set any of them per agent:

| Variable                          | Sets                                                           |
| --------------------------------- | -------------------------------------------------------------- |
| `PODDEBUGGER_<ROLE>_LLM_PROVIDER` | which AI service (`anthropic`, `openai`, `ollama`, `llamacpp`) |
| `PODDEBUGGER_<ROLE>_LLM_MODEL`    | which model                                                    |
| `PODDEBUGGER_<ROLE>_LLM_BASE_URL` | a custom endpoint (self-hosted / gateway)                      |

`<ROLE>` is the agent's `name` attribute, uppercased. For the built-ins
that's one of `SCOUT` `PLANNER` `COORDINATOR` `ANALYST` `PROBER`
`VERIFIER` `AUDITOR` `ADJUDICATOR` `REPORTER` — plus `REMEDIATOR` and
`LIBRARIAN` when those optional teammates are enabled. **Custom agents
get the same treatment**: a `MetricsAgent` with `name = "metrics"` is
routed by `PODDEBUGGER_METRICS_LLM_*`.

Rules of thumb:

- Set only the **model** → keeps the default provider.
- Set the **provider** → that provider's default model is used unless you also
  set the model.
- Agents that end up with the _same_ provider+model share one connection — no
  waste.

The CLI prints what it used, e.g.:
`(investigated with ollama:gemma4:e4b (per-agent: reporter=anthropic:claude-opus-4-7) — ...)`

---

## Where this runs

The same harness powers all three ways to use PodDebugger:

- **CLI** — `poddebugger analyze <pod>` runs an investigation on demand.
- **Watcher** — automatically investigates every crash it sees.
- **Operator** — investigates on request inside a Kubernetes cluster.

They all call the same default nine-agent team, layer in the optional
Librarian and Remediator (and any custom agents you've registered), and
produce the same kind of structured diagnosis.

---

## The wider team — optional and custom teammates

Past the default nine, the team can grow in three ways. All three are
built — they're listed here so you know what's on the menu.

- **The Remediator** ("the fixer"). Once the team agrees on a diagnosis,
  the Remediator picks a fix and, with permission, **actually applies it**
  — restart a workload, give it more memory, roll it back to the last
  good version. It chooses only from a **fixed menu of safe, reversible
  actions** — never an action the AI invented — and higher-impact fixes
  always wait for a human's approval. `poddebugger analyze --fix` runs
  the full **investigate → propose → apply** flow, with `--max-risk`
  capping the risk tier the agent may apply automatically. An opt-in
  `shell` action (`--allow-shell`) lets it escape the typed catalog when
  no catalog action fits, at the cost of the catalog-membership safety
  property — the approval gate is the remaining boundary. (Design:
  [HLD.md §12](HLD.md). Runnable demo:
  [examples/shell_action_demo.py](examples/shell_action_demo.py).)

- **The Librarian** ("the reference desk"). Searches the outside world
  (known issues, vendor docs, security advisories) to see whether anyone
  has hit this exact failure before, and brings back what it finds as
  extra clues. It only ever searches the _generalized_ error — IPs, pod
  suffixes, and UUIDs are stripped before any HTTP call — and it is off
  unless you switch it on with `analyze --research`. Default search
  backend: noop (air-gap safe). DuckDuckGo ships as the concrete
  option (`pip install 'poddebugger[search]'`); the `SearchBackend` ABC
  is pluggable. (Design: [HLD.md §13](HLD.md).)

- **Specialists** ("experts hired for one case"). With `analyze
  --specialists`, the Coordinator can notice mid-investigation that the
  team lacks a skill — say, deep PostgreSQL knowledge — and *create* that
  expert on the spot: it names the specialty and writes the new agent's
  marching orders itself, and that text becomes the spawned agent's
  system prompt. Specialists only read and advise — their insights land
  as evidence tagged `dynamic:<name>`, and they can never run commands
  or change anything, so the team gains expertise without gaining
  permissions. At most two are hired per run, and every generated
  prompt is saved in the run's audit trail so you can see exactly what
  the expert was told. (Design: [HLD.md §19.3](HLD.md).)

- **The Historian** ("the one who remembers"). With `analyze --learn`,
  the team stops starting from scratch: after a fix is verified (it
  worked — or it honestly didn't), a redacted record of the incident is
  kept on your machine. Next time a similar failure shows up, the
  Historian lays those past incidents on the table as evidence — "we
  tried a restart on this exact signature last month and it did NOT
  help; raising the memory limit did". It costs no extra AI calls
  (matching is plain scoring, not a model), secrets are masked before
  anything is written, and remembering never changes what the team is
  *allowed* to do — only what it knows. Inspect or wipe the memory with
  `poddebugger experience list|clear`. (Design: [HLD.md §19](HLD.md).
  Runnable demo: [examples/learning_demo.py](examples/learning_demo.py).)

- **An open team — bring your own teammate.** Every existing agent is a
  subclass of a small `Agent` base class, and you can drop your own in
  alongside them: a Metrics agent that pulls Prometheus data, a Runbook
  agent that searches your internal wiki, a Ticketing agent that checks
  whether a known incident covers this. Your agent gets the same
  investigation state, the same audit trail, the same per-model config
  — and the Coordinator learns about it automatically. Copy-and-customize
  templates live in [examples/custom_agent.py](examples/custom_agent.py)
  (a new agent) and [examples/custom_gate.py](examples/custom_gate.py)
  (a custom approval gate). (Design: [HLD.md §14](HLD.md).)

## The pause-before-act rule

The Remediator and the Prober's `deep_inspect` probe are gated by a
**per-step approval prompt**, the same `[Y]es / [A]lways (this run) /
[N]o` pattern Claude Code uses. The persistent rules file
(`~/.config/poddebugger/approvals.json`, managed via
`poddebugger approvals list/add/remove/check`) lets you pre-approve
trusted actions so the prompts only appear for things you haven't seen
yet. On a non-TTY (CI, pipe), the gate refuses unless `--yes` or a
matching rule says otherwise. The operator's asynchronous equivalent —
`spec.remediationMode: ApproveRequired` — implements the same model
declaratively. (Design: [HLD.md §16](HLD.md).)

## The team can leave the building

The whole "team-of-agents investigating a failure" pattern is not
specific to pods — it works just as well for diagnosing a slow database
query, a stuck CI pipeline, or a flood of support tickets. So the
machinery has been lifted out of PodDebugger into a standalone
framework: [`inquiro`](inquiro/) (Latin for _"I inquire"_). PodDebugger
is one reference application; [`examples/log_investigator/`](examples/log_investigator/)
is a second one (a tiny log-file diagnoser) that proves the boundary on
a non-pod domain. Build your own the same way without redoing the
design. (Design: [HLD.md §15](HLD.md) and
[FRAMEWORK.md](FRAMEWORK.md).)

---

The team's working rule does not change: outside knowledge and AI
suggestions are _clues and proposals_ — the cluster itself, and a human
where it matters, remain the final word.

---

_For the engineering design, see [HLD.md §11–15](HLD.md). To watch an
investigation live, run `poddebugger analyze <pod> --verbose`._
