# log_investigator

A tiny reference application built on the [`inquiro`](../../inquiro/)
framework. Reads a log file and runs a three-agent investigation
(Scout → Analyst → Reporter) to produce a finding about the most-recent
error.

This example exists as **boundary validation**: if a non-pod application
can drive `inquiro` end-to-end, the framework is genuinely reusable. The
test suite runs the whole loop offline with a scripted LLM — no API key
required.

## Install

```bash
pip install -e .                  # installs `log-investigator` CLI
# (depends on inquiro — install it from ../../inquiro/ first if needed)
```

## Test offline

```bash
python -m unittest discover tests
```

## Run against a real log

The example doesn't ship its own LLM client; it borrows PodDebugger's
provider factory (which already knows about Anthropic, OpenAI, Ollama,
llama.cpp). Install PodDebugger alongside it and set the usual env vars:

```bash
pip install -e ../../agent        # installs poddebugger
export ANTHROPIC_API_KEY=sk-ant-...

log-investigator /var/log/myapp.log
log-investigator /var/log/myapp.log --json
```

## What's intentionally minimal

A real engine would have a Coordinator loop, a Verifier, retries, budgets,
and an audit chain — see PodDebugger's `scaffold/engine.py` for the full
implementation. `log_investigator` is ~150 lines: it constructs the
inquiro primitives (`Agent`, `AgentContext`, `InvestigationState`,
`Workspace`, `AgentLLMs`) directly and walks them in a fixed order.

That's the point: the framework is small enough that a second-domain app
can stand up its own thin engine in an afternoon.
