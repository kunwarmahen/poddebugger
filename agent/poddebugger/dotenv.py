"""Minimal ``.env`` loader — zero external dependencies.

Parses ``KEY=VALUE`` lines from a ``.env`` file and populates ``os.environ``.
An already-set environment variable always wins: a ``.env`` file supplies
*defaults*, it does not override an explicitly-exported variable.
"""

from __future__ import annotations

import os
from pathlib import Path


def _parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        # Strip a surrounding pair of matching quotes.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def find_env_file(start: Path | None = None) -> Path | None:
    """Locate a ``.env`` file.

    Honors ``PODDEBUGGER_ENV_FILE`` if set; otherwise walks up from ``start``
    (default: current working directory) and returns the first ``.env`` found.
    """
    explicit = os.environ.get("PODDEBUGGER_ENV_FILE")
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None

    base = (start or Path.cwd()).resolve()
    for directory in [base, *base.parents]:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_dotenv(start: Path | None = None) -> Path | None:
    """Load a ``.env`` file into ``os.environ`` without overriding existing vars.

    Returns the path that was loaded, or ``None`` if no ``.env`` was found.
    """
    path = find_env_file(start)
    if path is None:
        return None
    try:
        parsed = _parse(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    for key, val in parsed.items():
        os.environ.setdefault(key, val)
    return path
