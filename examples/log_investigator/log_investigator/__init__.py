"""log_investigator — a tiny reference app built on the inquiro framework.

Reads a log file, runs a 3-agent multi-agent investigation (Scout →
Analyst → Reporter), and returns a structured finding about the most-recent
error. Exists to prove the inquiro boundary: nothing about pods or
containers reaches into the framework — the framework just orchestrates
agents over an InvestigationState.

See the README for usage; the test suite drives the full loop with a
scripted LLM so it runs offline.
"""

from .engine import MiniEngine
from .models import LogContext, LogFinding

__all__ = ["MiniEngine", "LogContext", "LogFinding"]
