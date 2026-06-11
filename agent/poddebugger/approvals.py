"""Human-in-the-loop interactive approvals (Phase 11 — HLD §16).

Per-action approval prompts that gate any side-effecting step before it
runs. Mirrors Claude Code's permission model: ``[Y]es once /
[A]lways (this session) / [P]ersist / [N]o``.

Public surface:

    Decision                          # enum
    ActionDescriptor                  # what the gate is asked about
    ApprovalGate (abc)                # the contract
    AutoApproveGate / DenyGate / TTYPromptGate / RulesGate
    load_rules(path) / save_rules(path, rules) / default_rules_path()
    make_gate(...)                    # factory the CLI uses

The rules file is JSON (not YAML) so PodDebugger stays zero-dep at
runtime. The HLD example uses YAML for readability — the actual file
shape is::

    { "version": 1, "rules": [ {kind, action, target, decision, expires?}, ... ] }
"""

from __future__ import annotations

import abc
import datetime
import enum
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, TextIO

from .models import WorkloadRef


# --- types ------------------------------------------------------------------


class Decision(enum.Enum):
    """What the gate returns. ``ALLOW_*`` proceed; ``DENY`` raises."""

    ALLOW_ONCE = "once"
    ALLOW_SESSION = "session"
    ALLOW_PERSISTENT = "persistent"
    DENY = "deny"


class ApprovalDenied(Exception):
    """Raised when the gate refuses an action (the caller must not execute)."""


@dataclass
class ActionDescriptor:
    """Everything the gate needs to render a prompt and match against rules."""

    kind: str               # "remediation" | "probe"
    action: str             # action name or probe name
    target: WorkloadRef
    risk: str = "low"       # low | medium
    summary: str = ""       # one-line preview ("scale deployment/web 3 → 5")
    plan: Optional[dict] = None   # for remediations: the Plan dict (old/new/reversal)


# --- gates ------------------------------------------------------------------


class ApprovalGate(abc.ABC):
    """Contract: turn an :class:`ActionDescriptor` into a :class:`Decision`."""

    @abc.abstractmethod
    def request(self, descriptor: ActionDescriptor) -> Decision:
        ...


class AutoApproveGate(ApprovalGate):
    """Always allow once. Used by ``--yes`` and by the operator (the CRD
    already encoded the human's approval)."""

    name = "auto-approve"

    def request(self, descriptor: ActionDescriptor) -> Decision:
        return Decision.ALLOW_ONCE


class DenyGate(ApprovalGate):
    """Always deny. The non-TTY default — so a forgotten ``| cat`` or a CI
    job can't silently mutate. Pair with :class:`RulesGate` to allow what
    a human pre-approved."""

    name = "deny"

    def request(self, descriptor: ActionDescriptor) -> Decision:
        return Decision.DENY


class TTYPromptGate(ApprovalGate):
    """Interactive y / a / [p] / n prompt on a TTY.

    ``allow_persist=True`` adds the ``[P]ersist`` option (only meaningful
    when wrapped by a :class:`RulesGate` that knows how to write the rule).
    ``input_fn`` / ``output_fn`` are injectable so tests can drive the
    prompt without a real terminal.
    """

    name = "tty-prompt"

    def __init__(
        self,
        *,
        allow_persist: bool = False,
        input_fn: Optional[Callable[[str], str]] = None,
        output_fn: Optional[Callable[[str], None]] = None,
    ):
        self._allow_persist = allow_persist
        self._input = input_fn or input
        self._output = output_fn or (lambda msg: print(msg, file=sys.stderr))

    def request(self, descriptor: ActionDescriptor) -> Decision:
        self._output(_render_descriptor(descriptor))
        options = ["[Y]es once", "[A]lways (this session)"]
        if self._allow_persist:
            options.append("[P]ersist (save rule)")
        options.append("[N]o")
        prompt = " / ".join(options) + " > "
        while True:
            answer = self._input(prompt).strip().lower()
            if answer in ("", "y", "yes"):
                return Decision.ALLOW_ONCE
            if answer in ("a", "always", "session"):
                return Decision.ALLOW_SESSION
            if self._allow_persist and answer in ("p", "persist"):
                return Decision.ALLOW_PERSISTENT
            if answer in ("n", "no", "deny"):
                return Decision.DENY
            choices = "y/a/p/n" if self._allow_persist else "y/a/n"
            self._output(f"  (unknown answer {answer!r} — please answer {choices})")


class RulesGate(ApprovalGate):
    """Wraps another gate and consults persistent rules first.

    Match order: explicit ``deny`` rules win over explicit ``allow`` rules
    for the same descriptor (so a deny is the last word). Session-only
    ``ALLOW_SESSION`` decisions from the inner gate are remembered in an
    in-memory set keyed by ``(kind, action, target)`` for the rest of the
    run. ``ALLOW_PERSISTENT`` decisions are written to ``save_path``
    (when set) as a new allow rule.
    """

    name = "rules"

    def __init__(
        self,
        inner: ApprovalGate,
        rules: list[dict],
        *,
        save_path: Optional[Path] = None,
    ):
        self._inner = inner
        self._rules: list[dict] = list(rules or [])
        self._save_path = save_path
        self._session: set[tuple] = set()

    @property
    def rules(self) -> list[dict]:
        return list(self._rules)

    def request(self, descriptor: ActionDescriptor) -> Decision:
        # 1. session-only allow short-circuit
        key = self._session_key(descriptor)
        if key in self._session:
            return Decision.ALLOW_ONCE

        # 2. persistent rules — deny wins
        match = self._match(descriptor)
        if match is not None:
            return match

        # 3. fall through to the inner gate (TTY, AutoApprove, Deny)
        decision = self._inner.request(descriptor)
        if decision == Decision.ALLOW_SESSION:
            self._session.add(key)
        elif decision == Decision.ALLOW_PERSISTENT:
            self._add_rule(descriptor)
        return decision

    # --- internals --------------------------------------------------------

    def _match(self, descriptor: ActionDescriptor) -> Optional[Decision]:
        active = [r for r in self._rules
                  if _rule_matches(r, descriptor) and not _rule_expired(r)]
        if not active:
            return None
        # deny is the last word — return it before honoring any allow.
        if any(r.get("decision") == "deny" for r in active):
            return Decision.DENY
        if any(r.get("decision") == "allow" for r in active):
            return Decision.ALLOW_ONCE
        return None

    def _add_rule(self, descriptor: ActionDescriptor) -> None:
        rule = {
            "kind": descriptor.kind,
            "action": descriptor.action,
            "target": _target_from_ref(descriptor.target),
            "decision": "allow",
        }
        self._rules.append(rule)
        if self._save_path is not None:
            save_rules(self._save_path, self._rules)

    @staticmethod
    def _session_key(descriptor: ActionDescriptor) -> tuple:
        t = descriptor.target
        return (descriptor.kind, descriptor.action,
                t.platform, t.namespace or "", t.name)


# --- rendering / matching helpers ------------------------------------------


def _render_descriptor(descriptor: ActionDescriptor) -> str:
    bar = "─" * 60
    lines = [
        "",
        bar,
        f"PodDebugger wants to {descriptor.kind} → "
        f"{descriptor.action}  (risk: {descriptor.risk})",
        f"  target:  {descriptor.target}  [{descriptor.target.platform}]",
    ]
    if descriptor.summary:
        lines.append(f"  summary: {descriptor.summary}")
    if descriptor.plan:
        old = descriptor.plan.get("old")
        new = descriptor.plan.get("new")
        if old or new:
            lines.append(f"  old → new: {old} → {new}")
        reversal = descriptor.plan.get("reversal")
        if reversal:
            lines.append(f"  reversal:  {reversal}")
        # Stage 13D — code actions carry the full script so the human sees
        # exactly what will run before saying yes.
        script = descriptor.plan.get("script")
        if script:
            body = str(script).splitlines()
            shown = body[:30]
            lines.append(f"  script ({descriptor.plan.get('language', '?')}):")
            lines.extend(f"    | {l}" for l in shown)
            if len(body) > len(shown):
                lines.append(f"    | ... ({len(body) - len(shown)} more lines)")
    lines.append(bar)
    return "\n".join(lines)


def _rule_matches(rule: dict, descriptor: ActionDescriptor) -> bool:
    """True if ``rule`` matches ``descriptor``.

    Each non-empty field in the rule must equal the descriptor's value.
    Empty / missing fields are wildcards.
    """
    if not isinstance(rule, dict):
        return False
    if rule.get("kind") and rule["kind"] != descriptor.kind:
        return False
    if rule.get("action") and rule["action"] != descriptor.action:
        return False
    target = rule.get("target") or {}
    if not isinstance(target, dict):
        return False
    if target.get("platform") and target["platform"] != descriptor.target.platform:
        return False
    if target.get("name") and target["name"] != descriptor.target.name:
        return False
    if target.get("namespace") and target["namespace"] != (descriptor.target.namespace or ""):
        return False
    return True


def _rule_expired(rule: dict) -> bool:
    exp = rule.get("expires")
    if not exp:
        return False
    try:
        date = datetime.date.fromisoformat(str(exp))
    except ValueError:
        # Malformed expiry — treat the rule as inactive so the user is
        # nudged to fix it via `approvals list`.
        return True
    return datetime.date.today() > date


def _target_from_ref(ref: WorkloadRef) -> dict:
    out: dict = {"platform": ref.platform}
    if ref.name:
        out["name"] = ref.name
    if ref.namespace:
        out["namespace"] = ref.namespace
    return out


# --- rules file I/O --------------------------------------------------------


def default_rules_path() -> Path:
    """Resolve the rules file path: env override → XDG → ``~/.config/``."""
    env = os.environ.get("PODDEBUGGER_APPROVALS_FILE")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "poddebugger" / "approvals.json"


def load_rules(path: Path) -> list[dict]:
    """Read the rules file. Returns an empty list if the file does not exist."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovalDenied(
            f"could not read approvals file {path}: {exc}"
        ) from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ApprovalDenied(
            f"approvals file {path}: missing or unsupported version "
            f"(expected 1, got {data.get('version') if isinstance(data, dict) else type(data).__name__})"
        )
    rules = data.get("rules") or []
    if not isinstance(rules, list):
        raise ApprovalDenied(
            f"approvals file {path}: 'rules' must be a list"
        )
    return rules


def save_rules(path: Path, rules: list[dict]) -> Path:
    """Write the rules file (best effort — creates parent dirs)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "rules": list(rules or [])}
    path.write_text(json.dumps(payload, indent=2, sort_keys=False))
    return path


# --- factory used by the CLI -----------------------------------------------


VALID_MODES = ("session", "persistent", "off")


def make_gate(
    *,
    yes: bool = False,
    no_prompt: bool = False,
    mode: str = "session",
    rules_path: Optional[Path] = None,
    stdin: Optional[TextIO] = None,
    output_fn: Optional[Callable[[str], None]] = None,
) -> ApprovalGate:
    """Build the gate the CLI should use, given the flag state.

    ``mode``:
        - ``"session"`` (default): TTY prompt offers Y/A/N; existing rules
          ARE consulted but new persistent rules cannot be written.
        - ``"persistent"``: TTY prompt also offers ``[P]ersist``; new rules
          go to ``rules_path``.
        - ``"off"``: the rules file is ignored entirely.

    ``yes`` collapses to :class:`AutoApproveGate` (no prompts, no rules).
    ``no_prompt`` or a non-TTY stdin uses :class:`DenyGate` as the inner
    gate (still wrapped by :class:`RulesGate` unless ``mode == "off"``).
    """
    if yes:
        return AutoApproveGate()
    if mode not in VALID_MODES:
        raise ValueError(f"approvals mode must be one of {VALID_MODES}, got {mode!r}")

    stdin = stdin or sys.stdin
    interactive = bool(getattr(stdin, "isatty", lambda: False)())
    if no_prompt:
        interactive = False

    if interactive:
        inner: ApprovalGate = TTYPromptGate(
            allow_persist=(mode == "persistent"),
            output_fn=output_fn,
        )
    else:
        inner = DenyGate()

    if mode == "off":
        return inner

    path = rules_path or default_rules_path()
    rules = load_rules(path) if path.exists() else []
    save_path = path if (mode == "persistent") else None
    return RulesGate(inner, rules, save_path=save_path)


# --- convenience: turn a Decision into a yes/no for callers ---------------


def is_allowed(decision: Decision) -> bool:
    """True for any ALLOW_*; False for DENY."""
    return decision in (
        Decision.ALLOW_ONCE,
        Decision.ALLOW_SESSION,
        Decision.ALLOW_PERSISTENT,
    )
