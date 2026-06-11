"""Offline prompt-pack optimizer (Phase 15C — HLD §19.4).

OPRO-style loop: score the scenario suite with the current pack, show an
LLM critic the target roles' prompts plus the failure table, let it
propose ONE role's replacement prompt, re-score, and adopt the edit into
the pack **only if the score strictly improves**.

Deliberately offline and human-gated: this never runs inside `analyze`,
and adopted edits land as plain file changes in the pack directory — keep
the pack under version control and review the diff before trusting it.
The score function and the critic LLM are injectable, so the loop is
fully testable without Podman or a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .framework.json_utils import extract_json
from .llm.base import LLMError
from .promptpack import (
    PromptPackError,
    default_prompts,
    load_pack,
    validate_prompt,
)

#: Roles the critic may edit by default — the judgment-heavy ones. The
#: Coordinator/Prober/Auditor/Adjudicator protocols are control flow and
#: riskier to mutate; pass --role explicitly to include them.
DEFAULT_TARGET_ROLES = (
    "Scout", "Planner", "Analyst", "Verifier", "Reporter", "Remediator",
)

CRITIC_SYSTEM = """You are a prompt engineer improving the system prompts of a \
multi-agent container-debugging team. You will see the current system prompts \
for some roles and a table of evaluation failures (expected vs. actual).

Propose ONE improved system prompt for the single role most responsible for
the failures. Make targeted edits — keep the role's job and its JSON
answer-format instructions intact (the engine parses each agent's reply as
JSON; a prompt that drops the format breaks the team).

Return JSON:
{ "role": "<one of the roles shown>",
  "diagnosis": "why this role causes the failures",
  "prompt": "the FULL replacement system prompt" }"""


@dataclass
class RoundReport:
    round: int
    adopted: bool
    role: str = ""
    detail: str = ""              # critic diagnosis or skip reason
    candidate_total: int | None = None


@dataclass
class OptimizeReport:
    baseline_total: int
    max_total: int
    final_total: int
    rounds: list[RoundReport] = field(default_factory=list)


def _failures_block(score) -> str:
    lines = []
    for r in score.results:
        status = f"{r.points}/{r.max_points}"
        bits = [f"- {r.name} [{status}]"]
        if r.error:
            bits.append(f"error: {r.error}")
        else:
            ok = "OK" if r.classification_ok else "WRONG"
            bits.append(f"classification: got {r.classification or '(none)'!r}, "
                        f"expected one of {list(r.expected_classification)} ({ok})")
            if r.expected_action is not None:
                ok = "OK" if r.action_ok else "WRONG"
                bits.append(f"proposed action: got {r.action or '(none)'!r}, "
                            f"expected one of {list(r.expected_action)} ({ok})")
        lines.append("  ".join(bits))
    return "\n".join(lines)


def build_critic_user(prompts: dict[str, str], score) -> str:
    blocks = [f"Evaluation results ({score.total}/{score.max_total}):",
              _failures_block(score), "", "Current system prompts:"]
    for role, text in prompts.items():
        blocks.append(f"\n=== {role} ===\n{text}")
    blocks.append("\nPropose the single most valuable prompt improvement.")
    return "\n".join(blocks)


def optimize(pack_dir: Path | str, *, score_fn, critic,
             roles=DEFAULT_TARGET_ROLES, rounds: int = 1,
             log=lambda msg: None) -> OptimizeReport:
    """Run the improve-and-keep-if-better loop against a pack directory.

    ``score_fn(pack: dict) -> SuiteScore`` and ``critic`` (an LLMClient)
    are injected. Adopted edits are written into ``pack_dir`` immediately
    so each round builds on the last.
    """
    pack_dir = Path(pack_dir)
    pack = load_pack(pack_dir)
    roles = tuple(roles)
    # What the engine would actually use per role: pack file or built-in.
    effective = default_prompts()
    effective.update(pack)

    best = score_fn(pack)
    log(f"baseline: {best.total}/{best.max_total}")
    report = OptimizeReport(baseline_total=best.total,
                            max_total=best.max_total,
                            final_total=best.total)

    for i in range(1, rounds + 1):
        if best.total >= best.max_total:
            log("perfect score — nothing to optimize")
            break
        user = build_critic_user(
            {r: effective[r] for r in roles if r in effective}, best)
        try:
            data = extract_json(critic.complete(CRITIC_SYSTEM, user))
        except LLMError as exc:
            log(f"round {i}: critic failed — {exc}")
            report.rounds.append(RoundReport(i, False, detail=f"critic: {exc}"))
            continue
        role = str(data.get("role", "")).strip()
        text = str(data.get("prompt", ""))
        diagnosis = str(data.get("diagnosis", ""))[:200]
        try:
            if role not in roles:
                raise PromptPackError(
                    f"critic picked {role!r} — not a target role")
            validate_prompt(role, text)
        except PromptPackError as exc:
            log(f"round {i}: rejected — {exc}")
            report.rounds.append(RoundReport(i, False, role=role,
                                             detail=str(exc)))
            continue

        candidate = dict(pack)
        candidate[role] = text
        cand = score_fn(candidate)
        log(f"round {i}: candidate edit to {role} scored "
            f"{cand.total}/{cand.max_total} (best {best.total})")
        if cand.total > best.total:
            pack, best = candidate, cand
            effective[role] = text
            (pack_dir / f"{role}.txt").write_text(text)
            log(f"round {i}: ADOPTED {role} -> {pack_dir / (role + '.txt')}")
            report.rounds.append(RoundReport(
                i, True, role=role, detail=diagnosis,
                candidate_total=cand.total))
        else:
            log(f"round {i}: discarded (no improvement)")
            report.rounds.append(RoundReport(
                i, False, role=role, detail="no improvement",
                candidate_total=cand.total))

    report.final_total = best.total
    return report
