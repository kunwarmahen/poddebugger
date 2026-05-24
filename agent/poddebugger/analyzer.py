"""Analyzer — turns a DiagnosticContext into a Diagnosis via the LLM."""

from __future__ import annotations

# ``_extract_json`` is framework code — re-exported below for backward compat.
from .framework.json_utils import extract_json as _extract_json
from .llm.base import LLMClient, LLMError
from .models import Diagnosis, DiagnosticContext, Fix
from .prompts import SYSTEM_PROMPT, build_user_prompt

__all__ = ["_extract_json", "_to_diagnosis", "analyze"]


def _to_diagnosis(data: dict) -> Diagnosis:
    fixes = []
    for f in data.get("suggested_fixes", []) or []:
        if isinstance(f, dict):
            fixes.append(
                Fix(
                    action=str(f.get("action", "")),
                    rationale=str(f.get("rationale", "")),
                    risk=str(f.get("risk", "unknown")),
                )
            )
        else:
            fixes.append(Fix(action=str(f)))
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return Diagnosis(
        summary=str(data.get("summary", "")),
        root_cause=str(data.get("root_cause", "")),
        confidence=max(0.0, min(1.0, confidence)),
        evidence=[str(e) for e in data.get("evidence", []) or []],
        suggested_fixes=fixes,
        needs_deep_inspection=bool(data.get("needs_deep_inspection", False)),
    )


def analyze(ctx: DiagnosticContext, llm: LLMClient) -> Diagnosis:
    """Run the LLM analysis phase and return a structured Diagnosis."""
    user_prompt = build_user_prompt(ctx)
    raw = llm.complete(SYSTEM_PROMPT, user_prompt)
    return _to_diagnosis(_extract_json(raw))
