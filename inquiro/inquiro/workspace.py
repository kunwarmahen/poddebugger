"""Per-run investigation workspace.

Each investigation gets its own directory, tracked as a git repository with one
commit per iteration — so a run is replayable, auditable, and resumable. If
git is unavailable the workspace still records state to disk; only the commit
history is skipped.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .state import InvestigationState

# A fixed identity so commits work without the host's global git config.
_GIT_IDENTITY = ["-c", "user.name=inquiro", "-c", "user.email=inquiro@localhost"]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-").lower() or "run"


def _default_base() -> Path:
    """Default run base, overridable by ``INQUIRO_RUNS_DIR`` (apps can also
    pass their own ``base=`` to :meth:`Workspace.create`)."""
    env = os.environ.get("INQUIRO_RUNS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "inquiro" / "runs"


class Workspace:
    """A directory holding one investigation's state and git history."""

    def __init__(self, path: Path, git_enabled: bool):
        self.path = path
        self.git_enabled = git_enabled

    @classmethod
    def create(cls, target: str, base: Path | None = None) -> "Workspace":
        """Create a fresh run workspace and `git init` it."""
        base = base or _default_base()
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = base / f"{stamp}-{_slug(target)}"
        path.mkdir(parents=True, exist_ok=True)
        return cls(path, git_enabled=cls._git_init(path))

    @staticmethod
    def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=path, capture_output=True, text=True, timeout=15
        )

    @classmethod
    def _git_init(cls, path: Path) -> bool:
        if not shutil.which("git"):
            return False
        try:
            return cls._git(path, "init", "-q").returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def commit(self, state: InvestigationState, message: str) -> None:
        """Persist the state and, if git is available, commit this iteration."""
        (self.path / "state.json").write_text(
            json.dumps(state.to_dict(), indent=2, sort_keys=True)
        )
        (self.path / "state.md").write_text(state.render())
        if not self.git_enabled:
            return
        try:
            self._git(self.path, "add", "-A")
            self._git(self.path, *_GIT_IDENTITY, "commit", "-q",
                      "--allow-empty", "-m", message)
        except (OSError, subprocess.SubprocessError):
            self.git_enabled = False  # degrade gracefully for the rest of the run

    def commit_count(self) -> int:
        """Number of commits recorded so far (0 if git is disabled)."""
        if not self.git_enabled:
            return 0
        proc = self._git(self.path, "rev-list", "--count", "HEAD")
        if proc.returncode != 0:
            return 0
        return int((proc.stdout or "0").strip() or 0)

    def load_state(self) -> InvestigationState:
        """Reload the persisted state — used to resume an investigation."""
        data = json.loads((self.path / "state.json").read_text())
        return InvestigationState.from_dict(data)
