"""Tests for the Librarian agent + engine ``research`` dispatch (Phase 8)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from poddebugger.llm.base import LLMClient, LLMError
from poddebugger.models import Event, Workload, WorkloadRef
from poddebugger.providers.base import ContainerPlatform
from poddebugger.scaffold import search
from poddebugger.scaffold.agents.librarian import Librarian
from poddebugger.scaffold.engine import InvestigationEngine
from poddebugger.scaffold.state import InvestigationState
from poddebugger.scaffold.workspace import Workspace


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class _FakeProvider(ContainerPlatform):
    name = "podman"

    def preflight(self): pass
    def resolve(self, target, namespace=None):
        return WorkloadRef(name=target, platform="podman")

    def get_workload(self, ref):
        return Workload(ref=ref, kind="container", status="exited",
                        running=False, image="img:1",
                        restart_count=1, exit_code=137, oom_killed=True)

    def get_events(self, ref): return [
        Event(timestamp="t", type="container", reason="oom-killed", message=""),
    ]
    def get_logs(self, ref, tail=200): return "OOMKilled\n"
    def get_spec(self, ref): return {"image": "img:1"}


class _RecordingBackend(search.SearchBackend):
    """Captures the *redacted* query passed to it and returns canned hits."""

    name = "recording"

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[str] = []

    def search(self, query, max_results=5):
        self.calls.append(query)
        return list(self._results)


class _LibrarianOnlyLLM(LLMClient):
    """Returns a canned Librarian response; refuses other roles loudly."""

    name = "librarian-scripted"
    model_id = "scripted-1"

    def __init__(self, response):
        self._response = response
        self.role_calls: list[str] = []

    def complete(self, system, user):
        if "ROLE: Librarian" in system:
            self.role_calls.append("Librarian")
            return json.dumps(self._response)
        raise AssertionError(f"unexpected role; system began {system[:60]!r}")


def _seed_engine(llm, *, research_enabled=True, search_backend=None,
                 base=None) -> InvestigationEngine:
    """Build an engine in a state where ``_do_research`` can run alone.

    Mimics what ``investigate`` sets up before the loop — without running
    Scout/Planner/etc.
    """
    eng = InvestigationEngine(
        _FakeProvider(), llm,
        workspace_base=base, verbose=False,
        research_enabled=research_enabled,
        search_backend=search_backend,
    )
    eng._ref = WorkloadRef(name="web", platform="podman")
    eng.state = InvestigationState(target="web", platform="podman")
    eng.workspace = Workspace.create("web", base=base)
    return eng


# ---------------------------------------------------------------------------
# the agent itself
# ---------------------------------------------------------------------------

class LibrarianApplyTest(unittest.TestCase):
    def setUp(self):
        self.lib = Librarian()

    def test_apply_returns_normalized_proposal(self):
        out = self.lib.apply(None, {  # type: ignore[arg-type]
            "query": "  OOMKilled Java 17  ",
            "rationale": "JVM heap",
        })
        self.assertEqual(out["query"], "OOMKilled Java 17")
        self.assertEqual(out["rationale"], "JVM heap")

    def test_apply_empty_query_passes_through(self):
        out = self.lib.apply(None, {"query": "",  # type: ignore[arg-type]
                                    "reason": "too generic"})
        self.assertEqual(out["query"], "")
        self.assertEqual(out["reason"], "too generic")


# ---------------------------------------------------------------------------
# engine-level: dispatch + redaction + dedup + tolerance
# ---------------------------------------------------------------------------

class ResearchDispatchTest(unittest.TestCase):
    def test_hits_become_evidence_and_record_dispatch(self):
        llm = _LibrarianOnlyLLM({
            "query": "OOMKilled Java 17 heap",
            "rationale": "look up the JVM signature",
        })
        backend = _RecordingBackend([
            search.SearchResult(
                title="Tune your JVM heap on Kubernetes",
                url="https://example.com/jvm",
                snippet="Set -Xmx below the cgroup limit",
                source="recording",
            ),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, search_backend=backend, base=Path(tmp))
            eng._do_research(ctx=None, instruction="JVM heap")

        # Evidence captured + tagged.
        self.assertEqual(len(eng.state.evidence), 1)
        ev = eng.state.evidence[0]
        self.assertIn("JVM heap", ev.summary)
        self.assertTrue(ev.source.startswith("web:"))
        self.assertIn("example.com", ev.source)
        # Audit trail.
        records = [d for d in eng.state.dispatch_history if d.role == "Librarian"]
        self.assertTrue(records, "expected a Librarian dispatch record")
        # The backend was called with the *redacted* query (here unchanged).
        self.assertEqual(backend.calls, ["OOMKilled Java 17 heap"])

    def test_redacts_before_hitting_backend(self):
        # The model insists on including a pod name + IP; the engine scrubs
        # them before the search backend ever sees the query.
        llm = _LibrarianOnlyLLM({
            "query": "pod web-7c4b9d-abc12 OOMKilled at 10.42.0.13",
            "rationale": "...",
        })
        backend = _RecordingBackend([])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, search_backend=backend, base=Path(tmp))
            eng._do_research(ctx=None, instruction="")

        self.assertEqual(len(backend.calls), 1)
        sent = backend.calls[0]
        self.assertNotIn("10.42.0.13", sent)
        self.assertNotIn("abc12", sent)
        self.assertIn("OOMKilled", sent)
        self.assertIn("<ip>", sent)

    def test_empty_query_skips_backend_call(self):
        llm = _LibrarianOnlyLLM({"query": "", "reason": "too generic"})
        backend = _RecordingBackend([])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, search_backend=backend, base=Path(tmp))
            eng._do_research(ctx=None, instruction="")
        self.assertEqual(backend.calls, [])
        # No evidence added, but a "skip" record is on the audit trail.
        self.assertEqual(eng.state.evidence, [])
        skip = [d for d in eng.state.dispatch_history if d.role == "Librarian"]
        self.assertTrue(skip and skip[0].action == "skip")

    def test_duplicate_query_is_deduped(self):
        llm = _LibrarianOnlyLLM({"query": "OOM Java", "rationale": "..."})
        backend = _RecordingBackend([
            search.SearchResult(title="x", url="https://e.com/x"),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, search_backend=backend, base=Path(tmp))
            eng._do_research(ctx=None, instruction="")
            eng._do_research(ctx=None, instruction="")
        self.assertEqual(len(backend.calls), 1, "second call should be deduped")

    def test_noop_backend_records_a_note(self):
        llm = _LibrarianOnlyLLM({"query": "anything", "rationale": "..."})
        with tempfile.TemporaryDirectory() as tmp:
            # research enabled but no backend explicitly passed → defaults to noop
            eng = _seed_engine(llm, base=Path(tmp))
            eng._do_research(ctx=None, instruction="")
        self.assertTrue(any(
            "PODDEBUGGER_SEARCH_BACKEND" in n for n in eng.state.notes
        ))
        self.assertEqual(eng.state.evidence, [])

    def test_backend_search_error_is_logged_not_raised(self):
        class _ThrowingBackend(search.SearchBackend):
            name = "throwing"

            def search(self, query, max_results=5):
                raise search.SearchError("rate limited")

        llm = _LibrarianOnlyLLM({"query": "OOM Java", "rationale": "..."})
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, search_backend=_ThrowingBackend(),
                               base=Path(tmp))
            eng._do_research(ctx=None, instruction="")
        # Run did not crash; the failure landed on the audit trail.
        self.assertTrue(any(e.source == "librarian:error"
                            for e in eng.state.evidence))


class ResearchDisabledTest(unittest.TestCase):
    """Without ``research_enabled``, the Librarian is unreachable."""

    def test_action_silently_ignored_when_disabled(self):
        llm = _LibrarianOnlyLLM({"query": "x", "rationale": "x"})
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, research_enabled=False, base=Path(tmp))
            # The engine's _do_research guards on 'Librarian' membership.
            self.assertNotIn("Librarian", eng._agents)
            eng._dispatch_action("research", ctx=None, target="", instruction="")
        # LLM never called.
        self.assertEqual(llm.role_calls, [])


if __name__ == "__main__":
    unittest.main()
