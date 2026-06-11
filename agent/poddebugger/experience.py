"""Cross-run experience memory (Phase 15A — HLD §19).

After a remediation run reaches a verified outcome, a redacted
:class:`ExperienceRecord` is persisted — the failure signature, what was
tried, and whether it worked. On later runs (opt-in via ``--learn``) the
engine recalls the most similar past records as Evidence, so the team
starts from "we have seen this before" instead of a blank slate.

Learning changes what the LLM is *told*, never what it is *allowed* to do:
recalled records are prompt context only — the catalog validator and the
approval gate remain the capability boundary.

Similarity is deterministic scoring (classification / exit code / OOM /
image / keyword overlap) — no LLM call, no embeddings, no new deps.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .models import DiagnosticContext
from .remediation import _looks_secret
from .scaffold.search import redact_query

DEFAULT_MAX_RECORDS = 500
DEFAULT_RECALL_K = 3
# A record must reach this score against the current signature to be
# recalled — below it, "similar" would just be noise.
MIN_RECALL_SCORE = 3

_KEYWORD_CAP = 12
# Generic words that would inflate overlap between unrelated failures.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "is", "was", "are", "for", "with",
    "this", "that", "from", "has", "have", "had", "of", "to", "in", "on",
    "at", "by", "be", "it", "its", "as", "not", "but", "into", "after",
    "error", "errors", "failed", "failure", "container", "pod", "exited",
    "exit", "code", "workload",
}
_HEXISH_RE = re.compile(r"^[0-9a-f]{6,}$")


def _keywords(text: str) -> list[str]:
    """Distinct, order-preserving signature keywords from free text."""
    out: list[str] = []
    for token in re.split(r"[^A-Za-z0-9_]+", text.lower()):
        if len(token) < 3 or token in _STOPWORDS:
            continue
        if token.isdigit() or _HEXISH_RE.match(token):
            continue
        if token not in out:
            out.append(token)
        if len(out) >= _KEYWORD_CAP:
            break
    return out


def _image_repo(image: str) -> str:
    """Image without tag/digest — registry:5000/app:1.2 -> registry:5000/app."""
    img = (image or "").split("@", 1)[0]
    head, _, tail = img.rpartition(":")
    if head and "/" not in tail:
        return head
    return img


def _redact_params(params) -> dict:
    """Mask values whose keys look secret-like (recurses into set-env's map)."""
    out = {}
    for key, value in (params or {}).items():
        if isinstance(value, dict):
            out[key] = _redact_params(value)
        elif _looks_secret(str(key)):
            out[key] = "***"
        else:
            out[key] = value
    return out


# --- the record --------------------------------------------------------------


@dataclass
class ExperienceRecord:
    """One remembered incident: signature + what was tried + the outcome."""

    id: str
    created: str               # ISO date-time (UTC)
    platform: str
    classification: str        # the Scout's failure category
    image: str = ""
    exit_code: int | None = None
    oom_killed: bool = False
    keywords: list[str] = field(default_factory=list)
    summary: str = ""
    root_cause: str = ""
    # Condensed trail: [{action, params, outcome}] — params already redacted.
    attempts: list[dict] = field(default_factory=list)
    outcome: str = ""          # recovered | unresolved

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExperienceRecord":
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    # --- recall rendering (what the agents get to see) -----------------------

    def recall_summary(self) -> str:
        verdict = "fix worked" if self.outcome == "recovered" else "fix did NOT work"
        return f"past incident ({verdict}): {self.root_cause or self.summary}"[:200]

    def recall_detail(self) -> str:
        lines = [
            f"classification: {self.classification or '(unknown)'}",
            f"image: {self.image or '(unknown)'}",
        ]
        if self.exit_code is not None:
            lines.append(f"exit_code: {self.exit_code}")
        if self.oom_killed:
            lines.append("oom_killed: true")
        for i, a in enumerate(self.attempts, 1):
            lines.append(
                f"tried {i}: {a.get('action')} {a.get('params', {})} "
                f"-> {a.get('outcome', '?')}"
            )
        lines.append(f"final outcome: {self.outcome}")
        return "\n".join(lines)


# --- signatures ---------------------------------------------------------------


def signature_from_context(ctx: DiagnosticContext, classification: str) -> dict:
    """The matchable shape of a failure, built from collected context."""
    w = ctx.workload
    # Keywords come from the most error-dense text we have; redact ids first
    # so a pod-name hash never becomes a "keyword".
    text = " ".join([classification or "", w.error or "", (ctx.logs or "")[-600:]])
    return {
        "platform": w.ref.platform,
        "classification": classification or "",
        "image": w.image or "",
        "exit_code": w.exit_code,
        "oom_killed": bool(w.oom_killed),
        "keywords": _keywords(redact_query(text)),
    }


def score(record: ExperienceRecord, signature: dict) -> int:
    """Deterministic similarity — higher is more similar."""
    s = 0
    if record.classification and record.classification == signature.get("classification"):
        s += 3
    if (record.exit_code is not None
            and record.exit_code == signature.get("exit_code")):
        s += 2
    if record.oom_killed and signature.get("oom_killed"):
        s += 2
    if record.image and _image_repo(record.image) == _image_repo(
            signature.get("image", "")):
        s += 1
    overlap = set(record.keywords) & set(signature.get("keywords") or [])
    s += min(3, len(overlap))
    return s


def make_record(signature: dict, *, summary: str, root_cause: str,
                attempts: list[dict], outcome: str) -> ExperienceRecord:
    """Build a redacted record ready to persist."""
    condensed = [{
        "action": a.get("action"),
        "params": _redact_params(a.get("params") or {}),
        "outcome": a.get("outcome", ""),
    } for a in attempts]
    return ExperienceRecord(
        id=uuid.uuid4().hex[:8],
        created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        platform=signature.get("platform", ""),
        classification=signature.get("classification", ""),
        image=signature.get("image", ""),
        exit_code=signature.get("exit_code"),
        oom_killed=bool(signature.get("oom_killed")),
        keywords=list(signature.get("keywords") or []),
        summary=redact_query(summary or "")[:300],
        root_cause=redact_query(root_cause or "")[:300],
        attempts=condensed,
        outcome=outcome,
    )


# --- the store ----------------------------------------------------------------


def default_store_dir() -> Path:
    env = os.environ.get("PODDEBUGGER_EXPERIENCE_DIR")
    if env:
        return Path(env)
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(data_home) / "poddebugger" / "experience"


class ExperienceStore:
    """One JSON file per record; prunes oldest beyond ``max_records``."""

    def __init__(self, base: Path | str | None = None,
                 max_records: int = DEFAULT_MAX_RECORDS):
        self.path = Path(base) if base else default_store_dir()
        self.max_records = max_records

    def _files(self) -> list[Path]:
        if not self.path.is_dir():
            return []
        # Oldest first. mtime (ns resolution) rather than the filename stamp
        # (second resolution) so same-second saves never tie during pruning.
        def age(p: Path):
            try:
                return (p.stat().st_mtime_ns, p.name)
            except OSError:
                return (0, p.name)
        return sorted(self.path.glob("*.json"), key=age)

    def save(self, record: ExperienceRecord) -> Path | None:
        """Best-effort persist. Returns the file path, or None on failure."""
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            stamp = record.created.replace(":", "").replace("-", "")
            target = self.path / f"{stamp}-{record.id}.json"
            target.write_text(json.dumps(record.to_dict(), indent=2))
            files = self._files()
            for stale in files[: max(0, len(files) - self.max_records)]:
                stale.unlink(missing_ok=True)
            return target
        except OSError:
            return None

    def load_all(self) -> list[ExperienceRecord]:
        """Newest first; malformed files are skipped, never fatal."""
        records = []
        for f in reversed(self._files()):
            try:
                records.append(ExperienceRecord.from_dict(
                    json.loads(f.read_text())))
            except (OSError, ValueError, TypeError):
                continue
        return records

    def find_similar(self, signature: dict,
                     k: int = DEFAULT_RECALL_K) -> list[tuple[ExperienceRecord, int]]:
        """Top-k records scoring at least MIN_RECALL_SCORE, best first."""
        scored = [(rec, score(rec, signature)) for rec in self.load_all()]
        scored = [x for x in scored if x[1] >= MIN_RECALL_SCORE]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def clear(self) -> int:
        """Delete every record; returns how many were removed."""
        files = self._files()
        for f in files:
            try:
                f.unlink()
            except OSError:
                pass
        return len(files)
