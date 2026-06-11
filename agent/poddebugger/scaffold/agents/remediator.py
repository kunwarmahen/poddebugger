"""Remediator — proposes ONE catalog action (HLD §12.3, Phase 7B).

The Remediator is the tenth scaffold agent — but unlike the nine investigative
roles, it is **only registered when remediation is enabled** (the engine
constructor's ``remediation_enabled=True``, set by ``analyze --fix``). It
never runs as part of the Coordinator's loop; the engine invokes it once,
after the Reporter has written the verdict.

The safety boundary (HLD §12.1) is non-negotiable: the LLM fills a **typed
form** — it picks an action from the catalog and proposes parameters. Code
then validates the proposal against :mod:`poddebugger.remediation` before it
is ever shown as executable.

Output JSON — one of:

    { "action": "<catalog-action>",
      "params": {...},
      "rationale": "...",
      "expected_effect": "...",
      "confidence": 0.0 }

    { "action": "none", "reason": "..." }
"""

from __future__ import annotations

import json

from ... import remediation
from ..prompts import PREAMBLE, evidence_block, list_block
from .base import Agent, AgentContext


_PARAM_HINTS: dict[str, str] = {
    "restart": "(no parameters)",
    "scale": "replicas (int, 0..100)",
    "set-resources": (
        "container, and at least one of memory_limit, cpu_limit, "
        "memory_request (k8s only), cpu_request (k8s only). "
        "Memory accepts Ki/Mi/Gi/K/M/G; cpu accepts '500m' or '0.5'."
    ),
    "adjust-probe": (
        "container, probe (liveness|readiness|startup), "
        "and at least one of initial_delay, period, timeout, failure_threshold "
        "(all integers, seconds where applicable)."
    ),
    "rollback": "revision (optional int >= 1; default rolls back one revision)",
    "set-env": (
        "container, env (object of KEY: value — sets/replaces; a null value "
        "deletes the key). Use this when the workload is missing or has wrong "
        "environment variables (DB credentials, feature flags, config)."
    ),
    "set-image": (
        "container, image (string, e.g. 'mysql:8.0'). Use when the image "
        "reference is wrong or needs a different tag/version."
    ),
    "recreate": (
        "container, plus any subset of image, env, command, args. HIGH RISK: "
        "throws away the running container. Use when several spec fields need "
        "to change together and no single typed action covers it."
    ),
    "shell": (
        "command (string) — runs inside the target container via `sh -c`. "
        "Output is captured and returned. HIGH RISK: no automatic reversal."
    ),
}


def catalog_menu(platform: str) -> str:
    """Render the catalog actions available for ``platform`` as a prompt block."""
    plat = "kubernetes" if platform == "openshift" else platform
    actions = list(remediation.list_actions(plat))
    if not actions:
        return "(no catalog actions for this platform)"
    lines: list[str] = []
    for name in actions:
        spec = remediation.get_spec(name)
        params = _PARAM_HINTS.get(name, "(see catalog)")
        lines.append(
            f"- {name}  (risk: {spec.risk})\n"
            f"    purpose: {spec.description}\n"
            f"    params:  {params}"
        )
    if "shell" in actions:
        lines.append(
            "\nNOTE: `shell` is freeform — prefer any typed action that fits the "
            "diagnosis. Use `shell` only when no typed action will do."
        )
    return "\n".join(lines)


SYSTEM = PREAMBLE + """

ROLE: Remediator. The team has agreed on a diagnosis. Propose AT MOST ONE
remediation action drawn from the **catalog** in the user prompt. You do NOT
emit a command — you fill a typed form, which code then validates against the
catalog schema before anything runs.

Rules — these are absolute:
- The action MUST be one of the catalog names verbatim.
- params MUST follow the parameter hints in the catalog block.
- If no catalog action fits (e.g. the fix is a code change), return
  {"action": "none", "reason": "..."} — be honest, do not invent.
- NEVER propose a free-form command or an action not in the menu.
- Prefer the lowest-risk action that addresses the confirmed finding.

MISSING VALUES (important):
- Some fixes need values only the operator knows — a database password, a
  correct image name, a database name, a config value. You will see a
  "Provided context" block listing values the user has supplied. USE those
  values verbatim in your params.
- NEVER invent a password, an image tag you are not sure of, or any
  credential. If the fix needs a value that is NOT in the provided context,
  do not guess — return the needs_context shape so the user can supply it.

PREVIOUS ATTEMPTS:
- If you see an "Attempt history" block, a prior fix did NOT recover the
  workload. Read the new evidence. Either propose a DIFFERENT action /
  different params that addresses the updated picture, or — if you believe
  the diagnosis itself is wrong and you have no better idea — return
  {"action":"none","reason":"..."}. Do not repeat a fix that already failed.

Return EXACTLY one JSON object — pick ONE of these shapes:

  { "action": "<catalog-action>",
    "params": { ... per the param hints ... },
    "rationale": "why this action follows from the diagnosis",
    "expected_effect": "what the workload should look like after",
    "confidence": 0.0 }

  { "action": "none",
    "reason": "why no catalog action fits — be specific" }

  { "action": "none",
    "reason": "missing context value(s)",
    "needs_context": [ {"key": "db_password",
                        "reason": "MySQL root password for set-env"} ] }"""


class Remediator(Agent):
    """Fills a typed remediation form from the team's confirmed diagnosis."""

    name = "Remediator"
    system_prompt = SYSTEM

    def build_user_prompt(self, ac: AgentContext) -> str:
        state = ac.state
        extras = ac.extras or {}
        platform = extras.get("platform") or ac.ref.platform
        diagnosis = extras.get("diagnosis")
        diagnosis_block = ""
        if diagnosis:
            diagnosis_block = (
                f"Summary:     {diagnosis.get('summary', '')}\n"
                f"Root cause:  {diagnosis.get('root_cause', '')}\n"
                f"Confidence:  {diagnosis.get('confidence', 0)}\n"
            )

        # Phase 13C — values the user supplied that the system can't infer.
        context: dict = extras.get("context") or {}
        if context:
            context_block = "\n".join(f"  {k} = {v}" for k, v in context.items())
        else:
            context_block = "  (none supplied)"

        # Phase 13A — what we already tried that didn't work.
        attempts: list = extras.get("attempts") or []
        attempt_block = ""
        if attempts:
            lines = []
            for i, a in enumerate(attempts, 1):
                lines.append(
                    f"  attempt {i}: {a.get('action')} {a.get('params', {})} "
                    f"→ {a.get('outcome', '?')} ({a.get('reason', '')})"
                )
            attempt_block = (
                "\nAttempt history (these did NOT recover the workload — "
                "try something different or return action=none):\n"
                + "\n".join(lines) + "\n"
            )

        return (
            f"Platform: {platform}\n"
            f"Target:   {ac.ref}\n\n"
            f"Diagnosis:\n{diagnosis_block or '(no diagnosis text supplied)'}\n\n"
            f"Provided context (use these values; do not invent missing ones):\n"
            f"{context_block}\n"
            f"{attempt_block}\n"
            f"Confirmed findings:\n"
            + list_block(
                state.confirmed_findings,
                lambda f: f"{f.id}: {f.statement} ({f.confidence:.0%})",
            )
            + "\n\n"
            f"Evidence:\n{evidence_block(state)}\n\n"
            f"Current spec excerpt:\n"
            f"{json.dumps(ac.ctx.spec_excerpt, indent=2, default=str)}\n\n"
            f"Catalog of available remediation actions:\n"
            f"{catalog_menu(platform)}\n\n"
            "Choose AT MOST ONE action and fill the form. Or return "
            '{"action":"none","reason":"..."} if no catalog action fits.'
        )

    def apply(self, ac: AgentContext, response: dict) -> dict:
        """Return the raw model proposal — the engine handles validation.

        Keeping validation in the engine (rather than here) means the
        ``apply`` step never raises on a malformed proposal: the proposal is
        always recorded, validation errors land in ``state.notes`` and on the
        returned dict so the operator / CLI can show them to a human.
        """
        action = str(response.get("action", "")).strip()
        if not action:
            return {"action": "none", "reason": "empty action from model"}
        if action == "none":
            out = {"action": "none",
                   "reason": str(response.get("reason", "")).strip()
                             or "model returned action=none"}
            # Phase 13C — surface a structured missing-context request.
            nc = response.get("needs_context")
            if isinstance(nc, list) and nc:
                out["needs_context"] = [
                    {"key": str(item.get("key", "")).strip(),
                     "reason": str(item.get("reason", "")).strip()}
                    for item in nc if isinstance(item, dict) and item.get("key")
                ]
            return out
        params = response.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        return {
            "action": action,
            "params": params,
            "rationale": str(response.get("rationale", "")).strip(),
            "expected_effect": str(response.get("expected_effect", "")).strip(),
            "confidence": response.get("confidence", 0.0),
        }
