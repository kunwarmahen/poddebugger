"""Compat re-export — the canonical home is :mod:`poddebugger.framework.workspace`.

Per-run investigation workspaces (HLD §11.4) are framework code. Phase 10B
moved the implementation under :mod:`poddebugger.framework`; this module
keeps the old import path working for existing callers.
"""

from __future__ import annotations

from ..framework.workspace import Workspace  # noqa: F401 — re-export

__all__ = ["Workspace"]
