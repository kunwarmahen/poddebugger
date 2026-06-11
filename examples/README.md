# Examples

Reference examples for the most common ways to extend or experiment
with PodDebugger.

| Example | What it teaches | Run it |
|---|---|---|
| [custom_agent.py](custom_agent.py) | Subclass `ActionAgent` to add your own teammate to the investigation. Ships a `MetricsAgent` that picks a metric and records it as Evidence. | `python custom_agent.py --demo` (offline) · `python custom_agent.py <container>` (live) |
| [custom_gate.py](custom_gate.py) | Subclass `ApprovalGate` to wrap or replace the default human-in-the-loop gate. Ships an `AuditingGate` (decorator) and a `BusinessHoursGate` (policy). | `python custom_gate.py --demo` (offline) · `python custom_gate.py --apply` (wires into `remediation.execute`) |
| [shell_action_demo.py](shell_action_demo.py) | Walk through the **opt-in freeform `shell` catalog action**: off-by-default, opt-in via `enable_shell_action()`, gated by the approval layer, denyable via persistent rules. | `python shell_action_demo.py --demo` (offline) · live invocations in the docstring |
| [adaptive_remediation_demo.py](adaptive_remediation_demo.py) | The **adaptive remediation loop** — a fix that doesn't recover the workload becomes evidence, the team replans, and the agent tries something different. Shows `set-env`/`set-image`/`recreate`, the `--context` channel, and the `needs_context` request. | `python adaptive_remediation_demo.py --demo` (offline) · live invocation in the docstring |
| [specialist_demo.py](specialist_demo.py) | **On-the-fly specialist agents** (`--specialists`) — the Coordinator names a specialty and writes the charter; the engine composes the new agent's system prompt, budgets spawns, and persists every generated prompt for audit. | `python specialist_demo.py --demo` (offline) · live invocation in the docstring |
| [learning_demo.py](learning_demo.py) | The **cross-run experience memory** (`--learn`) — how verified outcomes are recorded (redacted), and how a new failure's signature recalls similar past incidents, including fixes that did NOT work. | `python learning_demo.py --demo` (offline) · live invocations in the docstring |
| [log_investigator/](log_investigator/) | A complete **second-domain application** built on the `inquiro` framework — investigates errors in a log file using a Scout → Analyst → Reporter team. Proves the framework boundary on a non-pod problem. | `pip install -e ./log_investigator && python -m unittest discover log_investigator/tests` |

All of these run with **no API key** in `--demo` mode — they stub the LLM
or the provider so you can see the wiring without spending tokens.

## Conventions

Every example file ships with:

- A docstring at the top explaining *what* it teaches and *how* to run it.
- A `--demo` (or equivalent) flag that runs deterministically offline.
- A pointer to a live-against-Podman invocation when applicable.

The split mirrors the project's overall approach: deterministic tests
prove correctness; live runs prove the integration.

## Where to read next

- The framework these examples extend: [`../inquiro/`](../inquiro/) and
  [`../FRAMEWORK.md`](../FRAMEWORK.md).
- The PodDebugger application that ships eleven built-in agents and the
  full investigation engine: [`../agent/`](../agent/).
- The plain-language walkthrough of the agent team:
  [`../AGENT_HARNESS.md`](../AGENT_HARNESS.md).
- The full design: [`../HLD.md`](../HLD.md).
