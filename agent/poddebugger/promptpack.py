"""Prompt packs — agent system prompts as versioned data (Phase 15C, HLD §19.4).

A pack is a directory of ``<Role>.txt`` files (``Scout.txt``,
``Reporter.txt``, …), each a full system-prompt replacement for that role.
Missing files mean "use the built-in". Packs change what the agents are
*told* — the capability boundary (catalog validation, approval gate) is
untouched, so a bad pack can degrade quality but never safety.

Loading validates each file: known role, non-empty, size-capped, and the
JSON-protocol marker preserved — a pack cannot silently break the agents'
wire format. ``dump_pack`` materializes the built-in prompts as a starting
pack (the thing ``poddebugger optimize`` mutates); keep packs under
version control so winning edits arrive as reviewable diffs.
"""

from __future__ import annotations

from pathlib import Path

from .scaffold.agents import Coder, Librarian, Remediator, built_in_agents

#: Every agent's prompt instructs the model to answer in JSON; a replacement
#: prompt that drops this breaks the engine's wire format, so load refuses it.
PROTOCOL_MARKER = "JSON"

MAX_PROMPT_CHARS = 16000


class PromptPackError(ValueError):
    """A pack directory or file that cannot be used."""


def default_agents():
    """Fresh instances of every promptable built-in role (opt-ins included)."""
    return built_in_agents() + [Remediator(), Librarian(), Coder()]


def known_roles() -> list[str]:
    return [a.name for a in default_agents()]


def default_prompts() -> dict[str, str]:
    """Role -> built-in system prompt."""
    return {a.name: a.system_prompt for a in default_agents()}


def validate_prompt(role: str, text: str, *, roles: set[str] | None = None) -> None:
    """Raise :class:`PromptPackError` if this replacement prompt is unusable."""
    valid = roles if roles is not None else set(known_roles())
    if role not in valid:
        raise PromptPackError(
            f"unknown role {role!r} — known roles: {', '.join(sorted(valid))}"
        )
    if not text or not text.strip():
        raise PromptPackError(f"{role}: replacement prompt is empty")
    if len(text) > MAX_PROMPT_CHARS:
        raise PromptPackError(
            f"{role}: prompt is {len(text)} chars (max {MAX_PROMPT_CHARS})"
        )
    if PROTOCOL_MARKER not in text:
        raise PromptPackError(
            f"{role}: prompt must keep the {PROTOCOL_MARKER!r} answer-format "
            "instruction — without it the agent breaks the JSON wire format"
        )


def load_pack(directory: Path | str) -> dict[str, str]:
    """Read and validate a pack. Returns ``{role: prompt}``.

    Only ``*.txt`` files are considered; the stem is the role name.
    """
    path = Path(directory)
    if not path.is_dir():
        raise PromptPackError(f"prompt pack {path} is not a directory")
    pack: dict[str, str] = {}
    for f in sorted(path.glob("*.txt")):
        text = f.read_text()
        validate_prompt(f.stem, text)
        pack[f.stem] = text
    if not pack:
        raise PromptPackError(f"prompt pack {path} contains no <Role>.txt files")
    return pack


def dump_pack(directory: Path | str, *, force: bool = False) -> list[Path]:
    """Write the built-in prompts as a starting pack. Refuses to clobber an
    existing pack unless ``force``."""
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    existing = list(path.glob("*.txt"))
    if existing and not force:
        raise PromptPackError(
            f"{path} already holds {len(existing)} prompt file(s) — "
            "use --force to overwrite"
        )
    written = []
    for role, text in default_prompts().items():
        target = path / f"{role}.txt"
        target.write_text(text)
        written.append(target)
    return written


def describe_pack(directory: Path | str) -> list[tuple[str, int, bool]]:
    """``(role, chars, differs_from_builtin)`` per file — for ``prompts list``."""
    pack = load_pack(directory)
    defaults = default_prompts()
    return [(role, len(text), text != defaults.get(role))
            for role, text in sorted(pack.items())]
