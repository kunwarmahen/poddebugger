"""Tests for the Phase 15A cross-run experience memory (HLD §19.2)."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from poddebugger import experience
from poddebugger.experience import (
    ExperienceRecord,
    ExperienceStore,
    make_record,
    score,
    signature_from_context,
)
from poddebugger.models import DiagnosticContext, Workload, WorkloadRef


def _sig(**over) -> dict:
    base = {
        "platform": "podman",
        "classification": "CrashLoopBackOff",
        "image": "registry.example.com/app:1.0",
        "exit_code": 1,
        "oom_killed": False,
        "keywords": ["connection", "refused", "db", "5432"],
    }
    base.update(over)
    return base


def _record(**over) -> ExperienceRecord:
    rec = make_record(
        _sig(), summary="app crashes at boot",
        root_cause="db unreachable",
        attempts=[{"action": "restart", "params": {}, "outcome": "still-failing"}],
        outcome="unresolved",
    )
    for k, v in over.items():
        setattr(rec, k, v)
    return rec


# --- keywords / helpers -------------------------------------------------------

class KeywordsTest(unittest.TestCase):
    def test_drops_stopwords_digits_and_hex_ids(self):
        kws = experience._keywords(
            "the container exited with error 137 deadbeefcafe connection refused")
        self.assertNotIn("the", kws)
        self.assertNotIn("container", kws)   # domain-generic
        self.assertNotIn("137", kws)
        self.assertNotIn("deadbeefcafe", kws)
        self.assertIn("connection", kws)
        self.assertIn("refused", kws)

    def test_dedupes_and_caps(self):
        text = "alpha alpha " + " ".join(f"word{i}x" for i in range(30))
        kws = experience._keywords(text)
        self.assertEqual(kws.count("alpha"), 1)
        self.assertLessEqual(len(kws), experience._KEYWORD_CAP)


class ImageRepoTest(unittest.TestCase):
    def test_strips_tag(self):
        self.assertEqual(experience._image_repo("mysql:8"), "mysql")

    def test_preserves_registry_port(self):
        self.assertEqual(experience._image_repo("reg:5000/app:1.2"), "reg:5000/app")
        self.assertEqual(experience._image_repo("reg:5000/app"), "reg:5000/app")

    def test_strips_digest(self):
        self.assertEqual(experience._image_repo("app:1@sha256:abcd"), "app")


class RedactParamsTest(unittest.TestCase):
    def test_masks_secret_keys_recursively(self):
        out = experience._redact_params({
            "container": "db",
            "env": {"MYSQL_ROOT_PASSWORD": "hunter2", "MODE": "prod"},
            "API_TOKEN": "abc",
        })
        self.assertEqual(out["env"]["MYSQL_ROOT_PASSWORD"], "***")
        self.assertEqual(out["env"]["MODE"], "prod")
        self.assertEqual(out["API_TOKEN"], "***")
        self.assertEqual(out["container"], "db")


# --- record -------------------------------------------------------------------

class MakeRecordTest(unittest.TestCase):
    def test_roundtrip(self):
        rec = _record()
        again = ExperienceRecord.from_dict(rec.to_dict())
        self.assertEqual(again, rec)

    def test_from_dict_ignores_unknown_fields(self):
        d = _record().to_dict()
        d["future_field"] = "x"
        self.assertEqual(ExperienceRecord.from_dict(d).id, d["id"])

    def test_text_fields_are_scrubbed(self):
        rec = make_record(_sig(), summary="pod at 10.0.0.5 died",
                          root_cause="node 10.0.0.5 unreachable",
                          attempts=[], outcome="unresolved")
        self.assertNotIn("10.0.0.5", rec.summary)
        self.assertNotIn("10.0.0.5", rec.root_cause)

    def test_attempt_params_are_redacted(self):
        rec = make_record(_sig(), summary="s", root_cause="r", attempts=[
            {"action": "set-env",
             "params": {"env": {"APP_SECRET": "x", "PORT": "80"}},
             "outcome": "recovered"},
        ], outcome="recovered")
        self.assertEqual(rec.attempts[0]["params"]["env"]["APP_SECRET"], "***")
        self.assertEqual(rec.attempts[0]["params"]["env"]["PORT"], "80")

    def test_recall_rendering_mentions_outcome_and_attempts(self):
        rec = _record()
        self.assertIn("did NOT work", rec.recall_summary())
        self.assertIn("restart", rec.recall_detail())
        self.assertIn("final outcome: unresolved", rec.recall_detail())
        ok = _record(outcome="recovered")
        self.assertIn("fix worked", ok.recall_summary())


class SignatureFromContextTest(unittest.TestCase):
    def test_built_from_workload_and_logs(self):
        ref = WorkloadRef(name="c1", platform="podman")
        w = Workload(ref=ref, status="exited", image="mysql:8",
                     exit_code=137, oom_killed=True, error="OOM killed")
        ctx = DiagnosticContext(workload=w, logs="ERROR: out of memory\n")
        sig = signature_from_context(ctx, "OOMKilled")
        self.assertEqual(sig["platform"], "podman")
        self.assertEqual(sig["classification"], "OOMKilled")
        self.assertEqual(sig["exit_code"], 137)
        self.assertTrue(sig["oom_killed"])
        self.assertIn("memory", sig["keywords"])


# --- scoring ------------------------------------------------------------------

class ScoreTest(unittest.TestCase):
    def test_full_match_scores_high(self):
        self.assertGreaterEqual(score(_record(), _sig()), 9)

    def test_classification_alone_clears_recall_bar(self):
        rec = _record(image="", exit_code=None, keywords=[])
        self.assertEqual(score(rec, _sig()), 3)

    def test_keyword_overlap_capped_at_three(self):
        rec = _record(classification="", image="", exit_code=None)
        sig = _sig(classification="other", image="", exit_code=None)
        self.assertEqual(score(rec, sig), 3)  # 4 shared keywords -> capped

    def test_oom_flags_must_both_be_set(self):
        rec = _record(classification="", image="", exit_code=None,
                      keywords=[], oom_killed=True)
        self.assertEqual(score(rec, _sig(oom_killed=False)), 0)
        self.assertEqual(score(rec, _sig(oom_killed=True)), 2)

    def test_unrelated_record_scores_zero(self):
        rec = _record(classification="ImagePullBackOff", image="other:1",
                      exit_code=42, keywords=["registry", "auth"])
        self.assertEqual(score(rec, _sig()), 0)


# --- store --------------------------------------------------------------------

class StoreTest(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(tmp)
            saved = store.save(_record())
            self.assertIsNotNone(saved)
            self.assertTrue(saved.exists())
            records = store.load_all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].root_cause, "db unreachable")

    def test_prunes_oldest_beyond_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(tmp, max_records=3)
            first = store.save(_record())
            for _ in range(4):
                time.sleep(0.002)  # distinct mtimes
                store.save(_record())
            self.assertEqual(len(store.load_all()), 3)
            self.assertFalse(first.exists())

    def test_load_skips_malformed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(tmp)
            store.save(_record())
            (Path(tmp) / "00000000-junk.json").write_text("{not json")
            self.assertEqual(len(store.load_all()), 1)

    def test_find_similar_ranks_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(tmp)
            store.save(_record(id="close"))
            store.save(_record(id="far", classification="ImagePullBackOff",
                               image="other:1", exit_code=42,
                               keywords=["registry"]))
            matches = store.find_similar(_sig())
            self.assertEqual([r.id for r, _ in matches], ["close"])

    def test_find_similar_respects_k(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(tmp)
            for i in range(5):
                time.sleep(0.002)
                store.save(_record(id=f"r{i}"))
            self.assertEqual(len(store.find_similar(_sig(), k=2)), 2)

    def test_clear_removes_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ExperienceStore(tmp)
            store.save(_record())
            store.save(_record())
            self.assertEqual(store.clear(), 2)
            self.assertEqual(store.load_all(), [])

    def test_default_dir_honors_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ",
                                 {"PODDEBUGGER_EXPERIENCE_DIR": tmp}):
                self.assertEqual(ExperienceStore().path, Path(tmp))

    def test_save_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("")
            store = ExperienceStore(blocker / "sub")  # mkdir under a file fails
            self.assertIsNone(store.save(_record()))


if __name__ == "__main__":
    unittest.main()
