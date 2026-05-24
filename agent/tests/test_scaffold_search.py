"""Unit tests for the Phase 8 search subsystem (HLD §13)."""

from __future__ import annotations

import os
import sys
import types
import unittest

from poddebugger.scaffold import search


# ---------------------------------------------------------------------------
# query redaction
# ---------------------------------------------------------------------------

class RedactQueryTest(unittest.TestCase):
    def test_strips_ipv4(self):
        out = search.redact_query("connection refused to 10.42.0.13:5432")
        self.assertNotIn("10.42.0.13", out)
        self.assertIn("<ip>", out)

    def test_strips_ipv6(self):
        out = search.redact_query("fe80::1a3b:c4ff:fe9d:1234 not reachable")
        self.assertNotIn("fe80::", out)
        self.assertIn("<ip>", out)

    def test_strips_uuid(self):
        out = search.redact_query(
            "pod uid 0fa3b1d8-7c4e-4f9c-b0e1-1234567890ab failing"
        )
        self.assertIn("<uuid>", out)
        self.assertNotIn("0fa3b1d8", out)

    def test_strips_long_hex_id(self):
        out = search.redact_query("container deadbeefcafebabedeadbeefcafebabe died")
        self.assertIn("<id>", out)
        self.assertNotIn("deadbeefcafebabe", out)

    def test_strips_pod_suffix(self):
        # Deployment-style: "web-7c4b9d-abc12" -> "web-<id>"
        out = search.redact_query("pod web-7c4b9d-abc12 in namespace prod")
        self.assertNotIn("7c4b9d", out)
        self.assertNotIn("abc12", out)
        self.assertIn("web-<id>", out)

    def test_keeps_signature_words(self):
        # We MUST NOT scrub error class names / version tokens.
        out = search.redact_query("OOMKilled exit 137 Java 17 OutOfMemoryError")
        for keyword in ("OOMKilled", "Java", "17", "OutOfMemoryError"):
            self.assertIn(keyword, out)

    def test_collapses_whitespace(self):
        out = search.redact_query("foo   \n bar  baz")
        self.assertEqual(out, "foo bar baz")

    def test_empty_input(self):
        self.assertEqual(search.redact_query(""), "")
        self.assertEqual(search.redact_query(None), "")


# ---------------------------------------------------------------------------
# noop backend
# ---------------------------------------------------------------------------

class NoopBackendTest(unittest.TestCase):
    def test_returns_no_results(self):
        self.assertEqual(search.NoopBackend().search("anything"), [])


# ---------------------------------------------------------------------------
# duckduckgo backend (with ddgs mocked into sys.modules)
# ---------------------------------------------------------------------------

class _FakeDDGS:
    """Stand-in for the real `ddgs.DDGS` context manager."""

    last_query: str = ""
    last_max: int = 0
    canned: list[dict] = []
    raise_on_search: Exception | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def text(self, query, max_results=5):
        type(self).last_query = query
        type(self).last_max = max_results
        if type(self).raise_on_search is not None:
            raise type(self).raise_on_search
        return list(type(self).canned)


class DuckDuckGoBackendTest(unittest.TestCase):
    def setUp(self):
        # Install a fake `ddgs` module so the lazy import inside the backend
        # picks it up without touching the network.
        self._mod = types.ModuleType("ddgs")
        self._mod.DDGS = _FakeDDGS  # type: ignore[attr-defined]
        sys.modules["ddgs"] = self._mod
        _FakeDDGS.canned = []
        _FakeDDGS.raise_on_search = None

    def tearDown(self):
        sys.modules.pop("ddgs", None)

    def test_maps_ddgs_results_to_search_results(self):
        _FakeDDGS.canned = [
            {"title": "OOMKilled in Java pods",
             "body": "Tune -Xmx or set requests.memory",
             "href": "https://example.com/oom-java"},
        ]
        backend = search.DuckDuckGoBackend()
        out = backend.search("OOMKilled Java", max_results=3)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "OOMKilled in Java pods")
        self.assertEqual(out[0].url, "https://example.com/oom-java")
        self.assertEqual(out[0].source, "duckduckgo")
        self.assertEqual(out[0].domain(), "example.com")
        # And we forwarded max_results.
        self.assertEqual(_FakeDDGS.last_max, 3)

    def test_returns_empty_on_backend_exception(self):
        # ddgs raises a variety of types on throttle/timeout; the backend
        # must be tolerant — return [] rather than crashing the run.
        _FakeDDGS.raise_on_search = RuntimeError("rate limited")
        self.assertEqual(search.DuckDuckGoBackend().search("anything"), [])

    def test_filters_non_dict_results(self):
        _FakeDDGS.canned = [
            "not a dict",
            {"title": "ok", "href": "https://example.org", "body": ""},
        ]
        out = search.DuckDuckGoBackend().search("q")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].title, "ok")


class DuckDuckGoMissingDependencyTest(unittest.TestCase):
    def setUp(self):
        # Pretend `ddgs` is not installed.
        self._saved = sys.modules.pop("ddgs", None)

    def tearDown(self):
        if self._saved is not None:
            sys.modules["ddgs"] = self._saved

    def test_missing_dependency_raises_with_install_hint(self):
        with self.assertRaises(search.SearchError) as ctx:
            search.DuckDuckGoBackend().search("q")
        self.assertIn("ddgs", str(ctx.exception).lower())
        self.assertIn("install", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------

class GetBackendTest(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.pop("PODDEBUGGER_SEARCH_BACKEND", None)

    def tearDown(self):
        if self._saved_env is not None:
            os.environ["PODDEBUGGER_SEARCH_BACKEND"] = self._saved_env

    def test_default_is_noop(self):
        self.assertIsInstance(search.get_backend(), search.NoopBackend)
        self.assertIsInstance(search.get_backend("noop"), search.NoopBackend)
        self.assertIsInstance(search.get_backend("off"), search.NoopBackend)

    def test_env_var_override(self):
        os.environ["PODDEBUGGER_SEARCH_BACKEND"] = "duckduckgo"
        self.assertIsInstance(search.get_backend(), search.DuckDuckGoBackend)

    def test_short_alias_ddg(self):
        self.assertIsInstance(search.get_backend("ddg"), search.DuckDuckGoBackend)

    def test_dotted_import_path(self):
        # Use NoopBackend's own dotted path — the factory should accept it.
        b = search.get_backend("poddebugger.scaffold.search.NoopBackend")
        self.assertIsInstance(b, search.NoopBackend)

    def test_unknown_short_name_raises(self):
        with self.assertRaises(search.SearchError):
            search.get_backend("not-a-backend")

    def test_dotted_path_to_non_backend_raises(self):
        # int() is callable but isn't a SearchBackend subclass.
        with self.assertRaises(search.SearchError):
            search.get_backend("builtins.int")


if __name__ == "__main__":
    unittest.main()
