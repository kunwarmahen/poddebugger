"""Compat re-export — the canonical home is :mod:`poddebugger.framework.llm`.

The LLM client ABC is framework code. Phase 10B moved the implementation
under :mod:`poddebugger.framework`; this module keeps the old import path
working for callers (and for the existing per-provider client modules in
this package, which subclass :class:`LLMClient`).
"""

from __future__ import annotations

from ..framework.llm import LLMClient, LLMError  # noqa: F401 — re-exports

__all__ = ["LLMClient", "LLMError"]
