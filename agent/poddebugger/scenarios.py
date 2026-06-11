"""Scenario eval harness — deterministic quality scoring (Phase 15C, HLD §19.4).

Unit tests prove the *plumbing* with scripted LLMs; this harness measures
the *agent quality* a real model delivers. Each :class:`Scenario` is a
Podman container that fails a known way, plus deterministic assertions:

* ``expect_classification`` — acceptable Scout failure categories (1 pt)
* ``expect_action`` — acceptable proposed catalog actions, evaluated
  **propose-only**: the harness never applies a remediation (1 pt)

``poddebugger eval`` runs the suite and prints a score table. It needs
Podman and an LLM, so it stays OUT of CI — it is the local/nightly
regression suite for prompt changes (it accepts ``--prompt-pack``) and
the fitness function for ``poddebugger optimize``.

The Podman lifecycle and the investigation step are injectable, so the
machinery itself is fully testable offline.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field

_CONTAINER_PREFIX = "pd-eval-"
_ALPINE = "docker.io/library/alpine:latest"


# --- scenario definitions -----------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One known failure + what a good investigation should conclude."""

    name: str
    description: str
    image: str
    command: tuple[str, ...]
    run_args: tuple[str, ...] = ()          # extra `podman run` args
    context: dict = field(default_factory=dict)   # --context values offered
    expect_classification: tuple[str, ...] = ()
    expect_action: tuple[str, ...] | None = None  # None = don't score actions
    settle_seconds: float = 2.0

    @property
    def max_points(self) -> int:
        return 1 + (1 if self.expect_action is not None else 0)

    @property
    def container(self) -> str:
        return _CONTAINER_PREFIX + self.name


BUILTIN_SCENARIOS: dict[str, Scenario] = {s.name: s for s in [
    Scenario(
        name="missing-env",
        description="app refuses to start without APP_TOKEN",
        image=_ALPINE,
        command=("sh", "-c",
                 'test -n "$APP_TOKEN" || { echo "FATAL: APP_TOKEN missing — '
                 'refusing to start"; exit 1; }; sleep 600'),
        context={"APP_TOKEN": "eval-token-1"},
        expect_classification=("ConfigError",),
        expect_action=("set-env", "recreate"),
    ),
    Scenario(
        name="crash-loop",
        description="app dies on a missing config file",
        image=_ALPINE,
        command=("sh", "-c",
                 'echo "ERROR: config file /etc/app/app.conf not found"; '
                 'exit 1'),
        expect_classification=("ConfigError", "CrashLoopBackOff"),
        expect_action=None,   # several fixes are defensible — don't score
    ),
    Scenario(
        name="oom",
        description="memory hog inside a 32 MB limit gets OOM-killed",
        image=_ALPINE,
        command=("sh", "-c",
                 "head -c 200m /dev/zero | tail | sleep 600"),
        run_args=("-m", "32m"),
        expect_classification=("OOMKilled",),
        expect_action=("set-resources",),
        settle_seconds=5.0,
    ),
    Scenario(
        name="bad-command",
        description="entrypoint binary does not exist",
        image=_ALPINE,
        command=("/no/such/binary",),
        expect_classification=("ConfigError", "Unknown", "CrashLoopBackOff"),
        expect_action=None,
    ),
    Scenario(
        name="dns",
        description="startup fails resolving a bogus dependency host",
        image=_ALPINE,
        command=("sh", "-c",
                 'wget -T 3 -q http://no-such-host.invalid/health || '
                 '{ echo "ERROR: could not resolve no-such-host.invalid '
                 '(dependency check failed)"; exit 1; }; sleep 600'),
        expect_classification=("NetworkError", "ConfigError"),
        expect_action=None,
        settle_seconds=6.0,
    ),
]}


# --- results ------------------------------------------------------------------


@dataclass
class ScenarioResult:
    name: str
    points: int = 0
    max_points: int = 0
    classification: str = ""
    action: str = ""
    expected_classification: tuple[str, ...] = ()
    expected_action: tuple[str, ...] | None = None
    classification_ok: bool = False
    action_ok: bool | None = None     # None = not scored
    error: str = ""                   # setup/run error; scenario scored 0


@dataclass
class SuiteScore:
    results: list[ScenarioResult]

    @property
    def total(self) -> int:
        return sum(r.points for r in self.results)

    @property
    def max_total(self) -> int:
        return sum(r.max_points for r in self.results)


# --- the runner ---------------------------------------------------------------


def _podman(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["podman", *args], capture_output=True, text=True,
                          timeout=120)


def run_scenario(scenario: Scenario, investigate, *,
                 podman=_podman, sleep=time.sleep) -> ScenarioResult:
    """Stand the failing container up, investigate, score, tear down.

    ``investigate(container_name, scenario) -> (classification, action)`` is
    injected — the CLI builds the real engine; tests inject a fake.
    """
    result = ScenarioResult(
        name=scenario.name, max_points=scenario.max_points,
        expected_classification=scenario.expect_classification,
        expected_action=scenario.expect_action,
    )
    cname = scenario.container
    podman(["rm", "-f", cname])
    proc = podman(["run", "-d", "--restart=no", "--name", cname,
                   *scenario.run_args, scenario.image, *scenario.command])
    if proc.returncode != 0:
        result.error = f"podman run failed: {(proc.stderr or '').strip()[:200]}"
        return result
    try:
        sleep(scenario.settle_seconds)
        classification, action = investigate(cname, scenario)
        result.classification = classification or ""
        result.action = action or ""
        result.classification_ok = result.classification.lower() in {
            c.lower() for c in scenario.expect_classification}
        result.points += 1 if result.classification_ok else 0
        if scenario.expect_action is not None:
            result.action_ok = result.action in scenario.expect_action
            result.points += 1 if result.action_ok else 0
    except Exception as exc:  # noqa: BLE001 — one broken scenario must not kill the suite
        result.error = str(exc)[:300]
    finally:
        podman(["rm", "-f", cname])
    return result


def run_suite(scenarios, investigate, *, podman=_podman,
              sleep=time.sleep, log=None) -> SuiteScore:
    results = []
    for s in scenarios:
        if log:
            log(f"[eval] {s.name}: {s.description}")
        r = run_scenario(s, investigate, podman=podman, sleep=sleep)
        if log:
            log(f"[eval] {s.name}: {r.points}/{r.max_points}"
                + (f" — {r.error}" if r.error else ""))
        results.append(r)
    return SuiteScore(results)


def make_investigate(llms, *, prompt_pack=None, log_lines=200, verbose=False):
    """The real investigation step: PodmanProvider + engine, propose-only."""
    from .providers.podman import PodmanProvider
    from .scaffold.engine import InvestigationEngine

    def investigate(cname: str, scenario: Scenario):
        provider = PodmanProvider()
        ref = provider.resolve(cname)
        engine = InvestigationEngine(
            provider, llms, log_lines=log_lines, verbose=verbose,
            remediation_enabled=scenario.expect_action is not None,
            context=dict(scenario.context),
            prompt_pack=prompt_pack,
        )
        diagnosis = engine.investigate(ref)
        action = ""
        if scenario.expect_action is not None:
            outcome = engine.remediate(diagnosis, apply=False)
            proposal = outcome.get("final_proposal") or {}
            if proposal.get("validated"):
                action = proposal.get("action", "")
        return engine.state.classification, action

    return investigate


def render_table(score: SuiteScore) -> str:
    lines = [
        f"{'scenario':14} {'pts':>7}  {'classification':28} {'action':14} notes",
        "-" * 84,
    ]
    for r in score.results:
        cls = f"{r.classification or '-'} {'✓' if r.classification_ok else '✗'}"
        if r.expected_action is None:
            act = "(not scored)"
        else:
            act = f"{r.action or '-'} {'✓' if r.action_ok else '✗'}"
        note = r.error or ""
        lines.append(f"{r.name:14} {r.points}/{r.max_points:<5}  "
                     f"{cls:28} {act:14} {note}")
    lines.append("-" * 84)
    lines.append(f"{'TOTAL':14} {score.total}/{score.max_total}")
    return "\n".join(lines)
