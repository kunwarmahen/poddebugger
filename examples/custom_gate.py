"""Hello-World custom approval gate — wrap or replace the default one.

Counterpart to ``custom_agent.py``: that file shows how to add an
``ActionAgent``; this one shows how to add an ``ApprovalGate``. Subclass
``ApprovalGate`` and return a ``Decision`` for each ``ActionDescriptor``
the engine asks about — that's the whole contract.

Two patterns are illustrated:

1. **Wrapper / decorator gate** — ``AuditingGate`` takes any inner gate,
   logs every decision (to stderr and optionally a file), then returns
   the inner gate's verdict unchanged. Useful for compliance audit
   trails without changing the policy itself. Compose it with the
   built-in ``RulesGate`` / ``TTYPromptGate`` / ``AutoApproveGate``.

2. **Custom-policy gate** — ``BusinessHoursGate`` denies any action
   outside business hours, regardless of what the inner gate would say.
   The pattern generalizes — replace the time check with a Slack
   approval, an OPA query, a ticket-system lookup, etc.

How to run::

    # 1. Deterministic walk-through of every built-in gate plus the two
    #    custom ones — no LLM, no container, prints what each one decides.
    python ../examples/custom_gate.py --demo

    # 2. Drive `remediation.execute` directly with a custom gate. This
    #    skips the CLI's `make_gate` factory entirely so you can plug
    #    whatever gate you like into a script. Stubs the provider so no
    #    podman/kubectl is touched.
    python ../examples/custom_gate.py --apply

The CLI flags `--yes`, `--no-prompt`, `--approvals {session,persistent,off}`
and the `poddebugger approvals` sub-command work today *without* this
example — see [README.md](../README.md#approve-before-acting) and
[HLD.md §16](../HLD.md#16-human-in-the-loop-interactive-approvals-phase-11).
This file is for users who want a *programmatic* gate (e.g. a gate that
posts to Slack or queries OPA) and need a starting point.
"""

from __future__ import annotations

import datetime
import json
import sys
import tempfile
from pathlib import Path

from poddebugger.approvals import (
    ActionDescriptor,
    ApprovalGate,
    AutoApproveGate,
    Decision,
    DenyGate,
    RulesGate,
    is_allowed,
)
from poddebugger.models import WorkloadRef


# --- custom gates ---------------------------------------------------------


class AuditingGate(ApprovalGate):
    """Wraps any inner gate; logs every (descriptor, decision) pair.

    Drop-in replacement for any other gate — the inner gate's decision is
    passed through unchanged. Use this to add a structured audit trail
    around the existing approval flow without changing the policy.
    """

    name = "auditing"

    def __init__(self, inner: ApprovalGate, log_path: Path | None = None):
        self._inner = inner
        self._log_path = Path(log_path) if log_path else None

    def request(self, descriptor: ActionDescriptor) -> Decision:
        decision = self._inner.request(descriptor)
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "kind": descriptor.kind,
            "action": descriptor.action,
            "target": str(descriptor.target),
            "platform": descriptor.target.platform,
            "risk": descriptor.risk,
            "decision": decision.value,
            "inner_gate": type(self._inner).__name__,
        }
        line = json.dumps(record)
        print(f"[audit] {line}", file=sys.stderr)
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return decision


class BusinessHoursGate(ApprovalGate):
    """Denies anything outside business hours; otherwise defers to ``inner``.

    Concrete example of a *policy-imposing* wrapper. Replace the
    ``_in_hours`` check with whatever your environment needs — a Slack
    bot's response, an OPA query, an on-call schedule lookup.
    """

    name = "business-hours"

    def __init__(self, inner: ApprovalGate, *,
                 start_hour: int = 9, end_hour: int = 17,
                 now: callable = None):
        self._inner = inner
        self._start = start_hour
        self._end = end_hour
        # `now` is injectable so tests / demos can pretend it's any time.
        self._now = now or datetime.datetime.now

    def _in_hours(self) -> bool:
        h = self._now().hour
        return self._start <= h < self._end

    def request(self, descriptor: ActionDescriptor) -> Decision:
        if not self._in_hours():
            print(
                f"[hours] refused — outside {self._start:02d}:00–{self._end:02d}:00",
                file=sys.stderr,
            )
            return Decision.DENY
        return self._inner.request(descriptor)


# --- demo: walk every gate against the same descriptor -------------------


def _descriptor(action: str = "restart", platform: str = "kubernetes",
                namespace: str = "prod", name: str = "web",
                risk: str = "low") -> ActionDescriptor:
    return ActionDescriptor(
        kind="remediation", action=action, risk=risk,
        target=WorkloadRef(name=name, namespace=namespace, platform=platform),
        summary=f"{action} {namespace}/{name}",
    )


def run_demo() -> int:
    """Print what each gate decides for the same action descriptor."""
    d = _descriptor()
    print(f"Action under review: {d.kind}/{d.action} on {d.target} "
          f"[{d.target.platform}], risk={d.risk}\n")

    print("=== 1. AutoApproveGate (the --yes default) ===")
    decision = AutoApproveGate().request(d)
    print(f"  decision: {decision.value}  (allowed={is_allowed(decision)})\n")

    print("=== 2. DenyGate (the non-TTY default) ===")
    decision = DenyGate().request(d)
    print(f"  decision: {decision.value}  (allowed={is_allowed(decision)})\n")

    print("=== 3. RulesGate(Deny) + matching allow rule ===")
    rules = [{"kind": "remediation", "action": "restart",
              "target": {"platform": "kubernetes"}, "decision": "allow"}]
    decision = RulesGate(DenyGate(), rules).request(d)
    print(f"  decision: {decision.value}   "
          f"# rule short-circuits the inner DenyGate\n")

    print("=== 4. RulesGate — deny wins when both rules match ===")
    rules = [
        {"kind": "remediation", "action": "restart",
         "target": {"platform": "kubernetes"}, "decision": "allow"},
        {"kind": "remediation", "action": "restart",
         "target": {"platform": "kubernetes"}, "decision": "deny"},
    ]
    decision = RulesGate(AutoApproveGate(), rules).request(d)
    print(f"  decision: {decision.value}   # explicit deny beats explicit allow\n")

    print("=== 5. AuditingGate wrapping AutoApproveGate ===")
    print("       (writes one JSON line per decision; see stderr below)")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        log_path = Path(f.name)
    gate = AuditingGate(AutoApproveGate(), log_path=log_path)
    decision = gate.request(d)
    print(f"  decision: {decision.value}")
    print(f"  audit log written to: {log_path}")
    print(f"  log contents: {log_path.read_text().strip()}\n")
    log_path.unlink()

    print("=== 6. BusinessHoursGate (forced 'after hours' for the demo) ===")

    # Force "now" to 22:00 so the demo always refuses regardless of when
    # you run it.
    def fake_now():
        return datetime.datetime.now().replace(hour=22)
    decision = BusinessHoursGate(AutoApproveGate(), now=fake_now).request(d)
    print(f"  decision: {decision.value}   "
          f"# business-hours gate refused even though inner would allow\n")

    print("=== 7. Composed: Auditing( BusinessHours( Rules( TTY ) ) ) ===")
    print("       — a realistic production stack: audit everything,")
    print("         deny after-hours, otherwise honor persistent rules,")
    print("         fall back to a prompt (here AutoApprove for the demo).")

    def fake_now_in_hours():
        return datetime.datetime.now().replace(hour=10)
    rules_layer = RulesGate(AutoApproveGate(), rules=[])  # no rules → prompt
    hours_layer = BusinessHoursGate(rules_layer, now=fake_now_in_hours)
    audit_layer = AuditingGate(hours_layer)
    decision = audit_layer.request(d)
    print(f"  decision: {decision.value}\n")
    return 0


# --- "wire it into a real apply" --------------------------------------------


def run_apply() -> int:
    """Plug a custom gate into ``remediation.execute`` against a stubbed
    provider. Shows the gate denying a plan and the catalog returning an
    unexecuted ActionResult — no podman / kubectl call ever happens.
    """
    from poddebugger import remediation
    from poddebugger.providers.podman import PodmanProvider

    # A trivial stub — every call returns rc=0. We never reach it because
    # the gate denies the plan before execute() shells out.
    provider = PodmanProvider()
    provider._run = lambda args, check=True: type("CP", (), {
        "args": args, "returncode": 0, "stdout": "", "stderr": ""})()

    ref = WorkloadRef(name="web", platform="podman")
    plan = remediation.make_plan(provider, ref, "restart",
                                 remediation.parse_params("restart", []))

    print("--- gate denies the plan ---")
    audit_log = tempfile.NamedTemporaryFile(suffix=".log", delete=False).name
    deny_audit = AuditingGate(DenyGate(), log_path=Path(audit_log))
    result = remediation.execute(provider, ref, plan, gate=deny_audit)
    print(f"  executed: {result.executed}")
    print(f"  result:   {result.result}")
    print(f"  audit:    {Path(audit_log).read_text().strip()}\n")
    Path(audit_log).unlink()

    print("--- same plan, but the auditing+allow gate lets it through ---")
    allow_audit = AuditingGate(AutoApproveGate())
    result = remediation.execute(provider, ref, plan, gate=allow_audit)
    print(f"  executed: {result.executed}")
    print(f"  result:   {result.result}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        _usage()
        return 2
    if argv[0] == "--demo":
        return run_demo()
    if argv[0] == "--apply":
        return run_apply()
    _usage()
    return 2


def _usage() -> None:
    print("Hello-World custom approval gate.", file=sys.stderr)
    print(file=sys.stderr)
    print("usage:", file=sys.stderr)
    print("  python custom_gate.py --demo    "
          "# walk every built-in + custom gate", file=sys.stderr)
    print("  python custom_gate.py --apply   "
          "# wire a custom gate into remediation.execute", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
