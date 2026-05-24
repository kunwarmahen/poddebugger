"""Unit tests for the InvestigationState model — ids, lifecycle, serialization."""

import unittest

from poddebugger.scaffold.state import InvestigationState


def _state() -> InvestigationState:
    return InvestigationState(target="web-7c", platform="kubernetes")


class IdMintingTest(unittest.TestCase):
    def test_ids_are_unique_and_prefixed(self):
        st = _state()
        self.assertEqual(st.add_lead("a").id, "L1")
        self.assertEqual(st.add_lead("b").id, "L2")
        self.assertEqual(st.add_hypothesis("x").id, "H1")
        self.assertEqual(st.add_evidence("e").id, "E1")
        self.assertEqual(st.add_sanity_check("s").id, "S1")


class LifecycleTest(unittest.TestCase):
    def test_promote_moves_hypothesis_to_finding(self):
        st = _state()
        h = st.add_hypothesis("DB unreachable")
        h.confidence = 0.9
        finding = st.promote(h.id)
        self.assertEqual(st.hypotheses, [])
        self.assertEqual(st.confirmed_findings, [finding])
        self.assertEqual(finding.statement, "DB unreachable")
        self.assertEqual(finding.confidence, 0.9)
        self.assertEqual(finding.from_hypothesis, "H1")

    def test_refute_moves_hypothesis_to_ruled_out(self):
        st = _state()
        h = st.add_hypothesis("bad image tag")
        dead = st.refute(h.id, "image digest is valid")
        self.assertEqual(st.hypotheses, [])
        self.assertEqual(st.ruled_out, [dead])
        self.assertEqual(dead.reason, "image digest is valid")

    def test_demote_moves_finding_back_to_ruled_out(self):
        st = _state()
        finding = st.promote(st.add_hypothesis("OOM").id)
        dead = st.demote(finding.id, "adjudicator overturned it")
        self.assertEqual(st.confirmed_findings, [])
        self.assertEqual(len(st.ruled_out), 1)
        self.assertEqual(st.ruled_out[0].reason, "adjudicator overturned it")

    def test_promote_unknown_hypothesis_raises(self):
        with self.assertRaises(KeyError):
            _state().promote("H99")


class SerializationTest(unittest.TestCase):
    def test_round_trip_preserves_state(self):
        st = _state()
        st.classification = "CrashLoopBackOff"
        st.strategy = "check DB connectivity first"
        st.iteration = 4
        st.add_sanity_check("F(0) must equal exit 0")
        lead = st.add_lead("logs mention db timeout", source="logs")
        ev = st.add_evidence("connection refused", source="prober:logs")
        st.add_hypothesis("DB down", test="probe the port",
                          evidence_ids=[ev.id])
        st.promote(st.add_hypothesis("config error").id)
        st.refute(st.add_hypothesis("bad image").id, "image is fine")
        st.record_dispatch("Analyst", "formed hypothesis", "H1")
        st.notes.append("a note")

        restored = InvestigationState.from_dict(st.to_dict())

        self.assertEqual(restored.classification, "CrashLoopBackOff")
        self.assertEqual(restored.iteration, 4)
        self.assertEqual(restored.leads[0].source, "logs")
        self.assertEqual(restored.hypotheses[0].evidence_ids, [ev.id])
        self.assertEqual(len(restored.confirmed_findings), 1)
        self.assertEqual(len(restored.ruled_out), 1)
        self.assertEqual(restored.dispatch_history[0].role, "Analyst")
        self.assertEqual(restored.notes, ["a note"])
        # Seq counters survive so resumed runs keep minting unique ids.
        self.assertEqual(restored.seq, st.seq)
        self.assertEqual(restored.add_lead("new").id, "L2")

    def test_render_produces_markdown(self):
        st = _state()
        st.add_hypothesis("something")
        out = st.render()
        self.assertIn("# Investigation — web-7c", out)
        self.assertIn("## Hypotheses (1)", out)
        self.assertIn("## Leads (0)", out)


if __name__ == "__main__":
    unittest.main()
