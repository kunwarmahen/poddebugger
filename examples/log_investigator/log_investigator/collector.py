"""Read a log file into a :class:`LogContext` — the app's domain collector."""

from __future__ import annotations

from pathlib import Path

from .models import LogContext


def collect(path: str | Path, *, tail_lines: int = 500) -> LogContext:
    """Read up to ``tail_lines`` lines from the end of the log."""
    p = Path(path)
    raw = p.read_text(errors="replace").splitlines()
    return LogContext(path=str(p), lines=raw[-tail_lines:])
