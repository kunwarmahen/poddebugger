"""Unit tests for the Remediator agent (Phase 7B — HLD §12.3).

These tests focus on the agent in isolation and on the engine's
``propose_remediation`` validation layer (the safety boundary). The full
investigation loop is exercised by ``test_scaffold_engine.py``.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from poddebugger.llm.base import LLMClient, LLMError
from poddebugger.models import Diagnosis, Event, Workload, WorkloadRef
from poddebugger.providers.base import ContainerPlatform
from poddebugger.scaffold.agents.remediator import Remediator, catalog_menu
from poddebugger.scaffold.engine import InvestigationEngine
from poddebugger.scaffold.state import InvestigationState
from poddebugger.scaffold.workspace import Workspace
from tests.util import cp


# ----------------------------------------------------------------------------
# fakes shared by the engine-level tests
# ----------------------------------------------------------------------------

class PodmanFakeProvider(ContainerPlatform):
    """Mimics a podman provider — used so the catalog's restart action fits."""

    name = "podman"

    def preflight(self): pass

    def resolve(self, target, namespace=None):
        return WorkloadRef(name=target, platform="podman")

    def get_workload(self, ref):
        return Workload(ref=ref, kind="container", status="exited",
                        running=False, image="img:1",
                        restart_count=3, exit_code=137)

    def get_events(self, ref):
        return [Event(timestamp="t", type="container", reason="died",
                      message="exit_code=137 OOMKilled")]

    def get_logs(self, ref, tail=200):
        return "killed: out of memory\n"

    def get_spec(self, ref):
        return {"image": "img:1", "resources": {"memory_limit": 268435456}}


class RemediatorOnlyLLM(LLMClient):
    """Returns a canned proposal whenever the Remediator is called.

    Other roles get a minimal valid response (so the engine plumbing still
    runs if a test calls ``investigate``) — but the engine-level tests in
    this file call ``propose_remediation`` directly and only invoke the
    Remediator.
    """

    name = "remediator-scripted"
    model_id = "scripted-1"

    def __init__(self, proposal: dict):
        self._proposal = proposal
        self.calls: list[str] = []

    def complete(self, system, user):
        if "ROLE: Remediator" in system:
            self.calls.append("Remediator")
            return json.dumps(self._proposal)
        raise AssertionError(
            f"unexpected role; system began {system[:60]!r}"
        )


def _seed_engine(llm: LLMClient, *, remediation_enabled=True, base=None):
    """Build an engine in a state where ``propose_remediation`` can run.

    Mimics what ``investigate`` sets up before calling ``propose_remediation``
    — without running the whole loop.
    """
    eng = InvestigationEngine(
        PodmanFakeProvider(), llm,
        workspace_base=base, verbose=False,
        remediation_enabled=remediation_enabled,
    )
    eng._ref = WorkloadRef(name="web", platform="podman")
    eng.state = InvestigationState(target="web", platform="podman")
    eng.workspace = Workspace.create("web", base=base)
    return eng


# ----------------------------------------------------------------------------
# the agent itself — prompt / apply contract
# ----------------------------------------------------------------------------

class CatalogMenuTest(unittest.TestCase):
    def test_kubernetes_menu_lists_all_actions(self):
        menu = catalog_menu("kubernetes")
        for name in ("restart", "scale", "set-resources", "adjust-probe", "rollback"):
            self.assertIn(name, menu)

    def test_podman_menu_drops_k8s_only_actions(self):
        menu = catalog_menu("podman")
        self.assertIn("restart", menu)
        self.assertIn("set-resources", menu)
        self.assertNotIn("scale", menu)
        self.assertNotIn("adjust-probe", menu)


class RemediatorApplyTest(unittest.TestCase):
    def setUp(self):
        self.r = Remediator()

    def test_apply_passes_through_a_well_formed_proposal(self):
        out = self.r.apply(None, {  # type: ignore[arg-type]
            "action": "scale",
            "params": {"replicas": 3},
            "rationale": "reduce contention",
            "expected_effect": "faster recovery",
            "confidence": 0.7,
        })
        self.assertEqual(out["action"], "scale")
        self.assertEqual(out["params"], {"replicas": 3})
        self.assertEqual(out["rationale"], "reduce contention")

    def test_apply_normalizes_none(self):
        out = self.r.apply(None, {"action": "none", "reason": "code fix needed"})  # type: ignore[arg-type]
        self.assertEqual(out["action"], "none")
        self.assertIn("code fix", out["reason"])

    def test_apply_treats_empty_action_as_none(self):
        out = self.r.apply(None, {"params": {"replicas": 3}})  # type: ignore[arg-type]
        self.assertEqual(out["action"], "none")

    def test_apply_coerces_non_dict_params(self):
        out = self.r.apply(None, {  # type: ignore[arg-type]
            "action": "restart", "params": "not-a-dict",
        })
        self.assertEqual(out["params"], {})


# ----------------------------------------------------------------------------
# engine-level: validation is the safety boundary
# ----------------------------------------------------------------------------

class ProposeValidatedRestartTest(unittest.TestCase):
    """The happy path — a low-risk catalog action passes validation."""

    def test_validated_restart_proposal(self):
        llm = RemediatorOnlyLLM({
            "action": "restart", "params": {},
            "rationale": "OOM-killed; restart clears state",
            "expected_effect": "fresh start, watch for recurrence",
            "confidence": 0.6,
        })
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, base=Path(tmp))
            proposal = eng.propose_remediation(_dummy_diagnosis())

        self.assertIsNotNone(proposal)
        self.assertTrue(proposal["validated"])
        self.assertEqual(proposal["action"], "restart")
        self.assertEqual(proposal["risk"], "low")
        # Plan + reversal both reachable from the proposal dict.
        self.assertEqual(proposal["plan"]["action"], "restart")


class ProposeActionNoneTest(unittest.TestCase):
    def test_action_none_kept_verbatim(self):
        llm = RemediatorOnlyLLM({"action": "none", "reason": "code bug"})
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, base=Path(tmp))
            proposal = eng.propose_remediation(_dummy_diagnosis())
        self.assertEqual(proposal["action"], "none")
        self.assertIn("code bug", proposal["reason"])
        self.assertNotIn("validated", proposal)


class ProposeUnknownActionRejectedTest(unittest.TestCase):
    """The safety boundary: an LLM picking outside the catalog is rejected."""

    def test_unknown_action_becomes_none_with_validation_error(self):
        llm = RemediatorOnlyLLM({
            "action": "frob",
            "params": {"weird": True},
            "rationale": "made it up",
            "confidence": 0.9,
        })
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, base=Path(tmp))
            proposal = eng.propose_remediation(_dummy_diagnosis())

        self.assertEqual(proposal["action"], "none")
        self.assertIn("validation_error", proposal)
        # original (rejected) proposal is preserved for audit.
        self.assertEqual(proposal["rejected_proposal"]["action"], "frob")
        # the state recorded the rejection.
        self.assertTrue(any(
            "failed catalog validation" in n for n in eng.state.notes
        ))


class ProposeInvalidParamsRejectedTest(unittest.TestCase):
    """A valid action with out-of-bounds params is also rejected."""

    def test_negative_memory_rejected(self):
        llm = RemediatorOnlyLLM({
            "action": "set-resources",
            "params": {"container": "web", "memory_limit": "not-a-number"},
            "rationale": "...",
        })
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, base=Path(tmp))
            proposal = eng.propose_remediation(_dummy_diagnosis())
        self.assertEqual(proposal["action"], "none")
        self.assertIn("validation_error", proposal)


class ProposeDisabledReturnsNoneTest(unittest.TestCase):
    """Without ``remediation_enabled``, the Remediator never runs."""

    def test_disabled(self):
        llm = RemediatorOnlyLLM({"action": "restart", "params": {}})
        with tempfile.TemporaryDirectory() as tmp:
            eng = _seed_engine(llm, base=Path(tmp), remediation_enabled=False)
            self.assertIsNone(eng.propose_remediation(_dummy_diagnosis()))
            # The LLM was never called.
            self.assertEqual(llm.calls, [])


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def _dummy_diagnosis() -> Diagnosis:
    return Diagnosis(
        summary="container OOM-killed",
        root_cause="memory limit (256Mi) too low for the working set",
        confidence=0.8,
        evidence=["OOMKilled / exit 137", "no other failures"],
        suggested_fixes=[],
        needs_deep_inspection=False,
    )


if __name__ == "__main__":
    unittest.main()
