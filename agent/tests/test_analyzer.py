"""Unit tests for the analyzer — JSON extraction, Diagnosis mapping, analyze()."""

import unittest

from poddebugger.analyzer import _extract_json, _to_diagnosis, analyze
from poddebugger.llm.base import LLMClient, LLMError
from poddebugger.models import DiagnosticContext, Workload, WorkloadRef


class ExtractJsonTest(unittest.TestCase):
    def test_bare_json(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_fenced_without_lang(self):
        self.assertEqual(_extract_json('```\n{"a": 1}\n```'), {"a": 1})

    def test_embedded_in_prose(self):
        self.assertEqual(
            _extract_json('Here is the result:\n{"a": 1}\nHope that helps.'),
            {"a": 1},
        )

    def test_garbage_raises(self):
        with self.assertRaises(LLMError):
            _extract_json("not json at all")


class ToDiagnosisTest(unittest.TestCase):
    def test_full_object(self):
        d = _to_diagnosis({
            "summary": "OOM",
            "root_cause": "heap > limit",
            "confidence": 0.9,
            "evidence": ["exit 137"],
            "suggested_fixes": [
                {"action": "raise memory", "rationale": "needs more", "risk": "low"}
            ],
            "needs_deep_inspection": False,
        })
        self.assertEqual(d.summary, "OOM")
        self.assertEqual(d.confidence, 0.9)
        self.assertEqual(len(d.suggested_fixes), 1)
        self.assertEqual(d.suggested_fixes[0].risk, "low")

    def test_confidence_is_clamped(self):
        self.assertEqual(_to_diagnosis({"confidence": 1.7}).confidence, 1.0)
        self.assertEqual(_to_diagnosis({"confidence": -3}).confidence, 0.0)

    def test_bad_confidence_defaults_to_zero(self):
        self.assertEqual(_to_diagnosis({"confidence": "high"}).confidence, 0.0)

    def test_missing_fields_default(self):
        d = _to_diagnosis({})
        self.assertEqual(d.summary, "")
        self.assertEqual(d.suggested_fixes, [])
        self.assertFalse(d.needs_deep_inspection)

    def test_string_fixes_tolerated(self):
        d = _to_diagnosis({"suggested_fixes": ["just restart it"]})
        self.assertEqual(d.suggested_fixes[0].action, "just restart it")


class _FakeLLM(LLMClient):
    name = "fake"
    model_id = "fake-1"

    def __init__(self, response: str):
        self._response = response

    def complete(self, system: str, user: str) -> str:
        return self._response


class AnalyzeTest(unittest.TestCase):
    def _ctx(self) -> DiagnosticContext:
        return DiagnosticContext(workload=Workload(ref=WorkloadRef(name="x")))

    def test_analyze_parses_diagnosis(self):
        llm = _FakeLLM('```json\n{"summary": "s", "root_cause": "r", '
                       '"confidence": 0.5}\n```')
        d = analyze(self._ctx(), llm)
        self.assertEqual(d.summary, "s")
        self.assertEqual(d.root_cause, "r")

    def test_analyze_raises_on_bad_response(self):
        with self.assertRaises(LLMError):
            analyze(self._ctx(), _FakeLLM("the model rambled with no json"))


if __name__ == "__main__":
    unittest.main()
