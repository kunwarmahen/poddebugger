"""Pluggable web-search backends + query redaction (Phase 8 — HLD §13).

The :class:`Librarian` agent (``scaffold/agents/librarian.py``) formulates a
search query; this module *issues* the search. Keeping the network call out
of the agent's :meth:`apply` lets the engine apply the safety boundary in
exactly one place — query redaction before anything leaves the host.

Defaults are air-gap safe (HLD §13.3): the :class:`NoopBackend` is the
default. Set ``PODDEBUGGER_SEARCH_BACKEND=duckduckgo`` (or wire a custom
class via ``PODDEBUGGER_SEARCH_BACKEND=mypkg.mod.MyBackend``) to turn search
on. The :class:`DuckDuckGoBackend` uses the ``ddgs`` library lazily — install
it with ``pip install 'poddebugger[search]'``.

Public surface:

    redact_query(text) -> str           # strips IPs, IDs, k8s pod suffixes
    SearchResult                        # title / url / snippet / source
    SearchBackend                       # abc — subclass to add a backend
    NoopBackend                         # the safe default
    DuckDuckGoBackend                   # concrete: ddgs library
    get_backend(name)                   # factory; dotted-path or short name
"""

from __future__ import annotations

import abc
import importlib
import os
import re
from dataclasses import dataclass
from typing import Iterable


# --- redaction --------------------------------------------------------------

_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{2,5})?\b")
# Loose IPv6 — anything with at least two colons and only hex digits. Covers
# both fully-expanded forms and "::" compressed forms.
_IPV6 = re.compile(r"\b[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}\b")
# UUIDs and 12/40/64-char hex blobs (container IDs, sha hashes).
_HEX_ID = re.compile(r"\b[0-9a-fA-F]{12,64}\b")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# k8s pod suffixes — e.g. "web-7c4b9d-abc12" → "web-<id>". Match the
# Deployment-style trailing "-<replicaset-hash>-<pod-hash>" or just
# "-<pod-hash>" with at least 5 lowercase-alnum chars.
_POD_SUFFIX = re.compile(
    r"(-[a-z0-9]{6,10})(-[a-z0-9]{4,6})?(?=\b|\s|$)"
)
# Long random ports and arbitrary 6+ digit runs that aren't HTTP status codes.
_LONG_NUMBER = re.compile(r"\b\d{6,}\b")


def redact_query(text: str) -> str:
    """Strip identifiers from a query before it leaves the host (HLD §13.3).

    Conservative: replaces IPs, UUIDs, hex IDs, and Deployment-style pod
    suffixes with ``<id>``. Keeps reasons / error classes / image tags intact
    — those carry the failure signature the Librarian wants to look up.
    """
    if not text:
        return ""
    out = _UUID.sub("<uuid>", text)
    out = _IPV4.sub("<ip>", out)
    out = _IPV6.sub("<ip>", out)
    out = _HEX_ID.sub("<id>", out)
    out = _POD_SUFFIX.sub("-<id>", out)
    out = _LONG_NUMBER.sub("<n>", out)
    # collapse repeated whitespace
    return re.sub(r"\s+", " ", out).strip()


# --- result type ------------------------------------------------------------

@dataclass
class SearchResult:
    """One hit returned by a :class:`SearchBackend`."""

    title: str
    url: str = ""
    snippet: str = ""
    source: str = ""  # human-friendly origin tag, e.g. "duckduckgo"

    def domain(self) -> str:
        """Best-effort host extraction for use in Evidence.source."""
        m = re.search(r"^https?://([^/]+)", self.url or "")
        return m.group(1) if m else (self.source or "web")


# --- backends ---------------------------------------------------------------

class SearchBackend(abc.ABC):
    """A pluggable web-search backend.

    Subclasses live anywhere on the Python path and are wired with
    ``PODDEBUGGER_SEARCH_BACKEND=mypkg.mod.MyBackend`` (dotted import path)
    or the short alias ``duckduckgo`` / ``noop``. Backends are constructed
    with no arguments; configuration comes from env vars they choose.
    """

    name: str = "base"

    @abc.abstractmethod
    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        """Run the query and return up to ``max_results`` hits.

        Backends MUST be tolerant: a network failure, an empty result, or
        a rate-limit response should return ``[]`` (or raise
        :class:`SearchError`), never crash the investigation.
        """


class SearchError(RuntimeError):
    """Raised by a backend when search couldn't run (rate-limit, network)."""


class NoopBackend(SearchBackend):
    """The default — never makes a network call. Use it for air-gapped runs.

    A :class:`NoopBackend.search` call records *nothing* on the state — the
    engine knows this and degrades the ``research`` action accordingly.
    """

    name = "noop"

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        return []


class DuckDuckGoBackend(SearchBackend):
    """Search via the `ddgs` library (no API key).

    Lazily imports ``ddgs`` so the dependency stays optional. Install with
    ``pip install 'poddebugger[search]'``. DuckDuckGo may rate-limit cloud
    IPs; on throttle/timeout this returns an empty list rather than raising.
    """

    name = "duckduckgo"

    def __init__(self):
        self._client_cls = None

    def _client(self):
        if self._client_cls is None:
            try:
                from ddgs import DDGS  # type: ignore[import-not-found]
            except ImportError as exc:
                raise SearchError(
                    "the 'ddgs' library is not installed; install it with "
                    "`pip install 'poddebugger[search]'` or set "
                    "PODDEBUGGER_SEARCH_BACKEND=noop"
                ) from exc
            self._client_cls = DDGS
        return self._client_cls

    def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        try:
            cls = self._client()
        except SearchError:
            raise
        try:
            with cls() as client:
                raw = list(client.text(query, max_results=max_results))
        except Exception:  # noqa: BLE001 — ddgs raises a zoo of types on throttle
            # Tolerant by design: never let a search hiccup crash the run.
            return []
        results: list[SearchResult] = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            results.append(SearchResult(
                title=str(r.get("title") or r.get("body") or "")[:200],
                url=str(r.get("href") or r.get("url") or ""),
                snippet=str(r.get("body") or "")[:600],
                source="duckduckgo",
            ))
        return results


# --- factory ----------------------------------------------------------------

_SHORT_ALIASES: dict[str, type[SearchBackend]] = {
    "noop": NoopBackend,
    "off": NoopBackend,
    "disabled": NoopBackend,
    "duckduckgo": DuckDuckGoBackend,
    "ddg": DuckDuckGoBackend,
}


def get_backend(name: str | None = None) -> SearchBackend:
    """Resolve a backend by name. Defaults to NoopBackend.

    ``name`` accepts a short alias (``duckduckgo`` / ``noop`` / ...) or a
    dotted import path (``mypkg.mod.MyBackend``). An import or attribute
    error raises :class:`SearchError` so a misconfigured env var fails
    loudly rather than silently disabling search.
    """
    if not name or name.strip() in ("", "default"):
        name = os.environ.get("PODDEBUGGER_SEARCH_BACKEND", "noop")
    key = (name or "noop").strip().lower()
    if key in _SHORT_ALIASES:
        return _SHORT_ALIASES[key]()
    # Treat anything else as a dotted import path.
    module_name, _, attr = (name or "").rpartition(".")
    if not module_name or not attr:
        raise SearchError(
            f"unknown search backend {name!r}; expected one of "
            f"{sorted(_SHORT_ALIASES)} or a dotted import path"
        )
    try:
        mod = importlib.import_module(module_name)
        cls = getattr(mod, attr)
        backend = cls()
    except (ImportError, AttributeError, TypeError) as exc:
        raise SearchError(
            f"could not load search backend {name!r}: {exc}"
        ) from exc
    if not isinstance(backend, SearchBackend):
        raise SearchError(
            f"{name!r} is not a SearchBackend subclass"
        )
    return backend
