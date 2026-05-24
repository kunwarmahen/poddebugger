"""Unit tests for deep inspection — probe collection, shell check, truncation."""

import unittest

from poddebugger import deepinspect
from poddebugger.models import WorkloadRef
from poddebugger.providers.base import ProviderError

REF = WorkloadRef(name="web", platform="podman")


class FakeProvider:
    """Stand-in container platform exposing just exec()."""

    def __init__(self, has_shell=True, output="probe-output", fail=False):
        self.has_shell = has_shell
        self.output = output
        self.fail = fail
        self.calls = []

    def exec(self, ref, command):
        self.calls.append(command)
        script = command[-1]
        if deepinspect._MARKER in script:
            return deepinspect._MARKER if self.has_shell else "sh: not found"
        if self.fail:
            raise ProviderError("exec failed")
        return self.output


class DeepInspectTest(unittest.TestCase):
    def test_collects_all_probes(self):
        provider = FakeProvider()
        result = deepinspect.run(provider, REF)
        self.assertEqual(set(result), {label for label, _ in deepinspect.PROBES})
        for output in result.values():
            self.assertEqual(output, "probe-output")

    def test_no_shell_raises(self):
        with self.assertRaises(ProviderError):
            deepinspect.run(FakeProvider(has_shell=False), REF)

    def test_output_is_truncated(self):
        big = "x" * (deepinspect._MAX_OUTPUT + 500)
        result = deepinspect.run(FakeProvider(output=big), REF)
        sample = result["processes"]
        self.assertLess(len(sample), len(big))
        self.assertIn("truncated", sample)

    def test_probe_failure_is_recorded_not_raised(self):
        result = deepinspect.run(FakeProvider(fail=True), REF)
        self.assertIn("probe failed", result["processes"])


if __name__ == "__main__":
    unittest.main()
