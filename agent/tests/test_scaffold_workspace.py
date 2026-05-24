"""Unit tests for the investigation Workspace — persistence and git history."""

import json
import tempfile
import unittest
from pathlib import Path

from poddebugger.scaffold.state import InvestigationState
from poddebugger.scaffold.workspace import Workspace


class WorkspaceTest(unittest.TestCase):
    def test_create_makes_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace.create("web-7c", base=Path(tmp))
            self.assertTrue(ws.path.is_dir())
            self.assertIn("web-7c", ws.path.name)

    def test_commit_persists_state_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace.create("api", base=Path(tmp))
            st = InvestigationState(target="api", platform="podman")
            st.iteration = 1
            st.add_hypothesis("port conflict")
            ws.commit(st, "iteration 1")

            state_json = ws.path / "state.json"
            self.assertTrue(state_json.exists())
            self.assertTrue((ws.path / "state.md").exists())
            data = json.loads(state_json.read_text())
            self.assertEqual(data["target"], "api")
            self.assertEqual(len(data["hypotheses"]), 1)

    def test_commit_round_trips_via_load_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace.create("api", base=Path(tmp))
            st = InvestigationState(target="api")
            st.classification = "OOMKilled"
            st.add_lead("memory limit too low")
            ws.commit(st, "iteration 1")

            reloaded = ws.load_state()
            self.assertEqual(reloaded.classification, "OOMKilled")
            self.assertEqual(reloaded.leads[0].description, "memory limit too low")

    def test_one_commit_per_iteration(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace.create("api", base=Path(tmp))
            st = InvestigationState(target="api")
            for i in range(1, 4):
                st.iteration = i
                ws.commit(st, f"iteration {i}")
            if ws.git_enabled:
                self.assertEqual(ws.commit_count(), 3)
            else:
                self.skipTest("git not available — commit history skipped")


if __name__ == "__main__":
    unittest.main()
