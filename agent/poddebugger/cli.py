"""PodDebugger CLI — the read-only analysis entrypoint (MVP / Phase 1).

Usage:
    poddebugger analyze <container-or-pod> [options]

Gathers status/events/logs/spec for a workload, sends them to an LLM, and
prints a root-cause analysis with suggested fixes.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys

from . import __version__, approvals, remediation
from .collector import collect
from .config import Config
from .llm import LLMError
from .models import DiagnosticContext, Diagnosis
from .providers import ProviderError, get_provider
from .scaffold.engine import DEFAULT_MAX_ITERATIONS, InvestigationEngine
from .scaffold.llms import AgentLLMs
from .scaffold.search import SearchError, get_backend


# --- rendering ---------------------------------------------------------------

def _render_context(ctx: DiagnosticContext) -> str:
    w = ctx.workload
    lines = [f"workload: {w.ref}  ({w.ref.platform})", ""]
    lines += w.summary_lines()
    lines.append("")
    lines.append(f"events: {len(ctx.events)}")
    for e in ctx.events[-15:]:
        lines.append(f"  {e.line()}")
    if ctx.notes:
        lines.append("")
        lines.append("notes:")
        lines += [f"  - {n}" for n in ctx.notes]
    log_preview = (ctx.logs or "(no logs)").splitlines()[-20:]
    lines.append("")
    lines.append("logs (last 20 lines):")
    lines += [f"  {ln}" for ln in log_preview]
    if ctx.deep_inspection:
        lines.append("")
        lines.append("deep inspection:")
        for label, output in ctx.deep_inspection.items():
            lines.append(f"  [{label}]")
            lines += [f"    {ln}" for ln in output.splitlines()[:8]]
    return "\n".join(lines)


def _render_diagnosis(d: Diagnosis) -> str:
    bar = "=" * 60
    lines = [bar, "  PodDebugger — Diagnosis", bar, ""]
    lines.append(f"Summary:    {d.summary}")
    lines.append(f"Confidence: {d.confidence:.0%}")
    lines.append("")
    lines.append("Root cause:")
    lines.append(f"  {d.root_cause}")
    if d.evidence:
        lines.append("")
        lines.append("Evidence:")
        lines += [f"  - {e}" for e in d.evidence]
    if d.suggested_fixes:
        lines.append("")
        lines.append("Suggested fixes:")
        for i, f in enumerate(d.suggested_fixes, 1):
            lines.append(f"  {i}. [{f.risk} risk] {f.action}")
            if f.rationale:
                lines.append(f"     why: {f.rationale}")
    if d.needs_deep_inspection:
        lines.append("")
        lines.append(
            "! The model wants live in-container state — re-run with --deep "
            "(injects read-only probes; the container must be running)."
        )
    lines.append("")
    return "\n".join(lines)


def _diagnosis_to_dict(d: Diagnosis) -> dict:
    return dataclasses.asdict(d)


# --- command -----------------------------------------------------------------

def _cmd_analyze(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    if cfg.env_file:
        print(f"(loaded config from {cfg.env_file})", file=sys.stderr)
    if args.platform:
        cfg.platform = args.platform
    if args.llm_provider:
        cfg.llm_provider = args.llm_provider
    if args.model:
        cfg.llm_model = args.model
    if args.log_lines:
        cfg.log_lines = args.log_lines

    try:
        provider = get_provider(cfg.platform)
        provider.preflight()
        ref = provider.resolve(args.target, namespace=args.namespace)
        if args.container:
            ref.container = args.container
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Phase 11 — build the approval gate once; reused for direct deep
    # inspection (--no-llm --deep), the scaffold engine, and the
    # applied-proposal path below.
    analyze_gate = approvals.make_gate(
        yes=args.yes,
        no_prompt=args.no_prompt,
        mode=args.approvals or cfg.approvals_mode,
    )

    # --no-llm: dump the collected context only — no investigation.
    if args.no_llm:
        try:
            ctx = collect(provider, ref, log_lines=cfg.log_lines)
        except ProviderError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.deep and ctx.workload.running:
            from . import deepinspect

            try:
                ctx.deep_inspection = deepinspect.run(
                    provider, ref, gate=analyze_gate,
                )
            except ProviderError as exc:
                ctx.notes.append(f"deep inspection skipped: {exc}")
        print(_render_context(ctx))
        return 0

    # Full multi-agent investigation (the Phase 6 scaffold — HLD §11).
    # Each agent uses the default LLM unless a PODDEBUGGER_<ROLE>_LLM_* override
    # is set; AgentLLMs resolves that per role.
    try:
        llms = AgentLLMs.from_config(cfg)
        # Phase 8: --research enables the Librarian. The backend defaults to
        # noop (air-gap safe); ``--search-backend`` or PODDEBUGGER_SEARCH_BACKEND
        # picks a real one.
        search_backend = None
        if args.research:
            try:
                search_backend = get_backend(
                    args.search_backend or cfg.search_backend
                )
            except SearchError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        engine = InvestigationEngine(
            provider, llms,
            log_lines=cfg.log_lines,
            verbose=args.verbose,
            max_iterations=15 if args.deep else DEFAULT_MAX_ITERATIONS,
            remediation_enabled=bool(args.fix),
            research_enabled=bool(args.research),
            search_backend=search_backend,
            gate=analyze_gate,
        )
        diagnosis = engine.investigate(ref)
    except (LLMError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("(tip: use --no-llm to dump the collected context without an LLM)",
              file=sys.stderr)
        return 1

    # Phase 7B — investigate → propose → (optionally) apply.
    applied: dict | None = None
    if args.fix:
        try:
            proposal = engine.propose_remediation(diagnosis)
        except LLMError as exc:
            print(f"error: remediator failed: {exc}", file=sys.stderr)
            proposal = None
        diagnosis.proposed_remediation = proposal
        if args.confirm and proposal and proposal.get("validated"):
            # Phase 11: gate the apply. Printed proposal is informational;
            # the gate is consent (HLD §16.9). Reuse analyze_gate so a
            # session-allow on a probe earlier in the run carries over.
            applied = _apply_proposal(
                provider, ref, proposal, args.max_risk, gate=analyze_gate,
            )

    if args.json:
        out = _diagnosis_to_dict(diagnosis)
        if applied is not None:
            out["applied_remediation"] = applied
        print(json.dumps(out, indent=2))
    else:
        print(_render_diagnosis(diagnosis))
        if diagnosis.proposed_remediation:
            print(_render_proposal(diagnosis.proposed_remediation))
        if applied is not None:
            print(_render_applied(applied))
        st = engine.state
        print(f"(investigated with {llms.describe()} — "
              f"{st.iteration} iterations, {len(st.confirmed_findings)} findings; "
              f"trail: {engine.workspace.path})")
    return 0


def _apply_proposal(provider, ref, proposal: dict, max_risk: str,
                    *, verify_wait: int = remediation.DEFAULT_VERIFY_WAIT,
                    gate=None) -> dict:
    """Execute a validated proposal if its risk tier is allowed (HLD §12.5).

    Phase 7D: also captures a baseline, runs verify_recovery on the same ref
    after the action, and persists the result so ``remediate --undo`` can
    revert it.
    """
    risk = proposal.get("risk", "medium")
    ranking = {"low": 0, "medium": 1, "high": 2}
    if ranking.get(risk, 99) > ranking.get(max_risk, 0):
        nudge = "medium" if risk == "medium" else "high"
        return {
            "executed": False,
            "skipped": True,
            "reason": f"risk={risk} exceeds --max-risk={max_risk}; "
                      f"re-run with --max-risk {nudge} to apply",
            "action": proposal["action"],
        }
    # Rebuild a Plan from the dict (the engine already validated this proposal).
    try:
        params = remediation.parse_params(proposal["action"], proposal.get("params") or {})
        plan = remediation.make_plan(provider, ref, proposal["action"], params)
    except (remediation.RemediationError, ProviderError) as exc:
        return {"executed": False, "skipped": True, "reason": str(exc),
                "action": proposal["action"]}
    baseline = remediation.capture_baseline(provider, ref) if verify_wait > 0 else {}
    result = remediation.execute(provider, ref, plan, gate=gate)
    verification = None
    if result.executed and verify_wait > 0:
        verification = remediation.verify_recovery(
            provider, ref, baseline, plan.action, verify_wait,
        )
    saved_path = None
    if result.executed:
        saved_path = str(remediation.save_for_undo(ref, {
            "action": result.action,
            "executed": True,
            "result": result.result,
            "plan": result.plan,
            "reversal": result.reversal,
            "target": _ref_to_dict(ref),
            "verification": verification,
        }))
    return {
        "executed": result.executed,
        "skipped": False,
        "action": result.action,
        "result": result.result,
        "plan": result.plan,
        "reversal": result.reversal,
        "verification": verification,
        "saved_to": saved_path,
    }


def _render_proposal(p: dict) -> str:
    """Pretty-print the Remediator's proposal under the diagnosis."""
    if p.get("action") == "none":
        body = f"none — {p.get('reason', '(no reason given)')}"
        if "validation_error" in p:
            body += f"\n  (rejected proposal: {p['validation_error']})"
        return f"Proposed remediation:\n  {body}\n"
    lines = [
        "Proposed remediation:",
        f"  action:          {p['action']}  ({p.get('risk', '?')} risk)",
        f"  params:          {p.get('params', {})}",
        f"  rationale:       {p.get('rationale', '')}",
        f"  expected effect: {p.get('expected_effect', '')}",
        f"  model confidence: {p.get('confidence', 0)}",
    ]
    if p.get("reversal"):
        lines.append(f"  reversal:        {p['reversal']}")
    lines.append("  (use --confirm to apply; --max-risk medium to allow medium-risk actions)")
    return "\n".join(lines) + "\n"


def _render_applied(a: dict) -> str:
    if a.get("skipped"):
        return f"\nRemediation NOT applied: {a.get('reason', '')}\n"
    status = "executed" if a.get("executed") else "FAILED"
    lines = [
        "",
        f"Remediation [{a.get('action')}] {status}: {a.get('result', '')}",
    ]
    if a.get("verification"):
        lines.append(_render_verification(a["verification"]))
    if a.get("saved_to"):
        lines.append(f"(saved for --undo: {a['saved_to']})")
    return "\n".join(lines) + "\n"


def _cmd_remediate(args: argparse.Namespace) -> int:
    cfg = Config.from_env()
    if cfg.env_file:
        print(f"(loaded config from {cfg.env_file})", file=sys.stderr)
    if args.platform:
        cfg.platform = args.platform

    # --- Phase 7D: --undo replays the reversal captured on the prior apply.
    if args.undo is not None:
        return _cmd_remediate_undo(args, cfg)

    if not args.target:
        print("error: target is required (or pass --undo to revert the prior "
              "remediation)", file=sys.stderr)
        return 2

    try:
        provider = get_provider(cfg.platform)
        provider.preflight()
        ref = provider.resolve(args.target, namespace=args.namespace)
        if args.container:
            ref.container = args.container
        params = remediation.parse_params(args.action, args.param or [])
        plan = remediation.make_plan(provider, ref, args.action, params)
    except (ProviderError, remediation.RemediationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return _run_plan(provider, ref, plan, args, label="remediation")


def _cmd_remediate_undo(args: argparse.Namespace, cfg: Config) -> int:
    """Replay the reversal captured by a prior ``remediate --confirm``."""
    try:
        provider = get_provider(cfg.platform)
        provider.preflight()
        # --undo takes an optional PATH (empty string = "use the auto-saved
        # file for this target"); without an explicit target we *require* a
        # path so we know whose state to read.
        path = args.undo if args.undo else None
        ref_hint = None
        if not path:
            if not args.target:
                print("error: --undo without PATH needs the original target "
                      "(or pass --undo PATH directly)", file=sys.stderr)
                return 2
            ref_hint = provider.resolve(args.target, namespace=args.namespace)
            if args.container:
                ref_hint.container = args.container
        payload = remediation.load_for_undo(ref=ref_hint, path=path)
        ref, action, params = remediation.undo_from(payload)
        # The reversal action may be supported on this provider only if its
        # platform matches; trust the saved target's platform.
        if ref.platform != provider.name and not (
            ref.platform == "openshift" and provider.name == "kubernetes"
        ):
            print(f"error: saved remediation is for platform {ref.platform!r} "
                  f"but CLI is using {provider.name!r}; pass --platform to match",
                  file=sys.stderr)
            return 1
        cleaned = remediation.parse_params(action, params)
        plan = remediation.make_plan(provider, ref, action, cleaned)
    except (ProviderError, remediation.RemediationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return _run_plan(provider, ref, plan, args, label="undo")


def _run_plan(provider, ref, plan, args: argparse.Namespace, *, label: str) -> int:
    """Shared dry-run / confirm / execute / verify flow."""
    # Dry-run / no --confirm: print the plan, do not execute.
    if args.dry_run or not args.confirm:
        if args.json:
            print(json.dumps({
                "action": plan.action,
                "executed": False,
                "result": f"dry run — re-run with --confirm to {label}",
                "plan": _plan_to_dict(plan),
                "reversal": plan.reversal or None,
                "target": _ref_to_dict(ref),
            }, indent=2))
        else:
            print(remediation.render_plan(plan))
            print()
            print(f"dry run — re-run with --confirm to {label}")
        return 0

    # Capture baseline *before* mutating so verify_recovery has something
    # to compare against.
    wait_seconds = 0 if args.no_verify else max(0, args.verify_wait)
    baseline = remediation.capture_baseline(provider, ref) if wait_seconds > 0 else {}

    # Phase 11 — build the approval gate based on flags + TTY state.
    gate = approvals.make_gate(
        yes=args.yes,
        no_prompt=args.no_prompt,
        mode=getattr(args, "approvals", None) or Config.from_env().approvals_mode,
    )

    result = remediation.execute(provider, ref, plan, gate=gate)
    if not result.executed and result.result.startswith("refused by approval gate"):
        # Non-TTY refusal: surface a friendly hint about --yes / --no-prompt.
        hint = (
            "  hint: re-run on a TTY to be prompted, pass --yes to auto-approve, "
            "or add a rule with `poddebugger approvals add ...`"
        )
        if not args.json:
            print(hint, file=sys.stderr)

    verification = None
    if result.executed and wait_seconds > 0:
        verification = remediation.verify_recovery(
            provider, ref, baseline, plan.action, wait_seconds,
        )
    elif result.executed:
        verification = {"outcome": "skipped",
                        "reason": "verification disabled (--no-verify)",
                        "baseline": baseline, "observed": {},
                        "waited_seconds": 0}

    # Persist a successful execution so a follow-up `--undo` can read it.
    saved_path = None
    if result.executed:
        payload = {
            "action": result.action,
            "executed": True,
            "result": result.result,
            "plan": result.plan,
            "reversal": result.reversal,
            "target": _ref_to_dict(ref),
            "verification": verification,
        }
        saved_path = str(remediation.save_for_undo(ref, payload))

    if args.json:
        out = {
            "action": result.action,
            "executed": result.executed,
            "result": result.result,
            "plan": result.plan,
            "reversal": result.reversal,
            "target": _ref_to_dict(ref),
            "verification": verification,
            "saved_to": saved_path,
        }
        print(json.dumps(out, indent=2))
    else:
        status = "executed" if result.executed else "FAILED"
        print(remediation.render_plan(plan))
        print()
        print(f"{label} [{result.action}] {status}: {result.result}")
        if verification:
            print(_render_verification(verification))
        if saved_path:
            print(f"(saved for --undo: {saved_path})")
    return 0 if result.executed else 1


def _plan_to_dict(plan: remediation.Plan) -> dict:
    return dataclasses.asdict(plan)


def _ref_to_dict(ref) -> dict:
    return {
        "name": ref.name,
        "namespace": ref.namespace,
        "container": ref.container,
        "platform": ref.platform,
    }


def _render_verification(v: dict) -> str:
    """Pretty-print a verification block under a remediation result."""
    outcome = v.get("outcome", "unknown")
    icons = {"recovered": "✓", "still-failing": "✗",
             "unknown": "?", "skipped": "-"}
    line = f"verification:    [{icons.get(outcome, '?')}] {outcome}"
    reason = v.get("reason", "")
    if reason:
        line += f"  ({reason})"
    waited = v.get("waited_seconds", 0)
    if waited:
        line += f"  after {waited}s"
    return line


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="poddebugger",
        description="AI agent that troubleshoots failing pods/containers.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser(
        "analyze",
        help="diagnose a container or pod (runs the multi-agent investigation)",
    )
    a.add_argument("target", help="container or pod name/id")
    a.add_argument(
        "--platform",
        choices=["podman", "kubernetes", "openshift"],
        help="container runtime",
    )
    a.add_argument("--namespace", "-n", help="namespace (Kubernetes/OpenShift only)")
    a.add_argument("--container", help="specific container within a pod")
    a.add_argument("--log-lines", type=int, help="number of log lines to collect")
    a.add_argument(
        "--llm-provider",
        choices=["anthropic", "openai", "ollama", "llamacpp"],
        help="LLM backend",
    )
    a.add_argument("--model", help="LLM model id override")
    a.add_argument(
        "--deep",
        action="store_true",
        help="thorough mode: raise the investigation iteration budget",
    )
    a.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="show the investigation's role-by-role progress",
    )
    a.add_argument("--no-llm", action="store_true",
                   help="dump the collected context only, skip the investigation")
    a.add_argument("--json", action="store_true", help="emit the diagnosis as JSON")
    a.add_argument(
        "--fix",
        action="store_true",
        help="after the verdict, ask the Remediator agent to propose a "
             "catalog action; use --confirm to also apply it",
    )
    a.add_argument(
        "--confirm",
        action="store_true",
        help="with --fix: apply the proposed remediation if its risk is within "
             "--max-risk; without --confirm the proposal is shown but not executed",
    )
    a.add_argument(
        "--max-risk",
        choices=["low", "medium", "high"],
        default="low",
        help="with --fix --confirm: only auto-apply actions at or below this "
             "risk tier (default: low — restart/scale). `high` is needed for "
             "the opt-in `shell` action.",
    )
    a.add_argument(
        "--allow-shell",
        action="store_true",
        help="register the freeform `shell` action in the catalog for this "
             "run. Off by default. Combined with --fix --confirm --max-risk "
             "high, lets the Remediator agent pick `shell`; combined with "
             "`remediate --action shell --param command=...`, lets you "
             "invoke it directly. Equivalent to setting PODDEBUGGER_ALLOW_SHELL=1.",
    )
    a.add_argument(
        "--research",
        action="store_true",
        help="enable the Librarian agent — the Coordinator may dispatch a "
             "web search to look up known issues. Off by default (air-gap "
             "safe); needs a backend like 'duckduckgo'.",
    )
    a.add_argument(
        "--search-backend",
        default=None, metavar="NAME-OR-PATH",
        help="with --research: search backend short name "
             "('duckduckgo'/'noop'/'off') or dotted import path. "
             "Default: PODDEBUGGER_SEARCH_BACKEND or 'noop'.",
    )
    _add_approval_flags(a)
    a.set_defaults(func=_cmd_analyze)

    r = sub.add_parser(
        "remediate",
        help="execute a typed catalog action on a workload (restart / scale "
             "/ set-resources / adjust-probe / rollback; opt-in `shell`)",
    )
    r.add_argument(
        "target", nargs="?",
        help="container or pod name/id (omit with --undo PATH)",
    )
    r.add_argument(
        "--action",
        choices=sorted(remediation.list_actions()),
        default="restart",
        help="catalog action (default: restart)",
    )
    r.add_argument(
        "--param", action="append", metavar="KEY=VALUE",
        help="action parameter, repeatable (e.g. --param replicas=3)",
    )
    r.add_argument(
        "--platform",
        choices=["podman", "kubernetes", "openshift"],
        help="container runtime",
    )
    r.add_argument("--namespace", "-n", help="namespace (Kubernetes/OpenShift only)")
    r.add_argument("--container", help="specific container within a pod")
    r.add_argument(
        "--confirm",
        action="store_true",
        help="actually execute the action (without it, this is a dry run)",
    )
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="explicitly preview the plan and exit; overrides --confirm",
    )
    r.add_argument(
        "--undo", nargs="?", const="", default=None, metavar="PATH",
        help="replay the reversal captured by the prior --confirm run. "
             "With PATH reads that result file; without PATH reads the "
             "auto-saved file for the given target.",
    )
    r.add_argument(
        "--verify-wait", type=int, default=remediation.DEFAULT_VERIFY_WAIT,
        metavar="SECONDS",
        help=(f"wait this many seconds after the action then re-check the "
              f"workload (default: {remediation.DEFAULT_VERIFY_WAIT}). "
              "Set 0 (or pass --no-verify) to disable."),
    )
    r.add_argument(
        "--no-verify", action="store_true",
        help="skip the post-remediation recovery check",
    )
    r.add_argument("--json", action="store_true", help="emit the result as JSON")
    r.add_argument(
        "--allow-shell",
        action="store_true",
        help="register the freeform `shell` action in the catalog for this "
             "run, so `--action shell --param command=...` works. Off by "
             "default. Equivalent to setting PODDEBUGGER_ALLOW_SHELL=1.",
    )
    _add_approval_flags(r)
    r.set_defaults(func=_cmd_remediate)

    _build_approvals_parser(sub)

    return parser


def _cmd_approvals_list(args: argparse.Namespace) -> int:
    path = approvals.default_rules_path()
    try:
        rules = approvals.load_rules(path)
    except approvals.ApprovalDenied as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"path": str(path), "rules": rules}, indent=2))
        return 0
    if not rules:
        print(f"(no rules — {path})")
        return 0
    print(f"# {path}")
    for i, rule in enumerate(rules):
        tgt = rule.get("target") or {}
        bits = [f"{rule.get('decision', '?'):5}",
                f"{rule.get('kind', '?')}",
                f"action={rule.get('action', '*')}"]
        for k in ("platform", "name", "namespace"):
            if tgt.get(k):
                bits.append(f"{k}={tgt[k]}")
        if rule.get("expires"):
            bits.append(f"expires={rule['expires']}")
        print(f"  [{i}] " + " ".join(bits))
    return 0


def _cmd_approvals_add(args: argparse.Namespace) -> int:
    path = approvals.default_rules_path()
    try:
        rules = approvals.load_rules(path)
    except approvals.ApprovalDenied as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    target: dict = {}
    if args.target_platform:
        target["platform"] = args.target_platform
    if args.target_name:
        target["name"] = args.target_name
    if args.target_namespace:
        target["namespace"] = args.target_namespace
    rule: dict = {
        "kind": args.kind,
        "action": args.action,
        "target": target,
        "decision": args.decision,
    }
    if args.expires:
        rule["expires"] = args.expires
    rules.append(rule)
    saved = approvals.save_rules(path, rules)
    print(f"added rule [{len(rules) - 1}] -> {saved}")
    return 0


def _cmd_approvals_remove(args: argparse.Namespace) -> int:
    path = approvals.default_rules_path()
    try:
        rules = approvals.load_rules(path)
    except approvals.ApprovalDenied as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not (0 <= args.index < len(rules)):
        print(f"error: no rule at index {args.index} (have {len(rules)})",
              file=sys.stderr)
        return 1
    removed = rules.pop(args.index)
    approvals.save_rules(path, rules)
    print(f"removed [{args.index}] {removed.get('kind')}/{removed.get('action')}")
    return 0


def _cmd_approvals_check(args: argparse.Namespace) -> int:
    from .models import WorkloadRef
    descriptor = approvals.ActionDescriptor(
        kind=args.kind,
        action=args.action,
        target=WorkloadRef(
            name=args.target_name or "",
            namespace=args.target_namespace,
            platform=args.target_platform,
        ),
    )
    path = approvals.default_rules_path()
    rules = approvals.load_rules(path) if path.exists() else []
    gate = approvals.RulesGate(approvals.DenyGate(), rules)
    decision = gate.request(descriptor)
    verdict = "ALLOW" if approvals.is_allowed(decision) else "DENY"
    print(f"{verdict} ({decision.value}) — {descriptor.kind} {descriptor.action} on "
          f"{descriptor.target}")
    return 0


def _build_approvals_parser(sub) -> None:
    """`poddebugger approvals list/add/remove/check` — Phase 11C."""
    ap = sub.add_parser(
        "approvals",
        help="manage persistent approval rules (~/.config/poddebugger/approvals.json)",
    )
    ap_sub = ap.add_subparsers(dest="approvals_cmd", required=True)

    ap_list = ap_sub.add_parser("list", help="show the current rules")
    ap_list.add_argument("--json", action="store_true")
    ap_list.set_defaults(func=_cmd_approvals_list)

    ap_add = ap_sub.add_parser("add", help="add a rule")
    ap_add.add_argument("--kind", required=True, choices=["remediation", "probe"])
    ap_add.add_argument("--action", required=True,
                        help="catalog action name or probe name")
    ap_add.add_argument("--target-platform", default=None,
                        help="platform: podman | kubernetes | openshift")
    ap_add.add_argument("--target-name", default=None)
    ap_add.add_argument("--target-namespace", default=None)
    ap_add.add_argument("--decision", choices=["allow", "deny"], default="allow")
    ap_add.add_argument("--expires", default=None, metavar="YYYY-MM-DD",
                        help="optional ISO date; rule is ignored after this day")
    ap_add.set_defaults(func=_cmd_approvals_add)

    ap_rm = ap_sub.add_parser("remove", help="remove a rule by index")
    ap_rm.add_argument("index", type=int, help="index from `approvals list`")
    ap_rm.set_defaults(func=_cmd_approvals_remove)

    ap_chk = ap_sub.add_parser(
        "check", help="what would the rules decide for this descriptor?",
    )
    ap_chk.add_argument("--kind", required=True, choices=["remediation", "probe"])
    ap_chk.add_argument("--action", required=True)
    ap_chk.add_argument("--target-platform", required=True)
    ap_chk.add_argument("--target-name", default="")
    ap_chk.add_argument("--target-namespace", default=None)
    ap_chk.set_defaults(func=_cmd_approvals_check)


def _add_approval_flags(parser: argparse.ArgumentParser) -> None:
    """Shared --yes / --no-prompt / --approvals flags (Phase 11)."""
    parser.add_argument(
        "--yes",
        action="store_true",
        help="auto-approve every mutating step in this run — no prompts. "
             "Use for trusted automation; the approval gate is bypassed.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="never ask interactively. Refuses anything not pre-approved "
             "by a persistent rule. Right default for CI.",
    )
    parser.add_argument(
        "--approvals",
        choices=approvals.VALID_MODES, default=None,
        help="approvals mode: 'session' (default — prompt + remember "
             "in-memory), 'persistent' (also offer [P]ersist to save a rule), "
             "or 'off' (ignore the rules file entirely). Overrides "
             "PODDEBUGGER_APPROVALS_MODE.",
    )


def main(argv: list[str] | None = None) -> int:
    # Sniff --allow-shell BEFORE building the parser so the freeform shell
    # action is in the catalog when argparse computes `--action` choices.
    # The flag itself is still defined on both subparsers (for --help) — this
    # just makes it order-independent.
    effective_argv = sys.argv[1:] if argv is None else argv
    if "--allow-shell" in effective_argv:
        remediation.enable_shell_action()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
