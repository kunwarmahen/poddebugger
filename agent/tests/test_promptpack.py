"""Tests for Phase 15C prompt packs (HLD §19.4)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from poddebugger import promptpack
from poddebugger.promptpack import (
    MAX_PROMPT_CHARS,
    PromptPackError,
    describe_pack,
    dump_pack,
    load_pack,
    validate_prompt,
)
from poddebugger.scaffold.agents import Scout
from poddebugger.scaffold.engine import InvestigationEngine

from tests.test_scaffold_engine import FakeProvider, ScriptedLLM

VALID = "Do the job well. Return JSON: {\"x\": 1}"


class ValidateTest(unittest.TestCase):
    def test_accepts_a_sane_prompt(self):
        validate_prompt("Scout", VALID)  # no raise

    def test_unknown_role(self):
        with self.assertRaisesRegex(PromptPackError, "unknown role"):
            validate_prompt("Wizard", VALID)

    def test_empty_prompt(self):
        with self.assertRaisesRegex(PromptPackError, "empty"):
            validate_prompt("Scout", "   \n")

    def test_oversize_prompt(self):
        with self.assertRaisesRegex(PromptPackError, "chars"):
            validate_prompt("Scout", "JSON " + "x" * MAX_PROMPT_CHARS)

    def test_missing_protocol_marker(self):
        with self.assertRaisesRegex(PromptPackError, "JSON"):
            validate_prompt("Scout", "just answer in plain prose please")


class DumpLoadTest(unittest.TestCase):
    def test_round_trip_covers_all_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = dump_pack(tmp)
            pack = load_pack(tmp)
        self.assertEqual(len(written), len(promptpack.known_roles()))
        self.assertEqual(set(pack), set(promptpack.known_roles()))
        self.assertEqual(pack["Scout"], Scout().system_prompt)

    def test_dump_refuses_existing_pack_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump_pack(tmp)
            with self.assertRaisesRegex(PromptPackError, "force"):
                dump_pack(tmp)
            dump_pack(tmp, force=True)  # explicit overwrite is fine

    def test_load_rejects_missing_dir_and_empty_pack(self):
        with self.assertRaisesRegex(PromptPackError, "not a directory"):
            load_pack("/no/such/dir")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(PromptPackError, "no <Role>"):
                load_pack(tmp)

    def test_load_rejects_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Scout.txt").write_text("no marker here")
            with self.assertRaises(PromptPackError):
                load_pack(tmp)

    def test_describe_flags_modified_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump_pack(tmp)
            (Path(tmp) / "Scout.txt").write_text(VALID)
            rows = dict((r, d) for r, _, d in describe_pack(tmp))
        self.assertTrue(rows["Scout"])
        self.assertFalse(rows["Reporter"])


class EngineAppliesPackTest(unittest.TestCase):
    def test_override_is_per_instance(self):
        llm = ScriptedLLM(coordinator_actions=[], verifier_verdicts=[])
        with tempfile.TemporaryDirectory() as tmp:
            eng = InvestigationEngine(
                FakeProvider(), llm, workspace_base=Path(tmp),
                prompt_pack={"Scout": VALID})
            plain = InvestigationEngine(
                FakeProvider(), llm, workspace_base=Path(tmp))
        self.assertEqual(eng._agents["Scout"].system_prompt, VALID)
        self.assertNotEqual(plain._agents["Scout"].system_prompt, VALID)
        self.assertNotEqual(Scout().system_prompt, VALID)  # class untouched

    def test_unregistered_role_is_ignored(self):
        llm = ScriptedLLM(coordinator_actions=[], verifier_verdicts=[])
        with tempfile.TemporaryDirectory() as tmp:
            eng = InvestigationEngine(
                FakeProvider(), llm, workspace_base=Path(tmp),
                prompt_pack={"Remediator": VALID})  # --fix not in effect
        self.assertNotIn("Remediator", eng._agents)  # and no crash


if __name__ == "__main__":
    unittest.main()
