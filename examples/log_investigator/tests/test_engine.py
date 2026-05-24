"""End-to-end test — drives the full Scout → Analyst → Reporter loop with
a scripted LLM. Critically: imports ONLY ``inquiro`` and ``log_investigator``.
If this passes, the inquiro boundary is genuinely reusable on a non-pod domain.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from inquiro import LLMClient

from log_investigator import LogContext, MiniEngine
from log_investigator.collector import collect


class _ScriptedLLM(LLMClient):
    """Returns a canned JSON response based on the role marker in the prompt."""

    name = "scripted"
    model_id = "scripted-1"

    def __init__(self):
        self.calls: list[str] = []

    def complete(self, system, user):
        if "ROLE: Scout" in system:
            self.calls.append("Scout")
            return json.dumps({
                "classification": "OutOfMemoryError",
                "lead": "investigate the JVM heap size",
                "evidence": [
                    "java.lang.OutOfMemoryError: Java heap space",
                    "GC overhead limit exceeded shortly before exit",
                ],
            })
        if "ROLE: Analyst" in system:
            self.calls.append("Analyst")
            return json.dumps({
                "hypothesis": "heap size too small for the workload's allocation rate",
                "evidence": ["E1", "E2"],
            })
        if "ROLE: Reporter" in system:
            self.calls.append("Reporter")
            return json.dumps({
                "summary": "JVM ran out of heap under load",
                "likely_cause": "max heap (-Xmx) is undersized; raise it or "
                                "reduce allocation rate",
                "confidence": 0.85,
                "evidence": ["E1", "E2"],
            })
        raise AssertionError(f"unexpected role; system began {system[:60]!r}")


SAMPLE_LOG = """\
2026-05-23 09:00:01 INFO  startup: server listening on :8080
2026-05-23 09:00:42 INFO  request: GET /widgets
2026-05-23 09:01:11 WARN  gc: 3.2s pause, heap 95% full
2026-05-23 09:01:14 ERROR java.lang.OutOfMemoryError: Java heap space
2026-05-23 09:01:14 ERROR GC overhead limit exceeded
2026-05-23 09:01:14 FATAL process exiting
"""


class EndToEndTest(unittest.TestCase):
    def test_full_loop_produces_a_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "app.log"
            log.write_text(SAMPLE_LOG)
            ctx = collect(log)
            llm = _ScriptedLLM()
            engine = MiniEngine(llm, workspace_base=Path(tmp))
            finding = engine.investigate(ctx)

        self.assertEqual(llm.calls, ["Scout", "Analyst", "Reporter"])
        self.assertEqual(finding.classification, "OutOfMemoryError")
        self.assertIn("heap", finding.summary.lower())
        self.assertEqual(finding.confidence, 0.85)
        self.assertIn("E1", finding.evidence)


class CollectorTest(unittest.TestCase):
    def test_extracts_error_lines(self):
        ctx = LogContext(path="x", lines=SAMPLE_LOG.splitlines())
        errs = ctx.errors()
        # 3 ERROR/FATAL lines
        self.assertEqual(len(errs), 3)


if __name__ == "__main__":
    unittest.main()
