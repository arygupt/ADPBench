import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from adpbench.durable import atomic_json
from scripts.go_pilot import read_plan
from scripts.publish_go import publish, REPOSITORY


class PublishGoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = read_plan(Path(__file__).resolve().parent.parent / "pilot/go-core-20260922.json")
        self.plan["models"] = self.plan["models"][:1]
        self.run = {"id": 123, "run_attempt": 1, "head_sha": "a" * 40, "status": "completed", "event": "workflow_dispatch", "path": ".github/workflows/go-core.yml", "repository": {"full_name": REPOSITORY}, "html_url": "https://github.com/arygupt/ADPBench/actions/runs/123"}
        self.jobs = [{"id": 456, "name": "Evaluate mimo-v2.5 · dot product + GEMV", "status": "completed", "conclusion": "success", "completed_at": "2026-09-22T01:00:00Z"}]
        self.artifact = self.root / "artifacts/go-core-mimo-v2.5-123"
        atomic_json(self.artifact / "plan.json", self.plan)
        self.sources = []
        for problem in self.plan["problems"]:
            dest = self.artifact / "opencode-go-mimo-v2.5" / problem / "rep1"
            source = dest.parent / "rep1_frozen/dut.v"
            source.parent.mkdir(parents=True)
            source.write_text("module dut; endmodule\n")
            self.sources.append(source)
            gen = {"model": "mimo-v2.5", "problem": problem, "protocol": "single-shot", "response_id": "fixture", "error": "", "usage": {"completion_tokens": 30}, "github": {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40}}
            record = {"problem": problem, "label": "opencode-go/mimo-v2.5 [single-shot]", "attempt": 1, "result": {"correct": True, "ratio": 1.2, "cells": 10, "cycles": 20, "adp": 200, "metadata": {"stage": "ok"}}, "manifest": {"generation": gen, "submission_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}}
            atomic_json(dest / "record.json", record)
            atomic_json(dest / "generation.json", gen)

    def publish(self):
        return publish(self.root / "artifacts", self.plan, self.run, self.jobs, self.root / "published", self.root / "site")

    def test_exports_only_real_records_and_separate_protocol(self):
        board = self.publish()
        self.assertEqual(board["meta"]["protocol"], "single-shot")
        self.assertEqual(board["meta"]["output_tokens"], 60)
        self.assertEqual(len(board["problems"]), 2)
        self.assertEqual(board["models"][0]["correct"], 2)
        self.assertEqual(board["models"][0]["runs"][0]["execution"]["job_id"], 456)
        self.assertEqual(len(list((self.root / "published").glob("**/dut.v"))), 2)
        with self.assertRaises(FileExistsError):
            self.publish()

    def test_rejects_changed_frozen_rtl_before_writing(self):
        self.sources[0].write_text("module other; endmodule")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.publish()
        self.assertFalse((self.root / "published").exists())

    def test_rejects_wrong_run_and_preparation_jobs(self):
        self.run["id"] = 999
        with self.assertRaises(FileNotFoundError):
            self.publish()
        self.run["id"] = 123
        self.jobs[0]["conclusion"] = "skipped"
        with self.assertRaisesRegex(ValueError, "not evaluated"):
            self.publish()

    def test_rejects_wrong_generation_provenance(self):
        path = self.artifact / "opencode-go-mimo-v2.5/001_dot_product/rep1/generation.json"
        gen = json.loads(path.read_text())
        gen["github"]["GITHUB_SHA"] = "b" * 40
        atomic_json(path, gen)
        with self.assertRaisesRegex(ValueError, "provenance mismatch"):
            self.publish()

    def test_job_timeout_without_artifacts_is_explicitly_unknown_not_scored(self):
        shutil.rmtree(self.artifact)  # This test owns the temporary fixture only.
        self.jobs[0]["conclusion"] = "timed_out"
        board = self.publish()
        self.assertTrue(board["meta"]["incomplete_evidence"])
        self.assertEqual(board["meta"]["generation_requests"], 0)
        for run in board["models"][0]["runs"]:
            self.assertEqual(run["record_origin"], "github-job-status-only")
            self.assertEqual(run["execution"]["job_conclusion"], "timed_out")
            self.assertFalse(run["correct"])
            self.assertFalse(run["submission_sha256"])
            self.assertIsNone(run["generation"]["usage"])
            self.assertIn("unavailable", run["error"])

    def test_interrupted_job_preserves_generation_but_never_invents_a_score(self):
        path = self.artifact / "opencode-go-mimo-v2.5/002_gemv/rep1/record.json"
        path.unlink()  # This test owns the temporary fixture only.
        self.jobs[0]["conclusion"] = "cancelled"
        board = self.publish()
        self.assertTrue(board["meta"]["incomplete_evidence"])
        self.assertFalse(board["meta"]["incomplete_usage"])
        self.assertEqual(board["meta"]["output_tokens"], 60)
        run = next(r for r in board["models"][0]["runs"] if r["problem"] == "002_gemv")
        self.assertEqual(run["record_origin"], "github-generation-only")
        self.assertFalse(run["correct"])
        self.assertEqual(run["cells"], -1)
        self.assertEqual(run["execution"]["job_conclusion"], "cancelled")
        self.assertEqual(run["generation"]["usage"]["completion_tokens"], 30)
        self.assertEqual(run["submission_sha256"], hashlib.sha256(self.sources[1].read_bytes()).hexdigest())

    def test_successful_job_requires_all_scoring_records(self):
        path = self.artifact / "opencode-go-mimo-v2.5/002_gemv/rep1/record.json"
        path.unlink()  # This test owns the temporary fixture only.
        with self.assertRaisesRegex(ValueError, "no scoring record"):
            self.publish()
        self.assertFalse((self.root / "published").exists())


if __name__ == "__main__":
    unittest.main()
